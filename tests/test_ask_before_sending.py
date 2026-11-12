"""Ask before sending (quick_message only), profile key "ask_before_sending":
"always" asks up front as before; "never" doesn't ask; "when_unsure" (the
default) doesn't ask only when the recipient AND the message are the user's
own words (Api._agent_task, the original request) AND the Stage A checks
matched the recipient exactly; otherwise it asks (up front when not
grounded, inside the action when the match was only close). Everything else
that needs confirmation is unchanged in every mode. Logs carry labels only."""

import json
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from confirm_helpers import bind_confirmation
from core.agent import AgentLoop
from core.api import Api
from core.config import (ASK_BEFORE_SENDING_CHOICES, ASK_BEFORE_SENDING_DEFAULT,
                         ASK_BEFORE_SENDING_HELP, ASK_BEFORE_SENDING_LABELS, send_is_grounded)
from core.tools import SPECS
from ui.html import HTML

CONTACT = "Zorblax Quindlewhip"
MESSAGE = "the pelican lands at noon"
SEND = {"action": "quick_message", "parameter": f"discord|{CONTACT}|{MESSAGE}"}
DONE = {"action": "done", "speech": "ok"}
GOAL = f"tell {CONTACT} on Discord: The pelican lands at noon!"
CONTENT = ("zorblax", "quindlewhip", "pelican", "noon")


class _Memory:
    def __init__(self, stored=None):
        self.profile = {} if stored is None else {"ask_before_sending": stored}
        self.rows = []

    def get_profile(self, key, default=None):
        return self.profile.get(key, default)

    def set_profile(self, key, value):
        self.profile[key] = value

    def add_action(self, *a):
        self.rows.append(a)


def _run(monkeypatch, replies, answers, *, mode=None, goal=GOAL, seen=None,
         confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop; quick_message runs the REAL
    Api._act_quick_message (vision answers `seen`, default exact), typed
    confirmations answer from `answers`."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    seen = list(seen if seen is not None else [CONTACT] * 3)
    rec = SimpleNamespace(calls=[], keys=[], clips=[], asked=[], requests=[])
    memory = _Memory(mode)

    def answer(question):
        rec.asked.append(question)
        return answers.pop(0) if answers else None

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a in confirm),
        memory=memory,
        _resolve_path_placeholders=lambda p: p,
        speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _get_text_answer=lambda q, timeout=60: answer(q),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        stop_current_task=lambda: api._stop_requested.set(),
        _last_opened_app="",
        _agent_task=goal, _agent_generation=1,
        # what _act_quick_message needs
        vision=SimpleNamespace(ready=True),
        screen=SimpleNamespace(mark_action=lambda: None),
        is_app_running=lambda _a: True, switch_to_app=lambda _a: True,
        _foreground_process_matches=lambda _a: True,
        _get_app_search_hotkey=lambda _a: "ctrl+k",
        set_clipboard=lambda t: rec.clips.append(t) or True,
        _vision_read=lambda q: ((seen.pop(0), True) if seen else ("", False)),
        last_result_text="",
    )
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    api._qm_read_name = lambda *a, **k: Api._qm_read_name(api, *a, **k)

    def run(a, p):
        rec.calls.append((a, p))
        ok = Api._act_quick_message(api, p) if a == "quick_message" else True
        return SimpleNamespace(action=a, parameter=p, success=ok, detail=api.last_result_text,
                               verified=None, escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="")

    api.executor = SimpleNamespace(run=run)

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: rec.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: rec.keys.append("+".join(k)))
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", "text", None)
    rec.api, rec.loop = api, loop
    return rec


def _sent(rec):
    return MESSAGE in rec.clips and rec.keys.count("enter") == 2


# -- the setting -----------------------------------------------------------------------

def test_default_is_when_unsure():
    assert ASK_BEFORE_SENDING_DEFAULT == "when_unsure"
    api = SimpleNamespace(memory=_Memory())
    assert Api.ask_before_sending_mode(api) == "when_unsure"


@pytest.mark.parametrize("stored", ["", "bogus", "ALWAYS ", None, 3])
def test_a_bad_stored_value_falls_back_to_the_default(stored):
    api = SimpleNamespace(memory=_Memory(stored))
    expected = "always" if stored == "ALWAYS " else "when_unsure"
    assert Api.ask_before_sending_mode(api) == expected


def test_an_unreadable_profile_gives_the_default():
    def broken(*_a):
        raise OSError("locked")
    api = SimpleNamespace(memory=SimpleNamespace(get_profile=broken))
    assert Api.ask_before_sending_mode(api) == "when_unsure"


def test_set_validates_in_python_and_saves_the_profile_key():
    api = SimpleNamespace(memory=_Memory())
    for bad in ("sometimes", "", None, "always; drop table"):
        r = Api.set_ask_before_sending(api, bad)
        assert r["success"] is False and "Choose one of" in r["error"]
        assert api.memory.profile == {}
    r = Api.set_ask_before_sending(api, " Never ")
    assert r == {"success": True, "current": "never", "label": ASK_BEFORE_SENDING_LABELS["never"],
                 "help": ASK_BEFORE_SENDING_HELP["never"]}
    assert api.memory.profile == {"ask_before_sending": "never"}


def test_options_come_from_config():
    api = SimpleNamespace(memory=_Memory("always"))
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    o = Api.get_ask_before_sending_options(api)
    assert [c["id"] for c in o["choices"]] == list(ASK_BEFORE_SENDING_CHOICES)
    assert all(c["label"] == ASK_BEFORE_SENDING_LABELS[c["id"]] and c["help"] for c in o["choices"])
    assert o["current"] == "always"


@pytest.mark.parametrize("param, goal, grounded", [
    (f"discord|{CONTACT}|{MESSAGE}", GOAL, True),
    (f"discord|{CONTACT}|{MESSAGE}", GOAL.upper(), True),
    (f"discord|Zorblax|{MESSAGE}", GOAL, True),             # the user's words, shorter
    (f"discord|Zorb|{MESSAGE}", GOAL, False),               # not a whole word
    (f"discord|Someone Else|{MESSAGE}", GOAL, False),
    (f"discord|{CONTACT}|the pelican lands at 12", GOAL, False),
    (f"discord|{CONTACT}|", GOAL, False),
    ("discord|only two", GOAL, False),
    (f"discord|{CONTACT}|{MESSAGE}", "", False),
])
def test_send_is_grounded(param, goal, grounded):
    assert send_is_grounded(param, goal) is grounded


# -- the gate --------------------------------------------------------------------------

def test_grounded_and_exact_sends_without_asking(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], [])
    assert rec.asked == [] and _sent(rec)
    out = capsys.readouterr().out
    assert "[Confirm] grounded" in out and "[Confirm] exact" in out


def test_a_model_invented_recipient_asks_up_front(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], ["no"], goal=f"tell my friend: {MESSAGE}")
    assert len(rec.asked) == 1 and rec.calls == []          # asked before anything ran
    assert "[Confirm] not grounded" in capsys.readouterr().out


def test_a_model_edited_message_asks(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["yes"], goal=f"tell {CONTACT} the bird arrives at 12")
    assert len(rec.asked) == 1 and _sent(rec)


def test_a_phase_brief_does_not_ground_the_send(monkeypatch):
    # The task text the loop gets (here "send it") is not the user's request.
    rec = _run(monkeypatch, [SEND, DONE], ["no"], goal="send it")
    assert len(rec.asked) == 1 and rec.calls == []


def test_a_close_match_asks_inside_the_action_and_a_yes_sends(monkeypatch, capsys):
    close = "Zorblax Quindlewip"
    rec = _run(monkeypatch, [SEND, DONE], ["yes"], seen=[close, close, close])
    assert len(rec.asked) == 1 and _sent(rec)
    assert "[Confirm] not exact" in capsys.readouterr().out


def test_a_close_match_and_a_no_types_and_sends_nothing(monkeypatch):
    close = "Zorblax Quindlewip"
    rec = _run(monkeypatch, [SEND, DONE], ["no"], seen=[close, close])
    assert len(rec.asked) == 1
    assert MESSAGE not in rec.clips and rec.keys.count("enter") == 1
    assert rec.result["success"] is True                    # the model's done after the decline
    assert any("User explicitly declined" in m["content"]
               for m in rec.requests[-1]["messages"] if m["role"] == "user")


def test_a_close_match_and_an_unclear_answer_ends_the_phase(monkeypatch):
    close = "Zorblax Quindlewip"
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "hmm"], seen=[close, close])
    assert len(rec.asked) == 2 and MESSAGE not in rec.clips
    assert rec.result["error"] == "not confirmed"


def test_always_asks_even_when_grounded_and_exact(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["yes"], mode="always")
    assert len(rec.asked) == 1 and _sent(rec)


def test_never_does_not_ask(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], [], mode="never", goal="something else entirely")
    assert rec.asked == [] and _sent(rec)
    assert "[Confirm] skipped (setting)" in capsys.readouterr().out


def test_never_still_never_sends_to_a_mismatched_chat(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], [], mode="never", seen=[CONTACT, CONTACT, "Someone Else"])
    assert rec.keys.count("enter") == 1


def test_the_setting_is_read_at_call_time(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], [], mode="never")
    assert rec.asked == []
    rec.api.memory.profile["ask_before_sending"] = "always"
    assert rec.loop._send_gate(SEND["parameter"], "text", None, 1)[:2] == (True, None)


@pytest.mark.parametrize("mode", ASK_BEFORE_SENDING_CHOICES)
def test_destructive_actions_ask_in_every_mode(monkeypatch, mode):
    close = {"action": "window_close", "parameter": "Discord"}
    rec = _run(monkeypatch, [close, DONE], ["no"], mode=mode)
    assert len(rec.asked) == 1 and rec.calls == []


def test_an_approved_send_is_not_asked_again_in_the_same_task(monkeypatch):
    # The first try is approved, then fails its final check; the identical
    # retry is not asked again.
    rec = _run(monkeypatch, [SEND, SEND, DONE], ["yes"], goal="message my friend",
               seen=[CONTACT, CONTACT, "Someone Else", CONTACT, CONTACT, CONTACT])
    assert len(rec.asked) == 1
    assert len(rec.calls) == 2 and rec.keys.count("enter") == 3   # 2 opens + 1 send


def test_logs_carry_labels_only(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    close = "Zorblax Quindlewip"
    _run(monkeypatch, [SEND, DONE], [])
    _run(monkeypatch, [SEND, DONE], ["yes"], seen=[close, close, close])
    _run(monkeypatch, [SEND, DONE], ["no"], goal="tell my friend")
    _run(monkeypatch, [SEND, DONE], [], mode="never")
    out = capsys.readouterr()
    low = (out.out + out.err).lower()
    assert [w for w in CONTENT if w in low] == []


# -- the Settings dialog ---------------------------------------------------------------

def _script():
    return HTML.split("<script>", 1)[1].split("</script>", 1)[0]


def test_page_hardcodes_no_choice_id_label_or_help():
    for c in ASK_BEFORE_SENDING_CHOICES:            # ids, as a JS/HTML literal
        assert f"'{c}'" not in HTML and f'"{c}"' not in HTML, c
    assert "when_unsure" not in HTML
    for word in (*ASK_BEFORE_SENDING_LABELS.values(), *ASK_BEFORE_SENDING_HELP.values()):
        assert word not in HTML, word
    m = re.search(r'<select id="sel-send">(.*?)</select>', HTML, re.S)
    assert m and m.group(1).strip() == ""
    js = _script()
    assert "opt.value=String(c.id);opt.textContent=String(c.label||c.id);" in js
    assert "pywebview.api.set_ask_before_sending(e.target.value)" in js
    assert "innerHTML=" not in js.split("async function loadSendSetting", 1)[1].split(
        "selSend.innerHTML=''", 1)[1].split("selSend.onchange", 1)[0]


_HARNESS = r"""
const CONFIG = %CONFIG%;
const realSetTimeout = setTimeout;
globalThis.setInterval = () => 0;
const errors = [];
console.error = (...a) => errors.push(a.map(x => String(x && x.message || x)).join(' '));
function makeEl(tag) {
  const classes = new Set();
  return {tag, children: [], style: {}, dataset: {}, textContent: '', value: '', className: '',
    classList: {add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, f) { (f === undefined ? !classes.has(c) : f) ? classes.add(c) : classes.delete(c); },
      contains(c) { return classes.has(c); }},
    appendChild(c) { this.children.push(c); return c; }, remove() {}, focus() {},
    setAttribute() {}, addEventListener() {}, querySelector() { return makeEl('x'); },
    set innerHTML(_v) { this.children = []; }, get innerHTML() { return ''; }};
}
const els = {};
globalThis.document = {getElementById(id) { return els[id] || (els[id] = makeEl('#' + id)); },
                       createElement(t) { return makeEl(t); }};
globalThis.window = globalThis;
const listeners = {};
window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
const calls = [];
const impl = {
  get_ask_before_sending_options: () => CONFIG.options,
  set_ask_before_sending: (v) => CONFIG.set[v],
};
globalThis.pywebview = {api: new Proxy({}, {get: (_t, name) => (...args) => {
  calls.push(name);
  return Promise.resolve(impl[name] ? impl[name](...args) : {});
}})};
%PAGE%
const settle = () => new Promise(r => realSetTimeout(r, 30));
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const sel = document.getElementById('sel-send'), note = document.getElementById('send-note');
  const snap = () => ({options: sel.children.map(o => [o.value, o.textContent]), value: sel.value,
                       note: [note.textContent, note.className]});
  const before = {calls: calls.filter(c => c.includes('sending')), ...snap()};
  openAPIModal();
  await settle();
  const opened = snap();
  const steps = [];
  for (const v of CONFIG.change) { await sel.onchange({target: {value: v}}); await settle(); steps.push(snap()); }
  console.log(JSON.stringify({before, opened, steps, errors: errors.filter(e => e.includes('[send]')),
                              calls: calls.filter(c => c.includes('sending'))}));
})();
"""


def _run_page(tmp_path, change):
    api = SimpleNamespace(memory=_Memory())
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    options = Api.get_ask_before_sending_options(api)
    sets = {v: Api.set_ask_before_sending(api, v) for v in change}
    script = tmp_path / "page.js"
    script.write_text(_HARNESS.replace("%CONFIG%", json.dumps(
        {"options": options, "set": sets, "change": change})).replace("%PAGE%", _script()),
        encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_whole_page_dropdown_renders_python_options_and_saves_ids(tmp_path):
    r = _run_page(tmp_path, ["never", "bogus"])
    assert r["before"]["options"] == [] and r["before"]["calls"] == []   # not loaded at startup
    assert r["opened"]["options"] == [[c, ASK_BEFORE_SENDING_LABELS[c]]
                                      for c in ASK_BEFORE_SENDING_CHOICES]
    assert r["opened"]["value"] == "when_unsure"
    assert r["opened"]["note"] == [ASK_BEFORE_SENDING_HELP["when_unsure"], "card-note"]
    assert r["steps"][0]["note"] == [ASK_BEFORE_SENDING_HELP["never"], "card-note"]
    # A refused id shows Python's error and reloads the saved choice.
    assert r["steps"][1]["note"][1] == "card-note error"
    assert "Choose one of" in r["steps"][1]["note"][0]
    assert r["calls"] == ["get_ask_before_sending_options", "set_ask_before_sending",
                          "set_ask_before_sending", "get_ask_before_sending_options"]
    assert r["errors"] == []
