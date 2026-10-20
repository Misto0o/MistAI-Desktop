"""The page's startup and its Model / Gender cards (ui/html.py).

pywebview loads the page with NavigateToString, an opaque origin where every
browser storage API throws. The startup handler used to read localStorage
first, so it died before loading models, stats or status, and before starting
its intervals: an empty Model dropdown and a header stuck on "Checking...".

String checks always run. The Node tests run the WHOLE page script with those
storage APIs throwing, fire pywebviewready, and feed it the REAL
get_model_options()/set_model() output; they skip when Node is missing.
"""

import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from core.api import Api
from core.config import MODEL_CHOICES, MODEL_LABELS
from ui.html import HTML


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
