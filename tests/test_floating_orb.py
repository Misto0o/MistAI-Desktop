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


# -- geometry: placement and region math (physical px) -------------------------------

@pytest.mark.parametrize("work, scale, want", [
    # 1920x1080 with a 48 px (logical) taskbar at each scale; side = 112*s, gap = 16*s.
    ((0, 0, 1920, 1032), 1.0, (1792, 904, 112)),
    ((0, 0, 1920, 1020), 1.25, (1760, 860, 140)),
    ((0, 0, 1920, 1008), 1.5, (1728, 816, 168)),
])
def test_place_at_100_125_and_150_percent(work, scale, want):
    x, y, side = fo.place(work, scale)
    assert (x, y, side) == want
    l, t, r, b = work
    assert l <= x and t <= y and x + side <= r and y + side <= b        # fully inside
    assert r - (x + side) == round(16 * scale) == b - (y + side)        # same margin


def test_place_follows_a_taskbar_on_the_left_or_top():
    # The work area does not start at 0,0: the orb still sits bottom-right inside it.
    assert fo.place((60, 0, 1920, 1080), 1.25) == (1760, 920, 140)
    assert fo.place((0, 48, 1920, 1080), 1.0) == (1792, 952, 112)


def test_place_shrinks_and_clamps_in_a_small_work_area():
    x, y, side = fo.place((0, 0, 150, 100), 1.5)          # smaller than 168 + 24
    assert side == 100 and (x, y) == (50, 0)               # as big as fits, margin dropped
    assert x + side <= 150 and y + side <= 100
    x, y, side = fo.place((100, 100, 220, 220), 1.0)       # room for the orb, less margin
    assert side == 112 and (x, y) == (100, 100)
    assert x + side == 212 and 220 - (x + side) == 8       # margin shrunk to what is left
    assert fo.place((0, 0, 0, 0), 1.0) == (0, 0, 1)        # degenerate: never negative


def test_place_ignores_a_bad_scale():
    assert fo.place((0, 0, 1920, 1032), 0) == fo.place((0, 0, 1920, 1032), 1.0)
    assert fo.place((0, 0, 1920, 1032), None) == fo.place((0, 0, 1920, 1032), 1.0)


@pytest.mark.parametrize("w, h", [(112, 112), (140, 140), (168, 168)])
def test_the_region_box_is_the_whole_window_right_bottom_exclusive(w, h):
    assert fo.ellipse_box(w, h) == (0, 0, w + 1, h + 1)


@pytest.mark.parametrize("work, scale", [((0, 0, 1536, 816), 1.0),       # DPI-unaware view
                                         ((0, 0, 1920, 1020), 1.25)])    # DPI-aware view
def test_the_create_time_estimate_lands_where_harden_places_it(work, scale):
    # pywebview multiplies create-time x/y by the display scale (1.25 here).
    win = _Win(work=work, scale=scale)
    x, y = fo._corner_position(win=win)
    px, py, _ = fo.place((0, 0, 1920, 1020), 1.25)
    assert (round(x * 1.25), round(y * 1.25)) == (px, py)


def test_the_create_time_estimate_survives_errors():
    assert fo._corner_position(win=SimpleNamespace()) == (None, None)


# -- hardening (a fake Win32 adapter) --------------------------------------------------

class _Win:
    def __init__(self, work=(0, 0, 1920, 1020), scale=1.25, exclude_ok=True, move_ok=True,
                 region=0xBEEF, region_ok=True, rect=None, error=87):
        self.work, self.scale, self.exclude_ok, self.move_ok = work, scale, exclude_ok, move_ok
        self.region, self.region_ok, self.error = region, region_ok, error
        self.rect = rect
        self.calls = []

    def last_error(self):
        return self.error

    def set_affinity(self, hwnd, mode):
        self.calls.append(("affinity", mode))
        return (mode == fo.WDA_EXCLUDEFROMCAPTURE and self.exclude_ok) or mode == fo.WDA_MONITOR

    def add_exstyle(self, hwnd, bits):
        self.calls.append(("exstyle", bits))

    def primary_work_area(self):
        return "mon", self.work

    def monitor_scale(self, monitor, hwnd=None):
        return self.scale

    def move(self, hwnd, x, y, w, h):
        self.calls.append(("move", (x, y, w, h)))
        if self.move_ok and self.rect is None:
            self.rect = (x, y, x + w, y + h)
        return self.move_ok

    def window_rect(self, hwnd):
        return self.rect

    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return self.region

    def set_region(self, hwnd, region):
        self.calls.append(("region", region))
        return self.region_ok

    def delete_object(self, handle):
        self.calls.append(("delete", handle))


@pytest.mark.parametrize("scale, rect, side", [
    (1.0, (1792, 904, 1904, 1016), 112),
    (1.25, (1760, 860, 1900, 1000), 140),
    (1.5, (1728, 816, 1896, 984), 168),
])
def test_harden_places_it_in_the_work_area_and_cuts_a_physical_circle(scale, rect, side,
                                                                        capsys):
    work = (0, 0, 1920, 1080 - round(48 * scale))
    win = _Win(work=work, scale=scale)
    out = fo.harden(7, win=win)
    assert out == {"capture": "excluded", "noactivate": True, "round": True,
                   "position": (rect[0], rect[1], side, side, scale)}
    assert ("move", (rect[0], rect[1], side, side)) in win.calls
    assert ("ellipse", (0, 0, side + 1, side + 1)) in win.calls       # physical, not logical
    assert ("exstyle", fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE) in win.calls
    log = capsys.readouterr().out
    assert f"[FloatingOrb] at {rect[0]},{rect[1]} size {side},{side} monitor-scale {scale:.2f}" \
        in log
    assert "failed" not in log


def test_the_region_uses_the_real_rect_when_the_move_did_not_apply(capsys):
    win = _Win(move_ok=False, rect=(10, 20, 150, 160))
    out = fo.harden(7, win=win)
    assert ("ellipse", (0, 0, 141, 141)) in win.calls and out["round"] is True
    assert out["position"] == (10, 20, 140, 140, 1.25)
    assert "[FloatingOrb] SetWindowPos failed (error 87)" in capsys.readouterr().out


def test_a_failed_set_region_logs_the_error_code_and_frees_the_region(capsys):
    win = _Win(region_ok=False, error=1400)
    out = fo.harden(7, win=win)
    assert out["round"] is False
    assert ("delete", 0xBEEF) in win.calls
    log = capsys.readouterr().out
    assert "[FloatingOrb] SetWindowRgn failed (error 1400)" in log


def test_a_failed_create_region_logs_the_error_code(capsys):
    win = _Win(region=0, error=8)
    assert fo.harden(7, win=win)["round"] is False
    assert not any(c[0] == "region" for c in win.calls)
    assert "[FloatingOrb] CreateEllipticRgn failed (error 8)" in capsys.readouterr().out


def test_a_lost_window_rect_logs_and_stops(capsys):
    win = _Win(move_ok=False, error=1400)
    out = fo.harden(7, win=win)
    assert out["round"] is False and out["position"] is None
    assert "[FloatingOrb] GetWindowRect failed (error 1400)" in capsys.readouterr().out


def test_harden_falls_back_to_a_black_box_on_older_windows():
    win = _Win(exclude_ok=False)
    assert fo.harden(7, win=win)["capture"] == "black"
    assert [m for c, m in win.calls if c == "affinity"] == [fo.WDA_EXCLUDEFROMCAPTURE,
                                                            fo.WDA_MONITOR]


def test_harden_never_raises_and_logs_only_names_and_numbers(capsys):
    out = fo.harden(7, win=SimpleNamespace(last_error=lambda: 5))   # no other methods
    assert out == {"capture": "none", "noactivate": False, "round": False, "position": None}
    log = capsys.readouterr().out
    for line in log.splitlines():
        assert re.fullmatch(r"\[FloatingOrb\] [A-Za-z ]+(error|failed) \(?[A-Za-z0-9 ]*\)?",
                            line), line


def test_the_real_adapter_binds_the_region_calls_through_ctypes():
    # pywin32's win32gui has no CreateEllipticRgn (the round:False bug); the
    # adapter uses gdi32 directly. A region object only: no window, no screen.
    win = fo.Win32()
    region = win.create_ellipse(fo.ellipse_box(140, 140))
    assert region
    win.delete_object(region)
    assert "CreateEllipticRgn" not in inspect.getsource(fo.harden).replace(
        '"CreateEllipticRgn"', "")


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
