"""Wake-loop segmentation settings and the re-calibration schedule."""

import threading
from types import SimpleNamespace

import pytest
import speech_recognition as sr

import core.api as api_mod
from core.api import Api
from core.config import WAKE_ENERGY_MAX, WAKE_ENERGY_MIN
from core.wake_calibration import WakeCalibration


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
