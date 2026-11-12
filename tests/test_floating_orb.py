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


def test_the_bridge_exposes_only_the_state_and_talk():
    bridge = fo.OrbBridge(_state_api(busy=True))
    public = [n for n in dir(bridge) if not n.startswith("_")]
    assert public == ["orb_state", "talk"]
    assert bridge.orb_state() == {"state": "thinking"}


# -- geometry: placement and region math (physical px) -------------------------------

@pytest.mark.parametrize("work, scale, want", [
    # 1920x1080 with a 48 px (logical) taskbar at each scale; side = 110*s, gap = 16*s.
    ((0, 0, 1920, 1032), 1.0, (1794, 906, 110)),
    ((0, 0, 1920, 1020), 1.25, (1762, 862, 138)),
    ((0, 0, 1920, 1008), 1.5, (1731, 819, 165)),
])
def test_place_at_100_125_and_150_percent(work, scale, want):
    x, y, side = fo.place(work, scale)
    assert (x, y, side) == want
    l, t, r, b = work
    assert l <= x and t <= y and x + side <= r and y + side <= b        # fully inside
    assert r - (x + side) == round(16 * scale) == b - (y + side)        # same margin


def test_place_follows_a_taskbar_on_the_left_or_top():
    # The work area does not start at 0,0: the orb still sits bottom-right inside it.
    assert fo.place((60, 0, 1920, 1080), 1.25) == (1762, 922, 138)
    assert fo.place((0, 48, 1920, 1080), 1.0) == (1794, 954, 110)


def test_place_shrinks_and_clamps_in_a_small_work_area():
    x, y, side = fo.place((0, 0, 150, 100), 1.5)          # smaller than 168 + 24
    assert side == 100 and (x, y) == (50, 0)               # as big as fits, margin dropped
    assert x + side <= 150 and y + side <= 100
    x, y, side = fo.place((100, 100, 220, 220), 1.0)       # room for the orb, less margin
    assert side == 110 and (x, y) == (100, 100)
    assert x + side == 210 and 220 - (x + side) == 10      # margin shrunk to what is left
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
    (1.0, (1794, 906, 1904, 1016), 110),
    (1.25, (1762, 862, 1900, 1000), 138),
    (1.5, (1731, 819, 1896, 984), 165),
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


def test_the_orb_page_has_the_flat_tint_and_loads_nothing_external():
    from core.config import ORB_TINT
    for value in ORB_TINT.values():
        assert value in ORB_HTML
    assert "58%" in ORB_HTML and "inset 0" not in ORB_HTML     # flat: no inner bevel
    assert not re.search(r"https?:|@import|url\(|<link|src=", ORB_HTML)
    calls = set(re.findall(r"pywebview\.api\.(\w+)", ORB_HTML))
    assert calls == {"orb_state", "talk"}
    assert "prefers-reduced-motion" in ORB_HTML


@needs_node
def test_the_orb_page_follows_the_bridge(tmp_path):
    script = ORB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    js = r"""
const realSetTimeout = setTimeout;
const states = ['thinking', 'bogus', 'speaking'];
const wrap = {dataset: {}};
globalThis.document = {getElementById: () => wrap, addEventListener: () => {}};
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


# -- sizes, resize about the center, tween (Stage 1) -----------------------------------

@pytest.mark.parametrize("state, docked, want", [
    ("idle", False, 110), ("listening", False, 110), ("thinking", False, 94),
    ("speaking", False, 94), ("thinking", True, 36), ("idle", True, 36),
])
def test_each_state_has_its_size(state, docked, want):
    assert fo.size_for(state, docked) == want


@pytest.mark.parametrize("scale, big, medium, small", [
    (1.0, 110, 94, 36), (1.25, 138, 118, 45), (1.5, 165, 141, 54),
])
def test_resize_keeps_the_center_and_cuts_a_physical_circle(scale, big, medium, small):
    from core.orb_layout import resize_about_center
    x, y, side = fo.place((0, 0, 1920, 1080 - round(48 * scale)), scale)
    assert side == big
    for logical, want in ((94, medium), (36, small), (110, big)):
        nx, ny, ns = resize_about_center(x, y, side, round(logical * scale))
        assert ns == want
        assert abs((nx + ns / 2) - (x + side / 2)) <= 0.5      # same center (rounding)
        assert abs((ny + ns / 2) - (y + side / 2)) <= 0.5
        win = _Win()
        assert fo.apply_square(win, 7, nx, ny, ns) is True
        assert ("move", (nx, ny, ns, ns)) in win.calls
        assert ("ellipse", (0, 0, ns + 1, ns + 1)) in win.calls


def test_apply_square_frees_a_region_that_was_not_taken():
    win = _Win(region_ok=False)
    assert fo.apply_square(win, 7, 0, 0, 40) is False
    assert ("delete", 0xBEEF) in win.calls
    assert fo.apply_square(SimpleNamespace(), 7, 0, 0, 40) is False      # never raises


def test_the_tween_plays_every_frame_and_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(fo.Tween, "FRAME_S", 0)
    win, seen, done = _Win(), [], []
    tw = fo.Tween(win, 7)
    tw.play([(0, 0, 10), (1, 1, 12), (2, 2, 14)], on_frame=seen.append,
            done=lambda: done.append(1)).join(2)
    assert seen == [(0, 0, 10), (1, 1, 12), (2, 2, 14)] and done == [1]
    gate = threading.Event()
    slow = fo.Tween(win, 7)
    seen.clear()
    t = slow.play([(0, 0, 10)] * 50, on_frame=lambda sq: (seen.append(sq), gate.wait(1)))
    slow.cancel()
    gate.set()
    t.join(2)
    assert len(seen) < 50 and not slow.running


def test_tick_resizes_toward_the_state_size(monkeypatch):
    played = []
    api = _state_api(busy=True)
    orb = fo.FloatingOrb(api, webview=_Webview(), html="<p>")
    orb.visible, orb.square, orb.scale = True, (1762, 862, 138), 1.25
    orb._tween = SimpleNamespace(running=False,
                                 play=lambda sq, on_frame=None, done=None: played.append(sq))
    orb.tick()
    (squares,) = played
    assert squares[-1] == (1772, 872, 118)                    # medium, same center
    orb.square = squares[-1]
    played.clear()
    orb.tick()
    assert played == []                                       # already there


# -- flight (Stage 2: "Orb follows the task") ---------------------------------------------

class _FlightWin(_Win):
    def __init__(self, target=(100, 100, 1000, 800), fg=55, **kw):
        super().__init__(**kw)
        self.target, self.fg, self.styles = target, fg, []

    def foreground(self):
        return self.fg

    def window_rect(self, hwnd):
        return self.target if hwnd == self.fg else self.rect

    def monitor_for_rect(self, rect):
        return (0, 0, 1920, 1040), 1.25

    def set_click_through(self, hwnd, on):
        self.styles.append(on)


def _flight_orb(flight=True, busy=True, speaking=False, app="notepad", **kw):
    api = _state_api(busy=busy, speaking=speaking)
    api._last_opened_app = app
    api._window_belongs_to = lambda hwnd, a: hwnd == 55 and a == "notepad"
    api.orb_option = lambda name: flight and name == "orb_flight"
    orb = fo.FloatingOrb(api, webview=_Webview(), html="<p>")
    orb.visible, orb.hwnd, orb.scale = True, 4242, 1.25
    orb.square = orb.home = (1762, 862, 138)
    orb._win = _FlightWin(**kw)
    orb._win.rect = (1762, 862, 1900, 1000)
    played = []
    orb._tween = SimpleNamespace(running=False, play=lambda sq, on_frame=None,
                                 done=None: played.append(sq))
    return orb, played


def test_a_task_with_a_target_window_docks_small_beside_it(capsys):
    orb, played = _flight_orb()
    orb.tick()
    (path,) = played
    assert path[-1] == (1008, 108, 45)                     # right of the target, 36 * 1.25
    assert len(path) == orb.FLIGHT_FRAMES
    straight_mid_y = (862 + 108) / 2
    assert path[len(path) // 2][1] < straight_mid_y - 50   # bowed upward
    assert orb.dock == "right"
    assert orb._win.styles == [True]                        # click-through while acting
    log = capsys.readouterr().out
    assert "[FloatingOrb] dock right size 45" in log and "notepad" not in log


def test_a_task_without_a_target_goes_medium_at_home():
    orb, played = _flight_orb(app=None)
    orb.tick()
    assert played[0][-1] == (1772, 872, 118) and orb.dock is None


def test_a_target_not_in_front_or_the_orb_itself_is_not_a_target():
    orb, played = _flight_orb(fg=99)                         # another window in front
    assert orb._target() is None
    orb, played = _flight_orb(fg=4242)                       # the orb itself
    orb.hwnd = 4242
    assert orb._target() is None


def test_after_the_task_it_speaks_medium_then_flies_home_big():
    orb, played = _flight_orb(busy=False, speaking=True)
    orb.square = (1008, 108, 45)
    orb._win.rect = (1008, 108, 1053, 153)
    orb.click_through = True
    orb.tick()
    assert orb._win.styles == [False]                        # restored after acting
    assert played[-1][-1] == resize_about_center_(1008, 108, 45, 118)
    orb2, played2 = _flight_orb(busy=False)
    orb2.square = (1008, 108, 45)
    orb2._win.rect = (1008, 108, 1053, 153)
    orb2.tick()
    assert played2[-1][-1] == (1762, 862, 138)              # home, big


def resize_about_center_(x, y, s, n):
    from core.orb_layout import resize_about_center
    return resize_about_center(x, y, s, n)


def test_flight_off_never_docks_or_goes_click_through():
    orb, played = _flight_orb(flight=False)
    orb.tick()
    assert played[0][-1] == (1772, 872, 118) and orb.dock is None
    assert orb._win.styles == []


def test_a_running_tween_is_not_interrupted_but_click_through_still_syncs():
    orb, played = _flight_orb()
    orb._tween.running = True
    orb.tick()
    assert played == [] and orb._win.styles == [True]


def test_dragging_the_orb_at_home_moves_home():
    orb, played = _flight_orb(busy=False)
    orb._win.rect = (500, 500, 638, 638)
    orb.tick()
    assert orb.home == (500, 500, 138) and played == []


# -- the options (Settings switches; env flags override) ---------------------------------

@pytest.mark.parametrize("name, env", [("orb_flight", "MISTAI_ORB_FLIGHT"),
                                       ("orb_panels", "MISTAI_ORB_PANELS")])
def test_orb_options_are_off_by_default_saved_and_env_overridable(monkeypatch, name, env):
    monkeypatch.delenv(env, raising=False)
    api = _api()
    assert api.orb_option(name) is False
    assert api.set_orb_option(name, True) == {"success": True, "enabled": True}
    assert api.memory.rows[name] == "on" and api.orb_option(name) is True
    monkeypatch.setenv(env, "0")
    assert api.orb_option(name) is False
    monkeypatch.setenv(env, "1")
    api.set_orb_option(name, False)
    assert api.orb_option(name) is True
    assert api.set_orb_option("floating_orb", True)["success"] is False


def test_the_option_switches_start_off():
    for name, label in (("orb_flight", "Orb follows the task"), ("orb_panels", "Task panels")):
        assert f'<span>{label}</span><div class="sw" id="sw-{name}"' in HTML
    assert "if(typeof pywebview.api.get_orb_options!=='function')return;" in _script()


# -- hold the orb to talk (Stage 4) -----------------------------------------------------

def _talk_api(listening=False, result=None):
    calls = []
    api = SimpleNamespace(_ptt_wants_mic=lambda: listening,
                          push_to_talk=lambda: calls.append(1) or (result or {"success": True}))
    return api, calls


@pytest.mark.parametrize("kind, listening, pressed", [
    ("start", False, 1), ("start", True, 0), ("stop", True, 1), ("stop", False, 0),
    ("toggle", False, 1), ("toggle", True, 1),
])
def test_talk_maps_hold_and_tap_onto_push_to_talk(kind, listening, pressed):
    api, calls = _talk_api(listening)
    assert fo.OrbBridge(api).talk(kind)["success"] is True
    assert len(calls) == pressed


def test_talk_refuses_anything_else_and_passes_refusals_through(capsys):
    api, calls = _talk_api()
    for bad in ("ask_mistai", "", None, "START", {"x": 1}):
        assert fo.OrbBridge(api).talk(bad) == {"success": False}
    assert calls == []
    busy, _ = _talk_api(result={"success": False, "message": "Mist is working."})
    assert fo.OrbBridge(busy).talk("toggle") == {"success": False,
                                                 "message": "Mist is working."}
    broken = SimpleNamespace(_ptt_wants_mic=lambda: 1 / 0)
    assert fo.OrbBridge(broken).talk("toggle") == {"success": False}
    assert capsys.readouterr().out.strip() == "[FloatingOrb] talk failed (ZeroDivisionError)"


def test_a_click_answers_mouse_activate_with_no_activate():
    assert fo.mouse_activate_answer(fo.WM_MOUSEACTIVATE) == fo.MA_NOACTIVATE == 3
    assert fo.mouse_activate_answer(0x0201) is None              # WM_LBUTTONDOWN passes on


def test_the_guard_never_raises_on_a_bad_window():
    assert fo.guard_mouse_activate(0) is False
    assert fo.guard_mouse_activate(0, run_on_ui=lambda fn: fn()) is False


def test_shown_verifies_no_activate_and_logs_labels_only(monkeypatch, capsys):
    monkeypatch.setattr(fo, "harden", lambda h: {"capture": "excluded", "noactivate": True,
                                                 "round": True})
    monkeypatch.setattr(fo, "Win32", lambda: SimpleNamespace(
        exstyle=lambda h: fo.WS_EX_NOACTIVATE | fo.WS_EX_TOOLWINDOW))
    monkeypatch.setattr(fo, "guard_mouse_activate", lambda h, run: h == 4242)
    wv = _Webview()
    orb = fo.FloatingOrb(_state_api(), webview=wv, html="<p>")
    orb.show()
    for fn in wv.created[0].events.shown:
        fn()
    assert ("[FloatingOrb] no-activate verified: True | mouse-activate guard: on"
            in capsys.readouterr().out)


_POINTER = r"""
let now = 0, timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({at: now + ms, fn}); return timers.length; };
globalThis.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
const advance = (ms) => { now += ms; for (const t of timers) if (t.fn && t.at <= now) { const f = t.fn; t.fn = null; f(); } };
globalThis.setInterval = () => 1;
const wrap = {dataset: {}}, on = {}, listeners = {}, talked = [];
globalThis.document = {getElementById: () => wrap, addEventListener: (t, fn) => { on[t] = fn; }};
globalThis.window = {addEventListener: (t, fn) => { listeners[t] = fn; }};
globalThis.pywebview = {api: {orb_state: () => Promise.resolve({state: 'idle'}),
                              talk: (k) => { talked.push(k); return Promise.resolve({}); }}};
%SCRIPT%
listeners['pywebviewready']();
const P = (x, y) => ({button: 0, screenX: x, screenY: y});
const out = {};
on.pointerdown(P(10, 10)); advance(100); on.pointerup(P(10, 10)); out.tap = talked.splice(0);
on.pointerdown(P(10, 10)); advance(400); on.pointerup(P(10, 10)); out.hold = talked.splice(0);
on.pointerdown(P(10, 10)); on.pointermove(P(30, 12)); advance(400); on.pointerup(P(30, 12));
out.drag = talked.splice(0);
on.pointerdown(P(10, 10)); on.pointermove(P(13, 12)); on.pointerup(P(13, 12));
out.jitter = talked.splice(0);
on.pointerdown({button: 2, screenX: 0, screenY: 0}); advance(400); on.pointerup(P(0, 0));
out.right = talked.splice(0);
console.log(JSON.stringify(out));
"""


@needs_node
def test_the_orb_page_tells_hold_tap_and_drag_apart(tmp_path):
    script = ORB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    f = tmp_path / "pointer.js"
    f.write_text(_POINTER.replace("%SCRIPT%", script), encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"tap": ["toggle"], "hold": ["start", "stop"],
                                      "drag": [], "jitter": ["toggle"], "right": []}
