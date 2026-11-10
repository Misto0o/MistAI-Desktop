"""Text-layer wake-word harness: transcript -> wake / soft / none.

CURRENT pins what fuzzy_wake does today, including the command it hands on.
DESIRED lists real-log gaps (the wake loop heard "hey miss" when the user said
"hey mist") as strict xfails. When matching changes, they XPASS, which fails
the run, so whoever flips them has to move them into CURRENT on purpose.

"soft" (planned): the wake is accepted, but any inline command is dropped and
the user is asked "Yes?" first. fuzzy_wake has no soft tier yet, so
classify() returns only "wake" or "none" for now.

Merged by area (pure moves, no test changed) from: test_wake_matching.py, test_wake_word_safety.py, test_wake_calibration.py, test_wake_clips.py."""

import pytest
from core.config import fuzzy_wake
from core.config import fuzzy_wake, strip_wake_prefix
import threading
from types import SimpleNamespace
import speech_recognition as sr
import core.api as api_mod
from core.api import Api
from core.config import WAKE_ENERGY_MAX, WAKE_ENERGY_MIN
from core.wake_calibration import WakeCalibration
import json
import wave
from core.config import wake_clips_enabled



# ==========================================================================
# moved from tests/test_wake_matching.py
# ==========================================================================

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


@pytest.mark.parametrize("text,wake,command", CURRENT)
def test_current_wake_matching(text, wake, command):
    assert fuzzy_wake(text) == (wake, command)


# Real-log gaps: a near-miss of "mist" right after "hey". Each should become a
# soft wake. "hey missed" / "hey must" are left out on purpose because they
# collide with the negatives "hey missed you" / "hey must be nice".
DESIRED_SOFT = [
    "hey miss",                    # real log
    "hey miss open calculator",    # soft: the inline command must be dropped
    "hey midst",
    "hey mists",
]


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

def _cal():
    return WakeCalibration(low=60, high=1500, idle_s=90, max_failures=3, retry_s=30)


# -- schedule (pure) ----------------------------------------------------------

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

class _Mic:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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
    api.microphone = _Mic()
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

def _audio(seconds=0.5, rate=16000, width=2):
    return sr.AudioData(b"\x00\x01" * int(rate * seconds), rate, width)


def test_clip_saving_is_off_by_default(monkeypatch):
    monkeypatch.delenv("MISTAI_WAKE_SAVE_CLIPS", raising=False)
    assert wake_clips_enabled() is False
    monkeypatch.setenv("MISTAI_WAKE_SAVE_CLIPS", "1")
    assert wake_clips_enabled() is True


def test_clip_seconds():
    assert Api._clip_seconds(_audio(0.5)) == 0.5


def test_saved_clip_has_wav_and_sidecar(tmp_path):
    audio = _audio(0.5)
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
    Api._save_wake_clip(_audio(), None, 217, 0.5, error="RequestError",
                        directory=str(tmp_path))
    (meta_path,) = tmp_path.glob("*.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["transcript"] == "untranscribed"
    assert meta["error"] == "RequestError"


def test_save_failure_never_raises(tmp_path, capsys):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    Api._save_wake_clip(_audio(), "mist", 300, 0.5, directory=str(blocker))
    assert "Could not save clip" in capsys.readouterr().out
