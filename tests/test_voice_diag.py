"""MISTAI_VOICE_DIAG=1: one "[VoiceDiag]" line per _listen_once, numbers and
an exception type only. Off by default. Recognition settings are unchanged."""

import re
import struct
import threading
from types import SimpleNamespace

import pytest
import speech_recognition as sr

from core.api import Api
from core.voice_timing import VoiceTimer

SPOKEN = "yes send it to joanne quillfeather"
LINE_RX = re.compile(
    r"^\[VoiceDiag\] (\w+) listen: clip=(na|\d+\.\d\d)s rms=(na|\d+) peak=(na|\d+) "
    r"sr=(na|\d+) thr=(na|\d+)->(na|\d+) since_tts=(na|\d+\.\d\d)s "
    r"mic_wait=(na|\d+\.\d\d)s rec=(shared|ptt) result=(\w+)$")


class _Mic:
    SAMPLE_RATE = 16000
    SAMPLE_WIDTH = 2
    stream = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _audio(level=1000, seconds=0.5, rate=16000):
    n = int(seconds * rate)
    return sr.AudioData(struct.pack(f"<{n}h", *([level, -level] * (n // 2))), rate, 2)


def _api(monkeypatch, outcome, *, finished_ago=0.4, speaking=False, ptt=False):
    """A bare Api whose listen returns 0.5 s of a square wave at 1000 and whose
    recognizer then returns SPOKEN or raises ``outcome``."""
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _Mic()
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
            return _audio()

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


@pytest.fixture
def diag_on(monkeypatch):
    monkeypatch.setenv("MISTAI_VOICE_DIAG", "1")


def _lines(out):
    return [l for l in out.splitlines() if l.startswith("[VoiceDiag]")]


@pytest.mark.parametrize("outcome, result", [
    (None, "ok"), (sr.UnknownValueError, "UnknownValueError"),
    (sr.RequestError, "RequestError"), (sr.WaitTimeoutError, "WaitTimeoutError"),
])
def test_one_line_per_listen_with_numbers_and_the_exception_type(monkeypatch, capsys, diag_on,
                                                                 outcome, result):
    api = _api(monkeypatch, outcome)
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
    api = _api(monkeypatch, None)
    assert Api._listen_once(api, timeout=10) == SPOKEN
    out = capsys.readouterr().out
    tokens = set(re.findall(r"[a-z]+", _lines(out)[0].split("listen:", 1)[1]))
    assert tokens <= {"clip", "s", "rms", "peak", "sr", "thr", "since", "tts", "mic", "wait",
                      "rec", "shared", "result", "ok"}
    assert "quillfeather" not in out.lower() and "joanne" not in out.lower()


def test_label_falls_back_to_listen_and_since_tts_is_na_while_speaking(monkeypatch, capsys,
                                                                       diag_on):
    api = _api(monkeypatch, sr.UnknownValueError, speaking=True)
    Api._listen_once(api, timeout=10)
    line = _lines(capsys.readouterr().out)[0]
    assert line.startswith("[VoiceDiag] listen listen:") and "since_tts=nas" in line


def test_push_to_talk_reports_its_own_recognizer_and_settings_are_unchanged(monkeypatch,
                                                                             capsys, diag_on):
    api = _api(monkeypatch, None)
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
    api = _api(monkeypatch, None)
    Api._listen_once(api, timeout=10)
    assert api.calls == [("adjust", 0.3), ("listen", 10, 15), ("recognize",)]
    assert (api.recognizer.pause_threshold, api.recognizer.non_speaking_duration) == (1.0, 0.5)
    assert bool(_lines(capsys.readouterr().out)) is bool(diag)


def test_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VOICE_DIAG", raising=False)
    from core.config import voice_diag_enabled
    assert voice_diag_enabled() is False
    Api._listen_once(_api(monkeypatch, sr.UnknownValueError), timeout=10)
    assert _lines(capsys.readouterr().out) == []


def test_a_broken_stat_never_breaks_the_listen(monkeypatch, capsys, diag_on):
    api = _api(monkeypatch, None)
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
