"""Push-to-talk listening settings (Api._listen_once(ptt=True)).

A press listens on its OWN recognizer: 2.0s pause, 30s phrase limit, and the
0.3s noise adjustment only on the first press and once the measured threshold
is PTT_THRESHOLD_MAX_AGE old. Every other listen (wake conversation, voice
ask_user, confirmations) keeps the shared recognizer exactly as before.
[VoiceTiming] gains mic_open (mic_acquired -> mic_open: opening the mic;
mic_open -> listen_start: the adjustment). Stubbed mic and recognizers only.
"""

import os
import re
import threading

import pytest
import speech_recognition as sr

import core.api as api_mod
from core import config
from core.api import Api
from core.voice_timing import VoiceTimer
from test_voice_timing import SECRET_WORDS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class _Mic:
    """Opening the mic takes `open_s` on the fake clock."""

    def __init__(self, clock, open_s=0.0):
        self.clock, self.open_s = clock, open_s

    def __enter__(self):
        self.clock.t += self.open_s
        return self

    def __exit__(self, *a):
        return False


class _Rec:
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


@pytest.fixture
def setup(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(api_mod.time, "monotonic", clock)
    lines, calls = [], []
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _Mic(clock, open_s=0.25)
    api.voice_timer = VoiceTimer(clock=clock, log=lines.append)
    api.recognizer = _Rec(clock, "shared", calls)
    api.ptt_recognizer = _Rec(clock, "ptt", calls)
    return api, clock, lines, calls


def _press(api, timeout=6):
    api.voice_timer.start("ptt")
    return api._listen_once(timeout=timeout, ptt=True)


def _marks(lines):
    return [re.match(r"\[VoiceTiming\] \w+: (\S+) \+(\d+\.\d+)s", l).groups() for l in lines
            if l.startswith("[VoiceTiming]")]


# -- push-to-talk's own settings ----------------------------------------------------------

def test_a_press_uses_its_own_recognizer_and_settings(setup, capsys):
    api, _clock, lines, calls = setup
    assert _press(api) == "open my banking password"
    assert calls == [("ptt", "adjust", 0.3),
                     ("ptt", "listen", 6, 30, 2.0, 0.5)]   # timeout, limit, pause, non-speaking
    shared = api.recognizer                                  # never touched
    assert (shared.pause_threshold, shared.non_speaking_duration) == (0.8, 0.5)
    assert (config.PTT_PAUSE_THRESHOLD, config.PTT_PHRASE_TIME_LIMIT,
            config.PTT_THRESHOLD_MAX_AGE) == (2.0, 30, 600)
    out = capsys.readouterr().out + "\n".join(lines)
    # The heard words never reach a log ("open" is also in the mark mic_open).
    assert not any(w in out for w in SECRET_WORDS if w != "open")
    assert "open my" not in out


def test_mic_open_splits_opening_from_the_adjustment(setup):
    api, _clock, lines, _calls = setup
    _press(api)
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
    _press(api)
    clock.t += 30
    _press(api)                                              # 30s later: reused
    clock.t += config.PTT_THRESHOLD_MAX_AGE - 31 - 0.5       # just under 10 minutes old
    _press(api)
    adjusts = [c for c in calls if c[1] == "adjust"]
    assert len(adjusts) == 1
    out = capsys.readouterr().out
    assert out.count("[PushToTalk] Noise threshold reused") == 2
    # A reused press goes straight from the open mic to listening.
    reused = _marks(lines)[5:10]
    assert reused[1] == ("mic_open", "0.25") and reused[2] == ("listen_start", "0.00")

    clock.t += 1                                             # now over 10 minutes old
    _press(api)
    assert len([c for c in calls if c[1] == "adjust"]) == 2  # recalibrated once
    clock.t += 5
    _press(api)                                              # and reused again
    assert len([c for c in calls if c[1] == "adjust"]) == 2


def test_a_failed_adjustment_is_not_reused(setup):
    api, clock, _lines, calls = setup
    api.ptt_recognizer.adjust_raises = OSError("stream closed")
    assert _press(api) is None
    api.ptt_recognizer.adjust_raises = None
    clock.t += 5
    _press(api)
    assert [c[1] for c in calls if c[1] == "adjust"] == ["adjust", "adjust"]


def test_the_shared_path_never_reuses_or_shares_the_threshold(setup):
    api, _clock, _lines, calls = setup
    _press(api)
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
    assert len(found) - len(ptt) >= 6                        # the other callers, all default
