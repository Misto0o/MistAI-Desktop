"""Push-to-talk (Api.push_to_talk): one press, one voice command, no wake word.

Stubbed recognizer and microphone only: no audio device, no STT, no network.
The turn's thread runs inline (threading.Thread is replaced) unless a test
needs it to block.
"""

import threading
from types import SimpleNamespace

import pytest
import speech_recognition as sr

import core.api as api_mod
from core.api import Api
from test_voice_timing import SECRET_WORDS, _Mic, _wake_api


class _InlineThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _ptt_api(monkeypatch, heard="open my banking password", inline=True):
    """An Api with a stubbed mic/recognizer; heard is a string, None (silence)
    or an exception to raise from recognize_google."""
    if inline:
        monkeypatch.setattr(api_mod.threading, "Thread", _InlineThread)
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api._ptt_waiting = threading.Event()
    api.microphone = _Mic()
    api.events = []          # everything the turn does, in order
    api.asked = []
    profile = {"user_gender": "female"}
    api.memory = SimpleNamespace(get_profile=lambda k, d=None: profile.get(k, d),
                                 set_profile=profile.__setitem__)
    api.profile = profile
    api.tts = SimpleNamespace(interrupt=lambda: api.events.append("tts.interrupt"),
                              set_gender=lambda g: None)
    api._notify_js = lambda js: api.events.append(js)

    class Rec:
        def adjust_for_ambient_noise(self, src, duration=1):
            api.events.append(f"adjust (ptt_waiting={api._ptt_waiting.is_set()})")

        def listen(self, src, timeout=None, phrase_time_limit=None):
            api.events.append(f"listen timeout={timeout}")
            if heard is None:
                raise sr.WaitTimeoutError("listening timed out")
            return object()

        def recognize_google(self, audio):
            if isinstance(heard, Exception):
                raise heard
            return heard

    api.recognizer = Rec()

    def ask(message, model=None, gender=None, input_mode="text"):
        api.events.append("ask_mistai")
        api.asked.append((message, gender, input_mode, api._ptt_waiting.is_set(),
                          api._conversation_lock.locked()))
        return {"success": True}

    api.ask_mistai = ask
    return api


def _states(api):
    return [e for e in api.events if e.startswith("handleListenState")]


def test_a_press_listens_once_and_runs_the_normal_voice_turn(monkeypatch, capsys):
    api = _ptt_api(monkeypatch, heard="Hey Mist open my banking password")
    assert api.push_to_talk() == {"success": True}
    assert api.events == [
        "tts.interrupt",                                  # Mist stops talking first
        'handleListenState("waiting","")',
        "adjust (ptt_waiting=True)",                      # wake loop kept off the mic
        'handleListenState("listening","")',              # only once the mic is open
        "listen timeout=6",
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
    assert not any(w in capsys.readouterr().out for w in SECRET_WORDS)


def test_sir_or_maam_in_the_command_sets_the_gender_as_in_on_wake(monkeypatch):
    api = _ptt_api(monkeypatch, heard="open notepad sir")
    api.push_to_talk()
    assert api.asked[0][:3] == ("open notepad sir", "male", "voice")
    assert api.profile["user_gender"] == "male"


@pytest.mark.parametrize("heard", [None, sr.UnknownValueError(), "", "hey mist"])
def test_no_words_shows_a_status_line_and_runs_nothing(monkeypatch, heard):
    api = _ptt_api(monkeypatch, heard=heard)
    assert api.push_to_talk() == {"success": True}
    assert _states(api)[-1] == 'handleListenState("idle","Didn\\\'t catch that")'
    assert api.asked == []
    assert "speak" not in api.events                     # a status line, not spoken
    assert not api._ptt_waiting.is_set() and not api._conversation_lock.locked()


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
    assert not any(w in out for w in SECRET_WORDS)
    expected_idle = ('handleListenState("idle","")' if where == "ask"
                     else 'handleListenState("idle","Push-to-talk failed")')
    assert _states(api)[-1] == expected_idle
    assert api.push_to_talk() == {"success": True}      # usable again


def test_a_second_press_while_the_first_listens_is_refused(monkeypatch):
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
    assert api.push_to_talk()["error"] == "listening"
    finish.set()
    for _ in range(100):
        if not api._conversation_lock.locked():
            break
        threading.Event().wait(0.02)
    assert not api._conversation_lock.locked() and len(api.asked) == 1


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
    assert api._listen_once(timeout=3) == "open notepad"           # existing call form

    def bad():
        raise ValueError("nope")

    assert api._listen_once(timeout=3, on_listening=bad) == "open notepad"
    assert "[ListenOnce] on_listening failed (ValueError)" in capsys.readouterr().out


# -- the wake loop leaves the mic to a press -------------------------------------------

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
    from core import wake_calibration

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
