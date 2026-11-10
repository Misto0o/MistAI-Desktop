"""Voice turns in the chat (Api._voice_ask, ui/html.py handleVoiceTurn /
handleVoiceReply): push-to-talk and the wake conversation both show what Mist
HEARD and the result, like a typed turn. The words reach the page only,
escaped, and are never logged. Stubbed mic and recognizer; Node tests skip
when Node is missing.

Merged by area (pure moves, no test changed) from: test_voice_turns.py, test_task_ack_heartbeat.py."""

import threading
from types import SimpleNamespace
import pytest
from core.api import Api
from core.config import esc
from core.voice_timing import VoiceTimer
from test_push_to_talk import _ptt_api
from test_ui_page import HTML, _run_page, _script, needs_node
import core.api as api_mod
from core import config
from test_conversation import CHAT, _finish, _routing_api



# ==========================================================================
# moved from tests/test_voice_turns.py
# ==========================================================================

# A quote, an apostrophe and a script tag, in both the words and the reply.
HEARD = 'open "notes" it\'s </script><script>alert(1)</script> banking password'


REPLY = 'Opened "notes" </script><b>bold</b> it\'s done'


SECRET_WORDS = ("notes", "banking", "password", "alert", "script")


def _ok(speech=REPLY, steps=2):
    return {"success": True, "command": {"action": "done", "parameter": "", "speech": speech},
            "steps": steps, "error": ""}


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


def _turn(words):
    return f'handleVoiceTurn("{esc(words)}")'


def _reply(success, speech, steps, error):
    return (f'handleVoiceReply({"true" if success else "false"},"{esc(speech)}",'
            f'{steps},"{esc(error)}")')


# -- _voice_ask ---------------------------------------------------------------------

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
    assert not any(w in out for w in SECRET_WORDS)       # ...but never its words


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


def test_the_wake_conversation_shows_each_turn(monkeypatch, capsys):
    api = _conversation_api(monkeypatch, ["hey mist " + HEARD.replace("open", "close")])
    api._conversation_loop(HEARD, "none")
    second = HEARD.replace("open", "close")
    assert [e for e in api.events if isinstance(e, str)] == [
        _turn(HEARD), _reply(True, REPLY, 2, ""), _turn(second), _reply(True, REPLY, 2, ""),
    ]
    assert [e[1] for e in api.events if isinstance(e, tuple)] == [HEARD, second]
    assert not any(w in capsys.readouterr().out for w in SECRET_WORDS)


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


def _python_notifications(words, result, steps_between=()):
    api = _voice_api(result)
    api._voice_ask(words, "none")
    turn, reply = api.events[0], api.events[-1]
    return [{"js": turn}] + [{"js": s} for s in steps_between] + [{"js": reply}]


STEPS = ['handleAgentStep(1,"open_app","notepad","")', 'handleAgentStep(2,"type_text","x","")']


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

NO_ANSWER = {"success": False, "command": {"action": "done", "parameter": "",
                                           "speech": "I couldn't read the answer."},
             "steps": 3, "error": "no answer"}


NO_SPEECH = {"success": False, "command": {"speech": ""}, "steps": 0, "error": "boom"}


def _api_error_result():
    """What ask_mistai returns for a failed LLM call: the agent sets error and
    speech to the same describe_api_error() sentence."""
    from core.api_manager import describe_api_error
    response = SimpleNamespace(status_code=503, json=lambda: {"error": "overloaded"})
    message = describe_api_error(response, "cohere")
    return message, {"success": False, "command": {"action": "done", "parameter": "",
                                                   "speech": message},
                     "steps": 0, "error": message}


def test_show_result_prefers_speech_after_busy_and_still_running():
    js = _script()
    body = js.split("function showResult(res) {", 1)[1].split("\n}\n", 1)[0]
    order = [body.index(s) for s in ("res.error === 'busy'", "res.error === 'still_running'",
                                     "String(res.command.speech || '').trim()",
                                     "addMsg('Error: ' + (res.error || 'Unknown'), 'error');")]
    assert order == sorted(order)
    assert "addMsg(String(res.command.speech), 'assistant');" in body
    assert "innerHTML" not in body          # addMsg writes textContent only


def _typed(results):
    """Typed turns through the page's real send() and showResult()."""
    actions = [{"js": "inp.value='question " + str(i) + "';send();"} for i in range(len(results))]
    return actions, {"ask": results}


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
