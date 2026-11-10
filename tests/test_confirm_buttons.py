"""Yes/No buttons for confirmations (Api.ask_confirmation + the page).

- Voice: the question is spoken and shown with buttons; the listen gets the
  answer event as its stop_event, so a click ends it and wins over speech.
- Typed: the buttons or the text box.
- The event is cleared before asking and after answering; a stop request ends
  the wait; MistAI's window is shown while pending and minimized again after
  (MISTAI_CONFIRM_REMINIMIZE, default on).
- _listen_once without stop_event is unchanged.
"""

import json
import re
import subprocess
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core.api import Api
from core.planner import PlanRunner
from test_ui_model_card import _HARNESS, _real_outputs, _script, needs_node


def _api(listen=None, speaking=False):
    """A bare Api with the confirmation plumbing and recorders."""
    api = Api.__new__(Api)
    api._answer_lock = threading.Lock()
    api._answer_event = threading.Event()
    api._awaiting_text_answer = threading.Event()
    api._pending_answer = api._pending_source = None
    api._stop_requested = threading.Event()
    api._agent_lock = threading.Lock()
    api._agent_state_lock = threading.Lock()
    api.rec = {"js": [], "spoken": [], "listens": [], "window": []}
    api._notify_js = lambda js: api.rec["js"].append(js)
    api.speak = lambda text: api.rec["spoken"].append(text)
    api.tts = SimpleNamespace(speaking=speaking, interrupt=lambda: None)
    api._window = SimpleNamespace(restore=lambda: api.rec["window"].append("restore"))
    api.minimize_window = lambda: api.rec["window"].append("minimize")
    api.CONFIRM_TEXT_TIMEOUT = 0.5
    api._voice_start = lambda *_a, **_k: None
    api._voice_mark = lambda *_a, **_k: None

    def default_listen(timeout=6, stop_event=None, **_k):
        api.rec["listens"].append((timeout, stop_event is api._answer_event))
        return listen() if callable(listen) else listen

    api._listen_once = default_listen
    return api


def _click_during_listen(api, answer, then_heard="no"):
    """A listen that is still running when the user clicks: it waits until the
    stop_event is set (as the real stream proxy ends listen()) and reports
    what speech it would have returned."""
    def listen(timeout=6, stop_event=None, **_k):
        api.rec["listens"].append((timeout, stop_event is api._answer_event))
        threading.Timer(0.05, lambda: Api.submit_answer(api, answer, "button")).start()
        assert stop_event.wait(2)
        return None if stop_event.is_set() else then_heard
    api._listen_once = listen


# -- Api.ask_confirmation ------------------------------------------------------------------

def test_a_click_answers_while_a_listen_is_pending_and_stops_it(capsys):
    api = _api()
    _click_during_listen(api, "yes")
    assert Api.ask_confirmation(api, "Send hi to Ada?", "voice") == ("yes", "button")
    assert api.rec["listens"] == [(10, True)]          # the answer event is the stop_event
    assert api.rec["spoken"] == ["Send hi to Ada?"]
    assert api.rec["js"] == ['showConfirmPrompt("Send hi to Ada?")', "closeConfirmPrompt()"]
    assert not api._answer_event.is_set() and not api._awaiting_text_answer.is_set()
    assert "Ada" not in capsys.readouterr().out


def test_no_click_falls_back_to_speech():
    api = _api(listen="yes please")
    assert Api.ask_confirmation(api, "q?", "voice") == ("yes please", "voice")


def test_a_click_wins_over_speech_heard_in_the_same_listen():
    api = _api()

    def listen(timeout=6, stop_event=None, **_k):
        Api.submit_answer(api, "no", "button")       # clicked as the listen returned
        return "yes"
    api._listen_once = listen
    assert Api.ask_confirmation(api, "q?", "voice") == ("no", "button")


def test_a_click_while_the_question_is_still_spoken_skips_the_listen():
    api = _api()
    api.tts = SimpleNamespace(speaking=True)
    api.speak = lambda _t: Api.submit_answer(api, "yes", "button")
    assert Api.ask_confirmation(api, "q?", "voice") == ("yes", "button")
    assert api.rec["listens"] == []


def test_typed_mode_takes_a_button_or_the_text_box():
    api = _api()
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, "yes", "button")
    assert Api.ask_confirmation(api, "q?", "text") == ("yes", "button")
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, "Yes, go")
    assert Api.ask_confirmation(api, "q?", "text") == ("Yes, go", "typed")
    assert api.rec["listens"] == []                  # typed mode never listens


def test_typed_mode_with_no_answer_times_out_as_none():
    api = _api()
    api.CONFIRM_TEXT_TIMEOUT = 0.01
    assert Api.ask_confirmation(api, "q?", "text") == (None, "typed")


def test_a_stop_request_ends_the_wait(monkeypatch):
    api = _api()
    api._agent_lock.acquire()
    api.CONFIRM_TEXT_TIMEOUT = 5
    threading.Timer(0.05, lambda: Api.stop_current_task(api)).start()
    answer, _source = Api.ask_confirmation(api, "q?", "text")
    assert answer == "__mistai_stop_requested__" and api._stop_requested.is_set()


def test_a_stop_request_also_ends_a_pending_listen():
    api = _api()
    api._agent_lock.acquire()

    def listen(timeout=6, stop_event=None, **_k):
        threading.Timer(0.05, lambda: Api.stop_current_task(api)).start()
        assert stop_event.wait(2)
        return None
    api._listen_once = listen
    answer, _ = Api.ask_confirmation(api, "q?", "voice")
    assert answer == "__mistai_stop_requested__"


def test_no_stale_answer_leaks_into_the_next_confirmation():
    api = _api(listen="maybe")
    Api.submit_answer(api, "yes", "button")          # a late click from an old prompt
    assert Api.ask_confirmation(api, "q?", "voice") == ("maybe", "voice")
    _click_during_listen(api, "no")
    assert Api.ask_confirmation(api, "q?", "voice") == ("no", "button")
    Api.submit_answer(api, "yes", "button")          # a click after it was answered
    api._listen_once = lambda timeout=6, stop_event=None, **_k: "nope"
    assert Api.ask_confirmation(api, "q?", "voice") == ("nope", "voice")


@pytest.mark.parametrize("flag, expected", [(None, ["restore", "minimize"]),
                                            ("1", ["restore", "minimize"]),
                                            ("0", ["restore"])])
def test_the_window_is_shown_then_minimized_again(monkeypatch, flag, expected):
    if flag is None:
        monkeypatch.delenv("MISTAI_CONFIRM_REMINIMIZE", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_REMINIMIZE", flag)
    api = _api(listen="yes")
    Api.ask_confirmation(api, "q?", "voice")
    assert api.rec["window"] == expected


def test_ask_user_still_uses_the_text_prompt_and_never_minimizes():
    api = _api()
    api._notify_js = lambda js: (api.rec["js"].append(js), Api.submit_answer(api, "Documents"))
    assert Api._get_text_answer(api, "Which folder?", timeout=1) == "Documents"
    assert api.rec["js"] == ['showAskUserPrompt("Which folder?")']
    assert api.rec["window"] == ["restore"]


# -- the agent gate and planner._confirm ----------------------------------------------------

def _loop(monkeypatch, api, replies, mode="voice"):
    from core.agent import AgentLoop
    from core.tools import SPECS
    replies = iter(json.dumps(r) for r in replies)
    calls = []
    api.task_memory = SimpleNamespace(get_hints_for_task=lambda _t: [])
    api.caption_window, api.captions_enabled = None, False
    api.live = SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)})
    api.api_manager = SimpleNamespace(get_headers=lambda: {},
                                      get_api_url=lambda: "https://example.invalid/a",
                                      get_fallback_url=lambda: "https://example.invalid/b")
    api.tools = SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                                requires_confirmation=lambda a, *_x, **_k: a == "quick_message")
    api.executor = SimpleNamespace(run=lambda a, p: calls.append(a) or SimpleNamespace(
        action=a, parameter=p, success=True, detail="", verified=None,
        escalate_to_user=False, reliability_note=lambda: "", escalated_from=""))
    api.memory = SimpleNamespace(add_action=lambda *a: None)
    api._resolve_path_placeholders = lambda p: p
    api._last_opened_app = ""
    monkeypatch.setattr("core.agent.requests.post", lambda *_a, **_k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    return loop._loop("send it", "m", "none", mode, None), calls


SEND = {"action": "quick_message", "parameter": "Discord|Ada|hi"}
DONE = {"action": "done", "speech": "ok"}


def test_a_click_after_an_unclear_first_listen_answers(monkeypatch, capsys):
    api = _api()
    first = [True]

    def listen(timeout=6, stop_event=None, **_k):
        if first.pop() if first else False:
            return "marblewood"                      # unclear speech
        threading.Timer(0.05, lambda: Api.submit_answer(api, "yes", "button")).start()
        stop_event.wait(2)
        return None
    api._listen_once = listen
    result, calls = _loop(monkeypatch, api, [SEND, DONE])
    assert calls == ["quick_message"] and result["success"]
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear (voice)" in log
    assert "[Confirm] answer=yes (button) (retry)" in log
    assert "marblewood" not in log and "Ada" not in log


@pytest.mark.parametrize("answer, ran", [("yes", True), ("no", False)])
def test_a_button_answers_the_agent_gate_in_typed_mode(monkeypatch, capsys, answer, ran):
    api = _api()
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, answer, "button")
    _result, calls = _loop(monkeypatch, api, [SEND, DONE, DONE], mode="text")
    assert (calls == ["quick_message"]) is ran
    assert f"[Confirm] answer={answer} (button)" in capsys.readouterr().out


def test_planner_confirm_takes_a_button(capsys):
    api = _api()
    _click_during_listen(api, "yes")
    runner = PlanRunner.__new__(PlanRunner)
    runner.api = api
    assert runner._confirm("Continue with the independent phases?", "voice") is True
    assert "[Confirm] answer=yes (button)" in capsys.readouterr().out


# -- _listen_once with a stop_event --------------------------------------------------------

class _Stream:
    def __init__(self):
        self.reads = 0

    def read(self, size):
        self.reads += 1
        return b"\x10\x00" * 8

    def close(self):
        pass


class _Mic:
    SAMPLE_RATE, SAMPLE_WIDTH, CHUNK = 16000, 2, 8

    def __init__(self):
        self.stream = _Stream()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _listen_api(stop_after=None):
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _Mic()
    calls = []

    class Rec:
        energy_threshold = 300

        def adjust_for_ambient_noise(self, src, duration=1):
            calls.append(("adjust", duration))

        def listen(self, src, timeout=None, phrase_time_limit=None):
            calls.append(("listen", timeout, phrase_time_limit, type(src.stream).__name__))
            reads = 0
            while src.stream.read(8):                # ends when the proxy returns b""
                reads += 1
                if stop_after is not None and reads == stop_after[0]:
                    stop_after[1].set()
                if reads > 50:
                    break
            calls.append(("reads", reads))
            return object()

        def recognize_google(self, audio):
            calls.append(("recognize",))
            return "yes"

    api.recognizer = Rec()
    api.calls = calls
    return api


def test_listen_once_defaults_are_unchanged():
    api = _listen_api()
    assert Api._listen_once(api, timeout=10) == "yes"
    assert api.calls[0] == ("adjust", 0.3)
    assert api.calls[1] == ("listen", 10, 15, "_Stream")         # no proxy without stop_event
    assert api.calls[-1] == ("recognize",)
    assert (api.recognizer.pause_threshold, api.recognizer.non_speaking_duration) == (1.0, 0.5)


def test_a_stop_event_ends_the_listen_within_one_chunk_and_skips_recognition():
    stop = threading.Event()
    api = _listen_api(stop_after=(3, stop))
    assert Api._listen_once(api, timeout=10, stop_event=stop) is None
    assert ("listen", 10, 15, "_StoppableStream") in api.calls
    assert ("reads", 3) in api.calls                              # the next read ended it
    assert ("recognize",) not in api.calls


def test_an_unset_stop_event_changes_nothing_else():
    api = _listen_api()
    assert Api._listen_once(api, timeout=10, stop_event=threading.Event()) == "yes"
    assert api.calls[0] == ("adjust", 0.3) and api.calls[-1] == ("recognize",)


# -- the page (Node, the whole script) ------------------------------------------------------

def _page_harness():
    prefix = _HARNESS.split("%PAGE%", 1)[0]
    prefix = prefix.replace(
        "appendChild(c) { this.children.push(c); return c; }, remove() {},",
        "appendChild(c) { c.parent = this; this.children.push(c); return c; }, "
        "remove() { if (this.parent) this.parent.children = "
        "this.parent.children.filter(x => x !== this); this.parent = null; },")
    assert "c.parent = this" in prefix
    prefix = prefix.replace(
        "  ask_mistai: api('ask_mistai', () => (CONFIG.ask || []).shift()),",
        "  ask_mistai: api('ask_mistai', () => (CONFIG.ask || []).shift()),\n"
        "  submit_answer: (...args) => { submitted.push(args); return Promise.resolve({success: true}); },")
    return "const submitted = [];\n" + prefix


_DRIVER = r"""
const settle = () => new Promise(r => realSetTimeout(r, 20));
function confirmBox() { return chat.children.filter(m => m.className.includes('confirm')).pop(); }
function view() {
  const box = confirmBox();
  return {
    question: box ? box.children[0].textContent : null,
    buttons: box && box.children[1] && box.children[1].parent === box
      ? box.children[1].children.map(b => [b.textContent, b.className]) : [],
    answering: document.getElementById('inp').dataset.answering || '',
    inputDisabled: document.getElementById('inp').disabled,
    submitted: submitted.slice(),
    lastUser: (chat.children.filter(m => m.className === 'msg user').pop() || {}).textContent || null,
  };
}
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const out = {};
  for (const step of CONFIG.steps) {
    if (step.js) eval(step.js);
    if (step.click !== undefined) {
      const b = confirmBox().children[1].children[step.click];
      for (let i = 0; i < (step.times || 1); i++) await b.onclick();
    }
    if (step.type) { document.getElementById('inp').value = step.type; await send(); }
    await settle();
    out[step.name] = view();
  }
  out.consoleErrors = consoleErrors;
  console.log(JSON.stringify(out));
})();
"""


def _run_confirm_page(tmp_path, steps):
    options, set_results, set_gender = _real_outputs()
    cfg = {"options": options, "gender": "none", "set_model": set_results,
           "set_gender": set_gender, "flags": {"is_dev": False}, "steps": steps}
    script = tmp_path / "confirm.js"
    script.write_text(_page_harness().replace("%CONFIG%", json.dumps(cfg))
                      + _script() + _DRIVER, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


QUESTION = 'Send "<b>hi</b>" to Ada on Discord?'


@needs_node
def test_page_shows_text_and_buttons_and_one_click_sends_one_answer(tmp_path):
    r = _run_confirm_page(tmp_path, [
        {"name": "shown", "js": f"showConfirmPrompt({json.dumps(QUESTION)})"},
        {"name": "clicked", "click": 0, "times": 2},           # a double click
        {"name": "closed", "js": "closeConfirmPrompt()"},     # Python closes it after
    ])
    assert r["shown"]["question"] == QUESTION                  # text, not markup
    assert r["shown"]["buttons"] == [["Yes", "btn-save"], ["No", "btn-cancel"]]
    assert r["shown"]["answering"] == "true" and r["shown"]["inputDisabled"] is False
    assert r["clicked"]["submitted"] == [["yes", "button"]]     # exactly one submit
    assert r["clicked"]["buttons"] == [] and r["clicked"]["lastUser"] == "Yes"
    assert r["clicked"]["answering"] == "false"
    assert r["closed"]["submitted"] == [["yes", "button"]]
    assert r["consoleErrors"] == []


@needs_node
def test_page_buttons_go_away_after_a_voice_or_typed_answer(tmp_path):
    r = _run_confirm_page(tmp_path, [
        {"name": "shown", "js": "showConfirmPrompt('Proceed?')"},
        {"name": "voice", "js": "closeConfirmPrompt()"},      # answered by speech
        {"name": "again", "js": "showConfirmPrompt('Sorry, yes or no?')"},
        {"name": "typed", "type": "no"},
    ])
    assert r["voice"]["buttons"] == [] and r["voice"]["answering"] == "false"
    assert r["voice"]["submitted"] == []
    assert r["again"]["buttons"] == [["Yes", "btn-save"], ["No", "btn-cancel"]]
    assert r["typed"]["buttons"] == [] and r["typed"]["submitted"] == [["no"]]


def test_page_builds_the_prompt_with_text_only_and_no_storage():
    js = _script()
    body = js.split("function showConfirmPrompt(question){", 1)[1].split("\nfunction openAPIModal", 1)[0]
    assert "innerHTML" not in body
    assert "q.textContent=String(question||'');" in body
    assert "pywebview.api.submit_answer(answer,'button')" in body
    assert not re.search(r"localstorage|sessionstorage|indexeddb", js, re.I)
