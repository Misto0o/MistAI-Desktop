"""Voice tasks: a fixed spoken "On it." when the agent starts, and a fixed
"Still working." heartbeat while a long run goes on (core/api.py)."""

import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core import config
from core.api import Api
from test_conversation_layer import CHAT, _finish, _routing_api

TASK = '{"route": "task", "reply": ""}'


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
