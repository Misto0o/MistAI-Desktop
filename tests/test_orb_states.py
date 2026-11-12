"""The orb (ui/html.py): one visible state from what the page already knows.
orbState: the push-to-talk mic wins, then Mist's voice (is_agent_busy's
speaking), then a running task; else idle. setOrb writes it to the orb's
data-state; CSS draws breathe / pulse / swirl / ripples, still under
prefers-reduced-motion. Node tests skip when Node is missing."""

import json
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from core.api import Api
from ui.html import HTML
from test_ui_page import _HARNESS, _real_outputs, _script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _css():
    return HTML.split("<style>", 1)[1].split("</style>", 1)[0]


def _function(name):
    js = _script()
    start = js.index(f"function {name}(")
    return js[start:js.index("\n}", start) + 2]


# -- string checks -------------------------------------------------------------------

def test_markup_starts_idle_with_the_effect_layers():
    assert '<div class="orb-wrap" id="orb-wrap" data-state="idle">' in HTML
    assert '<div class="orb-swirl"></div>' in HTML
    assert HTML.count('class="orb-ripple') == 2
    assert 'id="orb"' in HTML and 'id="wave"' in HTML          # what the page already used


@pytest.mark.parametrize("state", ["listening", "thinking", "speaking"])
def test_each_active_state_has_its_own_effect(state):
    assert f'.orb-wrap[data-state="{state}"]' in _css()


def test_reduced_motion_stops_every_orb_animation_but_keeps_the_state_visible():
    css = _css()
    block = css.split("@media (prefers-reduced-motion: reduce){", 1)[1].split("\n}", 1)[0]
    assert "animation:none !important" in block
    for sel in (".orb,", ".orb-swirl", ".orb-ripple", ".orb-wrap::before", ".orb-wrap::after"):
        assert sel in block
    assert '[data-state="thinking"] .orb-swirl{opacity:.6}' in block
    assert '[data-state="speaking"] .orb-ripple{opacity:.5' in block


def test_the_orb_uses_no_external_resource_or_library():
    css = _css()
    orb_css = css[css.index(".orb-swirl{"):css.index("@media (prefers-reduced-motion")]
    assert not re.search(r"url\(|@import|https?:", orb_css)
    for name in ("orbState", "setOrb"):
        assert not re.search(r"https?:|import\(|require\(", _function(name))


# -- the state function in Node --------------------------------------------------------

@needs_node
def test_orb_state_priorities():
    cases = [
        ({"ptt": "idle", "speaking": False, "busy": False}, "idle"),
        ({"ptt": "idle", "speaking": False, "busy": True}, "thinking"),
        ({"ptt": "idle", "speaking": True, "busy": False}, "speaking"),
        ({"ptt": "idle", "speaking": True, "busy": True}, "speaking"),
        ({"ptt": "waiting", "speaking": False, "busy": False}, "listening"),
        ({"ptt": "listening", "speaking": True, "busy": True}, "listening"),
        ({"ptt": "bogus", "speaking": False, "busy": False}, "idle"),
    ]
    js = (_function("orbState") + "\nconsole.log(JSON.stringify("
          + json.dumps([c for c, _ in cases]) + ".map(orbState)));")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [want for _, want in cases]


_DRIVE = r"""
const settle = () => new Promise(r => realSetTimeout(r, 30));
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const wrap = document.getElementById('orb-wrap');
  const out = [];
  const snap = (label) => out.push([label, wrap.dataset.state]);
  const busyIs = (s) => { pywebview.api.is_agent_busy = () => Promise.resolve(s); };
  handleListenState('waiting', ''); snap('ptt waiting');
  handleListenState('listening', ''); snap('ptt listening');
  handleListenState('idle', "Didn't catch that"); snap('ptt idle with a note');
  setBusy(true); snap('task');
  busyIs({busy: true, speaking: true}); await syncBusy(); snap('task + voice');
  handleListenState('listening', ''); snap('mic wins');
  handleListenState('idle', '');
  busyIs({busy: false, speaking: true}); await syncBusy(); snap('voice after the task');
  busyIs({busy: false}); await syncBusy(); snap('back to idle');
  console.log(JSON.stringify({out, errors: consoleErrors}));
})();
"""


@needs_node
def test_the_page_drives_the_orb_from_its_existing_signals(tmp_path):
    options, set_results, set_gender = _real_outputs()
    cfg = {"options": options, "gender": "female", "set_model": set_results,
           "set_gender": set_gender, "flags": {"is_dev": False}}
    prefix = _HARNESS.split("%PAGE%", 1)[0]
    script = tmp_path / "orb.js"
    script.write_text(prefix.replace("%CONFIG%", json.dumps(cfg)) + _script() + _DRIVE,
                      encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["errors"] == []
    assert res["out"] == [
        ["ptt waiting", "listening"], ["ptt listening", "listening"],
        ["ptt idle with a note", "idle"], ["task", "thinking"],
        ["task + voice", "speaking"], ["mic wins", "listening"],
        ["voice after the task", "speaking"], ["back to idle", "idle"],
    ]


# -- the Python side: is_agent_busy reports Mist's voice --------------------------------

@pytest.mark.parametrize("speaking", [True, False])
def test_is_agent_busy_reports_speaking(speaking):
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_task = ""
    api._agent_started_at = 0
    api._stop_requested = threading.Event()
    api.tts = SimpleNamespace(speaking=speaking)
    assert api.is_agent_busy()["speaking"] is speaking


def test_is_agent_busy_without_tts_is_not_speaking():
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_task = ""
    api._agent_started_at = 0
    api._stop_requested = threading.Event()
    assert api.is_agent_busy()["speaking"] is False
