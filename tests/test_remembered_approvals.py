""""Always for this person" (quick_message, setting "when_unsure" only): a
third button on the Yes/No prompt answers yes and remembers the app + person
as HMAC-SHA256(per-install random key, app|recipient) in user_profile
("approval_key", "approved:<hmac>"); no name is stored. A remembered person
skips the question only when the recipient checks matched EXACTLY. Settings
shows only a count and a Forget all that removes only those rows."""

import json
import shutil
import sqlite3
import subprocess
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core.agent import AgentLoop
from core.api import APPROVED_PREFIX, Api
from core.memory import Memory
from core.tools import SPECS
from ui.html import HTML

CONTACT = "Zorblax Quindlewhip"
MESSAGE = "the pelican lands at noon"
SEND = {"action": "quick_message", "parameter": f"discord|{CONTACT}|{MESSAGE}"}
DONE = {"action": "done", "speech": "ok"}
NOT_GROUNDED = "message my friend about lunch"
CONTENT = ("zorblax", "quindlewhip", "pelican", "noon")
CLICK = ("remember", "button")


@pytest.fixture
def memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "memory.db"))
    m = Memory()
    m.db_file = tmp_path / "memory.db"
    return m


def _api(memory, mode="when_unsure", seen=None):
    api = SimpleNamespace(memory=memory)
    memory.set_profile("ask_before_sending", mode)
    for name in ("ask_before_sending_mode", "_approval_id", "remember_send_approval",
                 "is_send_approved", "get_remembered_approvals_count",
                 "forget_remembered_approvals", "_qm_read_name"):
        setattr(api, name, getattr(Api, name).__get__(api))
    return api


def _run(monkeypatch, memory, replies, answers, *, mode="when_unsure", goal=NOT_GROUNDED,
         seen=None, gen=1, confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop with the real quick_message
    handler. answers: (answer, source) per confirmation, in order."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    seen = list(seen if seen is not None else [CONTACT] * 3)
    rec = SimpleNamespace(calls=[], keys=[], clips=[], offers=[])
    api = _api(memory, mode)

    def ask_confirmation(question, input_mode, offer_remember=False):
        rec.offers.append(offer_remember)
        return answers.pop(0) if answers else (None, "typed")

    def run(a, p):
        rec.calls.append((a, p))
        ok = Api._act_quick_message(api, p) if a == "quick_message" else True
        return SimpleNamespace(action=a, parameter=p, success=ok, detail=api.last_result_text,
                               verified=None, escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="")

    api.__dict__.update(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a in confirm),
        executor=SimpleNamespace(run=run), ask_confirmation=ask_confirmation,
        _resolve_path_placeholders=lambda p: p, speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"}, stop_current_task=lambda: None, _last_opened_app="",
        _agent_task=goal, _agent_generation=gen,
        vision=SimpleNamespace(ready=True), screen=SimpleNamespace(mark_action=lambda: None),
        is_app_running=lambda _a: True, switch_to_app=lambda _a: True,
        _foreground_process_matches=lambda _a: True, _get_app_search_hotkey=lambda _a: "ctrl+k",
        set_clipboard=lambda t: rec.clips.append(t) or True,
        _vision_read=lambda q: ((seen.pop(0), True) if seen else ("", False)),
        last_result_text="",
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *_a, json=None, **_k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: rec.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: rec.keys.append("+".join(k)))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", "text", None)
    rec.api = api
    return rec


def _sent(rec):
    return MESSAGE in rec.clips and rec.keys.count("enter") == 2


def _profile_rows(memory):
    con = sqlite3.connect(memory.db_file)
    try:
        return con.execute("SELECT key, value FROM user_profile").fetchall()
    finally:
        con.close()


# -- when the button is offered -----------------------------------------------------------

def test_offered_up_front_in_when_unsure(monkeypatch, memory):
    rec = _run(monkeypatch, memory, [SEND, DONE], [("no", "button")])
    assert rec.offers == [True]


def test_offered_in_the_mid_action_question(monkeypatch, memory):
    close = "Zorblax Quindlewip"
    rec = _run(monkeypatch, memory, [SEND, DONE], [("yes", "button")],
               goal=f"tell {CONTACT}: {MESSAGE}", seen=[close] * 3)
    assert rec.offers == [True] and _sent(rec)


def test_not_offered_in_always(monkeypatch, memory):
    rec = _run(monkeypatch, memory, [SEND, DONE], [("yes", "button")], mode="always")
    assert rec.offers == [False]


def test_never_offered_for_another_action(monkeypatch, memory):
    close = {"action": "window_close", "parameter": "Discord"}
    rec = _run(monkeypatch, memory, [close, DONE], [CLICK, ("no", "button")])
    assert rec.offers == [False, False]
    assert rec.api.get_remembered_approvals_count() == 0


def test_the_page_call_carries_the_offer_only_when_asked():
    sent = []
    api = SimpleNamespace(_answer_lock=threading.Lock(), _answer_event=threading.Event(),
                          _awaiting_text_answer=threading.Event(), _pending_answer=None,
                          _pending_source=None, _window=None, CONFIRM_TEXT_TIMEOUT=0)
    api._notify_js = sent.append
    Api.ask_confirmation(api, "Send it?", "text")
    Api.ask_confirmation(api, "Send it?", "text", offer_remember=True)
    assert sent == ['showConfirmPrompt("Send it?")', "closeConfirmPrompt()",
                    'showConfirmPrompt("Send it?",true)', "closeConfirmPrompt()"]


# -- remembering and using it ---------------------------------------------------------------

def test_a_click_sends_and_remembers_without_storing_a_name(monkeypatch, memory, capsys):
    rec = _run(monkeypatch, memory, [SEND, DONE], [CLICK])
    assert _sent(rec)
    assert rec.api.get_remembered_approvals_count() == 1
    rows = dict(_profile_rows(memory))
    approved = [k for k in rows if k.startswith(APPROVED_PREFIX)]
    assert len(approved) == 1 and len(approved[0]) == len(APPROVED_PREFIX) + 64
    assert len(rows["approval_key"]) == 64
    dump = json.dumps(_profile_rows(memory)).lower()
    assert [w for w in CONTENT + ("discord|",) if w in dump] == []
    assert "[Confirm] approval remembered" in capsys.readouterr().out


def test_a_remembered_person_and_an_exact_match_skips_the_question(monkeypatch, memory, capsys):
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    other = {"action": "quick_message", "parameter": f"Discord|{CONTACT.lower()}|another thing"}
    rec = _run(monkeypatch, memory, [other, DONE], [], gen=2)    # a new task, any message
    assert rec.offers == [] and rec.keys.count("enter") == 2
    assert "[Confirm] remembered approval used" in capsys.readouterr().out


def test_a_remembered_person_and_a_close_match_still_asks(monkeypatch, memory):
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    close = "Zorblax Quindlewip"
    rec = _run(monkeypatch, memory, [SEND, DONE], [("no", "button")], gen=2, seen=[close] * 3)
    assert rec.offers == [True] and MESSAGE not in rec.clips


@pytest.mark.parametrize("param", [f"slack|{CONTACT}|{MESSAGE}", f"discord|Zorblax Other|{MESSAGE}"])
def test_another_app_or_person_asks(monkeypatch, memory, param):
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    rec = _run(monkeypatch, memory, [{"action": "quick_message", "parameter": param}, DONE],
               [("no", "button")], gen=2)
    assert rec.offers == [True] and rec.calls == []


def test_a_typed_or_spoken_remember_is_not_a_click(monkeypatch, memory):
    rec = _run(monkeypatch, memory, [SEND, DONE], [("remember", "typed"), ("remember", "voice")])
    assert rec.result["error"] == "not confirmed"
    assert rec.api.get_remembered_approvals_count() == 0


def test_always_ignores_remembered_approvals(monkeypatch, memory):
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    rec = _run(monkeypatch, memory, [SEND, DONE], [("yes", "button")], gen=2, mode="always")
    assert rec.offers == [False]


def test_forget_removes_only_the_approvals(monkeypatch, memory):
    for key, value in (("model", "cohere"), ("user_gender", "none"), ("hotkey_discord", "ctrl+k"),
                       ("approvedish", "x"), ("my_approved:1", "y")):
        memory.set_profile(key, value)
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    api = _api(memory)
    api.remember_send_approval("slack", "Someone")
    assert api.get_remembered_approvals_count() == 2
    before = {k: v for k, v in _profile_rows(memory) if not k.startswith(APPROVED_PREFIX)}
    assert api.forget_remembered_approvals() == {"success": True, "removed": 2}
    assert api.get_remembered_approvals_count() == 0
    assert dict(_profile_rows(memory)) == before          # every other key untouched
    assert not api.is_send_approved("discord", CONTACT)


def test_matching_is_normalized_and_checking_creates_no_key(memory):
    api = _api(memory)
    assert api.is_send_approved("discord", CONTACT) is False
    assert memory.get_profile("approval_key") is None     # a check never creates the key
    assert api.remember_send_approval("Discord", "  zorblax   QUINDLEWHIP. ") is True
    assert api.is_send_approved("discord", CONTACT) is True
    assert api.remember_send_approval("discord", "  ") is False


def test_logs_name_no_person(monkeypatch, memory, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _run(monkeypatch, memory, [SEND, DONE], [CLICK], gen=1)
    _run(monkeypatch, memory, [SEND, DONE], [], gen=2)
    _api(memory).forget_remembered_approvals()
    out = capsys.readouterr()
    low = (out.out + out.err).lower()
    assert [w for w in CONTENT if w in low] == []


# -- the page ------------------------------------------------------------------------------------

def _script():
    return HTML.split("<script>", 1)[1].split("</script>", 1)[0]


def test_page_strings():
    js = _script()
    assert "if(offerRemember)buttons.push(['remember','Always for this person','btn-cancel']);" in js
    assert "const offerRemember=arguments[1]===true;" in js
    assert "approvalsNote.textContent='Remembered approvals: '+n;" in js
    assert '<button class="btn-cancel" id="btn-forget-approvals"' in HTML


_HARNESS = r"""
const CONFIG = %CONFIG%;
const realSetTimeout = setTimeout;
globalThis.setInterval = () => 0;
let timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = () => {};
const errors = [];
console.error = (...a) => errors.push(a.map(x => String(x && x.message || x)).join(' '));
function makeEl(tag) {
  const classes = new Set();
  const el = {tag, children: [], style: {}, dataset: {}, textContent: '', value: '', className: '',
    classList: {add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, f) { (f === undefined ? !classes.has(c) : f) ? classes.add(c) : classes.delete(c); },
      contains(c) { return classes.has(c); }},
    appendChild(c) { c.parent = el; this.children.push(c); return c; },
    remove() { if (el.parent) el.parent.children = el.parent.children.filter(x => x !== el); },
    focus() {}, setAttribute() {}, addEventListener() {}, querySelector() { return makeEl('x'); },
    set innerHTML(_v) { this.children = []; }, get innerHTML() { return ''; }};
  return el;
}
const els = {};
globalThis.document = {getElementById(id) { return els[id] || (els[id] = makeEl('#' + id)); },
                       createElement(t) { return makeEl(t); }};
globalThis.window = globalThis;
const listeners = {};
window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
const calls = [];
let count = CONFIG.count;
const impl = {
  get_remembered_approvals_count: () => count,
  forget_remembered_approvals: () => { count = 0; return {success: true, removed: 2}; },
  submit_answer: (...a) => { submitted.push(a); return {success: true}; },
};
const submitted = [];
globalThis.pywebview = {api: new Proxy({}, {get: (_t, name) => (...args) => {
  calls.push(name);
  return Promise.resolve(impl[name] ? impl[name](...args) : {});
}})};
%PAGE%
const settle = () => new Promise(r => realSetTimeout(r, 30));
const buttons = () => {
  const all = document.getElementById('chat').children.flatMap(m => m.children)
    .filter(c => c.className === 'confirm-buttons');
  return all.flatMap(r => r.children.map(b => b.textContent));
};
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const out = {};
  showConfirmPrompt('Send it?'); out.plain = buttons(); closeConfirmPrompt();
  showConfirmPrompt('Send it?', 'yes'); out.notTrue = buttons(); closeConfirmPrompt();
  showConfirmPrompt('Send it?', true); out.offered = buttons();
  const row = document.getElementById('chat').children.flatMap(m => m.children)
    .find(c => c.className === 'confirm-buttons');
  await row.children[2].onclick(); await settle();
  out.submitted = submitted.slice(); out.afterClick = buttons();
  openAPIModal(); await settle();
  const note = document.getElementById('approvals-note'), btn = document.getElementById('btn-forget-approvals');
  out.note = note.textContent;
  await btn.onclick(); await settle();
  out.armed = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length];
  await btn.onclick(); await settle();
  out.forgot = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length, note.textContent];
  await btn.onclick(); timers.forEach(f => f()); await settle();
  out.disarmed = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length];
  out.errors = errors.filter(e => e.includes('[approvals]'));
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_whole_page_third_button_and_forget_all(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(_HARNESS.replace("%CONFIG%", json.dumps({"count": 2}))
                      .replace("%PAGE%", _script()), encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["plain"] == ["Yes", "No"] and r["notTrue"] == ["Yes", "No"]
    assert r["offered"] == ["Yes", "No", "Always for this person"]
    assert r["submitted"] == [["remember", "button"]] and r["afterClick"] == []
    assert r["note"] == "Remembered approvals: 2"
    assert r["armed"] == ["Click again to forget all", 0]            # the first click only asks
    assert r["forgot"] == ["Forget all", 1, "Remembered approvals: 0"]
    assert r["disarmed"] == ["Forget all", 1]                        # timed out: nothing forgotten
    assert r["errors"] == []
