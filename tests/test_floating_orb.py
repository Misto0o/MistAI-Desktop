"""The floating orb (core/floating_orb.py; Settings > Floating orb, off by
default): a small frameless always-on-top window that shows only the orb.
It is excluded from capture and from the OCR search, never activates, has no
title, talks to Python only through OrbBridge.orb_state(), and is never an
input target. Everything here runs on fakes: no window is created."""

import inspect
import json
import os
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import core.click_handler as ch
import core.enhanced_utils as eu
import core.floating_orb as fo
from core.api import Api
from core.config import is_self_process
from ui.html import HTML, ORB_HTML
from test_ui_page import _HARNESS, _real_outputs, _script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _state_api(ptt=False, speaking=False, busy=False):
    lock = threading.Lock()
    if busy:
        lock.acquire()
    return SimpleNamespace(_ptt_wants_mic=lambda: ptt, tts=SimpleNamespace(speaking=speaking),
                           _agent_lock=lock)


# -- state and bridge ----------------------------------------------------------------

@pytest.mark.parametrize("kw, want", [
    ({}, "idle"), ({"busy": True}, "thinking"), ({"speaking": True}, "speaking"),
    ({"speaking": True, "busy": True}, "speaking"), ({"ptt": True, "busy": True}, "listening"),
    ({"ptt": True, "speaking": True}, "listening"),
])
def test_orb_state_has_the_page_priorities(kw, want):
    assert fo.orb_state(_state_api(**kw)) == want


def test_orb_state_survives_a_broken_api():
    assert fo.orb_state(SimpleNamespace()) == "idle"


def test_the_bridge_exposes_only_the_state():
    bridge = fo.OrbBridge(_state_api(busy=True))
    public = [n for n in dir(bridge) if not n.startswith("_")]
    assert public == ["orb_state"]
    assert bridge.orb_state() == {"state": "thinking"}


# -- hardening -----------------------------------------------------------------------

class _User32:
    def __init__(self, exclude_ok=True):
        self.affinity, self.exstyle, self.exclude_ok = [], None, exclude_ok

    def SetWindowDisplayAffinity(self, hwnd, mode):
        self.affinity.append((hwnd, mode))
        return 1 if (mode == fo.WDA_EXCLUDEFROMCAPTURE and self.exclude_ok) or \
            mode == fo.WDA_MONITOR else 0

    def GetWindowLongW(self, hwnd, idx):
        assert idx == fo.GWL_EXSTYLE
        return 0x100

    def SetWindowLongW(self, hwnd, idx, value):
        self.exstyle = value


class _Gdi:
    def __init__(self):
        self.region = None

    def GetWindowRect(self, hwnd):
        return (100, 200, 240, 340)

    def CreateEllipticRgn(self, *box):
        return ("rgn", box)

    def SetWindowRgn(self, hwnd, rgn, redraw):
        self.region = (hwnd, rgn, redraw)


def test_harden_excludes_from_capture_makes_it_inactive_and_round():
    user32, gdi = _User32(), _Gdi()
    out = fo.harden(7, user32=user32, gdi=gdi)
    assert out == {"capture": "excluded", "noactivate": True, "round": True}
    assert user32.affinity == [(7, fo.WDA_EXCLUDEFROMCAPTURE)]
    assert user32.exstyle == 0x100 | fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE
    assert gdi.region == (7, ("rgn", (0, 0, 140, 140)), True)


def test_harden_falls_back_to_a_black_box_on_older_windows():
    user32 = _User32(exclude_ok=False)
    assert fo.harden(7, user32=user32, gdi=_Gdi())["capture"] == "black"
    assert [m for _h, m in user32.affinity] == [fo.WDA_EXCLUDEFROMCAPTURE, fo.WDA_MONITOR]


def test_harden_never_raises():
    boom = SimpleNamespace()          # no methods at all
    assert fo.harden(7, user32=boom, gdi=boom) == {
        "capture": "none", "noactivate": False, "round": False}


# -- the window ------------------------------------------------------------------------

class _Events:
    def __init__(self):
        self.shown = _Hook()


class _Hook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class _Webview:
    def __init__(self, fail=False):
        self.created, self.fail = [], fail

    def create_window(self, title, **kw):
        if self.fail:
            raise RuntimeError("no gui")
        win = SimpleNamespace(title=title, kw=kw, events=_Events(), shown=0, hidden=0,
                              native=SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 4242)))
        win.show = lambda: setattr(win, "shown", win.shown + 1)
        win.hide = lambda: setattr(win, "hidden", win.hidden + 1)
        self.created.append(win)
        return win


def test_the_window_is_small_frameless_on_top_untitled_and_unfocused(monkeypatch):
    monkeypatch.setattr(fo, "_corner_position", lambda size=fo.ORB_SIZE: (10, 20))
    wv = _Webview()
    api = _state_api()
    orb = fo.FloatingOrb(api, webview=wv)
    assert orb.show() is True
    (win,) = wv.created
    assert win.title == ""                                   # title lookups never find it
    kw = win.kw
    assert kw["html"] == ORB_HTML
    assert isinstance(kw["js_api"], fo.OrbBridge)            # never the full Api
    assert kw["js_api"] is not api
    assert (kw["width"], kw["height"], kw["x"], kw["y"]) == (fo.ORB_SIZE, fo.ORB_SIZE, 10, 20)
    assert kw["frameless"] and kw["on_top"] and not kw["focus"] and not kw["resizable"]
    assert "transparent" not in kw                           # opaque + round region instead


def test_hardening_runs_when_the_window_is_shown(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(fo, "harden", lambda h: seen.append(h) or
                        {"capture": "excluded", "noactivate": True, "round": True})
    wv = _Webview()
    orb = fo.FloatingOrb(_state_api(), webview=wv, html="<p>")
    orb.show()
    assert orb.handles() == []                               # no handle before "shown"
    for fn in wv.created[0].events.shown:
        fn()
    assert seen == [4242] and orb.handles() == [4242]
    assert "[FloatingOrb] shown | capture: excluded" in capsys.readouterr().out
    orb.hide()
    assert orb.handles() == [] and wv.created[0].hidden == 1
    orb.show()                                               # reused, not re-created
    assert len(wv.created) == 1 and wv.created[0].shown == 1


def test_a_window_that_cannot_open_reports_it(capsys):
    orb = fo.FloatingOrb(_state_api(), webview=_Webview(fail=True), html="<p>")
    assert orb.show() is False and orb.visible is False
    assert "could not show the orb (RuntimeError)" in capsys.readouterr().out


# -- the Api setting ----------------------------------------------------------------------

class _Memory:
    def __init__(self, **rows):
        self.rows = dict(rows)

    def get_profile(self, key, default=None):
        return self.rows.get(key, default)

    def set_profile(self, key, value):
        self.rows[key] = value


class _FakeOrb:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def show(self):
        self.calls.append("show")
        return self.ok

    def hide(self):
        self.calls.append("hide")

    def handles(self):
        return [99] if "show" in self.calls else []


def _api(memory=None, orb=None):
    api = Api.__new__(Api)
    api.memory = memory or _Memory()
    if orb is not None:
        api._orb = orb
    return api


def test_the_setting_is_off_by_default_and_nothing_is_created():
    api = _api()
    assert api.get_floating_orb() is False
    api.start_floating_orb_if_enabled()
    assert getattr(api, "_orb", None) is None
    assert api._orb_window_handles() == []


def test_turning_it_on_and_off():
    orb = _FakeOrb()
    api = _api(orb=orb)
    assert api.set_floating_orb(True) == {"success": True, "enabled": True}
    assert api.memory.rows["floating_orb"] == "on" and api.get_floating_orb() is True
    assert api._orb_window_handles() == [99]
    assert api.set_floating_orb(False) == {"success": True, "enabled": False}
    assert api.memory.rows["floating_orb"] == "off" and orb.calls == ["show", "hide"]


def test_a_window_that_cannot_open_turns_the_setting_back_off():
    api = _api(orb=_FakeOrb(ok=False))
    r = api.set_floating_orb(True)
    assert r["success"] is False and r["enabled"] is False and r["error"]
    assert api.memory.rows["floating_orb"] == "off"


def test_startup_shows_it_only_when_saved_on():
    orb = _FakeOrb()
    _api(memory=_Memory(floating_orb="on"), orb=orb).start_floating_orb_if_enabled()
    assert orb.calls == ["show"]


# -- never an input target -----------------------------------------------------------------

def test_input_is_refused_while_the_orb_is_in_front(monkeypatch):
    import win32gui

    api = _api(orb=_FakeOrb())
    api._orb.show()
    api._last_opened_app = None
    api._foreground_terminal = lambda: None
    api._input_target_report = lambda what: ("sent to the foreground", True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 99)
    report, ok = api._ensure_input_target("text")
    assert ok is False and report.startswith("NOT SENT") and "floating orb" in report
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 5)   # another window
    assert api._ensure_input_target("text") == ("sent to the foreground", True)


def test_the_orb_lives_in_mistais_own_process():
    # Open apps and process/window lookups skip MistAI's own PID (is_self_process).
    import psutil

    assert is_self_process(psutil.Process(os.getpid()).name(), os.getpid())


def test_untitled_windows_are_left_out_of_window_lists(monkeypatch):
    wins = [SimpleNamespace(title=""), SimpleNamespace(title="Untitled - Notepad")]
    monkeypatch.setattr(eu, "WINDOW_MGMT_AVAILABLE", True)
    monkeypatch.setattr(eu, "gw", SimpleNamespace(getAllWindows=lambda: wins), raising=False)
    assert eu.WindowManager.list_all_windows() == [{"title": "Untitled - Notepad"}]
    assert eu.WindowManager.find_window_normalized("") is None


# -- the OCR mask covers it -------------------------------------------------------------------

def test_the_ocr_mask_blacks_out_the_orb(monkeypatch):
    import win32gui

    h = ch.ImprovedClickHandler.__new__(ch.ImprovedClickHandler)
    h.extra_own_windows_fn = lambda: [99]
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hw: True)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hw: False)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hw: (10, 20, 30, 40))
    img = np.full((100, 100), 255, dtype=np.uint8)
    h._mask_extra_own_windows(img)
    assert (img[20:40, 10:30] == 0).all() and img[50, 50] == 255
    monkeypatch.setattr(win32gui, "IsIconic", lambda hw: True)      # minimized: nothing
    img = np.full((100, 100), 255, dtype=np.uint8)
    h._mask_extra_own_windows(img)
    assert (img == 255).all()
    h.extra_own_windows_fn = None
    assert h._mask_extra_own_windows(img) is img


def test_both_ocr_paths_apply_the_extra_mask_and_the_api_wires_it():
    src = inspect.getsource(ch.ImprovedClickHandler)
    assert src.count("self._mask_extra_own_windows(") == 2
    assert "self.click_handler.extra_own_windows_fn = self._orb_window_handles" in \
        inspect.getsource(Api.__init__)


# -- the pages ---------------------------------------------------------------------------------

def test_the_settings_switch_starts_off_and_its_loader_is_guarded():
    assert '<span>Floating orb</span><div class="sw" id="sw-orb"' in HTML
    assert 'id="sw-orb" class' not in HTML and "sw on\" id=\"sw-orb\"" not in HTML
    js = _script()
    assert "if(typeof pywebview.api.get_floating_orb!=='function')return;" in js
    assert "runStep('orb',loadOrbSetting);" in js


def test_the_orb_page_reuses_the_orb_css_and_loads_nothing_external():
    block = HTML[HTML.index("/* orb:start"):HTML.index("/* orb:end */")]
    assert block in ORB_HTML
    assert not re.search(r"https?:|@import|url\(|<link|src=", ORB_HTML)
    calls = set(re.findall(r"pywebview\.api\.(\w+)", ORB_HTML))
    assert calls == {"orb_state"}
    assert "prefers-reduced-motion" in ORB_HTML


@needs_node
def test_the_orb_page_follows_the_bridge(tmp_path):
    script = ORB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    js = r"""
const realSetTimeout = setTimeout;
const states = ['thinking', 'bogus', 'speaking'];
const wrap = {dataset: {}};
globalThis.document = {getElementById: () => wrap};
const listeners = {};
globalThis.window = {addEventListener: (t, fn) => { listeners[t] = fn; }};
globalThis.setInterval = () => 1;
globalThis.pywebview = {api: {orb_state: () => Promise.resolve({state: states.shift()})}};
""" + script + r"""
(async () => {
  const seen = [];
  listeners['pywebviewready']();
  await new Promise(r => realSetTimeout(r, 20)); seen.push(wrap.dataset.state);
  console.log(JSON.stringify(seen));
})();
"""
    f = tmp_path / "orb_page.js"
    f.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["thinking"]


_TOGGLE = r"""
const settle = () => new Promise(r => realSetTimeout(r, 30));
const orbCalls = [];
pywebview.api.get_floating_orb = () => Promise.resolve(false);
pywebview.api.set_floating_orb = (on) => { orbCalls.push(on);
  return Promise.resolve(on ? {success: true, enabled: true} : {success: true, enabled: false}); };
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const sw = document.getElementById('sw-orb');
  const seen = [sw.classList.contains('on')];
  await sw.onclick(); seen.push(sw.classList.contains('on'));
  await sw.onclick(); seen.push(sw.classList.contains('on'));
  console.log(JSON.stringify({seen, orbCalls, errors: consoleErrors}));
})();
"""


@needs_node
def test_the_switch_asks_python_and_shows_what_python_saved(tmp_path):
    options, set_results, set_gender = _real_outputs()
    cfg = {"options": options, "gender": "female", "set_model": set_results,
           "set_gender": set_gender, "flags": {"is_dev": False}}
    prefix = _HARNESS.split("%PAGE%", 1)[0]
    f = tmp_path / "toggle.js"
    f.write_text(prefix.replace("%CONFIG%", json.dumps(cfg)) + _script() + _TOGGLE,
                 encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res == {"seen": [False, True, False], "orbCalls": [True, False], "errors": []}
