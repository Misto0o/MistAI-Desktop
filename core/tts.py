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
  - If edge-tts fails (typically: no network), the utterance falls back to
    pyttsx3, which is initialised lazily on the worker thread the first
    time it's needed. A fallback that cannot initialise is reported once,
    loudly, instead of leaving MistAI silently mute.
"""

import asyncio
import io
import threading
import time
from queue import Queue, Empty

from .config import EDGE_TTS_VOICE

try:
    import edge_tts
    import sounddevice as sd
    import soundfile as sf

    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False
    sd = None


class NeuralTTS:
    # After an edge-tts failure (no network, TLS interception, ...) skip the
    # network for this long and go straight to pyttsx3, instead of paying a
    # failed round-trip on every utterance.
    NEURAL_COOLDOWN = 600.0

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
        # Bumped by interrupt(); an utterance whose generation is older than
        # the current one is dropped (even if its synthesis already finished).
        self._generation = 0
        # sounddevice.stop() is process-global. This flag is deliberately
        # narrower than `speaking`: it is set only while *our* sounddevice
        # playback is active, never while text is queued or being synthesized.
        self._sounddevice_playing = threading.Event()

        # -- fallback engine (lazy) ------------------------------------------
        self._engine = None
        self._engine_failed = False
        self._fallback_warned = False

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
                print(f"[TTS] Utterance dropped: {e}")
            finally:
                with self._cv:
                    self._outstanding = max(0, self._outstanding - 1)
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
                with self._cv:
                    if gen != self._generation:
                        return
                self._neural_down_until = time.monotonic() + self.NEURAL_COOLDOWN
                print(
                    f"[TTS] edge-tts failed ({e}) -- using pyttsx3 for the next "
                    f"{self.NEURAL_COOLDOWN / 60:.0f} min"
                )
        self._fallback(text, gen)

    def _neural(self, text, gen):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:

            async def _synth():
                c = edge_tts.Communicate(text, self._voice())
                chunks = []
                async for ch in c.stream():
                    if ch["type"] == "audio":
                        chunks.append(ch["data"])
                return b"".join(chunks)

            audio = loop.run_until_complete(asyncio.wait_for(_synth(), timeout=25))
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
            self._sounddevice_playing.set()
        try:
            sd.wait()  # returns early if interrupt() called sd.stop()
        finally:
            self._sounddevice_playing.clear()

    def _ensure_engine(self):
        """Lazy pyttsx3 init, on the worker thread (pyttsx3 engines are not
        happy being driven from a thread other than the one that created
        them). Returns False, and says why once, if it can't be created."""
        if self._engine is not None:
            return True
        if self._engine_failed:
            return False
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 168)
            print("[TTS] pyttsx3 fallback initialised")
            return True
        except Exception as e:
            self._engine_failed = True
            if not self._fallback_warned:
                self._fallback_warned = True
                self._report_error(
                    "No working speech engine is available. MistAI cannot speak "
                    "until online voice or the offline voice engine is available."
                )
                print(f"[TTS] pyttsx3 unavailable: {e}")
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
        self._engine.say(text)
        self._engine.runAndWait()
