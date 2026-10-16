"""[VoiceTiming] and [MicCompete] diagnostics: durations and counts only,
never transcript content, and no change to what the voice path does."""

import threading
from types import SimpleNamespace

import pytest
import speech_recognition as sr

import core.api as api_mod
from core.api import Api
from core.voice_timing import VoiceTimer

SECRET_WORDS = ("open", "banking", "password")   # never in a diagnostic line


def _timer(lines, times):
    it = iter(times)
    return VoiceTimer(clock=lambda: next(it), log=lines.append)


def _diag(lines):
    return [l for l in lines if l.startswith(("[VoiceTiming]", "[MicCompete]"))]


# -- VoiceTimer ---------------------------------------------------------------

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

class _Mic:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _listen_api(lines, recognize):
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _Mic()
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


def test_listen_once_marks_each_phase_without_content(capsys):
    lines = []
    api, seen = _listen_api(lines, lambda: "open my banking password")
    api.voice_timer.start("followup")

    assert api._listen_once(timeout=5) == "open my banking password"
    names = [l.split(": ", 1)[1].split(" +")[0] for l in lines]
    assert names == ["mic_acquired", "listen_start", "speech_end", "stt_done"]
    assert seen == {"waiting_while_listening": True, "timeout": 5}
    assert not api._listen_waiting.is_set()
    assert not any(w in l for l in lines for w in SECRET_WORDS)


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
    assert not any(w in l for l in lines for w in SECRET_WORDS)


# -- wake loop ----------------------------------------------------------------

def _wake_api(monkeypatch, transcripts, during_recognize=None):
    api = Api.__new__(Api)
    api._wake_shutdown = threading.Event()
    api._wake_enabled = threading.Event()
    api._wake_enabled.set()
    api._mic_lock = threading.Lock()
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _Mic()
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
    assert not any(w in l for l in compete for w in SECRET_WORDS)
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
    from test_conversation_layer import _routing_api

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
    assert not any(w in l for l in lines for w in SECRET_WORDS)
