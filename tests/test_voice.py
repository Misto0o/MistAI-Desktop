# ============================================================
# VOICE TESTS
# ============================================================
#
# Wake word, push-to-talk, voice turns, voice timing and diagnostics.
#
# SECTION INDEX
#   1. WAKE WORD
#   2. WAKE WORD HELPERS
#   3. WAKE WORD TESTS
#   4. WAKE WORD HELPERS (2)
#   5. WAKE WORD TESTS (2)
#   6. WAKE WORD HELPERS (3)
#   7. WAKE WORD TESTS (3)
#   8. WAKE WORD MIC
#   9. WAKE WORD MAKE API
#   10. WAKE WORD TESTS (4)
#   11. WAKE WORD HELPERS (4)
#   12. WAKE WORD TESTS (5)
#   13. VOICE TIMING
#   14. VOICE TIMING HELPERS
#   15. VOICE TIMING TESTS
#   16. VOICE TIMING MIC
#   17. VOICE TIMING LISTEN API
#   18. VOICE TIMING TESTS (2)
#   19. VOICE TIMING WAKE API
#   20. VOICE TIMING TESTS (3)
#   21. VOICE TIMING LOOP MARKS
#   22. VOICE TIMING HELPERS (2)
#   23. VOICE TIMING TESTS (4)
#   24. PUSH TO TALK
#   25. PUSH TO TALK INLINE THREAD
#   26. PUSH TO TALK PTT API
#   27. PUSH TO TALK HELPERS
#   28. PUSH TO TALK TESTS
#   29. PUSH TO TALK HELPERS (2)
#   30. PUSH TO TALK TESTS (2)
#   31. PUSH TO TALK RECORDING LOCK
#   32. PUSH TO TALK TESTS (3)
#   33. SECOND PRESS
#   34. SECOND PRESS HELPERS
#   35. SECOND PRESS STREAM
#   36. SECOND PRESS MIC
#   37. SECOND PRESS REC
#   38. SECOND PRESS API
#   39. SECOND PRESS HELPERS (2)
#   40. SECOND PRESS TESTS
#   41. LISTEN SETTINGS
#   42. LISTEN SETTINGS HELPERS
#   43. LISTEN SETTINGS CLOCK
#   44. LISTEN SETTINGS MIC
#   45. LISTEN SETTINGS REC
#   46. LISTEN SETTINGS HELPERS (2)
#   47. LISTEN SETTINGS TESTS
#   48. VOICE DIAGNOSTICS
#   49. VOICE DIAGNOSTICS HELPERS
#   50. VOICE DIAGNOSTICS MIC
#   51. VOICE DIAGNOSTICS HELPERS (2)
#   52. VOICE DIAGNOSTICS API
#   53. VOICE DIAGNOSTICS HELPERS (3)
#   54. VOICE DIAGNOSTICS TESTS
#   55. VOICE TURNS
#   56. VOICE TURNS HELPERS
#   57. VOICE TURNS VOICE API
#   58. VOICE TURNS HELPERS (2)
#   59. VOICE TURNS TESTS
#   60. VOICE TURNS CONVERSATION API
#   61. VOICE TURNS TESTS (2)
#   62. VOICE TURNS HELPERS (3)
#   63. VOICE TURNS TESTS (3)
#   64. VOICE TURNS HELPERS (4)
#   65. API ERROR RESULT
#   66. VOICE TURNS TESTS (4)
#   67. VOICE TURNS HELPERS (5)
#   68. VOICE TURNS TESTS (5)
#   69. VOICE TURNS HELPERS (6)
#   70. VOICE TURNS FAKE TIMER
#   71. VOICE TURNS HELPERS (7)
#   72. VOICE TURNS TESTS (6)
# ============================================================

"""Wake word, push-to-talk, voice turns, voice timing and diagnostics."""

import threading
from types import SimpleNamespace
import json
import wave
import re
import time
import os
import struct

import pytest
import speech_recognition as sr

from core.config import (
    fuzzy_wake,
    strip_wake_prefix,
    WAKE_ENERGY_MAX,
    WAKE_ENERGY_MIN,
    wake_clips_enabled,
    esc,
)
import core.api as api_mod
from core.api import Api, _StoppableStream
from core.tts import WakeCalibration, VoiceTimer
from confirm_helpers import bind_confirmation
from core import config
from test_api import HTML, _run_page, _script, needs_node
from test_planner import CHAT, _finish, _routing_api


# ============================================================
# WAKE WORD
# ============================================================

# Text-layer wake-word harness: transcript -> wake / soft / none.
#
# CURRENT pins what fuzzy_wake does today, including the command it hands on.
# DESIRED lists real-log gaps (the wake loop heard "hey miss" when the user said
# "hey mist") as strict xfails. When matching changes, they XPASS, which fails
# the run, so whoever flips them has to move them into CURRENT on purpose.
#
# "soft" (planned): the wake is accepted, but any inline command is dropped and
# the user is asked "Yes?" first. fuzzy_wake has no soft tier yet, so
# classify() returns only "wake" or "none" for now.
#
# Merged by area (pure moves, no test changed) from: test_wake_matching.py, test_wake_word_safety.py, test_wake_calibration.py, test_wake_clips.py.


# ============================================================
# WAKE WORD HELPERS
# ============================================================

def classify(text):
    ww, _ = fuzzy_wake(text)
    return "wake" if ww else "none"


# (transcript, expected canonical wake word or None, expected command)
CURRENT = [
    # Exact wake phrases (tier 1).
    ("mist", "mist", ""),
    ("Mist, open calculator", "mist", "open calculator"),
    ("hey mist", "hey mist", ""),
    ("Hey Mist, open the calculator", "hey mist", "open the calculator"),
    ("mistai", "mistai", ""),
    ("mist ai open notepad", "mist ai", "open notepad"),
    ("hey mist stop", "hey mist", "stop"),
    # Listed STT alternatives (tier 1).
    ("myst open calculator", "mist", "open calculator"),
    ("mest", "mist", ""),
    ("hey myst", "hey mist", ""),
    ("hey mest", "hey mist", ""),
    ("misty ai what time is it", "mistai", "what time is it"),
    ("miss eye", "mistai", ""),
    # First-word near-miss of "mist" (tier 2, ratio >= 0.85).
    ("mst", "mist", ""),
    # Rejected today.
    ("mists", None, None),           # "mist" + letters is never tier 2
    ("miss", None, None),
    ("missed", None, None),
    ("miss open calculator", None, None),
    ("a mist", None, None),
    ("hello", None, None),
    ("stop", None, None),
    ("", None, None),
    ("hey must be nice", None, None),
    ("hey missed you", None, None),
    ("hey listen", None, None),
    ("hey mister", None, None),
    ("they missed it", None, None),
    # Real-log transcripts the matcher is not meant to rescue. A lone "hey"
    # is a segmentation problem (the quiet "mist" was trimmed from the clip),
    # and so is "amidst" (ratio 0.80 vs "mist", not hey-led).
    ("hey", None, None),
    ("amidst", None, None),
]


# ============================================================
# WAKE WORD TESTS
# ============================================================

@pytest.mark.parametrize("text,wake,command", CURRENT)
def test_current_wake_matching(text, wake, command):
    assert fuzzy_wake(text) == (wake, command)


# ============================================================
# WAKE WORD HELPERS (2)
# ============================================================

# Real-log gaps: a near-miss of "mist" right after "hey". Each should become a
# soft wake. "hey missed" / "hey must" are left out on purpose because they
# collide with the negatives "hey missed you" / "hey must be nice".
DESIRED_SOFT = [
    "hey miss",                    # real log
    "hey miss open calculator",    # soft: the inline command must be dropped
    "hey midst",
    "hey mists",
]


# ============================================================
# WAKE WORD TESTS (2)
# ============================================================

@pytest.mark.parametrize("text", DESIRED_SOFT)
@pytest.mark.xfail(strict=True, reason="documented gap: no soft wake after 'hey' yet")
def test_desired_soft_wake_after_hey(text):
    assert classify(text) == "soft"


# ==========================================================================
# moved from tests/test_wake_word_safety.py
# Regression coverage for wake-word recognition and task cleanup.
# ==========================================================================

@pytest.mark.parametrize(
    "utterance",
    [
        "list",
        "must",
        "most",
        "mint",
        "mistake",
        "dismiss",
        "optimistic",
    ],
)
def test_wake_detection_does_not_match_inside_or_near_other_words(utterance):
    assert fuzzy_wake(utterance) == (None, None)


@pytest.mark.parametrize(
    "utterance",
    [
        "I miss you",
        "missed the bus",
        "the midst of nowhere",
        "I walked through the mist today",
        "misty morning",
    ],
)
def test_wake_alternatives_do_not_turn_ordinary_speech_into_commands(utterance):
    assert fuzzy_wake(utterance) == (None, None)


def test_wake_is_detected_and_prefix_is_removed_before_planning():
    assert fuzzy_wake("Mist, open calculator") == ("mist", "open calculator")
    assert strip_wake_prefix("Hey Mist, open calculator") == "open calculator"


def test_prefix_stripping_only_removes_an_actual_leading_wake_phrase():
    command = "Please tell Mist hello when you see her"

    assert strip_wake_prefix(command) == command


# ==========================================================================
# moved from tests/test_wake_calibration.py
# Wake-loop segmentation settings and the re-calibration schedule.
# ==========================================================================


# ============================================================
# WAKE WORD HELPERS (3)
# ============================================================

def _cal():
    return WakeCalibration(low=60, high=1500, idle_s=90, max_failures=3, retry_s=30)


# -- schedule (pure) ----------------------------------------------------------


# ============================================================
# WAKE WORD TESTS (3)
# ============================================================

def test_band_edges_leave_room_for_real_calibrations():
    # Real rooms calibrated at 217 and 668; neither may be clamped.
    assert WAKE_ENERGY_MIN <= 217 and 668 < WAKE_ENERGY_MAX


def test_startup_calibration_is_due_first():
    cal = _cal()
    assert cal.due(0) == "startup"
    assert cal.calibrated(300, 0) == 300
    assert cal.due(1) is None


def test_idle_triggers_recalibration_and_clips_reset_the_clock():
    cal = _cal()
    cal.calibrated(300, 0)
    assert cal.due(89) is None
    cal.heard(True, 80)
    assert cal.due(169) is None
    assert cal.due(170) == "idle 90s"


def test_three_untranscribed_clips_in_a_row_force_recalibration():
    cal = _cal()
    cal.calibrated(300, 0)
    cal.heard(False, 1)
    cal.heard(False, 2)
    cal.heard(True, 3)        # a transcription resets the streak
    cal.heard(False, 4)
    cal.heard(None, 5)        # a network error neither counts nor resets
    cal.heard(False, 6)
    assert cal.due(7) is None
    cal.heard(False, 8)
    assert cal.due(9) == "3 untranscribed clips"
    cal.calibrated(300, 9)
    assert cal.due(10) is None


@pytest.mark.parametrize("measured,expected", [(5, 60), (5000, 1500)])
def test_out_of_band_is_clamped_and_retried_once(measured, expected):
    cal = _cal()
    assert cal.calibrated(measured, 0) == expected
    assert cal.due(29) is None
    assert cal.due(30) == "threshold out of band"
    # Still out of band on the retry: keep the clamped value, no retry chain.
    assert cal.calibrated(measured, 30) == expected
    assert cal.due(59) is None
    assert cal.due(119) is None
    assert cal.due(120) == "idle 90s"


def test_failed_calibration_waits_for_idle_instead_of_looping():
    cal = _cal()
    cal.failed(0)
    assert cal.due(1) is None
    assert cal.due(90) == "idle 90s"


# -- the real wake loop with a fake recognizer/microphone --------------------


# ============================================================
# WAKE WORD MIC
# ============================================================

class _WakeWordMic:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# WAKE WORD MAKE API
# ============================================================

def _make_api(monkeypatch, transcripts, calibration_values):
    """transcripts: per listen, a str or an exception to raise. When they run
    out, the loop is shut down."""
    api = Api.__new__(Api)
    api._wake_shutdown = threading.Event()
    api._wake_enabled = threading.Event()
    api._wake_enabled.set()
    api._mic_lock = threading.Lock()
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api.microphone = _WakeWordMic()
    api.tts = SimpleNamespace(speaking=False, wait_until_done=lambda timeout=None: None)
    api._wait_for_tts = lambda timeout=None: True

    queue = list(transcripts)
    cal_values = list(calibration_values)

    class FakeRec:
        def __init__(self):
            self.energy_threshold = 300
            self.dynamic_energy_threshold = True
            self.pause_threshold = 0.8
            self.non_speaking_duration = 0.5
            self.calibrations = 0
            self.thresholds_at_recognize = []
            api.rec = self

        def adjust_for_ambient_noise(self, src, duration=1):
            self.calibrations += 1
            self.energy_threshold = cal_values.pop(0) if cal_values else 300

        def listen(self, src, timeout=None, phrase_time_limit=None):
            return sr.AudioData(b"\x00\x00" * 1600, 16000, 2)

        def recognize_google(self, audio):
            self.thresholds_at_recognize.append(self.energy_threshold)
            if not queue:
                api._wake_shutdown.set()
                raise sr.UnknownValueError()
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(api_mod.sr, "Recognizer", FakeRec)
    monkeypatch.setattr(api_mod, "wake_clips_enabled", lambda: False)
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    return api


# ============================================================
# WAKE WORD TESTS (4)
# ============================================================

def test_wake_loop_uses_fixed_threshold_and_keeps_trailing_audio(monkeypatch):
    api = _make_api(monkeypatch, [], [300])
    api._google_wake_loop()
    assert api.rec.dynamic_energy_threshold is False
    assert api.rec.pause_threshold == 0.8
    assert api.rec.non_speaking_duration == api.rec.pause_threshold


def test_wake_loop_recalibrates_after_three_untranscribed_clips(monkeypatch):
    noise = sr.UnknownValueError
    api = _make_api(monkeypatch, [noise(), noise(), noise()], [200, 450])
    api._google_wake_loop()
    assert api.rec.calibrations == 2                 # startup + recalibration
    assert api.rec.thresholds_at_recognize == [200, 200, 200, 450]


@pytest.mark.parametrize("measured,expected", [(5, WAKE_ENERGY_MIN),
                                                (50000, WAKE_ENERGY_MAX)])
def test_wake_loop_clamps_out_of_band_calibration(monkeypatch, measured, expected):
    api = _make_api(monkeypatch, [], [measured])
    api._google_wake_loop()
    assert api.rec.thresholds_at_recognize == [expected]


def test_wake_loop_never_calibrates_while_mic_lock_is_held(monkeypatch):
    api = _make_api(monkeypatch, [], [300])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            api._wake_shutdown.set()

    monkeypatch.setattr(api_mod.time, "sleep", fake_sleep)
    api._mic_lock.acquire()          # a conversation listen owns the mic
    try:
        api._google_wake_loop()
    finally:
        api._mic_lock.release()
    assert api.rec.calibrations == 0


def test_wake_loop_never_calibrates_over_tts(monkeypatch):
    api = _make_api(monkeypatch, [], [300])
    api.tts.speaking = True
    waits = []

    def still_speaking(timeout=None):
        waits.append(timeout)
        if len(waits) >= 3:
            api._wake_shutdown.set()
        return False

    api._wait_for_tts = still_speaking
    api._google_wake_loop()
    assert api.rec.calibrations == 0


# ==========================================================================
# moved from tests/test_wake_clips.py
# Opt-in wake clip capture (MISTAI_WAKE_SAVE_CLIPS) for the wake harness.
# ==========================================================================


# ============================================================
# WAKE WORD HELPERS (4)
# ============================================================

def _wake_word_audio(seconds=0.5, rate=16000, width=2):
    return sr.AudioData(b"\x00\x01" * int(rate * seconds), rate, width)


# ============================================================
# WAKE WORD TESTS (5)
# ============================================================

def test_clip_saving_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MISTAI_WAKE_SAVE_CLIPS", raising=False)
    assert wake_clips_enabled() is False
    monkeypatch.setenv("MISTAI_WAKE_SAVE_CLIPS", "1")
    assert wake_clips_enabled() is True


def test_clip_seconds():
    assert Api._clip_seconds(_wake_word_audio(0.5)) == 0.5


def test_saved_clip_has_wav_and_sidecar(tmp_path):
    audio = _wake_word_audio(0.5)
    Api._save_wake_clip(audio, "hey miss", 668.4, 0.5, directory=str(tmp_path))

    (meta_path,) = tmp_path.glob("*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta == {
        "wav": meta_path.stem + ".wav",
        "transcript": "hey miss",
        "energy_threshold": 668.4,
        "clip_seconds": 0.5,
    }
    with wave.open(str(tmp_path / meta["wav"]), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 8000)


def test_untranscribed_clip_records_error_type_only(tmp_path):
    Api._save_wake_clip(_wake_word_audio(), None, 217, 0.5, error="RequestError",
                        directory=str(tmp_path))
    (meta_path,) = tmp_path.glob("*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["transcript"] == "untranscribed"
    assert meta["error"] == "RequestError"


def test_save_failure_never_raises(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    Api._save_wake_clip(_wake_word_audio(), "mist", 300, 0.5, directory=str(blocker))
    assert "Could not save clip" in capsys.readouterr().out


# ============================================================
# VOICE TIMING
# ============================================================

# [VoiceTiming] and [MicCompete] diagnostics: durations and counts only,
# never transcript content, and no change to what the voice path does.


# ============================================================
# VOICE TIMING HELPERS
# ============================================================

VOICE_TIMING_SECRET_WORDS = ("open", "banking", "password")   # never in a diagnostic line


def _timer(lines, times):
    it = iter(times)
    return VoiceTimer(clock=lambda: next(it), log=lines.append)


def _diag(lines):
    return [l for l in lines if l.startswith(("[VoiceTiming]", "[MicCompete]"))]


# -- VoiceTimer ---------------------------------------------------------------


# ============================================================
# VOICE TIMING TESTS
# ============================================================

def test_marks_log_delta_and_total_and_end_closes_the_turn():
    lines = []
    t = _timer(lines, [10.0, 10.5, 12.0, 13.0])
    t.start("wake")                      # 10.0
    t.mark("wake_heard")                 # 10.5
    t.mark("ack_done", end=True)         # 12.0
    t.mark("first_step")                 # inactive: no clock read, no line
    assert lines == [
        "[VoiceTiming] wake: wake_heard +0.50s (total 0.50s)",
        "[VoiceTiming] wake: ack_done +1.50s (total 2.00s)",
    ]
    assert not t.active


def test_start_accepts_an_earlier_t0():
    lines = []
    t = _timer(lines, [5.0])
    t.start("wake", t0=3.0)
    t.mark("wake_heard")
    assert lines == ["[VoiceTiming] wake: wake_heard +2.00s (total 2.00s)"]


# -- _listen_once -------------------------------------------------------------


# ============================================================
# VOICE TIMING MIC
# ============================================================

class _VoiceTimingMic:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# VOICE TIMING LISTEN API
# ============================================================

def _listen_api(lines, recognize):
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _VoiceTimingMic()
    api.voice_timer = VoiceTimer(log=lines.append)
    seen = {}

    class Rec:
        def adjust_for_ambient_noise(self, src, duration=1):
            seen["waiting_while_listening"] = api._listen_waiting.is_set()

        def listen(self, src, timeout=None, phrase_time_limit=None):
            seen["timeout"] = timeout
            return object()

        def recognize_google(self, audio):
            return recognize()

    api.recognizer = Rec()
    return api, seen


# ============================================================
# VOICE TIMING TESTS (2)
# ============================================================

def test_listen_once_marks_each_phase_without_content(capsys):
    lines = []
    api, seen = _listen_api(lines, lambda: "open my banking password")
    api.voice_timer.start("followup")

    assert api._listen_once(timeout=5) == "open my banking password"
    names = [l.split(": ", 1)[1].split(" +")[0] for l in lines]
    assert names == ["mic_acquired", "listen_start", "speech_end", "stt_done"]
    assert seen == {"waiting_while_listening": True, "timeout": 5}
    assert not api._listen_waiting.is_set()
    assert not any(w in l for l in lines for w in VOICE_TIMING_SECRET_WORDS)


def test_failed_listen_ends_the_turn_with_the_exception_type_only():
    lines = []

    def fail():
        raise sr.UnknownValueError("open my banking password")

    api, _ = _listen_api(lines, fail)
    api.voice_timer.start("followup")
    assert api._listen_once(timeout=5) is None
    assert "listen_failed (UnknownValueError)" in lines[-1]
    assert not api.voice_timer.active
    assert not api._listen_waiting.is_set()
    assert not any(w in l for l in lines for w in VOICE_TIMING_SECRET_WORDS)


# -- wake loop ----------------------------------------------------------------


# ============================================================
# VOICE TIMING WAKE API
# ============================================================

def _wake_api(monkeypatch, transcripts, during_recognize=None):
    api = Api.__new__(Api)
    api._wake_shutdown = threading.Event()
    api._wake_enabled = threading.Event()
    api._wake_enabled.set()
    api._mic_lock = threading.Lock()
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _VoiceTimingMic()
    api.tts = SimpleNamespace(speaking=False, wait_until_done=lambda timeout=None: None)
    api._wait_for_tts = lambda timeout=None: True
    api.woken = []
    api._on_wake = lambda command=None: api.woken.append(command)
    queue = list(transcripts)

    class FakeRec:
        def __init__(self):
            self.energy_threshold = 300

        def adjust_for_ambient_noise(self, src, duration=1):
            pass

        def listen(self, src, timeout=None, phrase_time_limit=None):
            return sr.AudioData(b"\x00\x00" * 1600, 16000, 2)

        def recognize_google(self, audio):
            if during_recognize:
                during_recognize(api)
            if not queue:
                api._wake_shutdown.set()
                raise sr.UnknownValueError()
            return queue.pop(0)

    monkeypatch.setattr(api_mod.sr, "Recognizer", FakeRec)
    monkeypatch.setattr(api_mod, "wake_clips_enabled", lambda: False)
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    return api


# ============================================================
# VOICE TIMING TESTS (3)
# ============================================================

def test_clip_transcribed_while_a_listen_waits_is_logged_without_content(monkeypatch, capsys):
    api = _wake_api(monkeypatch, ["open my banking password"],
                    during_recognize=lambda a: a._listen_waiting.set())
    api._google_wake_loop()
    compete = [l for l in capsys.readouterr().out.splitlines() if "[MicCompete]" in l]
    assert compete[0] == (
        "[MicCompete] Wake loop handled a clip (4 words) while a _listen_once "
        "was waiting for the mic; outcome: dropped (no wake word)"
    )
    assert "outcome: untranscribed" in compete[1]
    assert not any(w in l for l in compete for w in VOICE_TIMING_SECRET_WORDS)
    assert api.woken == []          # behavior unchanged: still dropped


def test_no_compete_log_when_nothing_is_listening(monkeypatch, capsys):
    api = _wake_api(monkeypatch, ["open my banking password"])
    api._google_wake_loop()
    assert "[MicCompete]" not in capsys.readouterr().out


def test_wake_match_starts_a_voice_turn(monkeypatch):
    api = _wake_api(monkeypatch, ["hey mist open calculator"])
    lines = []
    api.voice_timer = VoiceTimer(log=lines.append)
    started = threading.Event()
    api._on_wake = lambda command=None: (api.woken.append(command), started.set())
    api._google_wake_loop()
    assert started.wait(2)
    assert api.woken == ["open calculator"]
    assert len(lines) == 1 and lines[0].startswith("[VoiceTiming] wake: wake_heard +")


# -- ask_mistai ---------------------------------------------------------------

@pytest.mark.parametrize("mode", ["voice", "text"])
def test_router_and_first_step_are_marked_in_voice_mode_only(monkeypatch, mode):
    from test_planner import _routing_api

    api = _routing_api(monkeypatch, '{"route": "task", "reply": ""}')
    lines = []
    api.voice_timer = VoiceTimer(log=lines.append)
    api.voice_timer.start("wake")
    Api.ask_mistai(api, "open my banking password", input_mode=mode)
    on_step = api.agent.callbacks["on_step"]
    on_step(1, "open_app", "notepad", "")
    on_step(2, "type_text", "secret", "")
    api.agent.callbacks["on_done"]({"success": True, "steps": 2})

    names = [l.split(": ", 1)[1].split(" +")[0] for l in lines]
    assert names == (["router_done", "first_step"] if mode == "voice" else [])
    assert not any(w in l for l in lines for w in VOICE_TIMING_SECRET_WORDS)


# -- AgentLoop: first_step before any gate; a confirmation listen is its own turn ----


# ============================================================
# VOICE TIMING LOOP MARKS
# ============================================================

def _loop_marks(monkeypatch, replies, mode="voice", answers=("yes",)):
    """One phase through the real AgentLoop with a "wake" turn open. The
    on_step callback marks first_step as Api._on_step does. Returns
    [(label, mark)] in log order."""
    import json
    from core.agent import AgentLoop
    from core.tools import SPECS

    lines, answers = [], list(answers)
    replies = iter(json.dumps(r) for r in replies)
    timer = VoiceTimer(log=lines.append)
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a == "quick_message"),
        executor=SimpleNamespace(run=lambda a, p: SimpleNamespace(
            action=a, parameter="", success=True, detail="", verified=None,
            escalate_to_user=False, reliability_note=lambda: "", escalated_from="")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        _last_opened_app="",
        voice_timer=timer,
    )
    api._voice_start = lambda label, t0=None: Api._voice_start(api, label, t0)
    api._voice_mark = lambda name, end=False: Api._voice_mark(api, name, end)

    def listen(timeout=10):                       # the real one's marks
        api._voice_mark("mic_acquired")
        api._voice_mark("stt_done")
        return answers.pop(0) if answers else None

    api._listen_once = listen
    api._get_text_answer = lambda _q, timeout=60: answers.pop(0) if answers else None

    def on_step(_n, _a, _p, _s):
        if mode == "voice":
            api._voice_mark("first_step", end=True)

    monkeypatch.setattr("core.agent.requests.post", lambda *_a, **_k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    timer.start("wake")
    loop._loop("open my banking password", "m", "none", mode, on_step)
    return [tuple(re.match(r"\[VoiceTiming\] (\w+): (\S+) \+", l).groups()) for l in lines]


# ============================================================
# VOICE TIMING HELPERS (2)
# ============================================================

DONE ={"action": "done", "speech": "ok"}
SEND = {"action": "quick_message", "parameter": "Discord|banking|password"}


# ============================================================
# VOICE TIMING TESTS (4)
# ============================================================

def test_the_first_step_ends_the_turn_once(monkeypatch):
    marks = _loop_marks(monkeypatch, [{"action": "open_app", "parameter": "notepad"}, DONE])
    assert marks == [("wake", "first_step")]


def test_a_first_step_confirmation_listen_is_its_own_turn(monkeypatch):
    marks = _loop_marks(monkeypatch, [SEND, DONE])
    assert marks == [("wake", "first_step"),
                     ("confirm", "mic_acquired"), ("confirm", "stt_done"),
                     ("confirm", "answered")]


def test_every_confirmation_listen_starts_a_fresh_turn(monkeypatch):
    marks = _loop_marks(monkeypatch, [{"action": "open_app", "parameter": "discord"},
                                      SEND, {**SEND, "parameter": "Discord|banking|open"},
                                      DONE], answers=("yes", "yes"))
    assert marks == [("wake", "first_step")] + [
        ("confirm", "mic_acquired"), ("confirm", "stt_done"), ("confirm", "answered")] * 2


def test_no_marks_in_text_mode(monkeypatch):
    assert _loop_marks(monkeypatch, [SEND, DONE], mode="text") == []


# ============================================================
# PUSH TO TALK
# ============================================================

# Push-to-talk (Api.push_to_talk): one press, one voice command, no wake word.
#
# Stubbed recognizer and microphone only: no audio device, no STT, no network.
# The turn's thread runs inline (threading.Thread is replaced) unless a test
# needs it to block.


# ============================================================
# PUSH TO TALK INLINE THREAD
# ============================================================

class _InlineThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


# ============================================================
# PUSH TO TALK PTT API
# ============================================================

def _ptt_api(monkeypatch, heard="open my banking password", inline=True):
    """An Api with a stubbed mic/recognizer; heard is a string, None (silence)
    or an exception to raise from recognize_google -- or a list of those, one
    per listen (the last one repeats)."""
    script = list(heard) if isinstance(heard, list) else [heard]
    if inline:
        monkeypatch.setattr(api_mod.threading, "Thread", _InlineThread)
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api._ptt_waiting = threading.Event()
    api.microphone = _VoiceTimingMic()
    api.events = []          # everything the turn does, in order
    api.asked = []
    profile = {"user_gender": "female"}
    api.memory = SimpleNamespace(get_profile=lambda k, d=None: profile.get(k, d),
                                 set_profile=profile.__setitem__)
    api.profile = profile
    api.tts = SimpleNamespace(interrupt=lambda: api.events.append("tts.interrupt"),
                              say=lambda *a, **k: api.events.append("tts.say"),
                              set_gender=lambda g: None)
    api._notify_js = lambda js: api.events.append(js)

    class Rec:
        """Push-to-talk's recognizer: each listen records its settings."""
        pause_threshold, non_speaking_duration = 0.8, 0.5     # sr.Recognizer defaults

        def adjust_for_ambient_noise(self, src, duration=1):
            api.events.append(f"adjust {duration}s (ptt_waiting={api._ptt_waiting.is_set()})")

        def listen(self, src, timeout=None, phrase_time_limit=None):
            api.events.append(f"listen timeout={timeout} limit={phrase_time_limit} "
                              f"pause={self.pause_threshold}")
            self.now = script.pop(0) if len(script) > 1 else script[0]
            if self.now is None:
                raise sr.WaitTimeoutError("listening timed out")
            return object()

        def recognize_google(self, audio):
            if isinstance(self.now, Exception):
                raise self.now
            return self.now

    class Shared:
        """The shared recognizer: push-to-talk must never use it."""
        def __getattr__(self, name):
            api.events.append(f"shared recognizer used: {name}")
            raise AssertionError("push-to-talk used the shared recognizer")

    api.ptt_recognizer = Rec()
    api.recognizer = Shared()

    def ask(message, model=None, gender=None, input_mode="text"):
        api.events.append("ask_mistai")
        api.asked.append((message, gender, input_mode, api._ptt_waiting.is_set(),
                          api._conversation_lock.locked()))
        return {"success": True}

    api.ask_mistai = ask
    return api


# ============================================================
# PUSH TO TALK HELPERS
# ============================================================

def _states(api):
    return [e for e in api.events if e.startswith("handleListenState")]


# ============================================================
# PUSH TO TALK TESTS
# ============================================================

def test_a_press_listens_once_and_runs_the_normal_voice_turn(monkeypatch, capsys):
    api = _ptt_api(monkeypatch, heard="Hey Mist open my banking password")
    assert api.push_to_talk() == {"success": True}
    assert api.events == [
        "tts.interrupt",                                  # Mist stops talking first
        'handleListenState("waiting","")',
        "adjust 0.3s (ptt_waiting=True)",                 # wake loop kept off the mic
        'handleListenState("listening","")',              # only once the mic is open
        "listen timeout=6 limit=30 pause=2.0",            # push-to-talk's own settings
        'handleListenState("idle","")',
        'handleVoiceTurn("open my banking password")',    # the chat turn (Stage 3)
        "ask_mistai",
        'handleVoiceReply(true,"",0,"")',
    ]
    # Wake prefix stripped, saved gender, voice mode; the mic flag is already
    # clear (the wake loop can hear "stop" mid-task) but the turn still owns
    # the conversation lock while the task runs.
    assert api.asked == [("open my banking password", "female", "voice", False, True)]
    assert not api._ptt_waiting.is_set() and not api._conversation_lock.locked()
    assert not any(w in capsys.readouterr().out for w in VOICE_TIMING_SECRET_WORDS)


def test_sir_or_maam_in_the_command_sets_the_gender_as_in_on_wake(monkeypatch):
    api = _ptt_api(monkeypatch, heard="open notepad sir")
    api.push_to_talk()
    assert api.asked[0][:3] == ("open notepad sir", "male", "voice")
    assert api.profile["user_gender"] == "male"


# ============================================================
# PUSH TO TALK HELPERS (2)
# ============================================================

NOTHING = 'handleListenState("idle","Didn\\\'t catch that")'


def _listens(api):
    return [e.split(" limit=")[0] for e in api.events if e.startswith("listen timeout=")]


# ============================================================
# PUSH TO TALK TESTS (2)
# ============================================================

@pytest.mark.parametrize("heard, listens", [
    (None, 1), (sr.UnknownValueError(), 1), ("", 1),
    ("hey mist", 2),            # wake phrase only: one follow-up, then give up
])
def test_no_words_shows_a_status_line_and_runs_nothing(monkeypatch, heard, listens):
    api = _ptt_api(monkeypatch, heard=heard)
    assert api.push_to_talk() == {"success": True}
    assert _states(api)[-1] == NOTHING
    assert api.asked == []
    assert len(_listens(api)) == listens
    assert "tts.say" not in api.events                   # a status line, not spoken
    assert not api._ptt_waiting.is_set() and not api._conversation_lock.locked()


# -- only the wake phrase heard: one silent follow-up listen ---------------------------

def test_wake_phrase_then_a_command_runs_the_command(monkeypatch, capsys):
    api = _ptt_api(monkeypatch, heard=["hey mist", "open my banking password"])
    api.push_to_talk()
    assert _listens(api) == ["listen timeout=6", "listen timeout=5"]   # 5s to start
    assert api.asked == [("open my banking password", "female", "voice", False, True)]
    assert "tts.say" not in api.events                   # silent: no "Yes?"
    assert _states(api) == [
        'handleListenState("waiting","")',
        'handleListenState("listening","")',
        'handleListenState("listening","")',             # the follow-up's mic is open
        'handleListenState("idle","")',
    ]
    assert not any(w in capsys.readouterr().out for w in VOICE_TIMING_SECRET_WORDS)


def test_the_follow_up_strips_the_wake_phrase_too(monkeypatch):
    api = _ptt_api(monkeypatch, heard=["mist", "hey mist open notepad"])
    api.push_to_talk()
    assert [a[0] for a in api.asked] == ["open notepad"]


@pytest.mark.parametrize("follow_up", ["hey mist", None, sr.UnknownValueError()],
                         ids=["wake phrase twice", "then silence", "then unclear"])
def test_one_follow_up_only_then_didnt_catch_that(monkeypatch, follow_up):
    api = _ptt_api(monkeypatch, heard=["hey mist", follow_up, "open notepad"])
    api.push_to_talk()
    assert len(_listens(api)) == 2                        # never a third listen
    assert _states(api)[-1] == NOTHING and api.asked == []
    assert "tts.say" not in api.events


@pytest.mark.parametrize("heard", [None, "open notepad"], ids=["silence", "a command"])
def test_no_follow_up_unless_only_the_wake_phrase_was_heard(monkeypatch, heard):
    api = _ptt_api(monkeypatch, heard=[heard, "should never be heard"])
    api.push_to_talk()
    assert _listens(api) == ["listen timeout=6"]


def test_the_wake_loop_stays_off_the_mic_through_the_follow_up(monkeypatch):
    api = _ptt_api(monkeypatch, heard=["hey mist", "open notepad"])
    flags = []
    real = api._listen_once

    def listen(**kw):
        flags.append(api._ptt_waiting.is_set())           # set before each listen...
        out = real(**kw)
        flags.append(api._ptt_waiting.is_set())           # ...and still set after it
        return out

    api._listen_once = listen
    api.push_to_talk()
    assert flags == [True, True, True, True]
    assert api.asked[0][3] is False                       # cleared after the LAST listen


def test_refused_while_a_task_runs_without_touching_the_mic(monkeypatch):
    api = _ptt_api(monkeypatch)
    api._agent_lock.acquire()
    assert api.push_to_talk() == {"success": False, "error": "busy",
                                  "message": "Mist is working. Press Stop or say stop."}
    assert api.events == [] and not api._conversation_lock.locked()


def test_refused_while_a_conversation_is_active(monkeypatch):
    api = _ptt_api(monkeypatch)
    api._conversation_lock.acquire()          # e.g. a wake conversation
    assert api.push_to_talk() == {"success": False, "error": "listening",
                                  "message": "Already listening."}
    assert api.events == []
    assert api._conversation_lock.locked()    # still the wake conversation's


@pytest.mark.parametrize("where", ["interrupt", "listen", "ask"])
def test_the_lock_and_flag_are_released_on_every_exception(monkeypatch, capsys, where):
    api = _ptt_api(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("open my banking password")

    if where == "interrupt":
        api.tts.interrupt = boom
    elif where == "listen":
        api._listen_once = boom
    else:
        api.ask_mistai = boom
    api.push_to_talk()
    assert not api._ptt_waiting.is_set() and not api._conversation_lock.locked()
    out = capsys.readouterr().out
    assert "[PushToTalk] Turn failed (RuntimeError)" in out
    assert not any(w in out for w in VOICE_TIMING_SECRET_WORDS)
    expected_idle = ('handleListenState("idle","")' if where == "ask"
                     else 'handleListenState("idle","Push-to-talk failed")')
    assert _states(api)[-1] == expected_idle
    assert api.push_to_talk() == {"success": True}      # usable again


def test_a_second_press_while_the_first_listens_stops_it(monkeypatch):
    api = _ptt_api(monkeypatch, inline=False)
    in_listen, finish = threading.Event(), threading.Event()
    real = api._listen_once

    def slow_listen(**kw):
        in_listen.set()
        finish.wait(5)
        return real(**kw)

    api._listen_once = slow_listen
    assert api.push_to_talk() == {"success": True}
    assert in_listen.wait(5)
    # Change 3b: a second press ends the listen (see test_ptt_second_press.py);
    # this stub's mic has no stream, so what was "heard" still runs.
    assert api.push_to_talk() == {"success": True, "stopped": True}
    assert api._ptt_stop.is_set()
    finish.set()
    for _ in range(100):
        if not api._conversation_lock.locked():
            break
        threading.Event().wait(0.02)
    assert not api._conversation_lock.locked() and len(api.asked) == 1
    assert not api._ptt_stop.is_set()                    # cleared when the turn ends


def test_thread_start_failure_releases_the_lock(monkeypatch):
    api = _ptt_api(monkeypatch)

    class NoThread:
        def __init__(self, **_k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(api_mod.threading, "Thread", NoThread)
    with pytest.raises(RuntimeError):
        api.push_to_talk()
    assert not api._conversation_lock.locked()


# -- _listen_once's optional callback ----------------------------------------------

def test_on_listening_is_optional_and_a_failing_callback_does_not_stop_the_listen(
        monkeypatch, capsys):
    api = _ptt_api(monkeypatch, heard="open notepad")
    api.recognizer = api.ptt_recognizer         # the shared path, with a working stub
    assert api._listen_once(timeout=3) == "open notepad"           # existing call form

    def bad():
        raise ValueError("nope")

    assert api._listen_once(timeout=3, on_listening=bad) == "open notepad"
    assert "[ListenOnce] on_listening failed (ValueError)" in capsys.readouterr().out


# -- the wake loop leaves the mic to a press -------------------------------------------


# ============================================================
# PUSH TO TALK RECORDING LOCK
# ============================================================

class _RecordingLock:
    """The wake loop's mic lock, recording whether a press was waiting at
    each attempt to take it."""

    def __init__(self, api):
        self._lock, self._api, self.takes = threading.Lock(), api, []

    def acquire(self, blocking=True):
        self.takes.append(self._api._ptt_waiting.is_set())
        if len(self.takes) > 50:              # a regression must fail, not hang
            self._api._wake_shutdown.set()
        return self._lock.acquire(blocking)

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()


# ============================================================
# PUSH TO TALK TESTS (3)
# ============================================================

def test_wake_loop_does_not_take_the_mic_while_a_press_waits(monkeypatch):
    api = _wake_api(monkeypatch, ["hey mist open calculator"])
    api._ptt_waiting = threading.Event()
    api._ptt_waiting.set()
    api._mic_lock = lock = _RecordingLock(api)
    spins = []

    def sleep(_s):
        spins.append(1)
        if len(spins) == 5:          # the press finishes listening
            api._ptt_waiting.clear()

    monkeypatch.setattr(api_mod.time, "sleep", sleep)
    started = threading.Event()
    api._on_wake = lambda command=None: (api.woken.append(command), started.set())
    api._google_wake_loop()
    assert len(spins) >= 5                      # it waited while the press was on
    assert lock.takes and not any(lock.takes)   # never tried the mic meanwhile
    assert started.wait(2) and api.woken == ["open calculator"]   # then resumed


def test_wake_calibration_also_waits_for_a_press(monkeypatch):
    from core import tts as wake_calibration

    api = _wake_api(monkeypatch, [])
    api._ptt_waiting = threading.Event()
    api._ptt_waiting.set()
    api._mic_lock = lock = _RecordingLock(api)
    monkeypatch.setattr(wake_calibration.WakeCalibration, "due",
                        lambda self, now: "startup")       # always wants to calibrate
    spins = []

    def sleep(_s):
        spins.append(1)
        if len(spins) == 5:
            api._wake_shutdown.set()

    monkeypatch.setattr(api_mod.time, "sleep", sleep)
    api._google_wake_loop()
    assert len(spins) == 5 and lock.takes == []    # never touched the mic


def test_voice_stop_during_a_task_is_unchanged(monkeypatch):
    """The stop-word path never looks at the press flag once it is clear."""
    api = _wake_api(monkeypatch, ["stop"])
    api._ptt_waiting = threading.Event()
    api._agent_lock.acquire()
    stopped, spoken = [], []
    api.stop_current_task = lambda: stopped.append(1)
    api.speak = lambda text, interrupt=False: spoken.append(text)
    api._google_wake_loop()
    assert stopped == [1] and spoken == ["Stopping."]


# ============================================================
# SECOND PRESS
# ============================================================

# A second press ends a push-to-talk listen early (Change 3b).
#
# The REAL SpeechRecognition listen() runs on a fake AudioSource whose stream
# plays scripted chunks (silence or speech); only recognize_google is stubbed.
# A press during the listen makes the stream proxy return b"" (the end of the
# stream): before any speech the turn ends quietly with no recognition call;
# after speech what was heard runs as usual. The stop event is cleared at the
# start and end of every turn. No audio device, no STT, no network.


# ============================================================
# SECOND PRESS HELPERS
# ============================================================

QUIET = b"\x00\x00" * 1024
LOUD = (6000).to_bytes(2, "little", signed=True) * 1024


# ============================================================
# SECOND PRESS STREAM
# ============================================================

class _Stream:
    """The real stream: plays `script` ("q" silence, "L" speech; then
    silence), calling `press()` during read number `press_at` (1-based)."""

    def __init__(self, script, press_at=None, press=None):
        self.script, self.press_at, self.press = list(script), press_at, press
        self.reads, self.closed = 0, False

    def read(self, size):
        self.reads += 1
        if self.press_at == self.reads:
            self.press()
        kind = self.script.pop(0) if self.script else "q"
        return LOUD if kind == "L" else QUIET

    def close(self):
        self.closed = True


# ============================================================
# SECOND PRESS MIC
# ============================================================

class _SecondPressMic(sr.AudioSource):
    """Like sr.Microphone: a fresh stream per `with`, and __exit__ closes
    whatever self.stream is (the proxy, during a press)."""
    CHUNK, SAMPLE_RATE, SAMPLE_WIDTH = 1024, 16000, 2

    def __init__(self, streams):
        self.streams, self.stream, self.opened, self.listened_on = list(streams), None, [], []

    def __enter__(self):
        self.stream = self.streams.pop(0)
        self.opened.append(self.stream)
        return self

    def __exit__(self, *a):
        try:
            self.stream.close()
        finally:
            self.stream = None


# ============================================================
# SECOND PRESS REC
# ============================================================

class _SecondPressRec(sr.Recognizer):
    def __init__(self, heard, mic):
        super().__init__()
        self.heard, self.mic, self.recognized = list(heard), mic, 0
        self.energy_threshold, self.dynamic_energy_threshold = 300, False

    def listen(self, source, *a, **k):
        self.mic.listened_on.append(source.stream)
        return super().listen(source, *a, **k)

    def recognize_google(self, audio, **k):
        self.recognized += 1
        return self.heard.pop(0)


# ============================================================
# SECOND PRESS API
# ============================================================

def _second_press_api(monkeypatch, streams, heard=()):
    api = _ptt_api(monkeypatch)
    api.microphone = _SecondPressMic(streams)
    api.ptt_recognizer = _SecondPressRec(heard, api.microphone)
    api._ptt_calibrated_at = time.monotonic()      # skip the 0.3s adjustment
    api._ptt_stop = threading.Event()
    return api


# ============================================================
# SECOND PRESS HELPERS (2)
# ============================================================

def _second_press_press(api):
    """The second press, from inside the listen (as the UI would)."""
    def press():
        api.second_press = api.push_to_talk()
    return press


# -- before any speech: a quiet cancel -------------------------------------------------------


# ============================================================
# SECOND PRESS TESTS
# ============================================================

def test_a_stop_before_speech_ends_within_one_chunk_with_no_recognition(monkeypatch, capsys):
    api = _second_press_api(monkeypatch, [])
    stream = _Stream("qqqqqqqq", press_at=5, press=_second_press_press(api))
    api.microphone.streams = [stream]
    assert api.push_to_talk() == {"success": True}
    assert api.second_press == {"success": True, "stopped": True}
    assert stream.reads == 5                          # nothing read after the press
    assert api.ptt_recognizer.recognized == 0         # no speech-recognition call
    assert api.asked == []
    assert NOTHING not in _states(api)                # no "Didn't catch that"
    assert _states(api)[-1] == 'handleListenState("idle","")'
    assert "[PushToTalk] Stopped before any speech (cancelled)" in capsys.readouterr().out
    assert not api._ptt_stop.is_set() and not api._conversation_lock.locked()


def test_the_proxy_wraps_the_listen_and_closes_the_real_stream(monkeypatch):
    api = _second_press_api(monkeypatch, [])
    stream = _Stream("qqq", press_at=2, press=_second_press_press(api))
    api.microphone.streams = [stream]
    api.push_to_talk()
    listened = api.microphone.listened_on[0]
    assert isinstance(listened, _StoppableStream)     # listen() read through the proxy
    assert stream.closed and api.microphone.stream is None


def test_the_proxy_alone(monkeypatch):
    stop, closed = threading.Event(), []
    real = _Stream("LqL")
    real.close = lambda: closed.append(True)
    rec = sr.Recognizer()
    rec.energy_threshold = 300
    p = _StoppableStream(real, stop, rec, 2)
    assert p.read(1024) == LOUD and p.speech is True
    stop.set()
    assert p.read(1024) == b"" and p.read(1024) == b"" and real.reads == 1
    p.close()
    assert closed == [True]


# -- after speech: what was heard runs ------------------------------------------------------

def test_a_stop_after_speech_sends_what_was_heard(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL", press_at=8, press=_second_press_press(api))
    api.microphone.streams = [stream]
    api.push_to_talk()
    assert api.second_press["stopped"] is True
    assert stream.reads == 8                          # ended at the press, not by a pause
    assert api.ptt_recognizer.recognized == 1
    assert [a[0] for a in api.asked] == ["open notepad"]
    assert not api._ptt_stop.is_set()


def test_without_a_press_the_listen_ends_by_the_pause(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL")
    api.microphone.streams = [stream]
    api.push_to_talk()
    pause_chunks = int(2.0 / (1024 / 16000))          # PTT_PAUSE_THRESHOLD of silence
    assert stream.reads > 10 + pause_chunks
    assert [a[0] for a in api.asked] == ["open notepad"]


def test_a_stop_after_only_the_wake_phrase_skips_the_follow_up(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["hey mist"])
    stream = _Stream("LLLLL", press_at=4, press=_second_press_press(api))
    api.microphone.streams = [stream, _Stream("LLLLLLLL")]   # a follow-up would open this
    api.push_to_talk()
    assert len(api.microphone.opened) == 1 and api.ptt_recognizer.recognized == 1
    assert _states(api)[-1] == NOTHING and api.asked == []


def test_a_stop_before_speech_in_the_follow_up_cancels(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["hey mist"])
    first = _Stream("LLLLLLLL")                       # "hey mist", ended by the pause
    follow = _Stream("qqqq", press_at=3, press=_second_press_press(api))
    api.microphone.streams = [first, follow]
    api.push_to_talk()
    assert len(api.microphone.opened) == 2 and follow.reads == 3
    assert api.ptt_recognizer.recognized == 1         # only the first listen
    assert api.asked == [] and NOTHING not in _states(api)


# -- the stop never leaks -------------------------------------------------------------------

def test_a_stale_stop_is_cleared_when_a_turn_starts(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLL")
    api.microphone.streams = [stream]
    api._ptt_stop.set()                               # left over from anywhere
    api.push_to_talk()
    assert stream.reads > 8                           # the listen was not cut short
    assert [a[0] for a in api.asked] == ["open notepad"]


def test_one_press_never_reaches_the_next_turn(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    first = _Stream("qqq", press_at=2, press=_second_press_press(api))
    second = _Stream("qqLLLLLL")
    api.microphone.streams = [first, second]
    api.push_to_talk()                                # cancelled
    assert not api._ptt_stop.is_set()
    api.push_to_talk()                                # a new turn listens normally
    assert second.reads > 8 and [a[0] for a in api.asked] == ["open notepad"]


def test_the_stop_is_cleared_even_when_the_turn_fails(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL", press_at=5, press=_second_press_press(api))
    api.microphone.streams = [stream]

    def boom(*a, **k):
        raise RuntimeError("ask failed")

    api.ask_mistai = boom
    api.push_to_talk()
    assert not api._ptt_stop.is_set() and not api._conversation_lock.locked()


# -- other presses keep today's refusals ----------------------------------------------------

def test_a_press_during_a_wake_conversation_is_still_refused(monkeypatch):
    api = _second_press_api(monkeypatch, [])
    api._conversation_lock.acquire()                  # the wake conversation holds it
    assert api.push_to_talk() == {"success": False, "error": "listening",
                                  "message": "Already listening."}
    assert not api._ptt_stop.is_set()


def test_a_press_after_the_listen_ended_is_refused_as_before(monkeypatch):
    api = _second_press_api(monkeypatch, [], heard=["open notepad"])
    api.microphone.streams = [_Stream("qqLLLLLLLL")]
    seen = []

    def ask(message, model=None, gender=None, input_mode="text"):
        seen.append(api.push_to_talk())               # the listen is over: not a stop
        api._agent_lock.acquire()                     # and once the task runs
        seen.append(api.push_to_talk())
        api._agent_lock.release()
        return {"success": True}

    api.ask_mistai = ask
    api.push_to_talk()
    assert [r.get("error") for r in seen] == ["listening", "busy"]
    assert not any(r.get("stopped") for r in seen)


def test_other_listens_never_use_the_proxy(monkeypatch):
    # The shared path still runs its 0.3s adjustment (a few quiet chunks) first.
    api = _second_press_api(monkeypatch, [_Stream("qqqqqqqLLLLLLLLLL")], heard=["yes"])
    api.recognizer = api.ptt_recognizer               # a working shared stub
    api._ptt_stop.set()                               # even with a stop pending
    assert api._listen_once(timeout=5) == "yes"       # e.g. a voice ask_user answer
    assert not isinstance(api.microphone.listened_on[0], _StoppableStream)


# ============================================================
# LISTEN SETTINGS
# ============================================================

# Push-to-talk listening settings (Api._listen_once(ptt=True)).
#
# A press listens on its OWN recognizer: 2.0s pause, 30s phrase limit, and the
# 0.3s noise adjustment only on the first press and once the measured threshold
# is PTT_THRESHOLD_MAX_AGE old. Every other listen (wake conversation, voice
# ask_user, confirmations) keeps the shared recognizer exactly as before.
# [VoiceTiming] gains mic_open (mic_acquired -> mic_open: opening the mic;
# mic_open -> listen_start: the adjustment). Stubbed mic and recognizers only.


# ============================================================
# LISTEN SETTINGS HELPERS
# ============================================================

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# LISTEN SETTINGS CLOCK
# ============================================================

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ============================================================
# LISTEN SETTINGS MIC
# ============================================================

class _ListenSettingsMic:
    """Opening the mic takes `open_s` on the fake clock."""

    def __init__(self, clock, open_s=0.0):
        self.clock, self.open_s = clock, open_s

    def __enter__(self):
        self.clock.t += self.open_s
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# LISTEN SETTINGS REC
# ============================================================

class _ListenSettingsRec:
    pause_threshold, non_speaking_duration = 0.8, 0.5     # sr.Recognizer defaults

    def __init__(self, clock, name, calls, adjust_raises=None):
        self.clock, self.name, self.calls = clock, name, calls
        self.adjust_raises = adjust_raises

    def adjust_for_ambient_noise(self, src, duration=1):
        self.calls.append((self.name, "adjust", duration))
        if self.adjust_raises:
            raise self.adjust_raises
        self.clock.t += duration

    def listen(self, src, timeout=None, phrase_time_limit=None):
        self.calls.append((self.name, "listen", timeout, phrase_time_limit,
                           self.pause_threshold, self.non_speaking_duration))
        return object()

    def recognize_google(self, audio):
        return "open my banking password"


# ============================================================
# LISTEN SETTINGS HELPERS (2)
# ============================================================

@pytest.fixture
def setup(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(api_mod.time, "monotonic", clock)
    lines, calls = [], []
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _ListenSettingsMic(clock, open_s=0.25)
    api.voice_timer = VoiceTimer(clock=clock, log=lines.append)
    api.recognizer = _ListenSettingsRec(clock, "shared", calls)
    api.ptt_recognizer = _ListenSettingsRec(clock, "ptt", calls)
    return api, clock, lines, calls


def _listen_settings_press(api, timeout=6):
    api.voice_timer.start("ptt")
    return api._listen_once(timeout=timeout, ptt=True)


def _marks(lines):
    return [re.match(r"\[VoiceTiming\] \w+: (\S+) \+(\d+\.\d+)s", l).groups() for l in lines
            if l.startswith("[VoiceTiming]")]


# -- push-to-talk's own settings ----------------------------------------------------------


# ============================================================
# LISTEN SETTINGS TESTS
# ============================================================

def test_a_press_uses_its_own_recognizer_and_settings(setup, capsys):
    api, _clock, lines, calls = setup
    assert _listen_settings_press(api) == "open my banking password"
    assert calls == [("ptt", "adjust", 0.3),
                     ("ptt", "listen", 6, 30, 2.0, 0.5)]   # timeout, limit, pause, non-speaking
    shared = api.recognizer                                  # never touched
    assert (shared.pause_threshold, shared.non_speaking_duration) == (0.8, 0.5)
    assert (config.PTT_PAUSE_THRESHOLD, config.PTT_PHRASE_TIME_LIMIT,
            config.PTT_THRESHOLD_MAX_AGE) == (2.0, 30, 600)
    out = capsys.readouterr().out + "\n".join(lines)
    # The heard words never reach a log ("open" is also in the mark mic_open).
    assert not any(w in out for w in VOICE_TIMING_SECRET_WORDS if w != "open")
    assert "open my" not in out


def test_mic_open_splits_opening_from_the_adjustment(setup):
    api, _clock, lines, _calls = setup
    _listen_settings_press(api)
    assert _marks(lines) == [("mic_acquired", "0.00"),
                             ("mic_open", "0.25"),          # opening the mic
                             ("listen_start", "0.30"),      # the noise adjustment
                             ("speech_end", "0.00"), ("stt_done", "0.00")]


def test_the_recognizer_is_created_once_and_is_not_the_shared_one():
    api = Api.__new__(Api)
    api.recognizer = sr.Recognizer()
    first = api._ptt_recognizer()
    assert isinstance(first, sr.Recognizer) and first is not api.recognizer
    assert api._ptt_recognizer() is first


# -- the noise threshold is reused, then recalibrated ---------------------------------------

def test_a_later_press_reuses_the_threshold_until_it_expires(setup, capsys):
    api, clock, lines, calls = setup
    _listen_settings_press(api)
    clock.t += 30
    _listen_settings_press(api)                                              # 30s later: reused
    clock.t += config.PTT_THRESHOLD_MAX_AGE - 31 - 0.5       # just under 10 minutes old
    _listen_settings_press(api)
    adjusts = [c for c in calls if c[1] == "adjust"]
    assert len(adjusts) == 1
    out = capsys.readouterr().out
    assert out.count("[PushToTalk] Noise threshold reused") == 2
    # A reused press goes straight from the open mic to listening.
    reused = _marks(lines)[5:10]
    assert reused[1] == ("mic_open", "0.25") and reused[2] == ("listen_start", "0.00")

    clock.t += 1                                             # now over 10 minutes old
    _listen_settings_press(api)
    assert len([c for c in calls if c[1] == "adjust"]) == 2  # recalibrated once
    clock.t += 5
    _listen_settings_press(api)                                              # and reused again
    assert len([c for c in calls if c[1] == "adjust"]) == 2


def test_a_failed_adjustment_is_not_reused(setup):
    api, clock, _lines, calls = setup
    api.ptt_recognizer.adjust_raises = OSError("stream closed")
    assert _listen_settings_press(api) is None
    api.ptt_recognizer.adjust_raises = None
    clock.t += 5
    _listen_settings_press(api)
    assert [c[1] for c in calls if c[1] == "adjust"] == ["adjust", "adjust"]


def test_the_shared_path_never_reuses_or_shares_the_threshold(setup):
    api, _clock, _lines, calls = setup
    _listen_settings_press(api)
    api._listen_once(timeout=5)                              # e.g. a wake follow-up
    api._listen_once(timeout=5)
    assert [c[:2] for c in calls if c[0] == "shared"] == [
        ("shared", "adjust"), ("shared", "listen"), ("shared", "adjust"), ("shared", "listen")]


# -- every other listen is unchanged ---------------------------------------------------------

def test_the_shared_listen_keeps_its_defaults(setup):
    api, _clock, lines, calls = setup
    api.voice_timer.start("followup")
    api._listen_once(timeout=5)
    assert calls == [("shared", "adjust", 0.3), ("shared", "listen", 5, 15, 1.0, 0.5)]
    # Its [VoiceTiming] marks are unchanged too (no mic_open).
    assert [m[0] for m in _marks(lines)] == ["mic_acquired", "listen_start", "speech_end",
                                            "stt_done"]


def test_only_push_to_talk_passes_ptt():
    """The wake conversation, voice ask_user and confirmations call
    _listen_once without ptt; only the two push-to-talk listens pass it."""
    found = []
    for rel in ("core/api.py", "core/agent.py", "core/planner.py"):
        with open(os.path.join(REPO, rel), encoding="utf-8") as f:
            for line in f:
                if re.search(r"\b(?:self|api)\._listen_once\(", line):
                    found.append((rel, "ptt=True" in line))
    ptt = [rel for rel, on in found if on]
    assert ptt == ["core/api.py", "core/api.py"]
    # The other callers, all default: the wake conversation (3), voice ask_user,
    # and Api.ask_confirmation (the agent's and the planner's confirmations).
    assert len(found) - len(ptt) >= 5


# ============================================================
# VOICE DIAGNOSTICS
# ============================================================

# MISTAI_VOICE_DIAG=1: one "[VoiceDiag]" line per _listen_once, numbers and
# an exception type only. Off by default. Recognition settings are unchanged.


# ============================================================
# VOICE DIAGNOSTICS HELPERS
# ============================================================

SPOKEN = "yes send it to joanne quillfeather"
LINE_RX = re.compile(
    r"^\[VoiceDiag\] (\w+) listen: clip=(na|\d+\.\d\d)s rms=(na|\d+) peak=(na|\d+) "
    r"sr=(na|\d+) thr=(na|\d+)->(na|\d+) since_tts=(na|\d+\.\d\d)s "
    r"mic_wait=(na|\d+\.\d\d)s rec=(shared|ptt) result=(\w+)$")


# ============================================================
# VOICE DIAGNOSTICS MIC
# ============================================================

class _VoiceDiagnosticsMic:
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    stream = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# VOICE DIAGNOSTICS HELPERS (2)
# ============================================================

def _voice_diagnostics_audio(level=1000, seconds=0.5, rate=16000):
    n = int(seconds * rate)
    return sr.AudioData(struct.pack(f"<{n}h", *([level, -level] * (n // 2))), rate, 2)


# ============================================================
# VOICE DIAGNOSTICS API
# ============================================================

def _voice_diagnostics_api(monkeypatch, outcome, *, finished_ago=0.4, speaking=False, ptt=False):
    """A bare Api whose listen returns 0.5 s of a square wave at 1000 and whose
    recognizer then returns SPOKEN or raises ``outcome``."""
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _VoiceDiagnosticsMic()
    api.voice_timer = VoiceTimer(log=lambda _l: None)
    now = 1000.0
    monkeypatch.setattr("core.api.time.monotonic", lambda: now)
    api.tts = SimpleNamespace(speaking=speaking,
                              finished_at=None if finished_ago is None else now - finished_ago)
    calls = []

    class Rec:
        energy_threshold = 300

        def adjust_for_ambient_noise(self, src, duration=1):
            calls.append(("adjust", duration))
            self.energy_threshold = 845

        def listen(self, src, timeout=None, phrase_time_limit=None):
            calls.append(("listen", timeout, phrase_time_limit))
            if outcome is sr.WaitTimeoutError:
                raise sr.WaitTimeoutError("listening timed out while waiting for phrase")
            return _voice_diagnostics_audio()

        def recognize_google(self, audio):
            calls.append(("recognize",))
            if isinstance(outcome, type) and issubclass(outcome, Exception):
                raise outcome()
            return SPOKEN

    api.recognizer = Rec()
    api.ptt_recognizer = Rec()
    api._ptt_calibrated_at = None
    api.calls = calls
    return api


# ============================================================
# VOICE DIAGNOSTICS HELPERS (3)
# ============================================================

@pytest.fixture
def diag_on(monkeypatch):
    monkeypatch.setenv("MISTAI_VOICE_DIAG", "1")


def _lines(out):
    return [l for l in out.splitlines() if l.startswith("[VoiceDiag]")]


# ============================================================
# VOICE DIAGNOSTICS TESTS
# ============================================================

@pytest.mark.parametrize("outcome, result", [
    (None, "ok"), (sr.UnknownValueError, "UnknownValueError"),
    (sr.RequestError, "RequestError"), (sr.WaitTimeoutError, "WaitTimeoutError"),
])
def test_one_line_per_listen_with_numbers_and_the_exception_type(monkeypatch, capsys, diag_on,
                                                                 outcome, result):
    api = _voice_diagnostics_api(monkeypatch, outcome)
    api.voice_timer.start("confirm")
    Api._listen_once(api, timeout=10)
    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1
    m = LINE_RX.match(lines[0])
    assert m, lines[0]
    label, clip, rms, peak, rate, thr0, thr1, since, wait, rec, res = m.groups()
    assert (label, rec, res, thr0, thr1, since, rate) == (
        "confirm", "shared", result, "300", "845", "0.40", "16000")
    if outcome is sr.WaitTimeoutError:
        assert (clip, rms, peak) == ("na", "na", "na")   # no audio was captured
    else:
        assert (clip, rms, peak) == ("0.50", "1000", "1000")


def test_the_line_never_carries_the_transcript(monkeypatch, capsys, diag_on):
    api = _voice_diagnostics_api(monkeypatch, None)
    assert Api._listen_once(api, timeout=10) == SPOKEN
    out = capsys.readouterr().out
    tokens = set(re.findall(r"[a-z]+", _lines(out)[0].split("listen:", 1)[1]))
    assert tokens <= {"clip", "s", "rms", "peak", "sr", "thr", "since", "tts", "mic", "wait",
                      "rec", "shared", "result", "ok"}
    assert "quillfeather" not in out.lower() and "joanne" not in out.lower()


def test_label_falls_back_to_listen_and_since_tts_is_na_while_speaking(monkeypatch, capsys,
                                                                       diag_on):
    api = _voice_diagnostics_api(monkeypatch, sr.UnknownValueError, speaking=True)
    Api._listen_once(api, timeout=10)
    line = _lines(capsys.readouterr().out)[0]
    assert line.startswith("[VoiceDiag] listen listen:") and "since_tts=nas" in line


def test_push_to_talk_reports_its_own_recognizer_and_settings_are_unchanged(monkeypatch,
                                                                             capsys, diag_on):
    api = _voice_diagnostics_api(monkeypatch, None)
    api._voice_mark = lambda *a, **k: None
    Api._listen_once(api, timeout=5, ptt=True)
    assert "rec=ptt" in _lines(capsys.readouterr().out)[0]
    from core.config import PTT_PAUSE_THRESHOLD, PTT_PHRASE_TIME_LIMIT
    assert api.ptt_recognizer.pause_threshold == PTT_PAUSE_THRESHOLD
    assert ("listen", 5, PTT_PHRASE_TIME_LIMIT) in api.calls


@pytest.mark.parametrize("diag", [None, "1"])
def test_the_shared_listen_settings_are_the_same_with_and_without_diag(monkeypatch, capsys,
                                                                       diag):
    if diag:
        monkeypatch.setenv("MISTAI_VOICE_DIAG", diag)
    else:
        monkeypatch.delenv("MISTAI_VOICE_DIAG", raising=False)
    api = _voice_diagnostics_api(monkeypatch, None)
    Api._listen_once(api, timeout=10)
    assert api.calls == [("adjust", 0.3), ("listen", 10, 15), ("recognize",)]
    assert (api.recognizer.pause_threshold, api.recognizer.non_speaking_duration) == (1.0, 0.5)
    assert bool(_lines(capsys.readouterr().out)) is bool(diag)


def test_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VOICE_DIAG", raising=False)
    from core.config import voice_diag_enabled
    assert voice_diag_enabled() is False
    Api._listen_once(_voice_diagnostics_api(monkeypatch, sr.UnknownValueError), timeout=10)
    assert _lines(capsys.readouterr().out) == []


def test_a_broken_stat_never_breaks_the_listen(monkeypatch, capsys, diag_on):
    api = _voice_diagnostics_api(monkeypatch, None)
    api.microphone.SAMPLE_RATE = "weird"
    api.recognizer.energy_threshold = None
    api.recognizer.adjust_for_ambient_noise = lambda src, duration=1: None
    assert Api._listen_once(api, timeout=10) == SPOKEN
    assert "thr=na->na" in _lines(capsys.readouterr().out)[0]


def test_tts_records_when_the_last_utterance_finished():
    from queue import Queue

    from core import tts as tts_mod
    t = tts_mod.NeuralTTS.__new__(tts_mod.NeuralTTS)
    t._q, t._cv, t._outstanding, t.finished_at = Queue(), threading.Condition(), 2, None
    t._speak_one = lambda gen, text: None
    threading.Thread(target=t._worker, daemon=True).start()
    t._q.put((0, "first"))
    t._q.join()
    assert t.finished_at is None and t.speaking          # one still outstanding
    t._q.put((0, "second"))
    t._q.join()
    assert t.finished_at is not None and not t.speaking


# ============================================================
# VOICE TURNS
# ============================================================

# Voice turns in the chat (Api._voice_ask, ui/html.py handleVoiceTurn /
# handleVoiceReply): push-to-talk and the wake conversation both show what Mist
# HEARD and the result, like a typed turn. The words reach the page only,
# escaped, and are never logged. Stubbed mic and recognizer; Node tests skip
# when Node is missing.
#
# Merged by area (pure moves, no test changed) from: test_voice_turns.py, test_task_ack_heartbeat.py.


# ============================================================
# VOICE TURNS HELPERS
# ============================================================

# A quote, an apostrophe and a script tag, in both the words and the reply.
HEARD = 'open "notes" it\'s </script><script>alert(1)</script> banking password'


REPLY = 'Opened "notes" </script><b>bold</b> it\'s done'


VOICE_TURNS_SECRET_WORDS = ("notes", "banking", "password", "alert", "script")


def _ok(speech=REPLY, steps=2):
    return {"success": True, "command": {"action": "done", "parameter": "", "speech": speech},
            "steps": steps, "error": ""}


# ============================================================
# VOICE TURNS VOICE API
# ============================================================

def _voice_api(result=None):
    api = Api.__new__(Api)
    api.events = []
    api._notify_js = api.events.append

    def ask(message, model=None, gender=None, input_mode="text"):
        api.events.append(("ask_mistai", message, gender, input_mode))
        if isinstance(result, Exception):
            raise result
        return result if result is not None else _ok()

    api.ask_mistai = ask
    return api


# ============================================================
# VOICE TURNS HELPERS (2)
# ============================================================

def _turn(words):
    return f'handleVoiceTurn("{esc(words)}")'


def _reply(success, speech, steps, error):
    return (f'handleVoiceReply({"true" if success else "false"},"{esc(speech)}",'
            f'{steps},"{esc(error)}")')


# -- _voice_ask ---------------------------------------------------------------------


# ============================================================
# VOICE TURNS TESTS
# ============================================================

@pytest.mark.parametrize("result, reply", [
    (_ok(), (True, REPLY, 2, "")),
    ({"success": False, "command": {"speech": "I'm still working on 'x'. Say stop, or wait."},
      "steps": 0, "error": "busy", "busy": True},
     (False, "I'm still working on 'x'. Say stop, or wait.", 0, "busy")),
    ({"success": False, "command": {"speech": ""}, "steps": 0, "error": "still_running"},
     (False, "", 0, "still_running")),
    ({"success": False, "error": "boom"}, (False, "", 0, "boom")),
])
def test_voice_ask_sends_the_words_then_the_result(result, reply):
    api = _voice_api(result)
    assert api._voice_ask(HEARD, "female") is result
    assert api.events == [_turn(HEARD), ("ask_mistai", HEARD, "female", "voice"),
                          _reply(*reply)]


def test_a_failed_turn_still_closes_the_chat_turn():
    api = _voice_api(RuntimeError(HEARD))
    with pytest.raises(RuntimeError):
        api._voice_ask(HEARD, "none")
    assert api.events[-1] == _reply(False, "", 0, "Voice turn failed")


def test_words_and_reply_are_escaped_for_the_js_string():
    api = _voice_api()
    api._voice_ask(HEARD, "none")
    turn, reply = api.events[0], api.events[-1]
    for js in (turn, reply):
        body = js.split("(", 1)[1].rsplit(")", 1)[0]
        # Every quote inside a string argument is escaped, so the call has
        # exactly its own delimiters and nothing can end the string early.
        unescaped = [i for i, c in enumerate(body) if c in "\"'" and body[i - 1] != "\\"]
        assert len(unescaped) == (2 if js is turn else 4)
    assert '\\"notes\\"' in turn and "it\\'s" in turn


# -- both voice paths go through it ---------------------------------------------------

def test_push_to_talk_shows_the_turn(monkeypatch, capsys):
    api = _ptt_api(monkeypatch, heard=HEARD)
    api.voice_timer = VoiceTimer()
    api.ask_mistai = lambda message, gender=None, input_mode="text", model=None: (
        api.events.append(("ask_mistai", message, input_mode)), _ok())[1]
    api.push_to_talk()
    turns = [e for e in api.events if isinstance(e, str) and e.startswith("handleVoice")]
    assert turns == [_turn(HEARD), _reply(True, REPLY, 2, "")]
    assert api.events.index(turns[0]) < api.events.index(("ask_mistai", HEARD, "voice")) \
        < api.events.index(turns[1])
    out = capsys.readouterr().out
    assert "[VoiceTiming] ptt:" in out                   # the turn was logged...
    assert not any(w in out for w in VOICE_TURNS_SECRET_WORDS)       # ...but never its words


# ============================================================
# VOICE TURNS CONVERSATION API
# ============================================================

def _conversation_api(monkeypatch, heard_after):
    api = _voice_api()
    api.voice_timer = VoiceTimer()
    api.spoken = []
    api.speak = lambda text, interrupt=False: api.spoken.append(text)
    api._wait_for_tts = lambda timeout=None: True
    api.memory = SimpleNamespace(set_profile=lambda *a: None)
    api.tts = SimpleNamespace(set_gender=lambda g: None)
    queue = list(heard_after)
    api._listen_once = lambda timeout=6, on_listening=None: queue.pop(0) if queue else None
    return api


# ============================================================
# VOICE TURNS TESTS (2)
# ============================================================

def test_the_wake_conversation_shows_each_turn(monkeypatch, capsys):
    api = _conversation_api(monkeypatch, ["hey mist " + HEARD.replace("open", "close")])
    api._conversation_loop(HEARD, "none")
    second = HEARD.replace("open", "close")
    assert [e for e in api.events if isinstance(e, str)] == [
        _turn(HEARD), _reply(True, REPLY, 2, ""), _turn(second), _reply(True, REPLY, 2, ""),
    ]
    assert [e[1] for e in api.events if isinstance(e, tuple)] == [HEARD, second]
    assert not any(w in capsys.readouterr().out for w in VOICE_TURNS_SECRET_WORDS)


def test_filler_in_the_wake_conversation_stays_unshown(monkeypatch):
    api = _conversation_api(monkeypatch, [])
    api._conversation_loop("thanks", "none")
    assert api.spoken == ["You're welcome."]
    assert api.events == []                       # no ask_mistai, nothing in the chat


def test_the_wake_conversation_still_ends_on_a_busy_reply(monkeypatch):
    api = _conversation_api(monkeypatch, ["should not be heard"])
    busy = {"success": False, "command": {"speech": "busy"}, "steps": 0, "error": "busy"}
    api.ask_mistai = lambda *a, **k: busy
    api._conversation_loop(HEARD, "none")
    assert api.events == [_turn(HEARD), _reply(False, "busy", 0, "busy")]


# -- the page -----------------------------------------------------------------------

def test_page_handlers_use_text_only_and_reset_the_step_card():
    js = _script()
    turn = js.split("function handleVoiceTurn(text){", 1)[1].split("\n}\n", 1)[0]
    reply = js.split("function handleVoiceReply(success,speech,steps,error){", 1)[1] \
        .split("\n}\n", 1)[0]
    assert "stepEl=null;" in turn and "stepEl=null;" in reply
    assert "addMsg(String(text||''),'user').classList.add('voice');" in turn
    assert "showResult(" in reply
    assert "innerHTML" not in turn + reply
    # One display path for typed and voice turns.
    assert js.count("function showResult(res)") == 1 and "showResult(res);" in js
    assert ".msg.user.voice::before{content:'\U0001F3A4';" in HTML


def test_page_has_no_control_characters():
    """The page is a normal (non-raw) Python string: a stray backslash
    escape such as \\1 or \\0 turns into a control character."""
    assert [hex(ord(c)) for c in HTML if ord(c) < 32 and c not in "\n\r\t"] == []


# ============================================================
# VOICE TURNS HELPERS (3)
# ============================================================

def _python_notifications(words, result, steps_between=()):
    api = _voice_api(result)
    api._voice_ask(words, "none")
    turn, reply = api.events[0], api.events[-1]
    return [{"js": turn}] + [{"js": s} for s in steps_between] + [{"js": reply}]


STEPS = ['handleAgentStep(1,"open_app","notepad","")', 'handleAgentStep(2,"type_text","x","")']


# ============================================================
# VOICE TURNS TESTS (3)
# ============================================================

@needs_node
def test_one_voice_turn_on_the_page(tmp_path):
    actions = _python_notifications(HEARD, _ok(), STEPS)
    r = _run_page(tmp_path, actions=actions)
    chat = r["actions"][-1]["chat"]
    assert chat == [
        ["msg user", HEARD, True],                  # exactly what was heard, with the mic
        ["step-progress", "", False],
        ["msg assistant", REPLY, False],
        ["msg memory", "Completed in 2 steps", False],
    ]
    assert r["actions"][-1]["errors"] == []


@needs_node
def test_two_voice_tasks_in_a_row_get_separate_step_cards(tmp_path):
    actions = (_python_notifications("open notepad", _ok("Done.", 1), STEPS[:1])
               + _python_notifications("open calculator", _ok("Done too.", 2), STEPS))
    chat = _run_page(tmp_path, actions=actions)["actions"][-1]["chat"]
    assert [c[0] for c in chat] == [
        "msg user", "step-progress", "msg assistant", "msg memory",
        "msg user", "step-progress", "msg assistant", "msg memory",
    ]
    assert [c[1] for c in chat if c[2]] == ["open notepad", "open calculator"]


@needs_node
def test_busy_and_still_running_voice_replies_look_like_typed_ones(tmp_path):
    busy = {"success": False, "command": {"speech": "I'm still working on 'x'."},
            "steps": 0, "error": "busy"}
    running = {"success": False, "command": {"speech": ""}, "steps": 0,
               "error": "still_running"}
    actions = _python_notifications("a", busy) + _python_notifications("b", running)
    chat = _run_page(tmp_path, actions=actions)["actions"][-1]["chat"]
    assert chat[1] == ["msg system", "I'm still working on 'x'.", False]
    assert chat[3][0] == "msg system" and chat[3][1].startswith("Still working on this")


# -- a failed result that has speech shows the speech, once -------------------------


# ============================================================
# VOICE TURNS HELPERS (4)
# ============================================================

NO_ANSWER = {"success": False, "command": {"action": "done", "parameter": "",
                                           "speech": "I couldn't read the answer."},
             "steps": 3, "error": "no answer"}


NO_SPEECH = {"success": False, "command": {"speech": ""}, "steps": 0, "error": "boom"}


# ============================================================
# API ERROR RESULT
# ============================================================

def _api_error_result():
    """What ask_mistai returns for a failed LLM call: the agent sets error and
    speech to the same describe_api_error() sentence."""
    from core.config import describe_api_error
    response = SimpleNamespace(status_code=503, json=lambda: {"error": "overloaded"})
    message = describe_api_error(response, "cohere")
    return message, {"success": False, "command": {"action": "done", "parameter": "",
                                                   "speech": message},
                     "steps": 0, "error": message}


# ============================================================
# VOICE TURNS TESTS (4)
# ============================================================

def test_show_result_prefers_speech_after_busy_and_still_running():
    js = _script()
    body = js.split("function showResult(res) {", 1)[1].split("\n}\n", 1)[0]
    order = [body.index(s) for s in ("res.error === 'busy'", "res.error === 'still_running'",
                                     "String(res.command.speech || '').trim()",
                                     "addMsg('Error: ' + (res.error || 'Unknown'), 'error');")]
    assert order == sorted(order)
    assert "addMsg(String(res.command.speech), 'assistant');" in body
    assert "innerHTML" not in body          # addMsg writes textContent only


# ============================================================
# VOICE TURNS HELPERS (5)
# ============================================================

def _typed(results):
    """Typed turns through the page's real send() and showResult()."""
    actions = [{"js": "inp.value='question " + str(i) + "';send();"} for i in range(len(results))]
    return actions, {"ask": results}


# ============================================================
# VOICE TURNS TESTS (5)
# ============================================================

@needs_node
def test_typed_no_answer_shows_the_honest_line_and_no_speech_shows_the_error(tmp_path):
    actions, cfg = _typed([NO_ANSWER, NO_SPEECH])
    r = _run_page(tmp_path, actions=actions, **cfg)
    chat = r["actions"][-1]["chat"]
    assert [c[:2] for c in chat] == [
        ["msg user", "question 0"],
        ["msg assistant", "I couldn't read the answer."],
        ["msg user", "question 1"],
        ["msg error", "Error: boom"],
    ]
    assert r["actions"][-1]["errors"] == []


@needs_node
def test_an_api_error_is_shown_once_typed_and_voice(tmp_path):
    message, result = _api_error_result()
    actions, cfg = _typed([result])
    actions += _python_notifications("a voice question", result)
    chat = _run_page(tmp_path, actions=actions, **cfg)["actions"][-1]["chat"]
    texts = [c[1] for c in chat]
    assert texts.count(message) == 2                       # once per turn
    assert not any(t.startswith("Error:") for t in texts)
    assert [c[0] for c in chat if c[1] == message] == ["msg assistant", "msg assistant"]


@needs_node
def test_voice_no_answer_shows_the_honest_line_and_no_speech_shows_the_error(tmp_path):
    actions = (_python_notifications("what does it say", NO_ANSWER)
               + _python_notifications("and now", NO_SPEECH))
    chat = _run_page(tmp_path, actions=actions)["actions"][-1]["chat"]
    assert chat == [
        ["msg user", "what does it say", True],
        ["msg assistant", "I couldn't read the answer.", False],
        ["msg user", "and now", True],
        ["msg error", "Error: boom", False],
    ]


# ==========================================================================
# moved from tests/test_task_ack_heartbeat.py
# Voice tasks: a fixed spoken "On it." when the agent starts, and a fixed
# "Still working." heartbeat while a long run goes on (core/api.py).
# ==========================================================================


# ============================================================
# VOICE TURNS HELPERS (6)
# ============================================================

TASK = '{"route": "task", "reply": ""}'


# ============================================================
# VOICE TURNS FAKE TIMER
# ============================================================

class FakeTimer:
    def __init__(self, delay, fn, args=()):
        self.delay, self.fn, self.args = delay, fn, args
        self.daemon = False
        self.started = self.cancelled = False
        FakeTimer.made.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn(*self.args)


# ============================================================
# VOICE TURNS HELPERS (7)
# ============================================================

@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    FakeTimer.made = []
    monkeypatch.setattr(api_mod.threading, "Timer", FakeTimer)
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: now[0])
    monkeypatch.delenv("MISTAI_TASK_ACK", raising=False)
    monkeypatch.delenv("MISTAI_NARRATION", raising=False)
    return now


def _voice_task(monkeypatch, message="open notepad", mode="voice", reply=TASK, **kw):
    api = _routing_api(monkeypatch, reply, **kw)
    result = Api.ask_mistai(api, message, input_mode=mode)
    return api, result


def _fire_last(clock, at):
    clock[0] = at
    FakeTimer.made[-1].fire()


# -- config -------------------------------------------------------------------------


# ============================================================
# VOICE TURNS TESTS (6)
# ============================================================

def test_task_ack_setting_defaults_on_and_is_read_at_call_time(monkeypatch):
    monkeypatch.delenv("MISTAI_TASK_ACK", raising=False)
    assert config.task_ack_enabled() is True
    monkeypatch.setenv("MISTAI_TASK_ACK", "0")
    assert config.task_ack_enabled() is False
    monkeypatch.setenv("MISTAI_TASK_ACK", "maybe")          # env_flag: default
    assert config.task_ack_enabled() is True


@pytest.mark.parametrize("value, mode", [
    (None, "milestones"), ("off", "off"), (" OFF ", "off"), ("milestones", "milestones"),
    ("verbose", "milestones"), ("", "milestones"),
])
def test_narration_mode(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("MISTAI_NARRATION", raising=False)
    else:
        monkeypatch.setenv("MISTAI_NARRATION", value)
    assert config.narration_mode() == mode


# -- "On it." -------------------------------------------------------------------


def test_a_voice_task_hears_on_it_exactly_once_before_the_minimize(monkeypatch, clock):
    api = _routing_api(monkeypatch, TASK)
    order = []
    api.speak = lambda text: order.append(("speak", text))     # no keyword arguments
    api.minimize_window = lambda: order.append(("minimize",))
    Api.ask_mistai(api, "open notepad", input_mode="voice")
    assert order == [("speak", "On it."), ("minimize",)]
    _finish(api)


@pytest.mark.parametrize("message, mode, reply, kw, spoken", [
    ("tell me a joke", "voice", CHAT, {}, ["Ha."]),                      # chat
    ("thanks", "voice", CHAT, {}, ["You're welcome."]),                  # filler
    ("open notepad", "text", TASK, {}, []),                              # typed
    ("resume Site", "voice", TASK, {"resumable": SimpleNamespace(name="Site")}, []),
])
def test_no_ack_for_chat_filler_typed_or_resume(monkeypatch, clock, message, mode, reply, kw,
                                                spoken):
    api, _ = _voice_task(monkeypatch, message, mode, reply, **kw)
    assert api.rec["spoken"] == spoken
    _finish(api)


def test_no_ack_for_a_busy_turn(monkeypatch, clock):
    api = _routing_api(monkeypatch, TASK)
    api._agent_lock.acquire()
    api._agent_task = "open calculator"
    result = Api.ask_mistai(api, "open notepad", input_mode="voice")
    assert result["error"] == "busy"
    assert "On it." not in api.rec["spoken"] and len(api.rec["spoken"]) == 1
    assert FakeTimer.made == []


def test_no_ack_with_the_setting_off(monkeypatch, clock):
    monkeypatch.setenv("MISTAI_TASK_ACK", "0")
    api, _ = _voice_task(monkeypatch)
    assert api.rec["spoken"] == []
    _finish(api)


# -- heartbeat ------------------------------------------------------------------------


def test_heartbeat_fires_at_20s_at_most_twice(monkeypatch, clock):
    api, _ = _voice_task(monkeypatch)
    assert api.rec["spoken"] == ["On it."]
    assert len(FakeTimer.made) == 1
    timer = FakeTimer.made[0]
    assert timer.started and timer.daemon and timer.delay == api_mod.HEARTBEAT_SECONDS
    _fire_last(clock, 1020.0)
    assert api.rec["spoken"] == ["On it.", "Still working."]
    assert FakeTimer.made[-1].delay == 20
    _fire_last(clock, 1040.0)
    assert api.rec["spoken"] == ["On it.", "Still working.", "Still working."]
    assert len(FakeTimer.made) == 2                       # no third timer
    _finish(api)


def test_heartbeat_counts_from_the_last_thing_mist_said(monkeypatch, clock):
    api, _ = _voice_task(monkeypatch)
    api._last_spoke_at = 1012.0                          # e.g. an ask_user question
    _fire_last(clock, 1020.0)
    assert api.rec["spoken"] == ["On it."]
    assert FakeTimer.made[-1].delay == pytest.approx(12.0)
    _fire_last(clock, 1032.0)
    assert api.rec["spoken"][-1] == "Still working."
    _finish(api)


@pytest.mark.parametrize("busy", ["_listen_waiting", "_ptt_waiting", "_awaiting_text_answer",
                                  "tts"])
def test_never_while_listening_or_speaking(monkeypatch, clock, busy):
    api, _ = _voice_task(monkeypatch)
    if busy == "tts":
        api.tts = SimpleNamespace(speaking=True)
    else:
        setattr(api, busy, threading.Event())
        getattr(api, busy).set()
    _fire_last(clock, 1020.0)
    _fire_last(clock, 1100.0)
    assert api.rec["spoken"] == ["On it."]
    assert FakeTimer.made[-1].delay == api_mod.HEARTBEAT_POLL_SECONDS
    # Once Mist is quiet again, the 20 s count starts from there.
    if busy == "tts":
        api.tts.speaking = False
    else:
        getattr(api, busy).clear()
    _fire_last(clock, 1101.0)
    assert api.rec["spoken"] == ["On it."]
    _fire_last(clock, 1120.0)
    assert api.rec["spoken"] == ["On it.", "Still working."]
    _finish(api)


def test_never_after_a_stop_request(monkeypatch, clock):
    api, _ = _voice_task(monkeypatch)
    api._stop_requested.set()
    n = len(FakeTimer.made)
    _fire_last(clock, 1020.0)
    assert api.rec["spoken"] == ["On it."]
    assert len(FakeTimer.made) == n                       # nothing rescheduled
    _finish(api)


@pytest.mark.parametrize("end", ["on_done", "on_error"])
def test_the_run_ending_cancels_the_heartbeat(monkeypatch, clock, end):
    api, _ = _voice_task(monkeypatch)
    timer = FakeTimer.made[-1]
    api.agent.callbacks[end]({"success": True} if end == "on_done" else "boom")
    assert timer.cancelled
    clock[0] = 1020.0
    timer.fn(*timer.args)          # a tick already under way when the cancel landed
    assert api.rec["spoken"] == ["On it."]
    assert len(FakeTimer.made) == 1


def test_an_error_before_the_agent_thread_starts_cancels_it(monkeypatch, clock):
    api = _routing_api(monkeypatch, TASK)

    def boom(*_a, **_k):
        raise RuntimeError("no agent")

    api.agent.run_project = boom
    result = Api.ask_mistai(api, "open notepad", input_mode="voice")
    assert result["success"] is False and not api._agent_lock.locked()
    assert FakeTimer.made[-1].cancelled


@pytest.mark.parametrize("mode, narration", [("text", None), ("voice", "off")])
def test_no_heartbeat_for_typed_tasks_or_with_narration_off(monkeypatch, clock, mode,
                                                            narration):
    if narration:
        monkeypatch.setenv("MISTAI_NARRATION", narration)
    api, _ = _voice_task(monkeypatch, mode=mode)
    assert FakeTimer.made == []
    _finish(api)


def test_the_heartbeat_log_carries_no_content(monkeypatch, clock, capsys):
    api, _ = _voice_task(monkeypatch, "message Joanne Quillfeather about the pier")
    _fire_last(clock, 1020.0)
    out = capsys.readouterr().out
    assert "[Heartbeat] 1/2" in out
    assert "Quillfeather" not in out and "pier" not in out
    _finish(api)


def test_the_real_timer_thread_never_outlives_the_task(monkeypatch):
    monkeypatch.delenv("MISTAI_NARRATION", raising=False)
    monkeypatch.setattr(api_mod, "HEARTBEAT_SECONDS", 0.05)
    api = _routing_api(monkeypatch, TASK)
    heard = threading.Event()
    spoken = []
    api.speak = lambda text: (spoken.append(text), text == "Still working." and heard.set())
    Api.ask_mistai(api, "open notepad", input_mode="voice")
    assert heard.wait(2)
    api.agent.callbacks["on_done"]({"success": True})

    def heartbeat_timers():
        return [t for t in threading.enumerate() if isinstance(t, threading.Timer)
                and getattr(t.function, "__func__", None) is Api._heartbeat_tick]

    for t in heartbeat_timers():
        t.join(1)
    assert heartbeat_timers() == []
    count = spoken.count("Still working.")
    threading.Event().wait(0.2)
    assert spoken.count("Still working.") == count <= api_mod.HEARTBEAT_MAX
