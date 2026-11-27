# ============================================================
# VOICE MODULE
# ============================================================
#
# Voice helpers: neural text-to-speech with the pyttsx3 fallback, the
# [VoiceTiming] log of one spoken turn, and wake-loop noise recalibration.
#
# SECTION INDEX
#   1. OPTIONAL SPEECH LIBRARIES
#   2. SPEECH LEVEL ENVELOPE
#   3. NEURAL SPEECH
#   4. DROPPED UTTERANCE COUNT
#   5. SPEECH MODULE CACHE REPAIR
#   6. VOICE TIMING
#   7. VOICE TURN TIMER
#   8. WAKE CALIBRATION
#   9. NOISE RECALIBRATION POLICY
# ============================================================

"""
core/tts.py — Neural (edge-tts) text-to-speech with pyttsx3 fallback.

Threading contract (this is what api.py / agent.py rely on):
  - `speaking` is True from the moment say() ACCEPTS text until the last
    queued utterance has finished playing. It is set synchronously inside
    say(), on the caller's thread, so `speak(x); while tts.speaking: ...`
    (or wait_until_done()) can never observe a false "idle" between the
    enqueue and the worker picking it up.
  - interrupt() clears everything queued AND aborts the utterance that is
    currently playing. stop_current_task() calls this.
  - If edge-tts fails (typically: no network), it is retried once with a
    short timeout, then the utterance falls back to pyttsx3, which is
    initialised lazily on the worker thread the first time it's needed
    (comtypes' generated modules go to a user-writable cache, see
    config.configure_comtypes_cache). A fallback that cannot initialise is
    reported once, loudly, instead of leaving MistAI silently mute; every
    utterance dropped for that counts in `dropped`, so callers that wait for
    an answer know their question was never heard.
"""

import asyncio
import io
import threading
import time
from queue import Queue, Empty

from .config import (
    EDGE_TTS_VOICE,
    configure_comtypes_cache,
    WAKE_ENERGY_MAX,
    WAKE_ENERGY_MIN,
    WAKE_RECAL_AFTER_FAILURES,
    WAKE_RECAL_IDLE_S,
    WAKE_RECAL_RETRY_S,
)


# ============================================================
# OPTIONAL SPEECH LIBRARIES
# ============================================================

try:
    import edge_tts
    import sounddevice as sd
    import soundfile as sf

    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False
    sd = None


# ============================================================
# SPEECH LEVEL ENVELOPE
# ============================================================

ENVELOPE_HZ = 50      # level values per second of decoded speech


def rms_envelope(data, samplerate, hz=ENVELOPE_HZ):
    """The RMS of each 1/hz-second window of decoded audio (float samples,
    mono or (frames, channels)), divided by the loudest window: a tuple of
    0..1 values, one per window. Empty audio or a bad rate gives ()."""
    try:
        import numpy as np

        a = np.asarray(data, dtype="float32")
        if a.ndim > 1:
            a = np.sqrt(np.mean(np.square(a), axis=1))   # channels -> one RMS per frame
        else:
            a = np.abs(a)
        rate, hz = float(samplerate), float(hz)
        if not (rate > 0 and hz > 0):
            return ()
        step = max(1, int(round(rate / hz)))
    except Exception:
        return ()
    if a.size == 0:
        return ()
    n = -(-a.size // step)                                # windows, the last one partial
    padded = np.zeros(n * step, dtype="float32")
    padded[:a.size] = a
    windows = padded.reshape(n, step)
    counts = np.full(n, step, dtype="float32")
    counts[-1] = a.size - (n - 1) * step
    rms = np.sqrt(np.sum(np.square(windows), axis=1) / counts)
    top = float(rms.max())
    if not top > 0:
        return tuple(0.0 for _ in range(n))
    return tuple(float(v) for v in np.clip(rms / top, 0.0, 1.0))


# ============================================================
# NEURAL SPEECH
# ============================================================

class NeuralTTS:
    # After an edge-tts failure (no network, TLS interception, ...) skip the
    # network for this long and go straight to pyttsx3, instead of paying a
    # failed round-trip on every utterance.
    NEURAL_COOLDOWN = 600.0
    # edge-tts timeouts (seconds): the first try, then one short retry before
    # the fallback (a one-off connection timeout should not cost the voice).
    NEURAL_CONNECT_S, NEURAL_TIMEOUT_S = 6, 20
    RETRY_CONNECT_S, RETRY_TIMEOUT_S = 3, 8
    NO_ENGINE_MESSAGE = (
        "Voice output is not working: neither the online voice nor the Windows "
        "voice could start. MistAI will show its replies and questions on "
        "screen instead.")

    def __init__(self, on_error=None):
        self._q = Queue()
        self._neural_down_until = 0.0
        self._gender = "none"
        self._on_error = on_error

        # -- speaking-state bookkeeping --------------------------------------
        # _outstanding counts utterances that have been accepted by say() and
        # not yet finished (queued + the one playing). Guarded by _cv so that
        # wait_until_done() can block on it instead of polling.
        self._cv = threading.Condition()
        self._outstanding = 0
        # Monotonic time the last utterance finished and nothing was left
        # (diagnostics: MISTAI_VOICE_DIAG's since_tts). None until then.
        self.finished_at = None
        # Bumped by interrupt(); an utterance whose generation is older than
        # the current one is dropped (even if its synthesis already finished).
        self._generation = 0
        # sounddevice.stop() is process-global. This flag is deliberately
        # narrower than `speaking`: it is set only while *our* sounddevice
        # playback is active, never while text is queued or being synthesized.
        self._sounddevice_playing = threading.Event()
        # The speech level (level()): (envelope, monotonic start of sd.play)
        # while edge-tts audio plays, else None; one tuple, swapped whole.
        self._play = None
        # True while the pyttsx3 fallback speaks: it plays through SAPI, so
        # there are no samples and no level.
        self._fallback_speaking = False

        # -- fallback engine (lazy) ------------------------------------------
        self._engine = None
        self._engine_failed = False
        self._fallback_warned = False
        # Utterances dropped because no engine could speak them (read by the
        # question/confirmation waits: an unheard question waits longer).
        self.dropped = 0

        threading.Thread(target=self._worker, daemon=True, name="TTS").start()
        if EDGE_TTS_AVAILABLE:
            print("[TTS] edge-tts ready (pyttsx3 fallback on demand)")
        else:
            print("[TTS] edge-tts unavailable -- using pyttsx3")

    # -- Public state ---------------------------------------------------------

    @property
    def speaking(self):
        with self._cv:
            return self._outstanding > 0

    def level(self, now=None):
        """The playing speech's loudness 0..1 at this moment (its RMS
        envelope at the time since sd.play started); 0.0 when nothing plays;
        None while the pyttsx3 fallback speaks (no samples to measure)."""
        if getattr(self, "_fallback_speaking", False):
            return None
        play = getattr(self, "_play", None)
        if play is None:
            return 0.0
        envelope, started = play
        t = (time.monotonic() if now is None else now) - started
        i = int(t * ENVELOPE_HZ)
        if i < 0 or i >= len(envelope):
            return 0.0
        return envelope[i]

    @property
    def no_engine(self):
        """True when nothing can speak right now: the offline voice failed to
        start and the online voice is unavailable or cooling down."""
        return bool(getattr(self, "_engine_failed", False)) and (
            not EDGE_TTS_AVAILABLE
            or time.monotonic() < getattr(self, "_neural_down_until", 0.0))

    def wait_until_done(self, timeout=None):
        """Block until nothing is queued or playing. Returns False on timeout."""
        with self._cv:
            return self._cv.wait_for(lambda: self._outstanding == 0, timeout=timeout)

    def set_gender(self, g):
        self._gender = g
        if EDGE_TTS_AVAILABLE:
            print(f"[TTS] Gender set to {g} -> {EDGE_TTS_VOICE.get(g, 'default')}")

    def _voice(self):
        return EDGE_TTS_VOICE.get(self._gender, EDGE_TTS_VOICE["none"])

    # -- Public control -------------------------------------------------------

    def say(self, text, interrupt=False):
        """Queue `text`. With interrupt=True, everything queued is dropped and
        the current utterance is cut off first. Note the ordering: interrupt
        is honoured even when `text` is empty, so say("", interrupt=True)
        is a valid "shut up" call (it used to be a silent no-op)."""
        if interrupt:
            self.interrupt()
        if not text:
            return
        with self._cv:
            gen = self._generation
            self._outstanding += 1
        self._q.put((gen, text))

    def interrupt(self):
        """Drop queued speech and abort the utterance currently playing."""
        with self._cv:
            self._generation += 1
            # Drain the queue under the queue's own mutex, then correct the
            # outstanding count by however many we discarded.
            with self._q.mutex:
                dropped = len(self._q.queue)
                self._q.queue.clear()
            self._outstanding = max(0, self._outstanding - dropped)
            sounddevice_playing = self._sounddevice_playing.is_set()
            self._cv.notify_all()
        # Abort in-flight audio. On some PortAudio builds, stopping while a
        # separate microphone stream is active can abort the host stream, so
        # only stop audio when this TTS instance is actually playing.
        try:
            if sd is not None and sounddevice_playing:
                sd.stop(ignore_errors=True)
        except Exception:
            pass
        try:
            if self._engine is not None:
                self._engine.stop()
        except Exception:
            pass

    # -- Worker ---------------------------------------------------------------

    def _worker(self):
        while True:
            gen, text = self._q.get()
            try:
                self._speak_one(gen, text)
            except Exception as e:
                self.dropped = getattr(self, "dropped", 0) + 1
                print(f"[TTS] Utterance dropped ({type(e).__name__})")
            finally:
                with self._cv:
                    self._outstanding = max(0, self._outstanding - 1)
                    if self._outstanding == 0:
                        self.finished_at = time.monotonic()
                    self._cv.notify_all()
                self._q.task_done()

    def _speak_one(self, gen, text):
        # Interrupted while sitting in the queue? Skip it.
        with self._cv:
            if gen != self._generation:
                return
        if EDGE_TTS_AVAILABLE and time.monotonic() >= self._neural_down_until:
            try:
                self._neural(text, gen)
                return
            except Exception as e:
                first = type(e).__name__
            with self._cv:
                if gen != self._generation:
                    return
            # One short retry: a single connection timeout is often transient.
            print(f"[TTS] edge-tts failed ({first}) -- retrying once")
            try:
                self._neural(text, gen, retry=True)
                return
            except Exception as e:
                with self._cv:
                    if gen != self._generation:
                        return
                self._neural_down_until = time.monotonic() + self.NEURAL_COOLDOWN
                print(
                    f"[TTS] edge-tts failed again ({type(e).__name__}) -- using "
                    f"pyttsx3 for the next {self.NEURAL_COOLDOWN / 60:.0f} min"
                )
        self._fallback(text, gen)

    def _neural(self, text, gen, retry=False):
        connect_s = self.RETRY_CONNECT_S if retry else self.NEURAL_CONNECT_S
        total_s = self.RETRY_TIMEOUT_S if retry else self.NEURAL_TIMEOUT_S
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:

            async def _synth():
                c = edge_tts.Communicate(text, self._voice(), connect_timeout=connect_s)
                chunks = []
                async for ch in c.stream():
                    if ch["type"] == "audio":
                        chunks.append(ch["data"])
                return b"".join(chunks)

            audio = loop.run_until_complete(asyncio.wait_for(_synth(), timeout=total_s))
        finally:
            loop.close()

        if not audio:
            raise RuntimeError("edge-tts returned no audio")
        data, sr_ = sf.read(io.BytesIO(audio), dtype="float32")
        # Synthesis is a network round-trip; an interrupt may have landed
        # while we were waiting for it. Don't play stale audio.
        # Keep the generation check and start of sd.play under one lock. An
        # interrupt can then either reject this stale utterance before play,
        # or see _sounddevice_playing and stop the playback it just started.
        with self._cv:
            if gen != self._generation:
                return
            sd.play(data, sr_)
            started = time.monotonic()
            self._sounddevice_playing.set()
        try:
            # The level envelope is computed after play started (sd.play does
            # not block), so it never delays the voice.
            self._play = (rms_envelope(data, sr_), started)
            sd.wait()  # returns early if interrupt() called sd.stop()
        finally:
            self._play = None
            self._sounddevice_playing.clear()

    def _ensure_engine(self):
        """Lazy pyttsx3 init, on the worker thread (pyttsx3 engines are not
        happy being driven from a thread other than the one that created
        them). comtypes' generated modules are pointed at MistAI's own cache
        first; a failed init is retried once after repairing that cache
        (_repair_speech_cache). Returns False, and says why once, if it
        can't be created."""
        if self._engine is not None:
            return True
        if self._engine_failed:
            return False
        configure_comtypes_cache()
        first = None
        for attempt in (1, 2):
            try:
                import pyttsx3

                self._engine = pyttsx3.init()
                self._engine.setProperty("rate", 168)
                print("[TTS] pyttsx3 fallback initialised"
                      + (" (after a cache repair)" if attempt == 2 else ""))
                return True
            except Exception as e:
                if attempt == 1:
                    first = type(e).__name__
                    print(f"[TTS] pyttsx3 init failed ({first}) -- repairing the "
                          "speech module cache and retrying once")
                    _repair_speech_cache()
                    continue
                self._engine = None
                self._engine_failed = True
                print(f"[TTS] pyttsx3 unavailable ({first}, then {type(e).__name__})")
        if not self._fallback_warned:
            self._fallback_warned = True
            self._report_error(self.NO_ENGINE_MESSAGE)
        return False

    def _report_error(self, message):
        print(f"[TTS] !! {message}")
        if self._on_error:
            try:
                self._on_error(message)
            except Exception:
                pass

    def _fallback(self, text, gen):
        with self._cv:
            if gen != self._generation:
                return
        if not self._ensure_engine():
            raise RuntimeError("no fallback engine")
        with self._cv:
            if gen != self._generation:
                return
        self._fallback_speaking = True
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        finally:
            self._fallback_speaking = False


# ============================================================
# DROPPED UTTERANCE COUNT
# ============================================================

def dropped_count(engine):
    """An engine's dropped-utterance count (NeuralTTS.dropped); 0 for a
    stand-in without one. Compare it before and after a question: a change
    means the question was never spoken."""
    n = getattr(engine, "dropped", 0)
    return n if isinstance(n, int) else 0


# ============================================================
# SPEECH MODULE CACHE REPAIR
# ============================================================

# pyttsx3's SAPI driver imports comtypes.gen.SpeechLib, which comtypes builds
# from the SAPI type library on first use. A cached copy that is truncated or
# from another comtypes version raises (SyntaxError, AttributeError...), which
# pyttsx3's own "except ImportError" does not catch, so the import fails for
# good. The repair forgets the half-imported modules and rebuilds the SAPI
# wrapper: into MistAI's cache (overwriting a bad copy there), else in memory
# only, which needs no disk at all.

_SPEECH_MODULES = ("pyttsx3.drivers.sapi5", "comtypes.gen.SpeechLib")
_SAPI_WRAPPER_PREFIX = "comtypes.gen._C866CA3A_32F7_11D2_9602_00C04F8EE628"


def _forget_speech_modules():
    import sys

    for name in list(sys.modules):
        if name in _SPEECH_MODULES or name.startswith(_SAPI_WRAPPER_PREFIX):
            sys.modules.pop(name, None)


def _repair_speech_cache(create=None, load=None):
    """Rebuild comtypes' SAPI wrapper; returns "dir", "memory" or "" (it
    failed both ways). Never raises. create / load: the COM factory and the
    module import (tests)."""
    try:
        if create is None:
            import comtypes.client

            create = comtypes.client.CreateObject
    except Exception:
        return ""
    for mode in ("dir", "memory"):
        _forget_speech_modules()
        if mode == "memory":
            configure_comtypes_cache(memory_only=True)
        try:
            create("SAPI.SpVoice")                    # generates the wrapper modules
            import importlib

            importlib.invalidate_caches()
            (load or importlib.import_module)("comtypes.gen.SpeechLib")
            print(f"[TTS] speech module cache rebuilt ({mode})")
            return mode
        except Exception as e:
            print(f"[TTS] speech module rebuild failed ({mode}, {type(e).__name__})")
    return ""


# ============================================================
# VOICE TIMING
# ============================================================

# core/voice_timing.py — [VoiceTiming] log lines for one spoken turn.
#
# A turn starts when the wake clip ends (or when a follow-up listen opens).
# Each mark logs the seconds since the previous mark and since the start, and
# the turn ends at the first agent step, a chat reply, or a failed listen.
# Durations and mark names only, never transcript text.

# ============================================================
# VOICE TURN TIMER
# ============================================================

class VoiceTimer:
    def __init__(self, clock=time.monotonic, log=print):
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._label = ""
        self._t0 = None
        self._last = None

    @property
    def active(self):
        with self._lock:
            return self._t0 is not None

    @property
    def label(self):
        """The active turn's label, or "" when no turn is active."""
        with self._lock:
            return self._label if self._t0 is not None else ""

    def start(self, label, t0=None):
        with self._lock:
            self._label = label
            self._t0 = self._clock() if t0 is None else t0
            self._last = self._t0

    def mark(self, name, end=False):
        """Log one step of the active turn; a no-op when none is active."""
        with self._lock:
            if self._t0 is None:
                return
            now = self._clock()
            delta, total = now - self._last, now - self._t0
            self._last = now
            label = self._label
            if end:
                self._t0 = None
        self._log(f"[VoiceTiming] {label}: {name} +{delta:.2f}s (total {total:.2f}s)")


# ============================================================
# WAKE CALIBRATION
# ============================================================

# core/wake_calibration.py — when the wake loop re-measures room noise.
#
# The wake loop runs with a fixed energy threshold (dynamic adjustment raised
# the bar during a loud "hey" until a quiet "mist" counted as silence). A fixed
# threshold goes stale, so it is re-calibrated:
#   * at startup,
#   * after WAKE_RECAL_IDLE_S with no clip heard,
#   * after WAKE_RECAL_AFTER_FAILURES untranscribable clips in a row (noise is
#     opening clips: the threshold is too low),
#   * once, WAKE_RECAL_RETRY_S after a result outside the band (it is clamped
#     meanwhile; a second out-of-band result is kept, clamped, to avoid a loop).
# Pure bookkeeping: the caller owns the microphone, TTS and lock checks.

# ============================================================
# NOISE RECALIBRATION POLICY
# ============================================================

class WakeCalibration:
    def __init__(self, low=WAKE_ENERGY_MIN, high=WAKE_ENERGY_MAX,
                 idle_s=WAKE_RECAL_IDLE_S, max_failures=WAKE_RECAL_AFTER_FAILURES,
                 retry_s=WAKE_RECAL_RETRY_S):
        self.low, self.high = low, high
        self.idle_s, self.max_failures, self.retry_s = idle_s, max_failures, retry_s
        self._forced = "startup"
        self._retry_at = None
        self._last_activity = None
        self._failures = 0

    def due(self, now):
        """The reason a calibration is due now, or None."""
        if self._forced:
            return self._forced
        if self._retry_at is not None and now >= self._retry_at:
            return "threshold out of band"
        if self._last_activity is not None and now - self._last_activity >= self.idle_s:
            return f"idle {self.idle_s:.0f}s"
        return None

    def calibrated(self, measured, now):
        """Record a finished calibration; return the threshold to use."""
        was_retry = self._retry_at is not None
        threshold = min(max(measured, self.low), self.high)
        self._forced = None
        self._failures = 0
        self._last_activity = now
        self._retry_at = (now + self.retry_s
                          if threshold != measured and not was_retry else None)
        return threshold

    def failed(self, now):
        """A calibration raised: keep the old threshold, retry when idle."""
        self._forced = None
        self._retry_at = None
        self._last_activity = now

    def heard(self, transcribed, now):
        """A clip ended. transcribed: True, False (no speech found) or None
        (recognizer/network error, which says nothing about the threshold)."""
        self._last_activity = now
        if transcribed is True:
            self._failures = 0
        elif transcribed is False:
            self._failures += 1
            if self._failures >= self.max_failures:
                self._forced = f"{self.max_failures} untranscribed clips"
                self._failures = 0
