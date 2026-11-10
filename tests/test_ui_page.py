"""The page's startup and its Model / Gender cards (ui/html.py).

pywebview loads the page with NavigateToString, an opaque origin where every
browser storage API throws. The startup handler used to read localStorage
first, so it died before loading models, stats or status, and before starting
its intervals: an empty Model dropdown and a header stuck on "Checking...".

String checks always run. The Node tests run the WHOLE page script with those
storage APIs throwing, fire pywebviewready, and feed it the REAL
get_model_options()/set_model() output; they skip when Node is missing.

Merged by area (pure moves, no test changed) from: test_ui_model_card.py, test_ui_facts.py, test_ui_flags.py, test_ui_ptt.py."""

import json
import re
import shutil
import subprocess
from types import SimpleNamespace
import pytest
from core.api import Api
from core.config import MODEL_CHOICES, MODEL_LABELS
from ui.html import HTML
from core.memory import Memory
import sys
from core.api_manager import APIKeyManager
import threading



# ==========================================================================
# moved from tests/test_ui_model_card.py
# ==========================================================================

def _script():
    return HTML.split("<script>", 1)[1].split("</script>", 1)[0]


def _ready_handler():
    js = _script()
    return js.split("window.addEventListener('pywebviewready'", 1)[1].split("});", 1)[0]


# -- string checks (always run) ---------------------------------------------------

def test_page_uses_no_browser_storage():
    found = re.findall(r"localstorage|sessionstorage|indexeddb|document\.cookie", HTML, re.I)
    assert found == []


def test_page_hardcodes_no_model_id_or_label():
    for word in (*MODEL_CHOICES, *MODEL_LABELS.values()):
        assert not re.search(rf"\b{re.escape(word)}\b", HTML, re.I), word
    m = re.search(r'<select id="sel-model">(.*?)</select>', HTML, re.S)
    assert m and m.group(1).strip() == ""


def test_startup_starts_intervals_first_and_runs_each_step_on_its_own():
    h = _ready_handler()
    assert h.startswith(",()=>{")                      # not async: nothing awaited in turn
    intervals = [h.index(f"setInterval({f}") for f in ("updateStats", "checkStatus", "syncBusy")]
    steps = [h.index(f"runStep('{s}',") for s in ("flags", "gender", "models", "stats", "status")]
    assert max(intervals) < min(steps)
    assert "await" not in h
    assert "async function runStep(name,step){\n  try{await step();}catch(e){console.error(" \
        in _script()


def test_gender_comes_from_python_and_is_saved_there():
    js = _script()
    assert "selGender.value=await pywebview.api.get_user_gender();" in js
    assert "const r=await pywebview.api.set_user_gender(e.target.value);" in js
    # A refusal shows Python's error and the select goes back to the saved value.
    assert "addMsg((r&&r.error)||'Could not save the gender address','error');await loadGender();" \
        in js


def test_model_card_shows_labels_sends_ids_and_has_a_visible_note():
    js = _script()
    assert '<div class="card-note" id="model-note">Loading models...</div>' in HTML
    assert "opt.value=String(c.id);opt.textContent=String(c.label||c.id);" in js
    assert "pywebview.api.set_model(e.target.value)" in js
    assert "'Model: '+(r.label||r.model)+' (applies from the next request)'" in js
    assert "setModelNote('Could not load the model list: '" in js


# -- the whole page in Node ----------------------------------------------------------

_HARNESS = r"""
const CONFIG = %CONFIG%;
const realSetTimeout = setTimeout;
const intervals = [];
// With CONFIG.fakeTimers the page's timeouts only run when an action says
// {advance: ms}, so a 3s note or a 30s safety restore can be checked at once.
let fakeNow = 0, timers = [], timerId = 0;
if (CONFIG.fakeTimers) {
  globalThis.setTimeout = (fn, ms) => { timers.push({id: ++timerId, at: fakeNow + (ms || 0), fn}); return timerId; };
  globalThis.clearTimeout = (id) => { timers = timers.filter(t => t.id !== id); };
}
function advance(ms) {
  fakeNow += ms;
  for (;;) {
    const due = timers.filter(t => t.at <= fakeNow).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers = timers.filter(t => t !== due);
    due.fn();
  }
}
globalThis.setInterval = (fn, ms) => { intervals.push(ms); return intervals.length; };
const consoleErrors = [];
console.error = (...a) => consoleErrors.push(a.map(x => String(x && x.message || x)).join(' '));

function makeEl(tag) {
  const classes = new Set();
  return {
    tag, children: [], style: {}, dataset: {},
    classList: {
      toggle(c, force) { const on = force === undefined ? !classes.has(c) : !!force;
        on ? classes.add(c) : classes.delete(c); return on; },
      add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); }, get list() { return [...classes].sort(); },
    },
    textContent: '', value: '', className: '', disabled: false, placeholder: '',
    scrollTop: 0, scrollHeight: 0, onclick: null, onchange: null, onkeypress: null,
    appendChild(c) { this.children.push(c); return c; }, remove() {}, focus() {},
    setAttribute() {}, addEventListener() {}, querySelector() { return makeEl('x'); },
    set innerHTML(_v) { this.children = []; }, get innerHTML() { return ''; },
  };
}
const els = {};
const denied = (what) => () => {
  throw new Error("Failed to read the '" + what + "' property from 'Window': Access is denied for this document.");
};
globalThis.document = {
  getElementById(id) { return els[id] || (els[id] = makeEl('#' + id)); },
  createElement(t) { return makeEl(t); },
};
Object.defineProperty(globalThis.document, 'cookie', {get: denied('cookie'), set: denied('cookie')});
for (const name of ['localStorage', 'sessionStorage', 'indexedDB'])
  Object.defineProperty(globalThis, name, {get: denied(name)});
globalThis.window = globalThis;
const listeners = {};
window.addEventListener = (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); };
window.open = () => {};

const calls = [];
function api(name, value) {
  return (...args) => {
    calls.push(name);
    const fail = (CONFIG.fail || {})[name];
    if (fail === 'reject') return Promise.reject(new Error(name + ' failed'));
    if (fail === 'hang') return new Promise(() => {});
    return Promise.resolve(typeof value === 'function' ? value(...args) : value);
  };
}
globalThis.pywebview = {api: {
  get_model_options: api('get_model_options', CONFIG.options),
  get_user_gender: api('get_user_gender', CONFIG.gender),
  set_user_gender: api('set_user_gender', (g) => CONFIG.set_gender[g]),
  set_model: api('set_model', (m) => CONFIG.set_model[m]),
  get_memory_stats: api('get_memory_stats',
    {conversation_length: 3, actions_performed: 2, facts_remembered: 1, pending_reminders: 0}),
  check_api_status: api('check_api_status', {online: true}),
  is_agent_busy: api('is_agent_busy', {busy: false}),
  get_ui_flags: api('get_ui_flags', CONFIG.flags),
  set_api_key: api('set_api_key', {success: true}),
  set_api_mode: api('set_api_mode', {success: true}),
  push_to_talk: api('push_to_talk', () => (CONFIG.ptt || []).shift() || {success: true}),
  start_wake_word_detection: api('start_wake_word_detection', {success: true}),
  stop_wake_word_detection: api('stop_wake_word_detection', {success: true}),
  import_my_facts: api('import_my_facts', () => CONFIG.facts_result),
  ask_mistai: api('ask_mistai', () => (CONFIG.ask || []).shift()),
}};

%PAGE%

const settle = () => new Promise(r => realSetTimeout(r, 30));
let out_actions = [];
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const sel = document.getElementById('sel-model'), note = document.getElementById('model-note');
  const snap = () => ({
    options: sel.children.map(o => [o.value, o.textContent]), value: sel.value,
    note: [note.textContent, note.className],
    msgs: document.getElementById('chat').children.map(m => [m.className, m.textContent]),
  });
  const out = {calls: calls.slice(), intervals, consoleErrors: consoleErrors.slice(),
    gender: document.getElementById('sel-gender').value,
    online: document.getElementById('online-text').textContent,
    // A real element stores textContent as a string.
    stats: String(document.getElementById('stat-msgs').textContent),
    // Not set by the page: the HTML default stays (display:none, string-checked).
    backendRow: document.getElementById('backend-row').style.display || 'unset', ...snap()};
  for (const m of CONFIG.change || []) await sel.onchange({target: {value: m}});
  const gsel = document.getElementById('sel-gender');
  for (const g of CONFIG.gender_change || []) await gsel.onchange({target: {value: g}});
  // Push-to-talk and wake actions, with a snapshot after each.
  const status = () => ({
    text: document.getElementById('mist-status-text').textContent,
    cls: document.getElementById('mist-status').className,
    ptt: document.getElementById('btn-ptt').classList.list,
    label: document.getElementById('ptt-label').textContent,
    wave: document.getElementById('wave').style.opacity || '',
  });
  out_actions = [];
  for (const a of CONFIG.actions || []) {
    if (a.click) await document.getElementById(a.click).onclick();
    if (a.state) handleListenState(a.state, a.message || '');
    if (a.advance) advance(a.advance);
    if (a.busy !== undefined) setBusy(a.busy);
    if (a.js) eval(a.js);            // a notification exactly as Python sends it
    await settle();
    out_actions.push({...status(), calls: calls.filter(c => c === 'push_to_talk').length,
                      wake: document.getElementById('sw-wake').classList.list,
                      wakeCalls: calls.filter(c => c.endsWith('wake_word_detection')),
                      chat: document.getElementById('chat').children.map(m =>
                        [m.className, m.textContent, m.classList.contains('voice')]),
                      facts: [document.getElementById('facts-note').textContent,
                              document.getElementById('facts-note').className],
                      statsCalls: calls.filter(c => c === 'get_memory_stats').length,
                      errors: consoleErrors.slice()});
  }
  for (const s of CONFIG.save || []) {   // the API modal's Save button
    document.getElementById('apiKeyInput').value = s.key;
    document.getElementById('useBackend').checked = s.useBackend;
    await saveAPIKey();
  }
  await settle();
  out.after = {...snap(), calls: calls.slice(out.calls.length)};
  out.actions = out_actions;
  console.log(JSON.stringify(out));
})();
"""


def _real_outputs():
    """The REAL Api methods' output, as the page would receive it."""
    api = Api.__new__(Api)
    api.memory = SimpleNamespace(set_profile=lambda *_a: None)
    api.tts = SimpleNamespace(set_gender=lambda _g: None)
    options = api.get_model_options()
    set_results = {m: api.set_model(m) for m in ("mistral", "bogus")}
    set_gender = {g: api.set_user_gender(g) for g in ("male", "robot")}
    return options, set_results, set_gender


def _run_page(tmp_path, **config):
    options, set_results, set_gender = _real_outputs()
    cfg = {"options": options, "gender": "female", "set_model": set_results,
           "set_gender": set_gender, "flags": {"is_dev": True}, **config}
    script = tmp_path / "page.js"
    script.write_text(_HARNESS.replace("%CONFIG%", json.dumps(cfg))
                      .replace("%PAGE%", _script()), encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


LABELLED = [["gemini", "Nova"], ["cohere", "Sage"], ["mistral", "Flux"]]


STARTUP_CALLS = {"get_ui_flags", "get_user_gender", "get_model_options", "get_memory_stats",
                 "check_api_status"}


@needs_node
def test_whole_page_starts_with_storage_denied_and_renders_the_real_options(tmp_path):
    r = _run_page(tmp_path, change=["mistral", "bogus"], gender_change=["male", "robot"])
    assert r["consoleErrors"] == []                 # storage was never touched
    assert STARTUP_CALLS <= set(r["calls"])
    assert r["intervals"] == [1000, 4000, 20000, 1500]   # clock, then the three
    assert r["gender"] == "female"                  # from Python, not the browser
    assert r["options"] == LABELLED and r["value"] == "cohere"
    assert r["note"] == ["", "card-note"]
    assert r["online"] == "Online" and r["stats"] == "3"
    # Choosing Flux sends the id; an id Python refuses shows its error and the
    # card reloads to the model really in use.
    assert r["after"]["msgs"] == [
        ["msg system", "Model: Flux (applies from the next request)"],
        ["msg error", "Unknown model. Choose one of: gemini, cohere, mistral."],
        # A refused gender shows Python's error and reloads the saved value.
        ["msg error", "Unknown gender address. Choose one of: male, female, none."],
    ]
    assert r["after"]["calls"] == ["set_model", "set_model", "get_model_options",
                                   "set_user_gender", "set_user_gender", "get_user_gender"]


@needs_node
def test_a_failed_model_load_shows_a_visible_error_and_blocks_nothing(tmp_path):
    r = _run_page(tmp_path, fail={"get_model_options": "reject"})
    assert r["note"] == ["Could not load the model list: get_model_options failed",
                         "card-note error"]
    assert any(e.startswith("[models] could not load the model list:")
               for e in r["consoleErrors"])
    assert r["online"] == "Online" and r["stats"] == "3" and r["gender"] == "female"
    assert r["intervals"] == [1000, 4000, 20000, 1500]


@needs_node
def test_an_empty_model_list_is_an_error_not_a_blank_dropdown(tmp_path):
    r = _run_page(tmp_path, options={"choices": [], "current": "cohere"})
    assert r["options"] == []
    assert r["note"] == ["Could not load the model list: no models were returned",
                         "card-note error"]


@needs_node
def test_a_step_that_never_answers_does_not_block_the_others(tmp_path):
    r = _run_page(tmp_path, fail={"get_user_gender": "hang"})
    assert r["options"] == LABELLED
    assert r["online"] == "Online" and r["stats"] == "3"
    assert r["intervals"] == [1000, 4000, 20000, 1500]



# ==========================================================================
# moved from tests/test_ui_facts.py
# The "Import my facts" button in Settings (ui/html.py #btn-import-facts).
#
# The page only shows what the REAL Api.import_my_facts() reports (computed
# here against a temporary database and facts file). Node tests skip when Node
# is missing.
# ==========================================================================

@pytest.fixture
def real_result(monkeypatch, tmp_path):
    """real_result(content) -> what Api.import_my_facts() returns for a facts
    file with that content (None: no file)."""
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "memory.db"))
    path = tmp_path / "my_facts.json"
    monkeypatch.setenv("MISTAI_MY_FACTS_PATH", str(path))
    api = Api.__new__(Api)
    api.memory = Memory()

    def run(content):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        return api.import_my_facts()

    yield run
    api.memory.conn.close()


def test_the_button_sits_in_settings_and_shows_text_only():
    assert '<button class="btn-cancel" id="btn-import-facts"' in HTML
    assert HTML.index('id="btn-import-facts"') < HTML.index('onclick="saveAPIKey()"')
    js = _script()
    handler = js.split("document.getElementById('btn-import-facts').onclick=async()=>{", 1)[1] \
        .split("\n};\n", 1)[0]
    assert "const r=await pywebview.api.import_my_facts();" in handler
    assert "note.textContent=(r&&r.message)||'Import finished';" in handler
    assert "innerHTML" not in handler and "try{" in handler and "}catch(e){" in handler


def _click(tmp_path, result=None, **config):
    r = _run_page(tmp_path, actions=[{"click": "btn-import-facts"}], facts_result=result,
                  **config)
    return r["actions"][0], r


@needs_node
def test_a_missing_file_is_a_plain_note(tmp_path, real_result):
    result = real_result(None)
    snap, _ = _click(tmp_path, result)
    assert snap["facts"] == [result["message"], "card-note"]          # not an error
    assert snap["facts"][0].startswith("No facts file found.")
    assert snap["errors"] == []


@needs_node
def test_an_import_reports_what_it_added_and_refreshes_the_counters(tmp_path, real_result):
    result = real_result(json.dumps(["I park on level 3", "My dentist is Dr. Lee"]))
    before = _run_page(tmp_path, facts_result=result)["calls"].count("get_memory_stats")
    snap, _ = _click(tmp_path, result)
    assert snap["facts"] == ["Imported 2 facts (0 already saved, 0 restored).", "card-note"]
    assert snap["statsCalls"] == before + 1            # the Facts count updates


@needs_node
def test_an_unreadable_file_and_a_failed_call_are_errors(tmp_path, real_result):
    snap, _ = _click(tmp_path, real_result("{not json"))
    assert snap["facts"][0].startswith("Could not read ") and snap["facts"][1] == "card-note error"
    snap, r = _click(tmp_path, None, fail={"import_my_facts": "reject"})
    assert snap["facts"] == ["Import failed: import_my_facts failed", "card-note error"]
    assert any(e.startswith("[facts] import failed:") for e in snap["errors"])



# ==========================================================================
# moved from tests/test_ui_flags.py
# Developer-only Settings controls (Api.get_ui_flags, ui/html.py).
#
# The "Use MistAI backend" switch shows only in a dev run: a source run, or a
# build with MISTAI_DEV=1. In a build, Save sets the key and never touches the
# route, so MISTAI_USE_BACKEND stays in charge. Node tests skip without Node.
# ==========================================================================

def _flags(monkeypatch, frozen, dev_env):
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    if dev_env is None:
        monkeypatch.delenv("MISTAI_DEV", raising=False)
    else:
        monkeypatch.setenv("MISTAI_DEV", dev_env)
    return Api.__new__(Api).get_ui_flags()


@pytest.mark.parametrize("frozen, dev_env, is_dev", [
    (False, None, True),      # source run
    (False, "0", True),
    (True, None, False),      # release build
    (True, "1", True),        # build opted into dev controls
    (True, " 1 ", True),
    (True, "0", False),
    (True, "true", False),    # only "1" opts in
])
def test_get_ui_flags(monkeypatch, frozen, dev_env, is_dev):
    assert _flags(monkeypatch, frozen, dev_env) == {"is_dev": is_dev}


@pytest.mark.parametrize("use_backend, expect_backend", [("true", True), ("false", False)])
def test_a_build_save_keeps_the_route_from_the_environment(monkeypatch, use_backend,
                                                           expect_backend):
    """What a non-dev Save does (set_api_key only) leaves MISTAI_USE_BACKEND's
    route in place."""
    monkeypatch.setenv("MISTAI_API_KEY", "mistai_test")
    monkeypatch.setenv("MISTAI_USE_BACKEND", use_backend)
    api = Api.__new__(Api)
    api.api_manager = APIKeyManager()
    before = api.api_manager.get_api_url()
    api.set_api_key("mistai_other")
    assert api.api_manager.get_api_url() == before
    assert before.endswith("/api/chat") is expect_backend


@pytest.mark.parametrize("frozen, dev_env, allowed", [
    (False, None, True),      # source run
    (True, "1", True),        # build opted into dev controls
    (True, None, False),      # release build
])
@pytest.mark.parametrize("env_backend", ["true", "false"])
def test_set_api_mode_is_refused_outside_dev(monkeypatch, frozen, dev_env, allowed,
                                             env_backend):
    _flags(monkeypatch, frozen, dev_env)
    monkeypatch.setenv("MISTAI_API_KEY", "mistai_test")
    monkeypatch.setenv("MISTAI_USE_BACKEND", env_backend)
    api = Api.__new__(Api)
    api.api_manager = APIKeyManager()
    env_route = api.api_manager.get_api_url()
    flip = env_backend != "true"                      # ask for the other route
    result = api.set_api_mode(flip)
    if allowed:
        assert result["success"] is True
        assert api.api_manager.get_api_url().endswith("/api/chat") is flip
    else:
        assert result == {"success": False,
                          "error": "Switching the API route is a developer setting. "
                                   "Set MISTAI_USE_BACKEND in .env instead."}
        assert api.api_manager.get_api_url() == env_route   # changed nothing


# -- string checks (always run) ---------------------------------------------------

def test_backend_switch_is_hidden_until_python_says_dev():
    row = re.search(r'<label id="backend-row" style="([^"]*)">', HTML)
    assert row and row.group(1).endswith("display:none")
    js = _script()
    assert "let devMode=false;" in js
    assert "devMode=!!(f&&f.is_dev===true);" in js
    assert "document.getElementById('backend-row').style.display=devMode?'block':'none';" in js
    assert "runStep('flags',loadFlags);" in js


def test_save_switches_the_route_only_in_dev():
    js = _script()
    save = js.split("async function saveAPIKey(){", 1)[1].split("\n}\n", 1)[0]
    assert save.count("set_api_mode") == 1
    assert "if(devMode && (key || useBackend)){" in save
    assert save.index("if(devMode && (key || useBackend)){") < save.index("set_api_mode")
    # A refusal from Python is shown, not reported as a switch.
    assert "if(r&&r.success===false)addMsg(r.error||'Could not change the API route','error');" \
        in save


# -- the whole page in Node ----------------------------------------------------------

SAVES = [{"key": "", "useBackend": True}, {"key": "mistai_test", "useBackend": False}]


@needs_node
@pytest.mark.parametrize("frozen, dev_env", [(False, None), (True, None), (True, "1")])
def test_the_page_follows_the_real_flags(monkeypatch, tmp_path, frozen, dev_env):
    flags = _flags(monkeypatch, frozen, dev_env)
    r = _run_page(tmp_path, flags=flags, save=SAVES)
    assert r["consoleErrors"] == []
    assert r["backendRow"] == ("block" if flags["is_dev"] else "none")
    if flags["is_dev"]:
        # The old behaviour, kept for developers: Save with the box ticked
        # switches to the backend; a key with it unticked goes direct.
        assert r["after"]["calls"] == ["set_api_mode", "set_api_key", "set_api_mode"]
    else:
        assert r["after"]["calls"] == ["set_api_key"]          # never the route
        assert ["msg system", "API key configured"] in r["after"]["msgs"]


@needs_node
@pytest.mark.parametrize("fail", ["reject", "hang"])
def test_flags_that_never_load_leave_the_page_as_a_build(tmp_path, fail):
    r = _run_page(tmp_path, fail={"get_ui_flags": fail}, save=SAVES)
    assert r["backendRow"] == "unset"                          # the hidden HTML default
    assert "set_api_mode" not in r["after"]["calls"]
    assert r["options"] and r["online"] == "Online"            # nothing else blocked



# ==========================================================================
# moved from tests/test_ui_ptt.py
# The push-to-talk button (ui/html.py #btn-ptt) and the single wake switch.
#
# The under-orb Wake Word button became push-to-talk; the wake word keeps only
# its sidebar switch ("Wake word (beta)"), off at startup. String checks always
# run; the Node tests run the whole page (tests/test_ui_model_card.py harness)
# with timers under the test's control, and skip when Node is missing.
# ==========================================================================

def _real_refusals():
    """What the REAL push_to_talk() returns when it refuses."""
    api = Api.__new__(Api)
    api._agent_lock, api._conversation_lock = threading.Lock(), threading.Lock()
    api._agent_lock.acquire()
    busy = api.push_to_talk()
    api._agent_lock.release()
    api._conversation_lock.acquire()
    listening = api.push_to_talk()
    return busy, listening


# -- string checks (always run) ---------------------------------------------------

def test_the_under_orb_wake_button_is_now_push_to_talk():
    assert "ctrl-wake" not in HTML and "ctrlWake" not in HTML
    m = re.search(r'<div class="control-item">\s*<button class="ctrl-btn" id="btn-ptt"[^>]*>'
                  r'[^<]*</button>\s*<div class="ctrl-label" id="ptt-label">Talk</div>', HTML)
    assert m, "btn-ptt sits in the under-orb control bar with the same styling"
    assert HTML.index('id="btn-ptt"') < HTML.index('id="ctrl-caption"')   # same position


def test_wake_word_is_one_sidebar_switch_off_at_startup():
    assert ('<div class="toggle-row"><span>Wake word (beta)</span>'
            '<div class="sw" id="sw-wake"></div></div>') in HTML
    js = _script()
    assert "let busy=false,wakeOn=false," in js
    assert js.count("start_wake_word_detection") == 1      # only from the switch
    assert "swWake.onclick=toggleWake;" in js
    wake = js.split("async function toggleWake(){", 1)[1].split("\n}\n", 1)[0]
    assert "btnPtt" not in wake                             # no second control to sync


def test_push_to_talk_button_and_states():
    js = _script()
    click = js.split("btnPtt.onclick=async()=>{", 1)[1].split("\n};\n", 1)[0]
    assert "const r=await pywebview.api.push_to_talk();" in click
    assert "try{" in click and "}catch(e){" in click
    assert "showPttNote((r&&r.message)||" in click
    assert "setMistStatus('listening','Waiting for mic...')" in js
    assert "setMistStatus('listening','Listening...')" in js
    assert "const PTT_NOTE_MS=3000,PTT_SAFETY_MS=45000;" in js
    # Notes go through setMistStatus, which writes textContent, never HTML.
    assert "mistStatusText.textContent=text;" in js
    note = js.split("function showPttNote(text){", 1)[1].split("\n}\n", 1)[0]
    assert "innerHTML" not in note


# -- the whole page in Node ----------------------------------------------------------

READY = {"text": "Ready", "cls": "mist-status ready", "ptt": [], "wave": "0"}


def _run(tmp_path, actions, ptt=None):
    r = _run_page(tmp_path, fakeTimers=True, actions=actions, ptt=ptt or [])
    assert r["consoleErrors"] == [] and r["actions"][-1]["errors"] == []
    return r["actions"]


def _status(snap):
    return {k: snap[k] for k in ("text", "cls", "ptt", "wave")}


@needs_node
def test_a_press_shows_waiting_then_listening_then_returns(tmp_path):
    a = _run(tmp_path, [
        {"click": "btn-ptt"},
        {"state": "waiting"},
        {"state": "listening"},
        {"state": "idle"},
    ])
    # The click alone changes nothing: Python drives the states. (The fake DOM
    # does not parse the HTML, so the untouched status text reads "".)
    assert a[0]["calls"] == 1 and a[0]["text"] == "" and a[0]["ptt"] == []
    assert _status(a[1]) == {"text": "Waiting for mic...", "cls": "mist-status listening",
                             "ptt": ["active"], "wave": "0"}
    assert _status(a[2]) == {"text": "Listening...", "cls": "mist-status listening",
                             "ptt": ["active"], "wave": "1"}
    assert _status(a[3]) == READY
    assert all(s["wake"] == [] for s in a)                  # wake stays off


@needs_node
def test_didnt_catch_that_shows_for_three_seconds(tmp_path):
    a = _run(tmp_path, [
        {"state": "listening"},
        {"state": "idle", "message": "Didn't catch that"},
        {"advance": 2900},
        {"advance": 200},
    ])
    assert _status(a[1]) == {"text": "Didn't catch that", "cls": "mist-status notice",
                             "ptt": [], "wave": "0"}
    assert a[2]["text"] == "Didn't catch that"
    assert _status(a[3]) == READY


@needs_node
def test_refusals_show_pythons_message_then_restore(tmp_path):
    busy, listening = _real_refusals()
    a = _run(tmp_path, [{"click": "btn-ptt"}, {"advance": 3000},
                        {"click": "btn-ptt"}, {"advance": 3000}],
             ptt=[busy, listening])
    assert a[0]["text"] == "Mist is working. Press Stop or say stop."
    assert a[0]["cls"] == "mist-status notice" and a[0]["ptt"] == []
    assert a[1]["text"] == "Ready"
    assert a[2]["text"] == "Already listening."
    assert a[3]["text"] == "Ready"


@needs_node
def test_a_lost_idle_is_restored_after_forty_five_seconds(tmp_path):
    a = _run(tmp_path, [{"state": "listening"}, {"advance": 44000}, {"advance": 1000}])
    assert a[1]["text"] == "Listening..." and a[1]["ptt"] == ["active"]
    assert _status(a[2]) == READY


@needs_node
def test_every_waiting_or_listening_message_restarts_the_safety_window(tmp_path):
    a = _run(tmp_path, [
        {"state": "waiting"},
        {"advance": 40000},
        {"state": "listening"},          # restarts: a fresh 45s
        {"advance": 40000},              # 80s since the press, 40s since listening
        {"state": "listening"},          # the wake-phrase follow-up: restarts again
        {"advance": 44000},
        {"advance": 1000},               # 45s of silence from Python: restored
    ])
    assert a[1]["text"] == "Waiting for mic..."
    assert a[3]["text"] == "Listening..." and a[3]["label"] == "Done"
    assert a[5]["text"] == "Listening..." and a[5]["ptt"] == ["active"]
    assert _status(a[6]) == READY and a[6]["label"] == "Talk"


@needs_node
def test_a_failing_call_is_shown_and_logged(tmp_path):
    r = _run_page(tmp_path, fakeTimers=True, actions=[{"click": "btn-ptt"}],
                  fail={"push_to_talk": "reject"})
    assert r["actions"][0]["text"] == "Push-to-talk failed: push_to_talk failed"
    assert any(e.startswith("[ptt] push_to_talk failed:") for e in r["actions"][0]["errors"])


@needs_node
def test_the_task_status_follows_a_listen_and_wake_mode(tmp_path):
    a = _run(tmp_path, [
        {"state": "listening"},
        {"busy": True},                  # a task starts while the label shows
        {"state": "idle"},
        {"busy": False},
        {"click": "sw-wake"},
        {"click": "sw-wake"},
    ])
    assert a[1]["text"] == "Listening..."                    # the listen keeps the label
    assert a[2]["text"] == "Working..."
    assert a[3]["text"] == "Ready"
    assert a[4]["text"] == "Listening for wake word..." and a[4]["wake"] == ["on"]
    assert a[5]["text"] == "Ready" and a[5]["wake"] == []
    assert a[5]["wakeCalls"] == ["start_wake_word_detection", "stop_wake_word_detection"]
    assert a[3]["wakeCalls"] == []                           # nothing started it before


# -- a second press ends the listen ("Done") -----------------------------------------

def _real_stop():
    """What the REAL push_to_talk() returns for a press during a listen."""
    api = Api.__new__(Api)
    api._agent_lock, api._conversation_lock = threading.Lock(), threading.Lock()
    api._ptt_waiting = threading.Event()
    api._ptt_waiting.set()
    return api.push_to_talk()


def test_the_label_reads_done_while_listening():
    assert '<div class="ctrl-label" id="ptt-label">Talk</div>' in HTML
    js = _script()
    assert "const pttLabel=document.getElementById('ptt-label');" in js
    state = js.split("function handleListenState(state,message){", 1)[1].split("\n}\n", 1)[0]
    assert "pttLabel.textContent=pttState==='listening'?'Done':'Talk';" in state
    assert "innerHTML" not in state


@needs_node
def test_a_second_press_reads_done_and_ends_quietly(tmp_path):
    stopped = _real_stop()
    assert stopped == {"success": True, "stopped": True}
    a = _run(tmp_path, [
        {"click": "btn-ptt"},
        {"state": "waiting"},
        {"state": "listening"},
        {"click": "btn-ptt"},            # "Done": Python ends the listen
        {"state": "idle"},               # cancelled: no note
    ], ptt=[{"success": True}, stopped])
    assert a[1]["label"] == "Talk"                           # waiting: not yet
    assert a[2]["label"] == "Done" and a[2]["ptt"] == ["active"]
    assert a[3]["calls"] == 2 and a[3]["text"] == "Listening..."   # no refusal note
    assert a[3]["label"] == "Done"
    assert _status(a[4]) == READY and a[4]["label"] == "Talk"


@needs_node
def test_the_label_returns_to_talk_after_a_note_and_the_safety_restore(tmp_path):
    a = _run(tmp_path, [
        {"state": "listening"},
        {"state": "idle", "message": "Didn't catch that"},
        {"state": "listening"},
        {"advance": 45000},
    ])
    assert a[0]["label"] == "Done" and a[1]["label"] == "Talk"
    assert a[2]["label"] == "Done" and a[3]["label"] == "Talk"
