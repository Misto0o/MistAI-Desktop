"""The push-to-talk button (ui/html.py #btn-ptt) and the single wake switch.

The under-orb Wake Word button became push-to-talk; the wake word keeps only
its sidebar switch ("Wake word (beta)"), off at startup. String checks always
run; the Node tests run the whole page (tests/test_ui_model_card.py harness)
with timers under the test's control, and skip when Node is missing.
"""

import re
import threading

from core.api import Api
from test_ui_model_card import HTML, _run_page, _script, needs_node


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
                  r'[^<]*</button>\s*<div class="ctrl-label">Talk</div>', HTML)
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
    assert "const PTT_NOTE_MS=3000,PTT_SAFETY_MS=30000;" in js
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
def test_a_lost_idle_is_restored_after_thirty_seconds(tmp_path):
    a = _run(tmp_path, [{"state": "listening"}, {"advance": 29000}, {"advance": 1000}])
    assert a[1]["text"] == "Listening..." and a[1]["ptt"] == ["active"]
    assert _status(a[2]) == READY


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
