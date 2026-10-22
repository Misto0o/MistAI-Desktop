"""A second press ends a push-to-talk listen early (Change 3b).

The REAL SpeechRecognition listen() runs on a fake AudioSource whose stream
plays scripted chunks (silence or speech); only recognize_google is stubbed.
A press during the listen makes the stream proxy return b"" (the end of the
stream): before any speech the turn ends quietly with no recognition call;
after speech what was heard runs as usual. The stop event is cleared at the
start and end of every turn. No audio device, no STT, no network.
"""

import threading
import time

import speech_recognition as sr

from core.api import _StoppableStream
from test_push_to_talk import NOTHING, _ptt_api, _states

QUIET = b"\x00\x00" * 1024
LOUD = (6000).to_bytes(2, "little", signed=True) * 1024


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


class _Mic(sr.AudioSource):
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


class _Rec(sr.Recognizer):
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


def _api(monkeypatch, streams, heard=()):
    api = _ptt_api(monkeypatch)
    api.microphone = _Mic(streams)
    api.ptt_recognizer = _Rec(heard, api.microphone)
    api._ptt_calibrated_at = time.monotonic()      # skip the 0.3s adjustment
    api._ptt_stop = threading.Event()
    return api


def _press(api):
    """The second press, from inside the listen (as the UI would)."""
    def press():
        api.second_press = api.push_to_talk()
    return press


# -- before any speech: a quiet cancel -------------------------------------------------------

def test_a_stop_before_speech_ends_within_one_chunk_with_no_recognition(monkeypatch, capsys):
    api = _api(monkeypatch, [])
    stream = _Stream("qqqqqqqq", press_at=5, press=_press(api))
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
    api = _api(monkeypatch, [])
    stream = _Stream("qqq", press_at=2, press=_press(api))
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
    api = _api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL", press_at=8, press=_press(api))
    api.microphone.streams = [stream]
    api.push_to_talk()
    assert api.second_press["stopped"] is True
    assert stream.reads == 8                          # ended at the press, not by a pause
    assert api.ptt_recognizer.recognized == 1
    assert [a[0] for a in api.asked] == ["open notepad"]
    assert not api._ptt_stop.is_set()


def test_without_a_press_the_listen_ends_by_the_pause(monkeypatch):
    api = _api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL")
    api.microphone.streams = [stream]
    api.push_to_talk()
    pause_chunks = int(2.0 / (1024 / 16000))          # PTT_PAUSE_THRESHOLD of silence
    assert stream.reads > 10 + pause_chunks
    assert [a[0] for a in api.asked] == ["open notepad"]


def test_a_stop_after_only_the_wake_phrase_skips_the_follow_up(monkeypatch):
    api = _api(monkeypatch, [], heard=["hey mist"])
    stream = _Stream("LLLLL", press_at=4, press=_press(api))
    api.microphone.streams = [stream, _Stream("LLLLLLLL")]   # a follow-up would open this
    api.push_to_talk()
    assert len(api.microphone.opened) == 1 and api.ptt_recognizer.recognized == 1
    assert _states(api)[-1] == NOTHING and api.asked == []


def test_a_stop_before_speech_in_the_follow_up_cancels(monkeypatch):
    api = _api(monkeypatch, [], heard=["hey mist"])
    first = _Stream("LLLLLLLL")                       # "hey mist", ended by the pause
    follow = _Stream("qqqq", press_at=3, press=_press(api))
    api.microphone.streams = [first, follow]
    api.push_to_talk()
    assert len(api.microphone.opened) == 2 and follow.reads == 3
    assert api.ptt_recognizer.recognized == 1         # only the first listen
    assert api.asked == [] and NOTHING not in _states(api)


# -- the stop never leaks -------------------------------------------------------------------

def test_a_stale_stop_is_cleared_when_a_turn_starts(monkeypatch):
    api = _api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLL")
    api.microphone.streams = [stream]
    api._ptt_stop.set()                               # left over from anywhere
    api.push_to_talk()
    assert stream.reads > 8                           # the listen was not cut short
    assert [a[0] for a in api.asked] == ["open notepad"]


def test_one_press_never_reaches_the_next_turn(monkeypatch):
    api = _api(monkeypatch, [], heard=["open notepad"])
    first = _Stream("qqq", press_at=2, press=_press(api))
    second = _Stream("qqLLLLLL")
    api.microphone.streams = [first, second]
    api.push_to_talk()                                # cancelled
    assert not api._ptt_stop.is_set()
    api.push_to_talk()                                # a new turn listens normally
    assert second.reads > 8 and [a[0] for a in api.asked] == ["open notepad"]


def test_the_stop_is_cleared_even_when_the_turn_fails(monkeypatch):
    api = _api(monkeypatch, [], heard=["open notepad"])
    stream = _Stream("qqLLLLLLLL", press_at=5, press=_press(api))
    api.microphone.streams = [stream]

    def boom(*a, **k):
        raise RuntimeError("ask failed")

    api.ask_mistai = boom
    api.push_to_talk()
    assert not api._ptt_stop.is_set() and not api._conversation_lock.locked()


# -- other presses keep today's refusals ----------------------------------------------------

def test_a_press_during_a_wake_conversation_is_still_refused(monkeypatch):
    api = _api(monkeypatch, [])
    api._conversation_lock.acquire()                  # the wake conversation holds it
    assert api.push_to_talk() == {"success": False, "error": "listening",
                                  "message": "Already listening."}
    assert not api._ptt_stop.is_set()


def test_a_press_after_the_listen_ended_is_refused_as_before(monkeypatch):
    api = _api(monkeypatch, [], heard=["open notepad"])
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
    api = _api(monkeypatch, [_Stream("qqqqqqqLLLLLLLLLL")], heard=["yes"])
    api.recognizer = api.ptt_recognizer               # a working shared stub
    api._ptt_stop.set()                               # even with a stop pending
    assert api._listen_once(timeout=5) == "yes"       # e.g. a voice ask_user answer
    assert not isinstance(api.microphone.listened_on[0], _StoppableStream)
