# ============================================================
# ORB TESTS
# ============================================================
#
# The floating orb: window, states, layout geometry, motion and panels.
#
# SECTION INDEX
#   1. FLOATING ORB WINDOW
#   2. FLOATING ORB WINDOW HELPERS
#   3. FLOATING ORB WINDOW STATE API
#   4. FLOATING ORB WINDOW WIN
#   5. FLOATING ORB WINDOW EVENTS
#   6. FLOATING ORB WINDOW HOOK
#   7. FLOATING ORB WINDOW WEBVIEW
#   8. FLOATING ORB WINDOW MEMORY
#   9. FLOATING ORB WINDOW FAKE ORB
#   10. FLOATING ORB WINDOW API
#   11. FLOATING ORB WINDOW HELPERS (2)
#   12. FLOATING ORB WINDOW STEP CLOCK
#   13. FLOATING ORB WINDOW FLIGHT WIN
#   14. FLOATING ORB WINDOW FLIGHT ORB
#   15. FLOATING ORB WINDOW HELPERS (3)
#   16. FLOATING ORB WINDOW TESTS
#   17. ORB FIXES
#   18. ORB FIXES CLAMP WIN
#   19. ORB FIXES HOOK
#   20. ORB FIXES WEBVIEW
#   21. ORB FIXES HELPERS
#   22. ORB FIXES PANEL WIN
#   23. ORB FIXES COUNT WIN
#   24. ORB FIXES HELPERS (2)
#   25. ORB FIXES FLIGHT WIN
#   26. ORB FIXES ORB
#   27. ORB FIXES HELPERS (3)
#   28. ORB FIXES SEQ WIN
#   29. ORB FIXES TASK
#   30. ORB FIXES HELPERS (4)
#   31. ORB FIXES TOUR BROWSER
#   32. ORB FIXES TOUR ORB
#   33. ORB FIXES HELPERS (5)
#   34. ORB FIXES ANSWER ORB
#   35. ORB FIXES TESTS
#   36. ORB LAYOUT
#   37. ORB LAYOUT HELPERS
#   38. ORB LAYOUT TESTS
#   39. ORB MOTION
#   40. ORB MOTION HELPERS
#   41. ORB MOTION FLY
#   42. ORB MOTION CLOCK
#   43. ORB MOTION PACER
#   44. ORB MOTION MOVE WIN
#   45. ORB MOTION PLAY
#   46. ORB MOTION FLYING ORB
#   47. ORB MOTION HELPERS (2)
#   48. ORB MOTION TOUR ORB
#   49. ORB MOTION HELPERS (3)
#   50. ORB MOTION TESTS
#   51. ORB PANELS
#   52. ORB PANELS CLOCK
#   53. ORB PANELS HELPERS
#   54. ORB PANELS TESTS
#   55. ORB PANELS WIN
#   56. ORB PANELS TESTS (2)
#   57. ORB PANELS HOOK
#   58. ORB PANELS WEBVIEW
#   59. ORB PANELS BUS
#   60. ORB PANELS HELPERS (2)
#   61. ORB PANELS TESTS (3)
#   62. ORB STATES
#   63. ORB STATES HELPERS
#   64. ORB STATES TESTS
#   65. ORB STATES HELPERS (2)
#   66. ORB STATES TESTS (2)
#   67. ORB NINE STATES
#   68. ORB NINE STATES HELPERS
#   69. ORB NINE STATES TESTS
# ============================================================

"""The floating orb: window, states, layout geometry, motion and panels."""

import inspect
import json
import os
import re
import shutil
import subprocess
import threading
from types import SimpleNamespace
import sys
import time
import math

import numpy as np
import pytest
from ui.html import HTML, ORB_HTML, PANEL_HTML

import core.click_handler as ch
import core.enhanced_utils as eu
import core.floating_orb as fo
import core.floating_orb as op
from core.api import Api
from core.config import is_self_process
from test_api import _HARNESS, _real_outputs, _script
from core import (
    config,
    live_state as events,
    floating_orb as ol,
    floating_orb as fo,
    floating_orb as om,
)
from core.agent import AgentLoop
from core.floating_orb import overlaps


# ============================================================
# FLOATING ORB WINDOW
# ============================================================

# The floating orb (core/floating_orb.py; Settings > Floating orb, off by
# default): a small frameless always-on-top window that shows only the orb.
# It is excluded from capture and from the OCR search, never activates, has no
# title, talks to Python only through OrbBridge.orb_state(), and is never an
# input target. Everything here runs on fakes: no window is created.


# ============================================================
# FLOATING ORB WINDOW HELPERS
# ============================================================

floating_orb_window_needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


# ============================================================
# FLOATING ORB WINDOW STATE API
# ============================================================

def _state_api(ptt=False, speaking=False, busy=False):
    lock = threading.Lock()
    if busy:
        lock.acquire()
    return SimpleNamespace(_ptt_wants_mic=lambda: ptt, tts=SimpleNamespace(speaking=speaking),
                           _agent_lock=lock)


# -- state and bridge ----------------------------------------------------------------


# -- geometry: placement and region math (physical px) -------------------------------


# -- hardening (a fake Win32 adapter) --------------------------------------------------


# ============================================================
# FLOATING ORB WINDOW WIN
# ============================================================

class _FloatingOrbWindowWin:
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

    style = 0

    def add_exstyle(self, hwnd, bits):
        self.calls.append(("exstyle", bits))

    def set_exstyle(self, hwnd, add=0, remove=0):
        self.calls.append(("exstyle", add))
        self.style = (self.style | add) & ~remove

    def exstyle(self, hwnd):
        return self.style

    def show(self, hwnd, on):
        self.calls.append(("show", on))

    def move_only(self, hwnd, x, y):
        self.calls.append(("move_only", (x, y)))
        return True

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


# -- the window ------------------------------------------------------------------------


# ============================================================
# FLOATING ORB WINDOW EVENTS
# ============================================================

class _Events:
    def __init__(self):
        self.shown = _FloatingOrbWindowHook()


# ============================================================
# FLOATING ORB WINDOW HOOK
# ============================================================

class _FloatingOrbWindowHook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


# ============================================================
# FLOATING ORB WINDOW WEBVIEW
# ============================================================

class _FloatingOrbWindowWebview:
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


# -- the Api setting ----------------------------------------------------------------------


# ============================================================
# FLOATING ORB WINDOW MEMORY
# ============================================================

class _Memory:
    def __init__(self, **rows):
        self.rows = dict(rows)

    def get_profile(self, key, default=None):
        return self.rows.get(key, default)

    def set_profile(self, key, value):
        self.rows[key] = value


# ============================================================
# FLOATING ORB WINDOW FAKE ORB
# ============================================================

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


# ============================================================
# FLOATING ORB WINDOW API
# ============================================================

def _api(memory=None, orb=None):
    api = Api.__new__(Api)
    api.memory = memory or _Memory()
    if orb is not None:
        api._orb = orb
    return api


# -- never an input target -----------------------------------------------------------------


# -- the OCR mask covers it -------------------------------------------------------------------


# -- the pages ---------------------------------------------------------------------------------


# ============================================================
# FLOATING ORB WINDOW HELPERS (2)
# ============================================================

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


# -- sizes, resize about the center, tween (Stage 1) -----------------------------------


# ============================================================
# FLOATING ORB WINDOW STEP CLOCK
# ============================================================

class _StepClock:
    """Advances a fixed step each time it is read."""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


# -- flight (Stage 2: "Orb follows the task") ---------------------------------------------


# ============================================================
# FLOATING ORB WINDOW FLIGHT WIN
# ============================================================

class _FloatingOrbWindowFlightWin(_FloatingOrbWindowWin):
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

    def window_info(self, hwnd):
        return (1234, "notepad.exe", "Notepad")

    def browser_window(self):
        return None


# ============================================================
# FLOATING ORB WINDOW FLIGHT ORB
# ============================================================

def _flight_orb(flight=True, busy=True, speaking=False, app="notepad", **kw):
    api = _state_api(busy=busy, speaking=speaking)
    api._last_opened_app = app
    api._window_belongs_to = lambda hwnd, a: hwnd == 55 and a == "notepad"
    api.orb_option = lambda name: flight and name == "orb_flight"
    orb = fo.FloatingOrb(api, webview=_FloatingOrbWindowWebview(), html="<p>")
    orb.visible, orb.hwnd, orb.scale = True, 4242, 1.25
    orb.square = orb.home = (1762, 862, 138)
    orb._win = _FloatingOrbWindowFlightWin(**kw)
    orb._win.rect = (1762, 862, 1900, 1000)
    orb._acted, orb._was_busy = True, busy        # mid-task: a step has already run
    played = []
    orb._tween = SimpleNamespace(running=False, play=lambda sq, on_frame=None,
                                 done=None: played.append(sq))
    return orb, played


# ============================================================
# FLOATING ORB WINDOW HELPERS (3)
# ============================================================

def resize_about_center_(x, y, s, n):
    from core.floating_orb import resize_about_center
    return resize_about_center(x, y, s, n)


# -- the options (Settings switches; env flags override) ---------------------------------


# -- hold the orb to talk (Stage 4) -----------------------------------------------------

def _talk_api(listening=False, result=None):
    calls = []
    api = SimpleNamespace(_ptt_wants_mic=lambda: listening,
                          push_to_talk=lambda: calls.append(1) or (result or {"success": True}))
    return api, calls


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


# ============================================================
# FLOATING ORB WINDOW TESTS
# ============================================================

class TestFloatingOrbWindow:
    @pytest.fixture(autouse=True)
    def _pinned_sizes(self, monkeypatch):
        """The geometry below was worked out for 110/94/36 px; MISTAI_ORB_SIZES pins
    it (the 82/70/28 defaults are tested in test_orb_fixes.py)."""
        monkeypatch.setenv("MISTAI_ORB_SIZES", "110,94,36")
        monkeypatch.delenv("MISTAI_ORB_DEBUG_CAPTURE", raising=False)

    @pytest.mark.parametrize("kw, want", [
        ({}, "idle"), ({"busy": True}, "thinking"), ({"speaking": True}, "speaking"),
        ({"speaking": True, "busy": True}, "speaking"), ({"ptt": True, "busy": True}, "listening"),
        ({"ptt": True, "speaking": True}, "listening"),
    ])
    def test_orb_state_has_the_page_priorities(self, kw, want):
        assert fo.orb_state(_state_api(**kw)) == want

    def test_orb_state_survives_a_broken_api(self):
        assert fo.orb_state(SimpleNamespace()) == "idle"

    def test_the_bridge_exposes_only_the_state_and_talk(self):
        bridge = fo.OrbBridge(_state_api(busy=True))
        public = [n for n in dir(bridge) if not n.startswith("_")]
        assert public == ["orb_state", "talk"]
        assert bridge.orb_state() == {"state": "thinking"}

    @pytest.mark.parametrize("work, scale, want", [
        # 1920x1080 with a 48 px (logical) taskbar at each scale; side = 110*s, gap = 16*s.
        ((0, 0, 1920, 1032), 1.0, (1794, 906, 110)),
        ((0, 0, 1920, 1020), 1.25, (1762, 862, 138)),
        ((0, 0, 1920, 1008), 1.5, (1731, 819, 165)),
    ])
    def test_place_at_100_125_and_150_percent(self, work, scale, want):
        x, y, side = fo.place(work, scale)
        assert (x, y, side) == want
        l, t, r, b = work
        assert l <= x and t <= y and x + side <= r and y + side <= b        # fully inside
        assert r - (x + side) == round(16 * scale) == b - (y + side)        # same margin

    def test_place_follows_a_taskbar_on_the_left_or_top(self):
        # The work area does not start at 0,0: the orb still sits bottom-right inside it.
        assert fo.place((60, 0, 1920, 1080), 1.25) == (1762, 922, 138)
        assert fo.place((0, 48, 1920, 1080), 1.0) == (1794, 954, 110)

    def test_place_shrinks_and_clamps_in_a_small_work_area(self):
        x, y, side = fo.place((0, 0, 150, 100), 1.5)          # smaller than 168 + 24
        assert side == 100 and (x, y) == (50, 0)               # as big as fits, margin dropped
        assert x + side <= 150 and y + side <= 100
        x, y, side = fo.place((100, 100, 220, 220), 1.0)       # room for the orb, less margin
        assert side == 110 and (x, y) == (100, 100)
        assert x + side == 210 and 220 - (x + side) == 10      # margin shrunk to what is left
        assert fo.place((0, 0, 0, 0), 1.0) == (0, 0, 1)        # degenerate: never negative

    def test_place_ignores_a_bad_scale(self):
        assert fo.place((0, 0, 1920, 1032), 0) == fo.place((0, 0, 1920, 1032), 1.0)
        assert fo.place((0, 0, 1920, 1032), None) == fo.place((0, 0, 1920, 1032), 1.0)

    @pytest.mark.parametrize("w, h", [(112, 112), (140, 140), (168, 168)])
    def test_the_region_box_is_the_whole_window_right_bottom_exclusive(self, w, h):
        assert fo.ellipse_box(w, h) == (0, 0, w + 1, h + 1)

    @pytest.mark.parametrize("work, scale", [((0, 0, 1536, 816), 1.0),       # DPI-unaware view
                                             ((0, 0, 1920, 1020), 1.25)])    # DPI-aware view
    def test_the_create_time_estimate_lands_where_harden_places_it(self, work, scale):
        # pywebview multiplies create-time x/y by the display scale (1.25 here).
        win = _FloatingOrbWindowWin(work=work, scale=scale)
        x, y = fo._corner_position(win=win)
        px, py, _ = fo.place((0, 0, 1920, 1020), 1.25)
        assert (round(x * 1.25), round(y * 1.25)) == (px, py)

    def test_the_create_time_estimate_survives_errors(self):
        assert fo._corner_position(win=SimpleNamespace()) == (None, None)

    @pytest.mark.parametrize("scale, rect, side", [
        (1.0, (1794, 906, 1904, 1016), 110),
        (1.25, (1762, 862, 1900, 1000), 138),
        (1.5, (1731, 819, 1896, 984), 165),
    ])
    def test_harden_places_it_in_the_work_area_and_cuts_a_physical_circle(self, scale, rect, side,
                                                                            capsys):
        work = (0, 0, 1920, 1080 - round(48 * scale))
        win = _FloatingOrbWindowWin(work=work, scale=scale)
        out = fo.harden(7, win=win)
        assert out == {"capture": "excluded", "noactivate": True, "round": True,
                       "position": (rect[0], rect[1], side, side, scale)}
        assert ("move", (rect[0], rect[1], side, side)) in win.calls
        assert ("ellipse", (0, 0, side + 1, side + 1)) in win.calls       # physical, not logical
        assert ("exstyle", fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE) in win.calls
        log = capsys.readouterr().out
        assert (f"[FloatingOrb] at {rect[0]},{rect[1]} requested {side},{side} actual "
                f"{side},{side} monitor-scale {scale:.2f}") in log
        assert "[FloatingOrb] taskbar: hidden" in log
        assert "failed" not in log

    def test_the_region_uses_the_real_rect_when_the_move_did_not_apply(self, capsys):
        win = _FloatingOrbWindowWin(move_ok=False, rect=(10, 20, 150, 160))
        out = fo.harden(7, win=win)
        assert ("ellipse", (0, 0, 141, 141)) in win.calls and out["round"] is True
        assert out["position"] == (10, 20, 140, 140, 1.25)
        assert "[FloatingOrb] SetWindowPos failed (error 87)" in capsys.readouterr().out

    def test_a_failed_set_region_logs_the_error_code_and_frees_the_region(self, capsys):
        win = _FloatingOrbWindowWin(region_ok=False, error=1400)
        out = fo.harden(7, win=win)
        assert out["round"] is False
        assert ("delete", 0xBEEF) in win.calls
        log = capsys.readouterr().out
        assert "[FloatingOrb] SetWindowRgn failed (error 1400)" in log

    def test_a_failed_create_region_logs_the_error_code(self, capsys):
        win = _FloatingOrbWindowWin(region=0, error=8)
        assert fo.harden(7, win=win)["round"] is False
        assert not any(c[0] == "region" for c in win.calls)
        assert "[FloatingOrb] CreateEllipticRgn failed (error 8)" in capsys.readouterr().out

    def test_a_lost_window_rect_logs_and_stops(self, capsys):
        win = _FloatingOrbWindowWin(move_ok=False, error=1400)
        out = fo.harden(7, win=win)
        assert out["round"] is False and out["position"] is None
        assert "[FloatingOrb] GetWindowRect failed (error 1400)" in capsys.readouterr().out

    def test_harden_falls_back_to_a_black_box_on_older_windows(self):
        win = _FloatingOrbWindowWin(exclude_ok=False)
        assert fo.harden(7, win=win)["capture"] == "black"
        assert [m for c, m in win.calls if c == "affinity"] == [fo.WDA_EXCLUDEFROMCAPTURE,
                                                                fo.WDA_MONITOR]

    def test_harden_never_raises_and_logs_only_names_and_numbers(self, capsys):
        out = fo.harden(7, win=SimpleNamespace(last_error=lambda: 5))   # no other methods
        assert out == {"capture": "none", "noactivate": False, "round": False, "position": None}
        log = capsys.readouterr().out
        for line in log.splitlines():
            assert re.fullmatch(r"\[FloatingOrb\] [A-Za-z ]+(error|failed) \(?[A-Za-z0-9 ]*\)?",
                                line), line

    def test_the_real_adapter_binds_the_region_calls_through_ctypes(self):
        # pywin32's win32gui has no CreateEllipticRgn (the round:False bug); the
        # adapter uses gdi32 directly. A region object only: no window, no screen.
        win = fo.Win32()
        region = win.create_ellipse(fo.ellipse_box(140, 140))
        assert region
        win.delete_object(region)
        assert "CreateEllipticRgn" not in inspect.getsource(fo.harden).replace(
            '"CreateEllipticRgn"', "")

    def test_the_window_is_small_frameless_on_top_untitled_and_unfocused(self, monkeypatch):
        monkeypatch.setattr(fo, "_corner_position", lambda size=None: (10, 20))
        wv = _FloatingOrbWindowWebview()
        api = _state_api()
        orb = fo.FloatingOrb(api, webview=wv)
        assert orb.show() is True
        (win,) = wv.created
        assert win.title == ""                                   # title lookups never find it
        kw = win.kw
        assert kw["html"] == ORB_HTML
        assert isinstance(kw["js_api"], fo.OrbBridge)            # never the full Api
        assert kw["js_api"] is not api
        assert (kw["width"], kw["height"], kw["x"], kw["y"]) == (110, 110, 10, 20)
        assert kw["min_size"] == (1, 1)                          # pywebview's (200, 100) clamps
        from core.config import orb_background
        assert kw["background_color"] == orb_background()
        assert kw["frameless"] and kw["on_top"] and not kw["focus"] and not kw["resizable"]
        assert "transparent" not in kw                           # opaque + round region instead

    def test_hardening_runs_when_the_window_is_shown(self, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(fo, "harden", lambda h: seen.append(h) or
                            {"capture": "excluded", "noactivate": True, "round": True})
        wv = _FloatingOrbWindowWebview()
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

    def test_a_window_that_cannot_open_reports_it(self, capsys):
        orb = fo.FloatingOrb(_state_api(), webview=_FloatingOrbWindowWebview(fail=True), html="<p>")
        assert orb.show() is False and orb.visible is False
        assert "could not show the orb (RuntimeError)" in capsys.readouterr().out

    def test_the_setting_is_off_by_default_and_nothing_is_created(self):
        api = _api()
        assert api.get_floating_orb() is False
        api.start_floating_orb_if_enabled()
        assert getattr(api, "_orb", None) is None
        assert api._orb_window_handles() == []

    def test_turning_it_on_and_off(self):
        orb = _FakeOrb()
        api = _api(orb=orb)
        assert api.set_floating_orb(True) == {"success": True, "enabled": True}
        assert api.memory.rows["floating_orb"] == "on" and api.get_floating_orb() is True
        assert api._orb_window_handles() == [99]
        assert api.set_floating_orb(False) == {"success": True, "enabled": False}
        assert api.memory.rows["floating_orb"] == "off" and orb.calls == ["show", "hide"]

    def test_a_window_that_cannot_open_turns_the_setting_back_off(self):
        api = _api(orb=_FakeOrb(ok=False))
        r = api.set_floating_orb(True)
        assert r["success"] is False and r["enabled"] is False and r["error"]
        assert api.memory.rows["floating_orb"] == "off"

    def test_startup_shows_it_only_when_saved_on(self):
        orb = _FakeOrb()
        _api(memory=_Memory(floating_orb="on"), orb=orb).start_floating_orb_if_enabled()
        assert orb.calls == ["show"]

    def test_input_is_refused_while_the_orb_is_in_front(self, monkeypatch):
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

    def test_the_orb_lives_in_mistais_own_process(self):
        # Open apps and process/window lookups skip MistAI's own PID (is_self_process).
        import psutil

        assert is_self_process(psutil.Process(os.getpid()).name(), os.getpid())

    def test_untitled_windows_are_left_out_of_window_lists(self, monkeypatch):
        wins = [SimpleNamespace(title=""), SimpleNamespace(title="Untitled - Notepad")]
        monkeypatch.setattr(eu, "WINDOW_MGMT_AVAILABLE", True)
        monkeypatch.setattr(eu, "gw", SimpleNamespace(getAllWindows=lambda: wins), raising=False)
        assert eu.WindowManager.list_all_windows() == [{"title": "Untitled - Notepad"}]
        assert eu.WindowManager.find_window_normalized("") is None

    def test_the_ocr_mask_blacks_out_the_orb(self, monkeypatch):
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

    def test_both_ocr_paths_apply_the_extra_mask_and_the_api_wires_it(self):
        src = inspect.getsource(ch.ImprovedClickHandler)
        assert src.count("self._mask_extra_own_windows(") == 2
        assert "self.click_handler.extra_own_windows_fn = self._orb_window_handles" in \
            inspect.getsource(Api.__init__)

    def test_the_settings_switch_starts_off_and_its_loader_is_guarded(self):
        assert '<span>Floating orb</span><div class="sw" id="sw-orb"' in HTML
        assert 'id="sw-orb" class' not in HTML and "sw on\" id=\"sw-orb\"" not in HTML
        js = _script()
        assert "if(typeof pywebview.api.get_floating_orb!=='function')return;" in js
        assert "runStep('orb',loadOrbSetting);" in js

    def test_the_orb_page_has_the_flat_tint_and_loads_nothing_external(self):
        from core.config import ORB_TINT
        for value in ORB_TINT.values():
            assert value in ORB_HTML
        assert "58%" in ORB_HTML and "inset 0" not in ORB_HTML     # flat: no inner bevel
        assert not re.search(r"https?:|@import|url\(|<link|src=", ORB_HTML)
        calls = set(re.findall(r"pywebview\.api\.(\w+)", ORB_HTML))
        assert calls == {"orb_state", "talk"}
        assert "prefers-reduced-motion" in ORB_HTML

    @floating_orb_window_needs_node
    def test_the_orb_page_follows_the_bridge(self, tmp_path):
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

    @floating_orb_window_needs_node
    def test_the_switch_asks_python_and_shows_what_python_saved(self, tmp_path):
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

    @pytest.mark.parametrize("state, docked, want", [
        ("idle", False, 110), ("listening", False, 110), ("thinking", False, 94),
        ("speaking", False, 94), ("thinking", True, 36), ("idle", True, 36),
    ])
    def test_each_state_has_its_size(self, state, docked, want):
        assert fo.size_for(state, docked) == want

    @pytest.mark.parametrize("scale, big, medium, small", [
        (1.0, 110, 94, 36), (1.25, 138, 118, 45), (1.5, 165, 141, 54),
    ])
    def test_resize_keeps_the_center_and_cuts_a_physical_circle(self, scale, big, medium, small):
        from core.floating_orb import resize_about_center
        x, y, side = fo.place((0, 0, 1920, 1080 - round(48 * scale)), scale)
        assert side == big
        for logical, want in ((94, medium), (36, small), (110, big)):
            nx, ny, ns = resize_about_center(x, y, side, round(logical * scale))
            assert ns == want
            assert abs((nx + ns / 2) - (x + side / 2)) <= 0.5      # same center (rounding)
            assert abs((ny + ns / 2) - (y + side / 2)) <= 0.5
            win = _FloatingOrbWindowWin()
            assert fo.apply_square(win, 7, nx, ny, ns) is True
            assert ("move", (nx, ny, ns, ns)) in win.calls
            assert ("ellipse", (0, 0, ns + 1, ns + 1)) in win.calls

    def test_apply_square_frees_a_region_that_was_not_taken(self):
        win = _FloatingOrbWindowWin(region_ok=False)
        assert fo.apply_square(win, 7, 0, 0, 40) is False
        assert ("delete", 0xBEEF) in win.calls
        assert fo.apply_square(SimpleNamespace(), 7, 0, 0, 40) is False      # never raises

    def test_the_tween_is_time_based_and_can_be_cancelled(self, monkeypatch):
        monkeypatch.setattr(fo.Tween, "FRAME_S", 0)
        win, seen, done = _FloatingOrbWindowWin(), [], []
        tw = fo.Tween(win, 7, clock=_StepClock(0.05))
        tw.play([((0, 0, 10), (0, 0, 20), 0.2, 0.0)], on_frame=seen.append,
                done=lambda: done.append(1)).join(2)
        assert seen[-1] == (0, 0, 20) and done == [1]
        assert 2 <= len(seen) <= 4                     # a resize step: ~0.2 s at 0.05 s per frame
        seen.clear()
        tw = fo.Tween(win, 7, clock=_StepClock(0.05))
        tw.play([((0, 0, 10), (100, 0, 10), 0.2, 0.0)], on_frame=seen.append,
                done=lambda: done.append(2)).join(2)
        assert seen[-1] == (100, 0, 10) and done == [1, 2]
        assert 3 <= len(seen) <= 14                    # a spring move: settled by ~0.6 s
        gate = threading.Event()
        slow = fo.Tween(win, 7, clock=_StepClock(0.001))
        seen.clear()
        t = slow.play([((0, 0, 10), (500, 0, 10), 10.0, 0.0)],
                      on_frame=lambda sq: (seen.append(sq), gate.wait(1)))
        slow.cancel()
        gate.set()
        t.join(2)
        assert len(seen) < 5 and not slow.running

    def test_tick_resizes_toward_the_state_size(self, monkeypatch):
        played = []
        api = _state_api(busy=True)
        orb = fo.FloatingOrb(api, webview=_FloatingOrbWindowWebview(), html="<p>")
        orb.visible, orb.square, orb.scale = True, (1762, 862, 138), 1.25
        orb._tween = SimpleNamespace(running=False,
                                     play=lambda sq, on_frame=None, done=None: played.append(sq))
        orb.tick()
        (segments,) = played
        assert segments[-1][1] == (1772, 872, 118)                # medium, same center
        orb.square = segments[-1][1]
        played.clear()
        orb.tick()
        assert played == []                                       # already there

    def test_a_task_with_a_target_window_docks_small_beside_it(self, capsys):
        orb, played = _flight_orb()
        orb.tick()
        (plan,) = played
        assert plan[-1][1] == (1008, 108, 45)                  # right of the target, 36 * 1.25
        assert plan[0][1][2] == 45 and plan[0][3] == 0.0        # shrinks first, in place
        fly = plan[-1]
        assert fly[0][2] == fly[1][2] == 45 and fly[3] > 0      # same size all flight, bowed
        from core.floating_orb import square_at
        straight_mid_y = (fly[0][1] + fly[1][1]) / 2
        assert square_at(fly[0], fly[1], 0.5, fly[3])[1] < straight_mid_y - 50
        assert orb.dock == "right"
        assert orb._win.styles == [True]                        # click-through while acting
        log = capsys.readouterr().out
        assert "[FloatingOrb] dock right size 45" in log and "notepad" not in log

    def test_a_task_without_a_target_goes_medium_at_home(self):
        orb, played = _flight_orb(app=None)
        orb._acted = False                                       # no step has run yet
        orb.tick()
        assert played[0][-1][1] == (1772, 872, 118) and orb.dock is None

    def test_a_target_not_in_front_or_the_orb_itself_is_not_a_target(self):
        orb, played = _flight_orb(fg=99)                         # the user's window, as before
        orb._pre_task_fg = 99
        assert orb._target() == (None, "unrelated-window")
        orb, played = _flight_orb(fg=4242)                       # the orb itself
        orb.hwnd = 4242
        assert orb._target() == (None, "no-window")

    def test_after_the_task_it_speaks_medium_then_flies_home_big(self):
        orb, played = _flight_orb(busy=False, speaking=True)
        orb.square = (1008, 108, 45)
        orb._win.rect = (1008, 108, 1053, 153)
        orb.click_through = True
        orb.tick()
        assert orb._win.styles == [False]                        # restored after acting
        assert played[-1][-1][1] == resize_about_center_(1008, 108, 45, 118)
        orb2, played2 = _flight_orb(busy=False)
        orb2.square = (1008, 108, 45)
        orb2._win.rect = (1008, 108, 1053, 153)
        orb2.tick()
        assert played2[-1][-1][1] == (1762, 862, 138)           # home, big

    def test_flight_off_never_docks_or_goes_click_through(self):
        orb, played = _flight_orb(flight=False)
        orb.tick()
        assert played[0][-1][1] == (1772, 872, 118) and orb.dock is None
        assert orb._win.styles == []

    def test_a_running_tween_is_not_interrupted_but_click_through_still_syncs(self):
        orb, played = _flight_orb()
        orb._tween.running = True
        orb.tick()
        assert played == [] and orb._win.styles == [True]

    def test_dragging_the_orb_at_home_moves_home(self):
        orb, played = _flight_orb(busy=False)
        orb._win.rect = (500, 500, 638, 638)
        orb.tick()
        assert orb.home == (500, 500, 138) and played == []

    @pytest.mark.parametrize("name, env", [("orb_flight", "MISTAI_ORB_FLIGHT"),
                                           ("orb_panels", "MISTAI_ORB_PANELS")])
    def test_orb_options_are_off_by_default_saved_and_env_overridable(self, monkeypatch, name, env):
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

    def test_the_option_switches_start_off(self):
        for name, label in (("orb_flight", "Orb follows the task"), ("orb_panels", "Task panels")):
            assert f'<span>{label}</span><div class="sw" id="sw-{name}"' in HTML
        assert "if(typeof pywebview.api.get_orb_options!=='function')return;" in _script()

    @pytest.mark.parametrize("kind, listening, pressed", [
        ("start", False, 1), ("start", True, 0), ("stop", True, 1), ("stop", False, 0),
        ("toggle", False, 1), ("toggle", True, 1),
    ])
    def test_talk_maps_hold_and_tap_onto_push_to_talk(self, kind, listening, pressed):
        api, calls = _talk_api(listening)
        assert fo.OrbBridge(api).talk(kind)["success"] is True
        assert len(calls) == pressed

    def test_talk_refuses_anything_else_and_passes_refusals_through(self, capsys):
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

    def test_a_click_answers_mouse_activate_with_no_activate(self):
        assert fo.mouse_activate_answer(fo.WM_MOUSEACTIVATE) == fo.MA_NOACTIVATE == 3
        assert fo.mouse_activate_answer(0x0201) is None              # WM_LBUTTONDOWN passes on

    def test_the_guard_never_raises_on_a_bad_window(self):
        assert fo.guard_mouse_activate(0) is False
        assert fo.guard_mouse_activate(0, run_on_ui=lambda fn: fn()) is False

    def test_shown_verifies_no_activate_and_logs_labels_only(self, monkeypatch, capsys):
        monkeypatch.setattr(fo, "harden", lambda h: {"capture": "excluded", "noactivate": True,
                                                     "round": True})
        monkeypatch.setattr(fo, "Win32", lambda: SimpleNamespace(
            exstyle=lambda h: fo.WS_EX_NOACTIVATE | fo.WS_EX_TOOLWINDOW))
        monkeypatch.setattr(fo, "guard_mouse_activate", lambda h, run: h == 4242)
        wv = _FloatingOrbWindowWebview()
        orb = fo.FloatingOrb(_state_api(), webview=wv, html="<p>")
        orb.show()
        for fn in wv.created[0].events.shown:
            fn()
        assert ("[FloatingOrb] no-activate verified: True | mouse-activate guard: on"
                in capsys.readouterr().out)

    @floating_orb_window_needs_node
    def test_the_orb_page_tells_hold_tap_and_drag_apart(self, tmp_path):
        script = ORB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        f = tmp_path / "pointer.js"
        f.write_text(_POINTER.replace("%SCRIPT%", script), encoding="utf-8")
        out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == {"tap": ["toggle"], "hold": ["start", "stop"],
                                          "drag": [], "jitter": ["toggle"], "right": []}


# ============================================================
# ORB FIXES
# ============================================================

# Fixes to the floating orb + panels prototype: real window sizes (pywebview's
# min_size clamp), taskbar/Alt-Tab hiding, smooth move-only flights, the orb's
# background, MISTAI_ORB_SIZES, following the task (maximized targets and
# element rects), panel placement/labels/closing, MISTAI_ORB_DEBUG_CAPTURE.
# Fakes only: no window is created.


# ============================================================
# ORB FIXES CLAMP WIN
# ============================================================

class _ClampWin:
    """A Win32 fake that clamps sizes like a WinForms MinimumSize does."""

    def __init__(self, min_size, scale=1.25, work=(0, 0, 1920, 1020)):
        self.min = (round(min_size[0] * scale), round(min_size[1] * scale))
        self.scale, self.work, self.rect, self.style, self.calls = scale, work, None, 0, []

    def set_affinity(self, hwnd, mode):
        self.calls.append(("affinity", mode))
        return True

    def set_exstyle(self, hwnd, add=0, remove=0):
        self.style = (self.style | add) & ~remove

    def exstyle(self, hwnd):
        return self.style

    def show(self, hwnd, on):
        self.calls.append(("show", on))

    def primary_work_area(self):
        return "mon", self.work

    def monitor_scale(self, monitor, hwnd=None):
        return self.scale

    def move(self, hwnd, x, y, w, h):
        w, h = max(w, self.min[0]), max(h, self.min[1])
        self.rect = (x, y, x + w, y + h)
        return True

    def window_rect(self, hwnd):
        return self.rect

    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return 1

    def set_region(self, hwnd, region):
        return True

    def delete_object(self, h):
        pass


# ============================================================
# ORB FIXES HOOK
# ============================================================

class _OrbFixesHook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


# ============================================================
# ORB FIXES WEBVIEW
# ============================================================

class _OrbFixesWebview:
    def __init__(self):
        self.created = []

    def create_window(self, title, **kw):
        w = SimpleNamespace(title=title, kw=kw, events=SimpleNamespace(shown=_OrbFixesHook()),
                            native=SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 77)))
        self.created.append(w)
        return w


# ============================================================
# ORB FIXES HELPERS
# ============================================================

def _created_min_size(make):
    wv = _OrbFixesWebview()
    make(wv)
    # pywebview's own default when min_size is not passed
    return wv.created[0].kw.get("min_size", (200, 100))


# -- 1. taskbar and Alt-Tab -------------------------------------------------------------------


# ============================================================
# ORB FIXES PANEL WIN
# ============================================================

class _PanelWin:
    def __init__(self, rect_override=None):
        self.calls, self.style, self.rects, self.override = [], fo.WS_EX_APPWINDOW, {}, None

    def set_affinity(self, hwnd, mode):
        self.calls.append(("affinity", mode))
        return True

    def set_exstyle(self, hwnd, add=0, remove=0):
        self.calls.append(("set_exstyle", add, remove))
        self.style = (self.style | add) & ~remove

    def exstyle(self, hwnd):
        return self.style

    def show(self, hwnd, on):
        self.calls.append(("show", on))

    def move(self, hwnd, x, y, w, h):
        self.calls.append(("move", (x, y, w, h)))
        self.rects[hwnd] = (x, y, x + w, y + h)
        return True

    def move_only(self, hwnd, x, y):
        self.calls.append(("move_only", (x, y)))
        return True

    def window_rect(self, hwnd):
        return self.rects.get(hwnd)

    def create_round_rect(self, box):
        self.calls.append(("roundrect", box))
        return 1

    def set_region(self, hwnd, region):
        return True

    def delete_object(self, h):
        pass

    def dwm_set(self, *a):
        return 1


# -- 2. smooth movement -----------------------------------------------------------------------


# ============================================================
# ORB FIXES COUNT WIN
# ============================================================

class _CountWin(_PanelWin):
    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return 1


# -- 3. background ----------------------------------------------------------------------------


# -- 4. sizes -----------------------------------------------------------------------------


# -- 5 / 5b. following the task -----------------------------------------------------------------


# ============================================================
# ORB FIXES HELPERS (2)
# ============================================================

ORB_FIXES_W = (0, 0, 1920, 1040)


# ============================================================
# ORB FIXES FLIGHT WIN
# ============================================================

class _OrbFixesFlightWin:
    def __init__(self, target, fg=55):
        self.target, self.fg, self.styles = target, fg, []

    def foreground(self):
        return self.fg

    def window_rect(self, hwnd):
        return self.target if hwnd == self.fg else (1700, 900, 1782, 982)

    def monitor_for_rect(self, rect):
        return ORB_FIXES_W, 1.0

    def set_click_through(self, hwnd, on):
        self.styles.append(on)

    def is_minimized(self, hwnd):
        return True

    def window_info(self, hwnd):
        return (1234, "studio64.exe", "SunAwtFrame")

    def browser_window(self):
        return None


# ============================================================
# ORB FIXES ORB
# ============================================================

def _orb(target=(100, 100, 1000, 800), app="studio", busy=True, element=None, flight=True):
    lock = threading.Lock()
    if busy:
        lock.acquire()
    api = SimpleNamespace(_ptt_wants_mic=lambda: False, tts=SimpleNamespace(speaking=False),
                          _agent_lock=lock, _last_opened_app=app,
                          _window_belongs_to=lambda hwnd, a: hwnd == 55,
                          orb_option=lambda n: flight and n == "orb_flight",
                          _stop_requested=threading.Event(),
                          click_handler=SimpleNamespace(last_element_rect=element))
    orb = fo.FloatingOrb(api, webview=_OrbFixesWebview(), html="<p>")
    orb.visible, orb.hwnd, orb.scale = True, 4242, 1.0
    orb.square = orb.home = (1700, 900, 82)
    orb._win = _OrbFixesFlightWin(target)
    orb._adopt_drag = lambda: None                   # no user drag in these tests
    orb._acted, orb._was_busy = True, busy           # mid-task: a step has already run
    played = []
    orb._tween = SimpleNamespace(running=False, side=82,
                                 play=lambda plan, on_frame=None, done=None: played.append(plan))
    return orb, played


# -- 5b. layout: maximized targets ------------------------------------------------------------


# ============================================================
# ORB FIXES HELPERS (3)
# ============================================================

MAXED = (-8, -8, 1928, 1048)


# -- 6. panels ------------------------------------------------------------------------------

SIZES = [(240, 176), (280, 140)]


# -- 7. debug capture --------------------------------------------------------------------------


# -- the general target: whatever the task brought to the front -------------------------------


# ============================================================
# ORB FIXES SEQ WIN
# ============================================================

class _SeqWin(_OrbFixesFlightWin):
    """A foreground that changes as the test says; windows described by
    (pid, process, class); an optional Playwright browser window."""

    INFO = {10: (500, "Code.exe", "Chrome_WidgetWin_1"),
            20: (600, "notepad.exe", "Notepad"),
            77: (700, "firefox.exe", "MozillaWindowClass"),
            30: (0, "explorer.exe", "Shell_TrayWnd"),
            31: (800, "Taskmgr.exe", "TaskManagerWindow"),
            4242: (900, "python.exe", "WindowsForms10"),
            88: (901, "python.exe", "WindowsForms10")}
    RECTS = {10: (0, 0, 900, 700), 20: (100, 100, 1000, 800), 77: (200, 50, 1100, 900),
             30: (0, 1040, 1920, 1080), 31: (300, 300, 900, 800), 88: (0, 0, 800, 600)}

    def __init__(self, fg=10, browser=None):
        super().__init__(target=None, fg=fg)
        self.browser = browser

    def window_rect(self, hwnd):
        return self.RECTS.get(hwnd, (1700, 900, 1782, 982))

    def window_info(self, hwnd):
        return self.INFO.get(hwnd, (0, "", ""))

    def browser_window(self):
        return self.browser


# ============================================================
# ORB FIXES TASK
# ============================================================

def _task(fg=10, browser=None, app=None):
    """An orb whose task starts now, with ``fg`` in front."""
    orb, played = _orb(app=app)
    orb._win = _SeqWin(fg=fg, browser=browser)
    orb._acted, orb._was_busy = False, False
    orb._api._window_belongs_to = lambda hwnd, a: (hwnd, a) == (20, "notepad")
    orb._api._own_window_handle = lambda: 88
    orb.tick()                                       # the task starts: pre-task window
    return orb, played


# ============================================================
# ORB FIXES HELPERS (4)
# ============================================================

def _step(orb, action, fg=None):
    if fg is not None:
        orb._win.fg = fg
    orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": action}))
    orb.tick()


# -- minimum dwell (c) ---------------------------------------------------------------------


# -- (b) browser element rects mapped to the screen ---------------------------------------------

def _info(dpr=1.0, rects=None, sx=100, sy=50):
    return dict(screenX=sx, screenY=sy, outerWidth=1216, innerWidth=1200, outerHeight=900,
                innerHeight=800, dpr=dpr,
                rects=rects if rects is not None else [[20, 100, 400, 120], [20, 200, 400, 220]])


# -- 2. the search tour ------------------------------------------------------------------------


# ============================================================
# ORB FIXES TOUR BROWSER
# ============================================================

class _TourBrowser:
    def __init__(self, info):
        self.info, self.calls = info, []

    def link_boxes(self, selector, n=4):
        self.calls.append((selector, n))
        return self.info


# ============================================================
# ORB FIXES TOUR ORB
# ============================================================

def _orb_fixes_tour_orb(info, browser_hwnd=77):
    orb, played = _task(fg=10, browser=browser_hwnd)
    orb._win.RECTS = {**_SeqWin.RECTS, 77: (0, 0, 1900, 1040)}
    orb._api.browser = _TourBrowser(info)
    orb._api.orb_option = lambda n: n == "orb_flight"
    now = [100.0]
    orb._clock = lambda: now[0]
    orb._sleep = lambda s: None
    orb._tween.running = False
    return orb, played, now


# ============================================================
# ORB FIXES HELPERS (5)
# ============================================================

ORB_FIXES_FOUR = [[20, 100 + 90 * i, 600, 124 + 90 * i] for i in range(4)]


# -- 3. the answer flow ------------------------------------------------------------------------

ANSWER_PANEL = (1600, 220, 1880, 360)


# ============================================================
# ORB FIXES ANSWER ORB
# ============================================================

def _answer_orb(tts=True):
    orb, played = _orb(busy=False)
    now = [100.0]
    orb._clock = lambda: now[0]
    orb.square = (1008, 108, 28)
    orb.panels = SimpleNamespace(rects={"answer": ANSWER_PANEL}, handles=lambda: [],
                                 hide_all=lambda: None, update=lambda *a, **k: None)
    if tts:
        orb._api.tts = SimpleNamespace(speaking=False)
    else:
        orb._api.tts = SimpleNamespace()
    orb._on_bus(events.Event(events.AGENT_STEP, {"action": "done",
                                                 "speech": "one two three four five six "
                                                           "seven eight nine ten"}))
    return orb, played, now


# ============================================================
# ORB FIXES TESTS
# ============================================================

class TestOrbFixes:
    @pytest.fixture(autouse=True)
    def _orb_fixes_env(self, monkeypatch):
        for name in ("MISTAI_ORB_SIZES", "MISTAI_ORB_DEBUG_CAPTURE"):
            monkeypatch.delenv(name, raising=False)

    def test_the_orb_window_gets_the_size_it_asks_for(self, monkeypatch, capsys):
        monkeypatch.setattr(fo, "_corner_position", lambda size=None: (1, 2))
        min_size = _created_min_size(lambda wv: fo.FloatingOrb(SimpleNamespace(), webview=wv,
                                                               html="<p>").show())
        win = _ClampWin(min_size)
        out = fo.harden(7, win=win)
        side = round(82 * 1.25)
        x, y, w, h, _s = out["position"]
        assert (w, h) == (side, side), "the window was clamped (pywebview min_size)"
        assert ("ellipse", (0, 0, side + 1, side + 1)) in win.calls
        log = capsys.readouterr().out
        assert f"requested {side},{side} actual {side},{side}" in log

    def test_the_size_check_catches_a_clamp(self, capsys):
        win = _ClampWin((200, 100))                              # pywebview's default
        out = fo.harden(7, win=win)
        assert out["position"][2:4] == (250, 125)                # built from the ACTUAL size
        assert ("ellipse", (0, 0, 251, 126)) in win.calls
        log = capsys.readouterr().out
        assert "requested 102,102 actual 250,125" in log
        assert "actual size differs from the requested size" in log

    def test_panels_are_created_without_the_min_size_clamp_and_log_actual_sizes(self, monkeypatch,
                                                                               capsys):
        wv = _OrbFixesWebview()
        win = _PanelWin()
        panels = op.OrbPanels(SimpleNamespace(), win, webview=wv, html="<p>")
        panels._was_busy = True                          # the task is already running
        panels.model.steps = [{"label": "Typing", "action": "type_text", "state": "run"}]
        panels.update((1000, 100, 35), None, (0, 0, 1920, 1040), 1.0, busy=True)
        (w,) = wv.created
        assert w.kw["min_size"] == (1, 1)
        for fn in w.events.shown:
            fn()
        log = capsys.readouterr().out
        assert "[OrbPanels] steps requested 240,176 actual 240,176" in log
        assert ("roundrect", (0, 0, 241, 177, 24, 24)) in win.calls

    def test_toolwindow_is_applied_and_the_window_re_shown_so_the_taskbar_drops_it(self, capsys):
        win = _PanelWin()
        assert fo.hide_from_taskbar(7, win, "FloatingOrb") is True
        assert win.style & fo.WS_EX_TOOLWINDOW and win.style & fo.WS_EX_NOACTIVATE
        assert not win.style & fo.WS_EX_APPWINDOW
        shows = [c[1] for c in win.calls if c[0] == "show"]
        assert shows == [False, True]                        # hidden, then shown again
        assert win.calls.index(("set_exstyle", fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE,
                                fo.WS_EX_APPWINDOW)) < win.calls.index(("show", False))
        assert "[FloatingOrb] taskbar: hidden" in capsys.readouterr().out

    def test_a_style_that_did_not_take_is_reported(self, capsys):
        win = _PanelWin()
        win.set_exstyle = lambda hwnd, add=0, remove=0: None       # the bits never stick
        assert fo.hide_from_taskbar(7, win, "OrbPanels") is False
        assert "[OrbPanels] taskbar: still listed" in capsys.readouterr().out

    def test_every_panel_gets_the_same_extended_styles(self):
        win = _PanelWin()
        op.harden_panel(9, win, 190, 30, 1.0, blur=False)
        assert win.style & fo.WS_EX_TOOLWINDOW and win.style & fo.WS_EX_NOACTIVATE
        assert not win.style & fo.WS_EX_APPWINDOW
        assert [c[1] for c in win.calls if c[0] == "show"] == [False, True]

    def test_frames_with_the_same_size_only_move_the_window(self):
        win = _CountWin()
        assert fo.apply_square(win, 7, 10, 20, 40, last_side=40) is True
        assert win.calls == [("move_only", (10, 20))]
        win.calls.clear()
        fo.apply_square(win, 7, 10, 20, 30, last_side=40)
        assert [c[0] for c in win.calls] == ["move", "ellipse"]

    def test_a_flight_recuts_the_region_only_when_the_size_changes(self, monkeypatch):
        monkeypatch.setattr(fo.Tween, "FRAME_S", 0)

        class Clock:
            t = 0.0

            def __call__(self):
                self.t += 1 / 60
                return self.t

        win = _CountWin()
        tw = fo.Tween(win, 7, clock=Clock())
        tw.side = 82
        plan = ol.flight_plan((1700, 900, 82), (300, 200, 28))
        tw.play(plan).join(5)
        regions = [c for c in win.calls if c[0] == "ellipse"]
        moves = [c for c in win.calls if c[0] == "move_only"]
        resized = [c[1][2] for c in win.calls if c[0] == "move"]
        assert len(moves) >= 20                            # the flight itself: moves only
        assert len(regions) == len(resized) <= 10          # the short shrink step only
        assert all(s >= 28 for s in resized) and resized[-1] == 28

    def test_flight_plans_resize_before_or_after_the_move_never_during(self):
        shrink = ol.flight_plan((1700, 900, 82), (300, 200, 28))
        assert [seg[0][2] == seg[1][2] for seg in shrink] == [False, True]
        grow = ol.flight_plan((300, 200, 28), (1700, 900, 82))
        assert [seg[0][2] == seg[1][2] for seg in grow] == [True, False]
        assert grow[-1][1] == (1700, 900, 82) and shrink[-1][1] == (300, 200, 28)
        near = ol.flight_plan((100, 100, 82), (106, 106, 70))
        assert len(near) == 1 and near[0][3] == 0.0

    def test_square_at_is_eased_and_exact_at_the_ends(self):
        a, b = (0, 0, 10), (100, 0, 10)
        assert ol.square_at(a, b, 0) == a and ol.square_at(a, b, 1) == b
        assert ol.square_at(a, b, 2) == b and ol.square_at(a, b, -1) == a
        assert ol.square_at(a, b, 0.1)[0] < 10             # eases in

    def test_the_orb_page_and_window_share_the_rim_colour_and_the_disc_fills_it(self):
        bg = config.orb_background()
        assert bg == "#203247"
        assert f"background:{bg}" in ORB_HTML
        assert "width:100vmin;height:100vmin" in ORB_HTML
        assert "#05080f" not in ORB_HTML
        assert "background:#161c26" in PANEL_HTML and op.PANEL_BACKGROUND == "#161c26"

    @pytest.mark.parametrize("raw, want", [
        (None, (82, 70, 28)), ("110,94,36", (110, 94, 36)), (" 60 , 50 , 20 ", (60, 50, 20)),
        ("1,2,3", (82, 70, 28)), ("50,60,20", (82, 70, 28)), ("a,b,c", (82, 70, 28)),
        ("80,70", (82, 70, 28)), ("", (82, 70, 28)), ("500,70,28", (82, 70, 28)),
    ])
    def test_orb_sizes_are_read_at_call_time_and_validated(self, monkeypatch, raw, want):
        if raw is not None:
            monkeypatch.setenv("MISTAI_ORB_SIZES", raw)
        assert config.orb_sizes() == want

    @pytest.mark.parametrize("scale, big, medium, small", [
        (1.0, 82, 70, 28), (1.25, 102, 88, 35), (1.5, 123, 105, 42),
    ])
    def test_default_sizes_are_dpi_scaled(self, scale, big, medium, small):
        _x, _y, side = fo.place((0, 0, 1920, 1080), scale)
        assert side == big
        assert round(fo.size_for("thinking") * scale) == medium
        assert round(fo.size_for("idle", docked=True) * scale) == small

    def test_no_target_yet_stays_where_it_is_not_in_a_corner(self, capsys):
        orb, played = _orb(app=None)
        orb._acted = False                               # no action has run yet
        orb.tick()
        end = played[0][-1][1]
        assert end == ol.resize_about_center(1700, 900, 82, 70)     # medium, same center
        assert "[FloatingOrb] target none (no-action-yet)" in capsys.readouterr().out

    def test_a_normal_window_is_docked_beside_and_logged_as_numbers(self, capsys):
        orb, played = _orb()
        orb.tick()
        assert played[0][-1][1] == (1008, 108, 28)
        log = capsys.readouterr().out
        assert "[FloatingOrb] target foreground rect 100,100,1000,800" in log
        assert "studio" not in log

    def test_a_maximized_target_docks_beside_the_element_inside_the_window(self, capsys):
        orb, played = _orb(target=(-8, -8, 1928, 1048))
        orb._busy_since = 0.0
        orb._was_busy = True
        orb._api.click_handler.last_element_rect = ((300, 200, 500, 240), time.monotonic())
        orb.tick()
        x, y, s = played[0][-1][1]
        assert s == 28 and not ol.overlaps((x, y, x + s, y + s), (300, 200, 500, 240))
        assert abs(x - 508) <= 1                         # right of the element: more room there
        assert orb.dock == "element"
        log = capsys.readouterr().out
        assert "target maximized-element rect -8,-8,1928,1048 element 300,200,500,240" in log

    def test_a_maximized_target_without_an_element_docks_at_its_nearest_inner_edge(self):
        orb, played = _orb(target=(-8, -8, 1928, 1048))
        orb.tick()
        x, y, s = played[0][-1][1]
        assert orb.dock == "inner-edge"
        assert (x, y) == (946, 1040 - 28 - 8)             # mid bottom edge, not the corner

    def test_a_click_point_from_before_this_task_is_ignored(self):
        orb, _ = _orb(target=(-8, -8, 1928, 1048))
        orb._busy_since = time.monotonic()
        orb._api.click_handler.last_element_rect = ((400, 400), orb._busy_since - 5)
        assert orb._element_rect((-8, -8, 1928, 1048), 1.0) is None
        orb._api.click_handler.last_element_rect = ((400, 400), orb._busy_since + 1)
        assert orb._element_rect((-8, -8, 1928, 1048), 1.25) == (370, 370, 430, 430)

    def test_a_step_or_launch_ticks_at_once_and_polls_faster_for_a_while(self):
        orb, _ = _orb()
        orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "open_app"}))
        assert orb._kick.is_set() and orb._fast_until > time.monotonic()
        orb._kick.clear()
        orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "click_on_text"}))
        assert orb._kick.is_set()

    def test_clicks_and_uia_record_the_element_they_touch(self):
        assert "self.last_element_rect = ((x, y), time.monotonic())" in inspect.getsource(
            __import__("core.click_handler", fromlist=["x"]).ImprovedClickHandler._execute_click)
        from core.click_handler import EnhancedUIANavigator
        assert "self.last_element_rect = ((rect.left" in inspect.getsource(
            EnhancedUIANavigator.click_element)

    def test_coverage(self):
        assert ol.coverage(MAXED, ORB_FIXES_W) == 1.0
        assert 0.24 < ol.coverage((0, 0, 960, 520), ORB_FIXES_W) < 0.26

    @pytest.mark.parametrize("element, where", [
        ((300, 200, 500, 240), "right"),        # more room to the right inside the window
        ((1500, 200, 1900, 240), "left"),
        ((0, 100, 1920, 140), "below"),         # full-width row: below it
        ((0, 900, 1920, 1030), "above"),
    ])
    def test_dock_inside_beside_the_element_on_the_emptier_side(self, element, where):
        x, y = ol.dock_inside(MAXED, ORB_FIXES_W, 28, element)
        assert not ol.overlaps((x, y, x + 28, y + 28), element)
        assert 0 <= x and x + 28 <= 1920 and 0 <= y and y + 28 <= 1040
        el, et, er, eb = element
        assert {"right": x >= er, "left": x + 28 <= el, "below": y >= eb,
                "above": y + 28 <= et}[where]

    def test_dock_inside_without_an_element_hugs_the_nearest_inner_edge(self):
        # The middle of the nearest edge, never at the orb's spot (a corner from home).
        assert ol.dock_inside(MAXED, ORB_FIXES_W, 28, near=(1890, 600)) == (1884, 506)
        assert ol.dock_inside(MAXED, ORB_FIXES_W, 28, near=(1900, 1030)) == (946, 1004)   # bottom-right
        assert ol.dock_inside(MAXED, ORB_FIXES_W, 28, near=(30, 500)) == (8, 506)
        assert ol.dock_inside(MAXED, ORB_FIXES_W, 28, near=(900, 10)) == (946, 8)
        assert ol.dock_inside(MAXED, ORB_FIXES_W, 28, near=(900, 1030)) == (946, 1004)

    def test_dock_inside_a_window_on_a_second_monitor(self):
        work = (1920, -200, 3840, 880)
        x, y = ol.dock_inside((1912, -208, 3848, 888), work, 35, (2000, -100, 2300, -60))
        assert 1920 <= x and x + 35 <= 3840 and -200 <= y and y + 35 <= 880
        assert x >= 2300

    def test_an_element_filling_the_window_still_keeps_the_orb_inside(self):
        x, y = ol.dock_inside(MAXED, ORB_FIXES_W, 28, (0, 0, 1920, 1040))
        assert 0 <= x <= 1920 - 28 and 0 <= y <= 1040 - 28

    def test_failure_pause_and_the_network_sentence_close_everything(self):
        m = op.PanelModel()
        m.on_event(events.Event(events.AGENT_STEP, {"action": "open_app"}))
        assert m.visible("steps", True)
        for name, data in ((events.TASK_FAILED, {}), (events.TASK_PAUSED, {}),
                           (events.AGENT_STEP, {"action": "failed"})):
            m.reset()
            m.on_event(events.Event(events.AGENT_STEP, {"action": "open_app"}))
            m.on_event(events.Event(name, data))
            assert not any(m.visible(k, True) for k in op.KINDS), name

    def test_a_stop_or_failure_sends_the_orb_home_at_once(self):
        orb, played = _orb()
        orb.square = (1008, 108, 28)
        orb._api._stop_requested.set()
        orb.tick()
        assert played[0][-1][1] == (1700, 900, 82)
        orb, played = _orb()
        orb.square = (1008, 108, 28)
        orb._on_bus(events.Event(events.TASK_FAILED, {}))
        orb._was_busy = True                               # the same task, not a new one
        orb.tick()
        assert played[0][-1][1] == (1700, 900, 82)

    def test_the_agent_signals_a_failed_call_and_the_network_sentence(self):
        src = inspect.getsource(AgentLoop)
        i = src.index("Network error:")
        assert 'self._publish_step("failed")' in src[i:i + 200]
        j = src.index("Agent call failed:")
        assert 'self._publish_step("failed")' in src[j:j + 200]

    def test_steps_and_answer_panels_appear_through_the_real_bus(self, monkeypatch):
        from core.live_state import EventBus
        from core.tools import ToolRegistry

        monkeypatch.setattr(op, "panel_blur_enabled", lambda: False)
        bus = EventBus()
        try:
            api = SimpleNamespace(bus=bus, tools=ToolRegistry())
            wv, win = _OrbFixesWebview(), _PanelWin()
            panels = op.OrbPanels(api, win, webview=wv, html="<p>")
            loop = AgentLoop.__new__(AgentLoop)
            loop.api = api
            panels.update((1000, 100, 35), None, ORB_FIXES_W, 1.0, busy=True)
            loop._publish_step("click_on_text")
            bus.publish(events.ACTION_EXECUTED, {"action": "click_on_text"})
            loop._publish_step("done", "All set.")
            deadline = time.monotonic() + 3
            while panels.model.answer_at is None and time.monotonic() < deadline:
                time.sleep(0.01)
            panels.update((1000, 100, 35), None, ORB_FIXES_W, 1.0, busy=False)
            kinds = [w.kw["js_api"]._kind for w in wv.created]
            assert kinds == ["steps", "answer"]
            assert panels.model.state("steps")["steps"] == [{"label": "Clicking", "state": "ok"}]
            assert panels.model.state("answer")["text"] == "All set."
        finally:
            bus.stop()

    def test_debug_capture_leaves_the_orb_and_panels_capturable(self, monkeypatch, capsys):
        monkeypatch.setenv("MISTAI_ORB_DEBUG_CAPTURE", "1")
        win = _ClampWin((1, 1))
        assert fo.harden(7, win=win)["capture"] == "debug-visible"
        assert not any(c[0] == "affinity" for c in win.calls)
        pw = _PanelWin()
        op.harden_panel(9, pw, 190, 30, 1.0, blur=False)
        assert not any(c[0] == "affinity" for c in pw.calls)
        assert "capture exclusion off (MISTAI_ORB_DEBUG_CAPTURE)" in capsys.readouterr().out

    def test_debug_capture_is_ignored_in_a_built_app(self, monkeypatch):
        monkeypatch.setenv("MISTAI_ORB_DEBUG_CAPTURE", "1")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert config.orb_debug_capture() is False
        win = _ClampWin((1, 1))
        assert fo.harden(7, win=win)["capture"] == "excluded"

    def test_nothing_is_followed_before_the_first_action(self, capsys):
        orb, played = _task(fg=20)
        assert orb._pre_task_fg == 20 and orb._cur_target is None
        assert "target none (no-action-yet)" in capsys.readouterr().out

    def test_a_search_task_follows_mistais_browser_window(self, capsys):
        orb, played = _task(fg=10, browser=77)
        _step(orb, "browser_search")                     # the user's editor is still in front
        assert orb._cur_target["rect"] == (200, 50, 1100, 900)
        assert played[-1][-1][1] == (1108, 58, 28)       # right of the browser window
        log = capsys.readouterr().out
        assert "[FloatingOrb] target browser rect 200,50,1100,900" in log
        assert "firefox" not in log.lower() and "code" not in log.lower()

    def test_without_its_window_a_browser_step_follows_the_new_foreground(self, capsys):
        orb, played = _task(fg=10, browser=None)
        _step(orb, "browser_open", fg=77)                # the browser came to the front
        assert orb._cur_target["rect"] == (200, 50, 1100, 900)
        assert "target foreground rect 200,50,1100,900" in capsys.readouterr().out

    def test_a_task_whose_foreground_never_changes_leaves_the_orb_put(self, capsys):
        orb, played = _task(fg=10)
        _step(orb, "run_command")
        _step(orb, "inspect_file")
        assert orb._cur_target is None
        assert played[-1][-1][1] == ol.resize_about_center(1700, 900, 82, 70)   # stays put
        assert "target none (unrelated-window)" in capsys.readouterr().out

    def test_the_app_the_step_touched_counts_even_if_it_was_already_in_front(self):
        orb, played = _task(fg=20, app="notepad")
        _step(orb, "switch_app")
        assert orb._cur_target["rect"] == (100, 100, 1000, 800)

    @pytest.mark.parametrize("fg", [4242, 88, 30, 31])     # orb, MistAI, taskbar, Task Manager
    def test_mistai_the_orb_the_shell_and_task_manager_are_never_targets(self, fg, capsys):
        orb, played = _task(fg=10)
        _step(orb, "click_on_text", fg=fg)
        assert orb._cur_target is None
        assert "target none (no-window)" in capsys.readouterr().out

    def test_panel_windows_are_never_targets(self):
        orb, played = _task(fg=10)
        orb.panels = SimpleNamespace(handles=lambda: [66], hide_all=lambda: None,
                                     update=lambda *a, **k: None)
        _step(orb, "click_on_text", fg=66)
        assert orb._cur_target is None

    def test_a_maximized_browser_window_docks_inside_it(self, capsys):
        orb, played = _task(fg=10, browser=77)
        orb._win.RECTS = {**_SeqWin.RECTS, 77: (-8, -8, 1928, 1048)}
        _step(orb, "browser_get_text")
        x, y, s = played[-1][-1][1]
        assert orb.dock == "inner-edge" and s == 28
        assert 0 <= x and x + s <= 1920 and 0 <= y and y + s <= 1040
        assert "target maximized-edge rect -8,-8,1928,1048" in capsys.readouterr().out

    def test_every_step_is_followed_by_fast_polling(self):
        orb, _ = _task(fg=10)
        for action in ("run_command", "browser_search", "click_on_text"):
            orb._fast_until = 0
            orb._on_bus(events.Event(events.ACTION_FAILED, {"action": action}))
            assert orb._fast_until > time.monotonic() and orb._last_action == action

    def test_a_docked_arrival_dwells_even_if_the_task_ends_sooner(self):
        orb, played = _orb()
        now = [100.0]
        orb._clock = lambda: now[0]
        orb.tick()                                        # flies to the dock beside the target
        plan = played[-1]
        flight = sum(seg[2] for seg in plan)
        assert orb._dwell_until == pytest.approx(100.0 + flight + 1.5)
        orb.square = plan[-1][1]
        orb._api._agent_lock.release()                    # the task ends at once
        now[0] += flight + 1.0
        orb.tick()
        assert len(played) == 1                           # still docked: dwell not over
        now[0] += 1.0
        orb.tick()
        assert played[-1][-1][1] == (1700, 900, 82)       # then home

    def test_stop_cuts_the_dwell_short(self):
        orb, played = _orb()
        orb._clock = lambda: 100.0
        orb.tick()
        orb.square = played[-1][-1][1]
        orb._api._stop_requested.set()
        orb.tick()
        assert played[-1][-1][1] == (1700, 900, 82)

    @pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5])
    def test_page_rects_map_to_physical_screen_pixels(self, dpr):
        window = (0, 0, 4000, 3000)
        got = ol.map_page_rects(_info(dpr), window)
        # x: screenX + border (8) + left; y: screenY + toolbar (100) + top; times the dpr
        assert got[0] == (round(128 * dpr), round(250 * dpr), round(508 * dpr), round(270 * dpr))

    def test_rects_outside_the_browser_window_or_empty_mean_no_rects(self):
        assert ol.map_page_rects(_info(), (0, 0, 300, 300)) is None          # outside the window
        assert ol.map_page_rects(_info(rects=[[20, 100, 20, 120]]), (0, 0, 4000, 3000)) is None
        assert ol.map_page_rects(_info(rects=[]), (0, 0, 4000, 3000)) is None
        assert ol.map_page_rects({"dpr": "x"}, (0, 0, 10, 10)) is None
        below = _info(rects=[[20, 100, 400, 120], [20, 900, 400, 920]])     # 2nd below the fold
        assert len(ol.map_page_rects(below, (0, 0, 4000, 3000))) == 1

    def test_the_margin_spot_prefers_the_right_then_the_left(self):
        area = (0, 0, 1000, 800)
        assert ol.margin_spot((100, 100, 500, 120), area, 28) == (508, 96)
        assert ol.margin_spot((100, 100, 990, 120), area, 28) == (64, 96)

    def test_a_tour_hop_moves_medium_and_lands_small(self):
        plan = ol.tour_plan((500, 100, 28), (500, 300, 28), 70)
        assert plan[0][1][2] == 70 and plan[1][0][2] == plan[1][1][2] == 70
        assert plan[-1][1] == (500, 300, 28)

    def test_the_tour_hops_down_the_results_in_order_with_a_dwell(self, capsys):
        orb, played, now = _orb_fixes_tour_orb(_info(rects=ORB_FIXES_FOUR, sx=0, sy=0))
        orb._prepare_tour(orb._tour_gen)
        assert orb._api.browser.calls == [(fo.RESULT_LINKS, fo.TOUR_CANDIDATES)]
        assert len(orb._tour["stops"]) == 4
        played.clear()
        orb.tick()
        first = played[-1]
        assert first[-1][1] == orb._tour["stops"][0]
        hop = sum(s[2] for s in first)
        orb.tick()
        assert len(played) == 1                                  # dwelling 0.7 s
        now[0] += hop + fo.TOUR_DWELL_S
        orb.tick()
        assert played[-1][-1][1] == orb._tour["stops"][1]
        ys = [s[1] for s in orb._tour["stops"]]
        assert ys == sorted(ys) and all(s[2] == 28 for s in orb._tour["stops"])
        assert all(s[0] >= 608 for s in orb._tour["stops"])     # right margin of the results
        log = capsys.readouterr().out
        assert "[FloatingOrb] tour stops 4" in log

    def test_a_new_action_or_done_cancels_the_tour(self):
        for action in ("browser_click", "done"):
            orb, played, now = _orb_fixes_tour_orb(_info(rects=ORB_FIXES_FOUR, sx=0, sy=0))
            orb._prepare_tour(orb._tour_gen)
            orb.tick()
            cancelled = []
            orb._tween.cancel = lambda: cancelled.append(1)
            orb._on_bus(events.Event(events.AGENT_STEP, {"action": action}))
            assert orb._tour is None and cancelled == [1]

    def test_a_tour_prepared_after_a_new_step_is_dropped(self):
        orb, played, now = _orb_fixes_tour_orb(_info(rects=ORB_FIXES_FOUR, sx=0, sy=0))
        gen = orb._tour_gen
        orb._on_bus(events.Event(events.AGENT_STEP, {"action": "browser_click"}))
        orb._prepare_tour(gen)
        assert orb._tour is None

    @pytest.mark.parametrize("info, hwnd", [
        (None, 77),                                               # browser not running
        (_info(rects=[]), 77),                                    # no result links
        (_info(rects=ORB_FIXES_FOUR, sx=5000), 77),                         # rects outside the window
        (_info(rects=ORB_FIXES_FOUR, sx=0, sy=0), None),                    # no browser window
    ])
    def test_missing_or_bad_rects_skip_the_tour(self, info, hwnd, capsys):
        orb, played, now = _orb_fixes_tour_orb(info, browser_hwnd=hwnd)
        orb._prepare_tour(orb._tour_gen)
        assert orb._tour is None
        assert "[FloatingOrb] tour skipped (no-rects)" in capsys.readouterr().out

    def test_the_tour_adds_no_steps_or_events(self):
        src = inspect.getsource(fo.FloatingOrb._prepare_tour) + inspect.getsource(
            fo.FloatingOrb._tour_step)
        assert "publish" not in src and "AGENT_STEP" not in src

    def test_only_a_successful_search_with_flight_on_starts_a_tour(self, monkeypatch):
        orb, played, now = _orb_fixes_tour_orb(_info(rects=ORB_FIXES_FOUR, sx=0, sy=0))
        started = []
        monkeypatch.setattr(fo.threading, "Thread",
                            lambda target, args, **k: SimpleNamespace(
                                start=lambda: started.append(target.__name__)))
        orb._on_bus(events.Event(events.ACTION_FAILED, {"action": "browser_search"}))
        orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "browser_get_text"}))
        assert started == []
        orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "browser_search"}))
        assert started == ["_prepare_tour"]
        orb._api.orb_option = lambda n: False
        orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "browser_search"}))
        assert started == ["_prepare_tour"]

    def test_answer_spot_left_of_the_panel_else_right(self):
        assert ol.answer_spot(ANSWER_PANEL, 70, ORB_FIXES_W) == (1518, 255)
        assert ol.answer_spot((16, 220, 296, 360), 70, ORB_FIXES_W) == (308, 255)      # no room on the left

    def test_the_orb_speaks_beside_the_answer_panel_then_flies_home(self):
        orb, played, now = _answer_orb()
        assert "speech" not in orb._answer and orb._answer["words"] == 10   # no text kept
        orb.tick()                                        # voice about to start
        assert played[-1][-1][1] == (1518, 255, 70) and orb.dock == "answer"
        orb.square = played[-1][-1][1]
        orb._api.tts.speaking = True
        now[0] += 5
        orb.tick()
        assert len(played) == 1                           # stays while speaking
        orb._api.tts.speaking = False
        orb.tick()
        assert played[-1][-1][1] == (1700, 900, 82)       # home, big

    def test_without_a_tts_flag_the_speech_length_is_estimated(self):
        orb, played, now = _answer_orb(tts=False)
        orb.tick()
        orb.square = played[-1][-1][1]
        now[0] += 3.0                                     # 10 words / 2.5 = 4 s
        orb.tick()
        assert len(played) == 1
        now[0] += 1.5
        orb.tick()
        assert played[-1][-1][1] == (1700, 900, 82)

    def test_the_estimate_is_capped(self):
        orb, played, now = _answer_orb(tts=False)
        orb._answer["words"] = 1000
        now[0] += fo.SPEECH_CAP_S + 0.1
        assert orb._answer_speaking() is False

    def test_stop_during_the_answer_sends_the_orb_home_at_once(self):
        orb, played, now = _answer_orb()
        orb._api.tts.speaking = True
        orb._api._stop_requested.set()
        orb.tick()
        assert played[-1][-1][1] == (1700, 900, 82) and orb._answer is None

    def test_panels_close_eight_seconds_after_the_speech_ends(self):
        clock = [100.0]
        m = op.PanelModel(lambda: clock[0])
        m.on_event(events.Event(events.AGENT_STEP, {"action": "open_app"}))
        m.on_event(events.Event(events.AGENT_STEP, {"action": "done", "speech": "hi"}))
        clock[0] += 20                                     # a long answer, still speaking
        m.mark_speaking(True)
        assert not m.expired()
        clock[0] += op.ANSWER_S - 0.5
        assert not m.expired() and m.visible("answer", False)
        clock[0] += 1
        assert m.expired() and not m.visible("answer", False)


# ============================================================
# ORB LAYOUT
# ============================================================

# core/orb_layout.py: pure geometry for the floating orb and its panels
# (physical px; rects are (l, t, r, b), the orb a square (x, y, side)).


# ============================================================
# ORB LAYOUT HELPERS
# ============================================================

ORB_LAYOUT_W = (0, 0, 1920, 1040)


# -- panels anchored to the top of the target's monitor --------------------------------------

PANELS = [(240, 176), (280, 140)]                      # steps above answer
ORB_LAYOUT_WORK = (0, 0, 1920, 1040)


def _rects(spots, sizes=PANELS):
    return [(x, y, x + w, y + h) for (x, y), (w, h) in zip(spots, sizes)]


# ============================================================
# ORB LAYOUT TESTS
# ============================================================

class TestOrbLayout:
    def test_resize_about_center_rounds_to_whole_pixels(self):
        assert ol.resize_about_center(100, 100, 110, 94) == (108, 108, 94)
        assert ol.resize_about_center(100, 100, 110, 0) == (154, 154, 1)

    def test_frames_end_exactly_on_the_goal_and_bow_upward(self):
        path = ol.frames((0, 500, 110), (1000, 500, 36), n=12, bow=0.25)
        assert len(path) == 12 and path[-1] == (1000, 500, 36)
        mid = path[5]
        assert mid[1] < 500 - 100                         # well above the straight line
        assert all(p[1] <= 500 for p in path)
        sides = [p[2] for p in path]
        assert sides == sorted(sides, reverse=True)       # shrinks monotonically

    def test_frames_without_bow_are_a_straight_line(self):
        path = ol.frames((0, 0, 10), (100, 100, 10), n=4)
        assert all(abs(x - y) <= 1 for x, y, _ in path)

    @pytest.mark.parametrize("sq, want", [
        ((-50, -50, 40), (0, 0, 40)), ((1900, 1060, 40), (1880, 1040, 40)),
        ((10, 10, 5000), (10, 0, 1080)),
    ])
    def test_clamp_square_keeps_it_inside(self, sq, want):
        assert ol.clamp_square(*sq, (0, 0, 1920, 1080)) == want

    @pytest.mark.parametrize("target, side, where, pos", [
        ((100, 100, 1000, 800), 45, "right", (1008, 108)),
        ((900, 100, 1900, 800), 45, "left", (847, 108)),
        ((0, 300, 1920, 1000), 45, "above", (1867, 247)),
        ((0, 0, 1920, 700), 45, "below", (1867, 708)),
    ])
    def test_dock_prefers_right_then_left_then_above_then_below(self, target, side, where, pos):
        assert ol.dock_side(target, ORB_LAYOUT_W, side, 8) == where
        x, y = ol.dock_position(target, ORB_LAYOUT_W, side)
        assert (x, y) == pos
        assert not ol.overlaps((x, y, x + side, y + side), target)
        assert ORB_LAYOUT_W[0] <= x and ORB_LAYOUT_W[1] <= y and x + side <= ORB_LAYOUT_W[2] and y + side <= ORB_LAYOUT_W[3]

    def test_a_full_screen_target_falls_back_to_the_screen_edge(self):
        maximized = (-8, -8, 1928, 1048)                 # real maximized rects overhang
        assert ol.dock_side(maximized, ORB_LAYOUT_W, 45, 8) == "edge"
        x, y = ol.dock_position(maximized, ORB_LAYOUT_W, 45)
        assert (x, y) == (1867, 8)
        assert x + 45 <= ORB_LAYOUT_W[2] and y >= ORB_LAYOUT_W[1]

    def test_a_tiny_work_area_still_keeps_the_orb_inside(self):
        x, y = ol.dock_position((0, 0, 10, 10), (0, 0, 30, 30), 45)
        assert (x, y) == (0, 0)                          # side clamps to 30, fills the area
        x, y = ol.dock_position((0, 0, 20, 60), (0, 0, 90, 60), 45)
        assert ol.dock_side((0, 0, 20, 60), (0, 0, 90, 60), 45, 8) == "right"
        assert (x, y) == (28, 8) and x + 45 <= 90

    @pytest.mark.parametrize("work, target, want", [
        ((-1920, 0, 0, 1040), (-1800, 100, -900, 800), (-892, 108)),        # monitor on the left
        ((1920, -200, 3840, 880), (2000, -100, 3000, 600), (3008, -92)),     # right, shifted up
        ((1920, -200, 3840, 880), (1920, -200, 3840, 880), (3787, -192)),    # full screen there
    ])
    def test_multi_monitor_offsets(self, work, target, want):
        x, y = ol.dock_position(target, work, 45)
        assert (x, y) == want
        assert work[0] <= x and work[1] <= y and x + 45 <= work[2] and y + 45 <= work[3]

    def test_a_target_hanging_off_the_monitor_is_clipped_first(self):
        # A window half off the right edge: the right side has no room on this monitor.
        assert ol.dock_side((1500, 100, 2500, 800), ORB_LAYOUT_W, 45, 8) == "left"

    def test_never_over_the_target_when_any_side_fits_randomized(self):
        import random
        rnd = random.Random(1234)
        for _ in range(2000):
            wl, wt = rnd.randint(-3000, 3000), rnd.randint(-1500, 1500)
            work = (wl, wt, wl + rnd.randint(200, 3840), wt + rnd.randint(200, 2160))
            l = rnd.randint(work[0] - 50, work[2] - 10)
            t = rnd.randint(work[1] - 50, work[3] - 10)
            target = (l, t, l + rnd.randint(10, 4000), t + rnd.randint(10, 2500))
            side = rnd.choice([36, 45, 54])
            x, y = ol.dock_position(target, work, side)
            assert work[0] <= x and work[1] <= y and x + side <= work[2] and y + side <= work[3]
            if ol.dock_side(target, work, side, 8) != "edge":
                assert not ol.overlaps((x, y, x + side, y + side), target), (work, target)

    @pytest.mark.parametrize("anchor, xs", [
        ("top-right", [1664, 1624]), ("top-left", [16, 16]), ("top-center", [840, 820]),
    ])
    def test_each_anchor_stacks_down_from_the_top_with_inset_and_gap(self, anchor, xs):
        spots, used = ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, anchor)
        assert used == anchor and [x for x, _ in spots] == xs
        assert [y for _, y in spots] == [16, 16 + 176 + 12]

    @pytest.mark.parametrize("scale, inset, gap", [(1.0, 16, 12), (1.25, 20, 15), (1.5, 24, 18)])
    def test_inset_and_gap_scale_with_dpi(self, scale, inset, gap):
        sizes = [(round(240 * scale), round(176 * scale)), (round(280 * scale), round(140 * scale))]
        spots, _ = ol.anchored_panels(sizes, ORB_LAYOUT_WORK, "top-right", scale)
        assert spots[0] == (1920 - inset - sizes[0][0], inset)
        assert spots[1][1] == inset + sizes[0][1] + gap

    @pytest.mark.parametrize("orb", [(100, 900, 82), (1700, 900, 82), (900, 500, 28)])
    def test_the_position_does_not_depend_on_the_orb(self, orb):
        assert ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-right", orb=orb)[0] == \
            ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-right")[0]

    def test_panels_never_cover_the_orb(self):
        orb = (1700, 20, 82)                                # docked in the top-right corner
        spots, used = ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-right", orb=orb)
        assert not any(ol.overlaps(r, (1700, 20, 1782, 102)) for r in _rects(spots))
        assert used == "top-right" and spots[0][1] == 102 + 12   # moved below the orb

    def test_a_target_covering_the_corner_moves_the_panels_to_the_next_anchor(self):
        target = (1000, 0, 1920, 800)                       # covers the top-right
        spots, used = ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-right", target_rect=target)
        assert used == "top-left"
        assert not any(ol.overlaps(r, target) for r in _rects(spots))
        both = (0, 0, 700, 800), (1200, 0, 1920, 800)       # left and right covered: centre
        for t in both:
            assert ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-left", target_rect=t)[1] in (
                "top-left", "top-right", "top-center")
        wide = (0, 0, 1920, 600)                            # covers every anchor
        assert ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-center", target_rect=wide)[1] == "top-center"

    def test_a_maximized_target_keeps_the_configured_anchor(self):
        spots, used = ol.anchored_panels(PANELS, ORB_LAYOUT_WORK, "top-left", target_rect=(-8, -8, 1928, 1048),
                                         maximized=True)
        assert used == "top-left" and spots[0] == (16, 16)

    @pytest.mark.parametrize("work", [(-1920, 0, 0, 1040), (1920, -200, 3840, 880),
                                      (0, 0, 2560, 1400)])
    def test_multi_monitor_offsets(self, work):
        spots, _ = ol.anchored_panels(PANELS, work, "top-right")
        assert spots[0] == (work[2] - 16 - 240, work[1] + 16)
        for x, y, r, b in _rects(spots):
            assert work[0] <= x and r <= work[2] and work[1] <= y and b <= work[3]

    def test_a_small_work_area_keeps_every_panel_inside(self):
        work = (0, 0, 300, 250)
        spots, _ = ol.anchored_panels(PANELS, work, "top-right")
        for x, y, r, b in _rects(spots):
            assert 0 <= x and r <= 300 and 0 <= y and b <= 250

    def test_the_anchor_setting_is_read_at_call_time_and_validated(self, monkeypatch):
        from core.config import panel_anchor
        monkeypatch.delenv("MISTAI_PANEL_ANCHOR", raising=False)
        assert panel_anchor() == "top-right"
        for raw, want in (("top-left", "top-left"), (" TOP-CENTER ", "top-center"),
                          ("bottom-left", "top-right"), ("", "top-right")):
            monkeypatch.setenv("MISTAI_PANEL_ANCHOR", raw)
            assert panel_anchor() == want


# ============================================================
# ORB MOTION
# ============================================================

# Smooth, lively orb motion: the spring flight (core/orb_motion.py), the frame
# loop and pacing in core/floating_orb.Tween, frame stats, and the search
# tour's candidate counting. Fake clocks and a fake Win32 only: nothing moves
# on screen.


# ============================================================
# ORB MOTION HELPERS
# ============================================================

DISTANCES = (12, 60, 180, 250, 600, 1200, 2600)


# ============================================================
# ORB MOTION FLY
# ============================================================

def _fly(flight, dt=1 / 240, limit=3.0):
    """Sample a Flight every ``dt`` until it settles: (samples, settle time)."""
    t, out = flight.t0, []
    while t - flight.t0 < limit:
        x, y, done = flight.at(t)
        out.append((t, x, y))
        if done:
            return out, t - flight.t0
        t += dt
    raise AssertionError("never settled")


# -- the spring ----------------------------------------------------------------------------


# -- frame stats ----------------------------------------------------------------------------


# -- the frame loop (fake clock, fake Win32) ------------------------------------------------


# ============================================================
# ORB MOTION CLOCK
# ============================================================

class _OrbMotionClock:
    """Returns scripted times, then keeps stepping by ``step``."""

    def __init__(self, script=(), step=1 / 60, start=0.0):
        self.script, self.step, self.t = list(script), step, start

    def __call__(self):
        if self.script:
            self.t = self.script.pop(0)
        else:
            self.t += self.step
        return self.t


# ============================================================
# ORB MOTION PACER
# ============================================================

class _Pacer:
    kind = "fake"

    def __init__(self):
        self.waits = self.closed = 0

    def wait(self):
        self.waits += 1

    def close(self):
        self.closed += 1


# ============================================================
# ORB MOTION MOVE WIN
# ============================================================

class _MoveWin:
    def __init__(self, animations=True, hz=60):
        self.calls, self.animations, self.hz = [], animations, hz
        self.pacer = _Pacer()
        self.prio = []

    def move_only(self, hwnd, x, y):
        self.calls.append(("move_only", (x, y)))
        return True

    def move(self, hwnd, x, y, w, h):
        self.calls.append(("move", (x, y, w)))
        return True

    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return 1

    def set_region(self, hwnd, region):
        return True

    def delete_object(self, h):
        pass

    def animations_enabled(self):
        return self.animations

    def refresh_rate(self, hwnd):
        if self.hz is None:
            raise OSError("no")
        return self.hz

    def make_pacer(self, hz):
        self.pacer.hz = hz
        return self.pacer

    def boost_thread(self):
        self.prio.append("up")
        return 0

    def restore_thread(self, prev):
        self.prio.append(("restore", prev))


# ============================================================
# ORB MOTION PLAY
# ============================================================

def _play(win, segments, clock, side=None):
    tw = fo.Tween(win, 7, clock=clock)
    tw.side = side if side is not None else segments[0][0][2]
    seen, done = [], []
    tw.play(segments, on_frame=lambda sq: seen.append((clock.t, sq)),
            done=lambda: done.append(1)).join(5)
    return tw, seen, done


# -- the orb in flight ----------------------------------------------------------------------


# ============================================================
# ORB MOTION FLYING ORB
# ============================================================

def _flying_orb(goal_dest):
    from test_orb import _flight_orb

    orb, played = _flight_orb()
    calls = []
    orb._tween = SimpleNamespace(running=True, in_flight=True, goal=goal_dest,
                                 retarget=lambda g: calls.append(g) or True,
                                 play=lambda *a, **k: played.append(a))
    return orb, played, calls


# -- the search tour's candidates ------------------------------------------------------------


# ============================================================
# ORB MOTION HELPERS (2)
# ============================================================

def _page(rects, found=None, inner_h=800):
    return dict(screenX=0, screenY=0, outerWidth=1216, innerWidth=1200, outerHeight=900,
                innerHeight=inner_h, dpr=1.25, rects=rects,
                found=len(rects) if found is None else found)


WINDOW = (-10, -10, 1930, 1300)


# ============================================================
# ORB MOTION TOUR ORB
# ============================================================

def _orb_motion_tour_orb(*infos):
    from test_orb import _orb_fixes_tour_orb as make

    orb, played, now = make(infos[0])
    seq = list(infos)
    orb._api.browser.link_boxes = lambda sel, n: seq.pop(0) if len(seq) > 1 else seq[0]
    slept = []
    orb._sleep = slept.append
    return orb, slept


# ============================================================
# ORB MOTION HELPERS (3)
# ============================================================

ORB_MOTION_FOUR = [[20, 100 + 90 * i, 600, 124 + 90 * i, 0] for i in range(4)]


# ============================================================
# ORB MOTION TESTS
# ============================================================

class TestOrbMotion:
    @pytest.fixture(autouse=True)
    def _orb_motion_env(self, monkeypatch):
        for name in ("MISTAI_ORB_MOTION", "MISTAI_ORB_FRAME_STATS", "MISTAI_ORB_SIZES"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(fo.Tween, "_logged_animate", True)

    @pytest.mark.parametrize("motion, bound", [("lively", 0.6), ("calm", 0.65)])
    @pytest.mark.parametrize("dist", DISTANCES)
    def test_the_spring_settles_exactly_within_the_bound(self, motion, bound, dist):
        f = om.Flight((100, 500), (100 + dist, 400), 0.0, motion, bow=0.3)
        samples, settle = _fly(f)
        assert settle <= bound
        assert samples[-1][1:] == (100.0 + dist, 400.0)             # snapped exactly

    @pytest.mark.parametrize("dist", (100, 600, 2600))
    def test_lively_overshoots_a_little_and_calm_does_not(self, dist):
        lively, _ = _fly(om.Flight((0, 0), (dist, 0), 0.0, "lively"))
        calm, _ = _fly(om.Flight((0, 0), (dist, 0), 0.0, "calm"))
        over = max(x for _t, x, _y in lively) - dist
        assert 1.0 < over <= om.OVERSHOOT_PX + 0.5 and over < 0.12 * dist
        assert max(x for _t, x, _y in calm) <= dist + 1e-6
        assert min(x for _t, x, _y in calm) >= -1e-6                   # no anticipation either

    def test_long_lively_flights_lean_back_first_and_short_ones_do_not(self):
        long_, _ = _fly(om.Flight((0, 0), (800, 0), 0.0, "lively"))
        back = [x for t, x, _y in long_ if t <= om.ANTICIPATION_S]
        assert -om.ANTICIPATION_PX - 0.01 <= min(back) < -om.ANTICIPATION_PX * 0.9
        short, _ = _fly(om.Flight((0, 0), (150, 0), 0.0, "lively"))
        assert min(x for _t, x, _y in short) >= 0

    def test_most_of_a_long_flight_is_done_fast(self):
        for motion, by in (("lively", 0.3), ("calm", 0.4)):
            f = om.Flight((0, 0), (1500, 0), 0.0, motion)
            assert f.position(by)[0] >= 0.9 * 1500

    def test_the_arc_bows_upward_and_vanishes_at_both_ends(self):
        f = om.Flight((0, 800), (1000, 800), 0.0, "calm", bow=0.3)
        mids = [f.position(t)[1] for t in (0.1, 0.15, 0.2)]
        assert min(mids) < 800 - 40                                    # up (screen y grows down)
        assert f.position(0.0) == (0.0, 800.0)
        vertical = om.Flight((0, 0), (0, 900), 0.0, "calm", bow=0.3)
        assert all(abs(vertical.position(t)[0]) < 1e-9 for t in (0.05, 0.1, 0.2))

    def test_retargeting_keeps_position_and_velocity(self):
        for motion in ("lively", "calm"):
            f = om.Flight((0, 0), (900, 300), 0.0, motion, bow=0.3)
            t = 0.14
            pos, vel = f.position(t), f.velocity(t)
            f.retarget(t, (200, 900))
            assert f.position(t) == pytest.approx(pos, abs=1e-6)
            assert f.velocity(t + 2e-5) == pytest.approx(vel, rel=0.02, abs=20)
            samples, settle = _fly(f)
            assert samples[-1][1:] == (200.0, 900.0) and settle <= om.MAX_FLIGHT_S

    def test_a_retarget_has_no_jump_between_frames(self):
        f = om.Flight((0, 0), (1200, 0), 0.0, "lively", bow=0.3)
        dt, t, last, steps = 1 / 60, 0.0, None, []
        while t < 1.5:
            if abs(t - 0.2) < dt / 2:
                f.retarget(t, (1200, 700))
            x, y, done = f.at(t)
            if last is not None:
                steps.append(math.hypot(x - last[0], y - last[1]))
            last = (x, y)
            if done:
                break
            t += dt
        assert (x, y) == (1200.0, 700.0)
        around = steps[int(0.2 / dt) - 2:int(0.2 / dt) + 3]
        assert max(around) < 2.5 * max(steps[int(0.2 / dt) - 4:int(0.2 / dt) - 1])

    def test_the_path_depends_on_time_not_on_the_frame_count(self):
        smooth = om.Flight((10, 700), (1400, 120), 5.0, "lively", bow=0.3)
        choppy = om.Flight((10, 700), (1400, 120), 5.0, "lively", bow=0.3)
        times = [5.0, 5.016, 5.1, 5.117, 5.3, 5.31, 5.45]          # dropped frames between
        for t in times:
            assert choppy.at(t)[:2] == pytest.approx(smooth.position(t), abs=1e-9)
        assert _fly(smooth)[0][-1][1:] == _fly(choppy)[0][-1][1:] == (1400.0, 120.0)

    def test_spring_params_follow_the_motion_setting(self):
        zeta, omega = om.spring_params(600, "lively")
        assert 0.55 <= zeta <= 0.7 and om.LIVELY_MIN_OMEGA <= omega <= om.MAX_OMEGA
        assert om.spring_params(600, "calm")[0] == 1.0
        assert om.spring_params(2600, "lively")[1] >= om.spring_params(60, "lively")[1]

    def test_the_motion_and_stats_flags_are_read_at_call_time(self, monkeypatch):
        assert config.orb_motion() == "lively"
        monkeypatch.setenv("MISTAI_ORB_MOTION", "Calm")
        assert config.orb_motion() == "calm"
        monkeypatch.setenv("MISTAI_ORB_MOTION", "bouncy")
        assert config.orb_motion() == "lively"
        assert config.orb_frame_stats() is False
        monkeypatch.setenv("MISTAI_ORB_FRAME_STATS", "1")
        assert config.orb_frame_stats() is True
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert config.orb_frame_stats() is False                     # ignored in a built app

    def test_frame_stat_math(self):
        times = [0.0, 0.016, 0.033, 0.050, 0.100, 0.116]           # one 50 ms gap
        s = om.frame_stats(times, 60)
        assert s["frames"] == 6 and s["duration_ms"] == 116.0
        assert s["mean_ms"] == pytest.approx(23.2) and s["max_ms"] == 50.0
        assert s["p95_ms"] == 50.0 and s["slow"] == 1 and s["refresh"] == 60
        s144 = om.frame_stats(times, 144)                          # 1.5 x 6.9 ms = 10.4 ms
        assert s144["slow"] == 5 and s144["refresh"] == 144
        assert om.frame_stats([1.0], 60)["frames"] == 1 and om.frame_stats([], 60)["mean_ms"] == 0
        line = om.stats_line(s, "dwm", "lively")
        assert line.startswith("[FloatingOrb] frame stats frames 6 duration 116 ms interval mean 23.2")
        assert line.endswith("slow 1 refresh 60 Hz pacer dwm motion lively")

    @pytest.mark.parametrize("raw, hz", [(144, 144), (59.94, 60), (0, 60), (1, 60), (None, 60),
                                         ("x", 60), (1000, 60), (240, 240)])
    def test_the_refresh_rate_falls_back_to_60(self, raw, hz):
        assert om.valid_refresh(raw) == hz

    def test_win32_refresh_rate_falls_back_when_the_read_fails(self, monkeypatch):
        import win32api

        monkeypatch.setattr(win32api, "MonitorFromWindow",
                            lambda *a: (_ for _ in ()).throw(OSError("no")))
        assert fo.Win32().refresh_rate(1) == 60
        monkeypatch.setattr(win32api, "MonitorFromWindow", lambda *a: 5)
        monkeypatch.setattr(win32api, "GetMonitorInfo", lambda m: {"Device": "D1"})
        monkeypatch.setattr(win32api, "EnumDisplaySettings",
                            lambda dev, mode: SimpleNamespace(DisplayFrequency=1))
        assert fo.Win32().refresh_rate(1) == 60
        monkeypatch.setattr(win32api, "EnumDisplaySettings",
                            lambda dev, mode: SimpleNamespace(DisplayFrequency=165))
        assert fo.Win32().refresh_rate(1) == 165

    def test_a_move_frame_is_one_setwindowpos_and_nothing_else(self):
        win = _MoveWin()
        tw, seen, done = _play(win, [((1700, 900, 35), (300, 200, 35), 0.5, 0.3)], _OrbMotionClock())
        assert done == [1] and seen[-1][1] == (300, 200, 35)
        assert {c[0] for c in win.calls} == {"move_only"}            # no resize, no region
        assert win.pacer.waits >= 20 and win.pacer.closed == 1
        assert win.prio == ["up", ("restore", 0)]

    def test_tween_frames_follow_the_clock_even_with_dropped_frames(self):
        script = [0.0, 0.016, 0.033, 0.21, 0.226, 0.4]               # 0.033 -> 0.21: dropped
        win = _MoveWin()
        tw, seen, done = _play(win, [((0, 600, 35), (1300, 100, 35), 0.5, 0.3)], _OrbMotionClock(script))
        ref = om.Flight((0, 600), (1300, 100), 0.0, "lively", 0.3)
        for t, sq in seen:
            if t <= 0.4:
                x, y = ref.position(t)
                assert sq == (int(round(x)), int(round(y)), 35)
        assert seen[-1][1] == (1300, 100, 35)

    def test_resizes_stay_short_timed_steps_before_take_off_and_after_landing(self):
        win = _MoveWin()
        plan = ol.flight_plan((1700, 900, 82), (300, 200, 28))
        tw, seen, done = _play(win, plan, _OrbMotionClock())
        kinds = [c[0] for c in win.calls]
        first_move = kinds.index("move_only")
        assert "move" not in kinds[first_move:]                      # never resized in flight
        assert all(k in ("move", "ellipse") for k in kinds[:first_move])
        assert seen[-1][1] == (300, 200, 28)

    def test_animations_off_jumps_to_each_segment_end(self, capsys):
        win = _MoveWin(animations=False)
        plan = ol.flight_plan((1700, 900, 82), (300, 200, 28))
        tw, seen, done = _play(win, plan, _OrbMotionClock())
        assert [sq for _t, sq in seen] == [seg[1] for seg in plan]
        assert done == [1] and win.pacer.waits == 0
        assert capsys.readouterr().out.count("animation effects are off") == 1
        _play(win, plan, _OrbMotionClock())
        assert "animation effects are off" not in capsys.readouterr().out   # once, not per flight

    def test_retarget_redirects_the_move_in_flight_and_resizes_after_landing(self):
        gate, go = threading.Event(), threading.Event()

        class Clock(_OrbMotionClock):
            def __call__(self):
                t = super().__call__()
                if t > 0.15 and not gate.is_set():
                    gate.set()
                    go.wait(2)
                return t

        win, clock = _MoveWin(), Clock()
        tw = fo.Tween(win, 7, clock=clock)
        tw.side = 35
        seen = []
        th = tw.play([((0, 0, 35), (1000, 0, 35), 0.5, 0.0)], on_frame=seen.append)
        assert gate.wait(2) and tw.in_flight
        assert tw.retarget((600, 600, 28)) is True and tw.goal == (600, 600, 28)
        go.set()
        th.join(5)
        assert seen[-1] == (600, 600, 28)
        moves = [sq for sq in seen if sq[2] == 35]
        steps = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(moves, moves[1:])]
        ref = om.Flight((0, 0), (1000, 0), 0.0, "lively")             # the same flight, no retarget
        ref_pts = [ref.position(i / 60) for i in range(40)]
        ref_max = max(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(ref_pts, ref_pts[1:]))
        assert max(steps) <= ref_max * 1.1 + 2                        # no jump at the retarget
        assert [sq[2] for sq in seen].index(28) > len(moves) - 1      # resized only after landing
        assert tw.retarget((1, 1, 35)) is False                       # nothing flying now

    def test_frame_stats_are_logged_once_per_flight_only_when_asked(self, monkeypatch, capsys):
        win = _MoveWin(hz=None)                                       # refresh read fails: 60
        _play(win, [((0, 0, 35), (500, 0, 35), 0.5, 0.0)], _OrbMotionClock())
        assert "frame stats" not in capsys.readouterr().out
        monkeypatch.setenv("MISTAI_ORB_FRAME_STATS", "1")
        tw, seen, done = _play(win, [((0, 0, 35), (500, 0, 35), 0.5, 0.0)], _OrbMotionClock())
        out = [ln for ln in capsys.readouterr().out.splitlines() if "frame stats" in ln]
        assert len(out) == 1 and "refresh 60 Hz pacer fake motion lively" in out[0]
        assert tw.last_stats["frames"] >= 20 and tw.last_stats["slow"] == 0
        assert win.pacer.hz == 60

    def test_calm_motion_is_used_when_set(self, monkeypatch):
        monkeypatch.setenv("MISTAI_ORB_MOTION", "calm")
        win = _MoveWin()
        tw, seen, done = _play(win, [((0, 0, 35), (900, 0, 35), 0.5, 0.0)], _OrbMotionClock(step=1 / 240))
        assert max(sq[0] for _t, sq in seen) == 900 and min(sq[0] for _t, sq in seen) >= 0

    def test_the_flight_boost_is_restored(self, monkeypatch):
        before = sys.getswitchinterval()
        prev = fo._boost_enter(_MoveWin())
        inner = fo._boost_enter(_MoveWin())
        assert sys.getswitchinterval() == pytest.approx(fo.FLIGHT_SWITCH_INTERVAL)
        fo._boost_exit(_MoveWin(), inner)
        assert sys.getswitchinterval() == pytest.approx(fo.FLIGHT_SWITCH_INTERVAL)
        fo._boost_exit(_MoveWin(), prev)
        assert sys.getswitchinterval() == pytest.approx(before)

    def test_a_new_play_waits_for_the_old_thread(self):
        joined = []
        tw = fo.Tween(_MoveWin(), 7, clock=_OrbMotionClock())
        tw._thread = SimpleNamespace(is_alive=lambda: True, join=lambda s: joined.append(s))
        tw.play([]).join(2)
        assert joined == [fo.Tween.JOIN_S]

    def test_the_pacer_falls_back_from_dwmflush(self):
        class Now:
            t = 0.0

            def __call__(self):
                return self.t

        p = fo.FramePacer(60, clock=Now())
        p._dwm = lambda: 0                                            # returns at once: no vsync
        try:
            for _ in range(fo.FramePacer.FAST_LIMIT - 1):
                p.wait()
                assert p.kind == "dwm"
            p.wait()
            assert p.kind in ("timer", "sleep")
        finally:
            p.close()
        q = fo.FramePacer(60, clock=Now())
        q._dwm = lambda: -2147467259                                  # E_FAIL
        try:
            q.wait()
            assert q.kind in ("timer", "sleep")
        finally:
            q.close()

    def test_the_win32_move_flags(self):
        assert fo.SWP_FRAME == (fo.SWP_NOSIZE | fo.SWP_NOZORDER | fo.SWP_NOACTIVATE
                                | fo.SWP_NOOWNERZORDER | fo.SWP_ASYNCWINDOWPOS)
        assert not fo.SWP_FRAME & 0x0100                              # no SWP_NOCOPYBITS

    def test_a_goal_that_moves_in_flight_retargets_instead_of_waiting(self):
        orb, played, calls = _flying_orb((1008, 108, 35))             # already heading there
        orb.tick()
        assert calls == [] and played == []
        orb._win.target = (300, 300, 1200, 900)                       # the window moved
        orb.tick()
        assert calls and calls[-1] == (1208, 308, 35) and played == []

    def test_no_retarget_during_a_tour_or_a_resize_step(self):
        orb, played, calls = _flying_orb((5, 5, 45))
        orb._tour = {"stops": [], "i": 0, "next_at": 0.0}
        orb.tick()
        orb._tour = None
        orb._tween.in_flight = False
        orb.tick()
        assert calls == []

    def test_a_dock_dwell_starts_at_the_landing(self):
        orb, played, _calls = _flying_orb(None)
        now = [100.0]
        orb._clock = lambda: now[0]
        orb.dock = "right"
        now[0] = 103.0
        orb._landed()
        assert orb._dwell_until == pytest.approx(103.0 + orb.DWELL_S)
        orb.dock, orb._dwell_until = None, 0.0
        orb._landed()
        assert orb._dwell_until == 0.0                                # not a dock: no dwell

    def test_a_cancelled_tour_in_flight_is_retargeted_not_stopped(self):
        orb, played, calls = _flying_orb((5, 5, 45))
        cancelled = []
        orb._tween.cancel = lambda: cancelled.append(1)
        orb._tour = {"stops": [], "i": 0, "next_at": 0.0}
        orb._cancel_tour()
        assert orb._tour is None and cancelled == []
        orb.tick()
        assert calls                                                  # flies on to the dock

    def test_candidates_are_counted_accepted_and_rejected_by_label(self):
        rects = [[20, -40, 600, -10, 0],          # above the fold
                 [20, 100, 600, 124, 2],          # an ad
                 [20, 150, 600, 174, 0],          # ok 1
                 [20, 200, 600, 200, 0],          # zero height: hidden
                 [20, 250, 600, 274, 1],          # hidden by style
                 [20, 300, 600, 324, 0],          # ok 2
                 [20, 400, 600, 424, 0],          # ok 3
                 [20, 780, 600, 810, 0],          # ok 4: partly visible, clipped
                 [20, 500, 600, 524, 0],          # limit
                 [20, 900, 600, 924, 0]]          # below the fold
        got, counts = ol.classify_page_rects(_page(rects, found=14), WINDOW, 4)
        assert len(got) == 4 and counts["accepted"] == 4
        assert counts["found"] == 14 and counts["measured"] == 10 and counts["rejected"] == 6
        assert {k: counts[k] for k in ol.TOUR_REJECTIONS} == {
            "hidden": 2, "ad": 1, "above-fold": 1, "below-fold": 1, "outside-window": 0, "limit": 1}
        assert got[3][3] == round((100 + 800) * 1.25)                 # clipped to the viewport

    def test_a_rect_outside_the_window_voids_the_tour(self):
        got, counts = ol.classify_page_rects(_page([[20, 100, 600, 124, 0]]), (0, 0, 300, 300))
        assert got is None and counts["outside-window"] == 1 and counts["accepted"] == 0

    def test_bad_info_counts_nothing_and_never_raises(self):
        got, counts = ol.classify_page_rects({"dpr": "x"}, WINDOW)
        assert got is None and counts["found"] == 0 and counts["accepted"] == 0

    def test_the_tour_logs_the_counts_and_keeps_up_to_four(self, capsys):
        orb, slept = _orb_motion_tour_orb(_page(ORB_MOTION_FOUR + [[20, 700, 600, 724, 2]]))
        orb._prepare_tour(orb._tour_gen)
        out = capsys.readouterr().out
        assert len(orb._tour["stops"]) == 4 and slept == []
        assert ("[FloatingOrb] tour candidates found 5 measured 5 accepted 4 rejected 1 (ad 1)"
                in out)
        assert "tour stops 4" in out

    def test_a_page_still_rendering_is_read_once_more(self, capsys):
        orb, slept = _orb_motion_tour_orb(_page(ORB_MOTION_FOUR[:1]), _page(ORB_MOTION_FOUR))
        orb._prepare_tour(orb._tour_gen)
        assert slept == [fo.TOUR_RETRY_S] and len(orb._tour["stops"]) == 4
        assert "found 4 measured 4 accepted 4" in capsys.readouterr().out

    def test_nothing_usable_still_skips_the_tour(self, capsys):
        orb, slept = _orb_motion_tour_orb(_page([[20, 900, 600, 924, 0]] * 2))
        orb._prepare_tour(orb._tour_gen)
        out = capsys.readouterr().out
        assert orb._tour is None and "tour skipped (no-rects)" in out
        assert "accepted 0 rejected 2 (below-fold 2)" in out

    def test_the_link_box_script_reads_no_text(self):
        js = fo.Win32 and __import__("core.browser_session", fromlist=["x"]).BrowserSession._LINK_BOXES_JS
        for word in ("innerText", "textContent", "innerHTML", "title"):
            assert word not in js
        assert "found:" in js and "f |= 2" in js


# ============================================================
# ORB PANELS
# ============================================================

# Task panels beside the floating orb (core/orb_panels.py; Settings "Task
# panels" / MISTAI_ORB_PANELS, default off). Fakes only: no window is created.


# ============================================================
# ORB PANELS CLOCK
# ============================================================

class _OrbPanelsClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# ============================================================
# ORB PANELS HELPERS
# ============================================================

def _ev(name, **data):
    return events.Event(name, data)


# ============================================================
# ORB PANELS TESTS
# ============================================================

@pytest.mark.parametrize("action, label", [
    ("open_app", "Opening an app"), ("click_on_text", "Clicking"), ("type_text", "Typing"),
    ("press_key", "Typing"), ("read_screen_text", "Reading the screen"),
    ("analyze_screen", "Reading the screen"), ("run_command", "Running a command"),
    ("browser_get_text", "Reading the screen"), ("not_a_tool", "Working"), ("", "Working"),
])
def test_labels_come_from_the_tools_category_never_the_action_id(action, label):
    from core.tools import ToolRegistry
    api = SimpleNamespace(tools=ToolRegistry())
    got = op.label_for(action, op.tool_categories(api, action))
    assert got == label
    assert "_" not in got
    assert op.tool_categories(SimpleNamespace(), action) == ()      # never raises


def test_the_model_follows_steps_ticks_and_the_answer():
    clock = _OrbPanelsClock()
    cats = {"open_app": ("app",), "type_text": ("type",)}
    m = op.PanelModel(clock, lambda a: cats.get(a, ()))
    m.on_event(_ev(events.AGENT_STEP, action="open_app"))
    assert m.state("steps")["steps"] == [{"label": "Opening an app", "state": "run"}]
    m.on_event(_ev(events.ACTION_EXECUTED, action="open_app", parameter="secret"))
    m.on_event(_ev(events.AGENT_STEP, action="type_text"))
    m.on_event(_ev(events.ACTION_FAILED, action="type_text"))
    assert [s["state"] for s in m.state("steps")["steps"]] == ["ok", "fail"]
    assert m.visible("steps", True) and op.KINDS == ("steps", "answer")       # no pill
    assert not m.visible("answer", True)
    m.on_event(_ev(events.AGENT_STEP, action="done", speech="It says 42."))
    assert m.state("answer")["text"] == "It says 42." and m.visible("answer", False)
    assert m.visible("steps", False)                  # stays with the answer
    clock.t += op.ANSWER_S + 0.1
    assert not any(m.visible(k, False) for k in op.KINDS)


def test_steps_are_capped_and_a_bad_event_is_ignored():
    m = op.PanelModel(_OrbPanelsClock())
    for i in range(op.MAX_STEPS + 5):
        m.on_event(_ev(events.AGENT_STEP, action=f"a{i}"))
    assert len(m.state("steps")["steps"]) == op.MAX_STEPS
    m.on_event(SimpleNamespace(name=events.AGENT_STEP, data=None))
    m.on_event(object())                              # never raises


def test_the_bridge_exposes_only_its_own_state_and_close():
    panels = SimpleNamespace(model=op.PanelModel(_OrbPanelsClock()), side="left")
    bridge = op.PanelBridge(panels, "answer")
    assert [n for n in dir(bridge) if not n.startswith("_")] == ["dismiss", "panel_state"]
    assert bridge.panel_state() == {"kind": "answer", "text": "", "side": "left"}
    panels.model.answer = "hi"
    bridge.dismiss()
    assert panels.model.closed == {"answer"}
    panels.model.steps = [{"label": "Typing", "action": "type_text", "state": "run"}]
    assert not panels.model.visible("answer", True) and panels.model.visible("steps", True)


# -- windows ------------------------------------------------------------------------------


# ============================================================
# ORB PANELS WIN
# ============================================================

class _OrbPanelsWin:
    def __init__(self, dwm=0):
        self.calls, self.dwm = [], dwm

    def set_affinity(self, hwnd, mode):
        self.calls.append(("affinity", hwnd, mode))
        return True

    style = 0

    def add_exstyle(self, hwnd, bits):
        self.calls.append(("exstyle", hwnd, bits))

    def set_exstyle(self, hwnd, add=0, remove=0):
        self.calls.append(("exstyle", hwnd, add))
        self.style = (self.style | add) & ~remove

    def exstyle(self, hwnd):
        return self.style

    def move_only(self, hwnd, x, y):
        self.calls.append(("move_only", hwnd, (x, y)))
        return True

    rects = {}

    def window_rect(self, hwnd):
        return self.rects.get(hwnd)

    def dwm_set(self, hwnd, attr, value):
        self.calls.append(("dwm", attr, value))
        return self.dwm

    def create_round_rect(self, box):
        self.calls.append(("roundrect", box))
        return 0xBEEF

    def set_region(self, hwnd, region):
        return True

    def delete_object(self, h):
        pass

    def move(self, hwnd, x, y, w, h):
        self.calls.append(("move", hwnd, (x, y, w, h)))
        self.rects = dict(self.rects)
        self.rects[hwnd] = (x, y, x + w, y + h)
        return True

    def show(self, hwnd, on):
        self.calls.append(("show", hwnd, on))


# ============================================================
# ORB PANELS TESTS (2)
# ============================================================

def test_plain_style_is_a_rounded_region_and_blur_needs_the_flag_and_dwm():
    win = _OrbPanelsWin()
    assert op.harden_panel(7, win, 300, 44, 1.25, blur=False) == "plain"
    assert ("roundrect", (0, 0, 301, 45, 30, 30)) in win.calls
    assert ("exstyle", 7, fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE) in win.calls
    assert ("affinity", 7, fo.WDA_EXCLUDEFROMCAPTURE) in win.calls
    assert not any(c[0] == "dwm" for c in win.calls)
    assert op.harden_panel(7, _OrbPanelsWin(dwm=0), 300, 44, 1.0, blur=True) == "blur"
    assert op.harden_panel(7, _OrbPanelsWin(dwm=-2147024809), 300, 44, 1.0, blur=True) == "plain"


# ============================================================
# ORB PANELS HOOK
# ============================================================

class _OrbPanelsHook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


# ============================================================
# ORB PANELS WEBVIEW
# ============================================================

class _OrbPanelsWebview:
    def __init__(self):
        self.created = []

    def create_window(self, title, **kw):
        n = len(self.created)
        w = SimpleNamespace(title=title, kw=kw, events=SimpleNamespace(shown=_OrbPanelsHook()),
                            native=SimpleNamespace(Handle=SimpleNamespace(
                                ToInt32=lambda n=n: 500 + n)))
        self.created.append(w)
        return w


# ============================================================
# ORB PANELS BUS
# ============================================================

class _Bus:
    def __init__(self):
        self.subs = {}

    def subscribe(self, name, fn):
        self.subs.setdefault(name, []).append(fn)
        return lambda: None

    def publish(self, name, data=None, source=""):
        for fn in self.subs.get(name, []):
            fn(events.Event(name, data or {}, source))


# ============================================================
# ORB PANELS HELPERS (2)
# ============================================================

def _panels(monkeypatch, blur=False):
    monkeypatch.setattr(op, "panel_blur_enabled", lambda: blur)
    bus, wv, win, clock = _Bus(), _OrbPanelsWebview(), _OrbPanelsWin(), _OrbPanelsClock()
    panels = op.OrbPanels(SimpleNamespace(bus=bus), win, webview=wv, html="<p>", clock=clock)
    return panels, bus, wv, win, clock


def _show_all(wv):
    for w in wv.created:
        for fn in w.events.shown:
            fn()


ORB_PANELS_WORK = (0, 0, 1920, 1040)
TARGET = (100, 100, 1000, 800)
ORB = (1008, 108, 45)


# ============================================================
# ORB PANELS TESTS (3)
# ============================================================

def test_panels_open_untitled_unfocused_beside_the_orb_away_from_the_target(monkeypatch,
                                                                             capsys):
    panels, bus, wv, win, clock = _panels(monkeypatch)
    panels.update(ORB, TARGET, ORB_PANELS_WORK, 1.0, busy=True)
    assert len(wv.created) == 0                              # nothing before a step
    bus.publish(events.AGENT_STEP, {"action": "open_app"})
    panels.update(ORB, TARGET, ORB_PANELS_WORK, 1.0, busy=True)
    assert len(wv.created) == 1                              # steps
    for w in wv.created:
        kw = w.kw
        assert w.title == "" and kw["frameless"] and kw["on_top"] and not kw["focus"]
        assert isinstance(kw["js_api"], op.PanelBridge)
    _show_all(wv)
    assert sorted(panels.handles()) == [500]
    moves = [c[2] for c in win.calls if c[0] == "move"]
    for x, y, w, h in moves:
        assert not overlaps((x, y, x + w, y + h), TARGET)
        assert x >= ORB[0] + ORB[2]                          # right of the orb
    assert panels.side == "top-right"                        # anchored at the top
    bus.publish(events.AGENT_STEP, {"action": "done", "speech": "The total is 42."})
    panels.update(ORB, TARGET, ORB_PANELS_WORK, 1.0, busy=False)
    assert len(wv.created) == 2                              # + answer
    _show_all(wv)
    log = capsys.readouterr().out
    assert log.count("[OrbPanels] panel style: plain") == 1
    assert "42" not in log and "open_app" not in log        # content never logged
    clock.t += op.ANSWER_S + 1
    panels.update(ORB, TARGET, ORB_PANELS_WORK, 1.0, busy=False)
    assert panels.handles() == []                            # closed on their own
    assert ("show", 500, False) in win.calls


def test_a_new_task_clears_the_last_one(monkeypatch):
    panels, bus, wv, win, clock = _panels(monkeypatch)
    bus.publish(events.AGENT_STEP, {"action": "done", "speech": "old"})
    panels.update(ORB, None, ORB_PANELS_WORK, 1.0, busy=False)
    panels.update(ORB, None, ORB_PANELS_WORK, 1.0, busy=True)           # rising edge
    assert panels.model.answer == "" and panels.model.steps == []


def test_panels_are_part_of_the_orbs_handles_so_masks_and_input_guards_cover_them():
    orb = fo.FloatingOrb(SimpleNamespace(), webview=_OrbPanelsWebview(), html="<p>")
    orb.visible, orb.hwnd = True, 42
    orb.panels = SimpleNamespace(handles=lambda: [500, 501], hide_all=lambda: None)
    assert orb.handles() == [42, 500, 501]
    orb.hide()
    assert orb.handles() == []


def test_the_option_off_hides_panels_and_creates_none():
    orb = fo.FloatingOrb(SimpleNamespace(orb_option=lambda n: False), webview=_OrbPanelsWebview(),
                         html="<p>")
    orb._sync_panels(True)
    assert orb.panels is None
    hidden = []
    orb.panels = SimpleNamespace(hide_all=lambda: hidden.append(1))
    orb._sync_panels(True)
    assert hidden == [1]


# -- the agent event and the page -------------------------------------------------------

def test_the_agent_publishes_steps_and_the_final_speech():
    bus = _Bus()
    seen = []
    bus.subscribe(events.AGENT_STEP, lambda ev: seen.append(ev.data))
    loop = AgentLoop.__new__(AgentLoop)
    loop.api = SimpleNamespace(bus=bus)
    loop._publish_step("open_app")
    loop._publish_step("done", "Hello")
    assert seen == [{"action": "open_app"}, {"action": "done", "speech": "Hello"}]
    loop.api = SimpleNamespace()                             # no bus: never raises
    loop._publish_step("open_app")


def test_the_panel_page_is_local_calls_only_its_bridge_and_never_injects_html():
    assert not re.search(r"https?:|@import|url\(|<link|src=", PANEL_HTML)
    assert set(re.findall(r"pywebview\.api\.(\w+)", PANEL_HTML)) == {"panel_state", "dismiss"}
    assert "innerHTML" not in PANEL_HTML and "textContent" in PANEL_HTML
    assert "prefers-reduced-motion" in PANEL_HTML and "<title></title>" in PANEL_HTML


# ============================================================
# ORB STATES
# ============================================================

# The orb (ui/html.py): one visible state from what the page already knows.
# orbState: the push-to-talk mic wins, then Mist's voice (is_agent_busy's
# speaking), then a running task; else idle. setOrb writes it to the orb's
# data-state; CSS draws breathe / pulse / swirl / ripples, still under
# prefers-reduced-motion. Node tests skip when Node is missing.


# ============================================================
# ORB STATES HELPERS
# ============================================================

orb_states_needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _css():
    return HTML.split("<style>", 1)[1].split("</style>", 1)[0]


def _function(name):
    js = _script()
    start = js.index(f"function {name}(")
    return js[start:js.index("\n}", start) + 2]


# -- string checks -------------------------------------------------------------------


# ============================================================
# ORB STATES TESTS
# ============================================================

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

@orb_states_needs_node
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


# ============================================================
# ORB STATES HELPERS (2)
# ============================================================

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


# ============================================================
# ORB STATES TESTS (2)
# ============================================================

@orb_states_needs_node
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


# ============================================================
# ORB NINE STATES
# ============================================================

# The floating orb's nine states beyond idle (core/floating_orb.py ORB STATE
# SIGNALS, ORB SIGNAL MODEL, ORB STATE FROM PYTHON FACTS; ui/html.py ORB
# PAGE): content-free events.ORB_SIGNAL signals from PlanRunner, the loop and
# Api, published only while the orb is shown; the waiting and error status
# panels; every look in CSS, still under reduced motion or Windows animation
# effects off.


# ============================================================
# ORB NINE STATES HELPERS
# ============================================================

NINE_STATES = ("listening", "thinking", "planning", "executing", "reading", "waiting",
               "speaking", "completed", "error")


class _SyncBus:
    """Delivers every publish to its subscribers at once; records them."""

    def __init__(self):
        self.subs, self.sent = [], []

    def publish(self, name, data=None, source=""):
        self.sent.append((name, dict(data or {})))
        for want, fn in list(self.subs):
            if want in (None, name):
                fn(events.Event(name, data or {}, source))

    def subscribe(self, name, fn):
        entry = (name, fn)
        self.subs.append(entry)
        return lambda: self.subs.remove(entry) if entry in self.subs else None

    def signals(self):
        return [d for n, d in self.sent if n == events.ORB_SIGNAL]


def _sig_api(shown=True, panels=True, busy=True, ptt=False, speaking=False):
    api = _state_api(ptt=ptt, speaking=speaking, busy=busy)
    api.bus = _SyncBus()
    api._orb = SimpleNamespace(visible=shown)
    api.orb_option = lambda name: panels if name == "orb_panels" else False
    return api


def _signals(api, clock=None):
    model = fo.OrbSignals(clock or _OrbPanelsClock())
    api.bus.subscribe(None, model.on_event)
    return model


_PANEL_DOM = r"""
const realSetTimeout = setTimeout;
function el(tag) {
  return {tag, children: [], className: '', parentNode: null, _t: '',
    appendChild(c) { c.parentNode = this; this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter(x => x !== c); c.parentNode = null; },
    set textContent(v) { this._t = String(v); this.children = []; },
    get textContent() { return this._t + this.children.map(c => c.textContent).join(''); }};
}
const panel = el('div'), body = el('div'), close = el('button');
panel.appendChild(body); panel.appendChild(close);
const ids = {panel, body, close};
globalThis.document = {getElementById: (id) => ids[id], createElement: el};
const listeners = {};
globalThis.window = {addEventListener: (t, fn) => { listeners[t] = fn; }};
globalThis.setInterval = () => 1;
globalThis.pywebview = {api: {panel_state: () => Promise.resolve(%STATE%), dismiss: () => {}}};
%SCRIPT%
(async () => {
  listeners['pywebviewready']();
  await new Promise(r => realSetTimeout(r, 20));
  const texts = body.children.map(c => [c.className, c.textContent]);
  console.log(JSON.stringify({texts, closeLeft: panel.children.includes(close),
                              cls: panel.className}));
})();
"""


# ============================================================
# ORB NINE STATES TESTS
# ============================================================

@pytest.mark.parametrize("feed, want", [
    ([], "thinking"),
    ([("planning", True)], "planning"),
    ([("planning", True), ("planning", False)], "thinking"),
    ([("reading", True)], "reading"),
    ([("reading", True), ("reading", True), ("reading", False)], "reading"),   # nested
    ([("reading", True), ("reading", False), ("reading", False)], "thinking"),  # never < 0
    ([("planning", True), ("reading", True)], "reading"),
    ([("waiting", True)], "waiting"),
    ([("waiting", True), ("waiting", False)], "thinking"),
    ([("step", "open_app")], "executing"),
    ([("step", "open_app"), ("executed", "open_app")], "thinking"),
    ([("step", "open_app"), ("waiting", True)], "waiting"),
    ([("step", "done")], "thinking"),
])
def test_signals_map_to_states(feed, want):
    api = _sig_api()
    model = _signals(api)
    for kind, value in feed:
        if kind == "step":
            api.bus.publish(events.AGENT_STEP, {"action": value})
        elif kind == "executed":
            api.bus.publish(events.ACTION_EXECUTED, {"action": value})
        else:
            fo.orb_signal(api, kind, value)
    assert fo.orb_state(api, model) == want


def test_state_priorities_over_the_signals():
    api = _sig_api(ptt=True)
    model = _signals(api)
    fo.orb_signal(api, "waiting", True)
    assert fo.orb_state(api, model) == "listening"             # the mic wins
    api = _sig_api(speaking=True)
    model = _signals(api)
    fo.orb_signal(api, "waiting", True)
    assert fo.orb_state(api, model) == "waiting"               # an open question beats the voice
    fo.orb_signal(api, "waiting", False)
    fo.orb_signal(api, "reading", True)
    assert fo.orb_state(api, model) == "speaking"
    api = _sig_api()
    model = _signals(api)
    assert fo.orb_state(api, model, docked=True) == "executing"   # docked beside the target
    api = _sig_api(busy=False)
    model = _signals(api)
    fo.orb_signal(api, "reading", True)
    fo.orb_signal(api, "waiting", True)
    assert fo.orb_state(api, model) == "idle"                  # no task: stale flags ignored


@pytest.mark.parametrize("result, outcome, hold", [
    ({"success": True, "speech": "Done."}, "completed", fo.COMPLETED_S),
    ({"success": False, "error": "Hit max steps (15)"}, "error", fo.ERROR_S),
])
def test_completed_and_error_return_to_idle(result, outcome, hold):
    clock = _OrbPanelsClock()
    api = _sig_api()
    model = _signals(api, clock)
    fo.orb_signal(api, "started")
    fo.task_finished(api, result)
    api._agent_lock.release()                                  # the task is over
    assert fo.orb_state(api, model) == outcome
    clock.t += hold - 0.1
    assert fo.orb_state(api, model) == outcome
    clock.t += 0.2
    assert fo.orb_state(api, model) == "idle"
    assert model.outcome is None


def test_the_outcome_is_held_from_the_end_of_speech():
    clock = _OrbPanelsClock()
    api = _sig_api(busy=False, speaking=True)
    model = _signals(api, clock)
    fo.task_finished(api, {"success": True})
    clock.t += 10
    assert fo.orb_state(api, model) == "speaking"              # the answer is being spoken
    api.tts.speaking = False
    clock.t += 1
    assert fo.orb_state(api, model) == "completed"             # held from the end of speech
    clock.t += fo.COMPLETED_S
    assert fo.orb_state(api, model) == "idle"


def test_a_stop_is_not_an_error_and_a_new_task_clears_the_outcome():
    api = _sig_api(busy=False)
    model = _signals(api)
    fo.task_finished(api, {"success": False, "error": "Stopped by user"})
    assert fo.orb_state(api, model) == "idle"
    fo.task_finished(api, {"success": False, "error": "x"})
    assert fo.orb_state(api, model) == "error"
    fo.orb_signal(api, "started")
    assert fo.orb_state(api, model) == "idle"


@pytest.mark.parametrize("result, want", [
    (None, ("error", "crash")),
    ({"success": True}, ("completed", "")),
    ({"success": False, "paused": True, "error": "Stopped -- progress saved"}, ("stopped", "")),
    ({"success": False, "error": "Stopped by user"}, ("stopped", "")),
    ({"success": False, "error": "I couldn't reach my server, so I stopped. Please try again in a moment."},
     ("error", "network")),
    ({"success": False, "error": "Agent call failed (ValueError)"}, ("error", "network")),
    ({"success": False, "error": "not confirmed"}, ("error", "declined")),
    ({"success": False, "error": "User declined the requested action."}, ("error", "declined")),
    ({"success": False, "error": "No response to question -- task paused"}, ("error", "no_reply")),
    ({"success": False, "error": "no answer"}, ("error", "no_answer")),
    ({"success": False, "error": "loop"}, ("error", "stuck")),
    ({"success": False, "error": "Hit max steps (15)"}, ("error", "steps")),
    ({"success": False, "error": "1 phase(s) failed, 0 blocked, 1 completed"}, ("error", "phases")),
    ({"success": False, "error": "Could not parse LLM response"}, ("error", "model")),
    ({"success": False, "error": "something new"}, ("error", "failed")),
])
def test_task_outcomes_and_reasons(result, want):
    assert fo.task_outcome(result) == want
    assert want[1] == "" or want[1] in fo.ERROR_REASONS


def test_the_agent_error_strings_are_the_ones_the_outcome_knows():
    from core import agent

    assert fo.task_outcome({"error": agent.NETWORK_FAILURE_SPEECH}) == ("error", "network")
    assert fo.task_outcome({"error": agent.NOT_CONFIRMED_ERROR}) == ("error", "declined")


def test_the_network_sentence_makes_the_error_reason_network():
    api = _sig_api(busy=False)
    model = _signals(api)
    api.bus.publish(events.AGENT_STEP, {"action": "failed"})
    fo.task_finished(api, {"success": False, "error": "whatever"})
    assert model.outcome == "error" and model.reason == "network"


def test_nothing_is_published_while_the_orb_is_hidden():
    hidden = _sig_api(shown=False)
    with fo.signal_scope(hidden, "reading"):
        pass
    fo.orb_signal(hidden, "started")
    fo.task_finished(hidden, {"success": True})
    with fo.waiting_scope(hidden, "confirm", "voice", "Send it?"):
        pass
    assert hidden.bus.sent == []
    no_orb = SimpleNamespace(bus=_SyncBus())                   # the orb never created
    fo.orb_signal(no_orb, "started")
    assert no_orb.bus.sent == []
    shown = _sig_api()
    fo.orb_signal(shown, "bogus")                              # unknown names: nothing
    assert shown.bus.sent == []


def test_scopes_turn_off_even_when_the_body_raises():
    api = _sig_api()
    with pytest.raises(RuntimeError):
        with fo.signal_scope(api, "reading"):
            raise RuntimeError("boom")
    assert api.bus.signals() == [{"signal": "reading", "on": True},
                                 {"signal": "reading", "on": False}]


@pytest.mark.parametrize("panels", [True, False])
def test_waiting_carries_the_question_only_with_task_panels(panels):
    api = _sig_api(panels=panels)
    with fo.waiting_scope(api, "confirm", "voice", "Send hi to Ada?"):
        pass
    first = api.bus.signals()[0]
    assert first["kind"] == "confirm" and first["mode"] == "voice"
    assert ("question" in first) is panels
    if panels:
        assert first["question"] == "Send hi to Ada?"
    assert api.bus.signals()[1] == {"signal": "waiting", "on": False}


def test_ask_confirmation_sets_and_clears_waiting():
    seen = []
    api = Api.__new__(Api)
    api._answer_lock, api._answer_event = threading.Lock(), threading.Event()
    api._awaiting_text_answer = threading.Event()
    api._pending_answer = api._pending_source = None
    api._notify_js = lambda js: None
    api._window = None
    api.speak = lambda t: None
    api.tts = SimpleNamespace(speaking=False)
    api._voice_start = api._voice_mark = lambda *x, **k: None
    api.bus = _SyncBus()
    api._orb = SimpleNamespace(visible=True)
    api.orb_option = lambda name: True
    model = _signals(api)
    lock = threading.Lock()
    lock.acquire()
    api._agent_lock, api._ptt_wants_mic = lock, lambda: False

    def listen(**_kw):
        seen.append(fo.orb_state(api, model))
        return "yes"

    api._listen_once = listen
    assert Api.ask_confirmation(api, "Delete the file?", "voice") == ("yes", "voice")
    assert seen == ["waiting"]
    assert fo.orb_state(api, model) == "thinking"             # cleared when answered
    sig = api.bus.signals()
    assert [s["on"] for s in sig] == [True, False] and sig[0]["question"] == "Delete the file?"


def test_the_loop_and_api_publish_at_their_choke_points():
    src = inspect.getsource(AgentLoop._loop_steps)
    i = src.index('waiting_scope(self.api, "ask", input_mode, question)')
    block = src[i:i + 900]
    assert "_get_text_answer(question" in block and "_listen_once(" in block
    j = src.index('with signal_scope(self.api, "reading"):   # vision or UIA')
    assert "self._get_screen_context()" in src[j:j + 200]
    k = src.index('with signal_scope(self.api, "reading"):\n')
    assert "_get_screen_context(lazy=1)" in src[k:k + 200]
    api_src = inspect.getsource(Api._exec_sync)
    a = api_src.index("with signal_scope(self, \"reading\"):     # the orb's reading look")
    assert "self.analyze_screen()" in api_src[a:a + 120]
    b = api_src.index('with signal_scope(self, "reading"):\n')
    assert "self.read_screen_text(param)" in api_src[b:b + 120]
    confirm = inspect.getsource(Api.ask_confirmation)
    assert 'waiting_scope(self, "confirm", input_mode, question)' in confirm
    assert confirm.index("waiting.__exit__") > confirm.index("finally:")


def test_signals_and_models_never_print(capsys):
    api = _sig_api()
    model = _signals(api)
    panels = op.PanelModel(_OrbPanelsClock())
    api.bus.subscribe(None, panels.on_event)
    with fo.waiting_scope(api, "ask", "text", "SECRET-QUESTION-42"):
        fo.orb_state(api, model)
    with fo.signal_scope(api, "reading"):
        pass
    api.bus.publish(events.AGENT_STEP, {"action": "done", "speech": "SECRET-ANSWER-42"})
    fo.task_finished(api, {"success": False, "error": "SECRET-ERROR-42"})
    fo.orb_state(api, model)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


@pytest.mark.parametrize("state, docked, want", [
    ("idle", False, 82), ("listening", False, 82), ("thinking", False, 70),
    ("planning", False, 70), ("reading", False, 70), ("waiting", False, 70),
    ("speaking", False, 70), ("completed", False, 70), ("error", False, 70),
    ("executing", False, 70), ("executing", True, 28), ("waiting", True, 28),
])
def test_each_of_the_nine_states_has_its_size(monkeypatch, state, docked, want):
    monkeypatch.delenv("MISTAI_ORB_SIZES", raising=False)
    assert fo.size_for(state, docked) == want


def test_the_bridge_reports_still_when_windows_animations_are_off():
    api = _sig_api()
    orb = fo.FloatingOrb(api)
    orb._win = SimpleNamespace(animations_enabled=lambda: False)
    bridge = fo.OrbBridge(api, orb)
    assert bridge.orb_state() == {"state": "thinking", "still": True}
    orb._win, orb._anim = SimpleNamespace(animations_enabled=lambda: True), (True, 0.0)
    assert bridge.orb_state() == {"state": "thinking"}
    assert [n for n in dir(bridge) if not n.startswith("_")] == ["orb_state", "talk"]


def test_the_orb_goes_home_medium_after_an_error_with_flight_on():
    orb, played = _orb()
    orb.square = (1008, 108, 28)
    orb._was_busy = True
    finished = events.Event(events.ORB_SIGNAL, {"signal": "finished", "on": True,
                                                "outcome": "error", "reason": "steps"})
    orb._on_bus(finished)
    orb.signals.on_event(finished)
    assert orb.aborted is True
    orb.tick()
    hx, hy, hs = orb.home
    x, y, side = played[0][-1][1]
    assert side == round(fo.orb_sizes()[1] * orb.scale)             # medium at home
    assert abs((x + side / 2) - (hx + hs / 2)) <= 1 and abs((y + side / 2) - (hy + hs / 2)) <= 1


# -- panels -----------------------------------------------------------------------------

def test_the_waiting_panel_shows_the_question_and_a_hint():
    api = _sig_api()
    m = op.PanelModel(_OrbPanelsClock())
    api.bus.subscribe(None, m.on_event)
    with fo.waiting_scope(api, "confirm", "voice", "Send hi to Ada?"):
        assert m.visible("waiting", True)
        assert m.state("waiting") == {"kind": "waiting", "text": "Send hi to Ada?",
                                      "hint": "Say yes or no."}
    assert not m.visible("waiting", True)
    with fo.waiting_scope(api, "ask", "text", "Which folder?"):
        assert m.state("waiting")["hint"] == "Type your answer in MistAI."


def test_the_error_panel_shows_a_plain_reason_and_closes():
    clock = _OrbPanelsClock()
    api = _sig_api()
    m = op.PanelModel(clock)
    api.bus.subscribe(None, m.on_event)
    api.bus.publish(events.AGENT_STEP, {"action": "open_app"})
    api.bus.publish(events.AGENT_STEP, {"action": "failed"})     # the network sentence
    fo.task_finished(api, {"success": False, "error": "Agent call failed (OSError)"})
    assert m.ended and not m.visible("steps", False)            # task panels close as before
    assert m.visible("error", False)
    assert m.state("error") == {"kind": "error", "text": fo.ERROR_REASONS["network"]}
    clock.t += fo.ERROR_S + 0.1
    assert not m.visible("error", False)


def test_the_status_panels_cannot_be_dismissed_and_kinds_stay_put():
    panels = SimpleNamespace(model=op.PanelModel(), side="top-right")
    assert op.PanelBridge(panels, "waiting").dismiss() == {"ok": False}
    assert op.PanelBridge(panels, "error").dismiss() == {"ok": False}
    assert op.PanelBridge(panels, "steps").dismiss() == {"ok": True}
    assert op.KINDS == ("steps", "answer")
    assert op.ALL_KINDS == ("waiting", "error", "steps", "answer")


def test_the_waiting_panel_window_appears_through_the_real_bus(monkeypatch):
    from core.live_state import EventBus

    monkeypatch.setattr(op, "panel_blur_enabled", lambda: False)
    bus = EventBus()
    try:
        api = SimpleNamespace(bus=bus, _orb=SimpleNamespace(visible=True),
                              orb_option=lambda name: True)
        wv, win = _OrbFixesWebview(), _PanelWin()
        panels = op.OrbPanels(api, win, webview=wv, html="<p>")
        panels.update((1000, 100, 35), None, ORB_FIXES_W, 1.0, busy=True)
        fo.orb_signal(api, "waiting", True, kind="confirm", mode="text", question="Go on?")
        deadline = time.monotonic() + 3
        while panels.model.waiting is None and time.monotonic() < deadline:
            time.sleep(0.01)
        panels.update((1000, 100, 35), None, ORB_FIXES_W, 1.0, busy=True)
        assert [w.kw["js_api"]._kind for w in wv.created] == ["waiting"]
        assert panels.model.state("waiting")["text"] == "Go on?"
    finally:
        bus.stop()


# -- pages ------------------------------------------------------------------------------

def test_the_orb_page_has_a_look_for_each_of_the_nine_states():
    css = ORB_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    for state in NINE_STATES:
        assert f'.orb-wrap[data-state="{state}"]' in css, state
    script = ORB_HTML.split("<script>", 1)[1]
    for state in ("idle",) + NINE_STATES:
        assert f"'{state}'" in script
    # shape or glyph, not colour alone
    for cls in ("g-pause", "g-check", "g-excl", "nodes", "bars", "scan", "halo", "burst"):
        assert re.search(rf'class="[^"]*\b{cls}\b', ORB_HTML), cls
    burst = css.split('.orb-wrap[data-state="completed"] .burst{', 1)[1].split("}", 1)[0]
    shake = css.split('.orb-wrap[data-state="error"]{', 1)[1].split("}", 1)[0]
    assert "infinite" not in burst and " 1 " in burst          # one burst, then calm
    assert "infinite" not in shake and " 1" in shake           # one shake, then steady


def test_reduced_motion_and_windows_animations_off_freeze_every_state():
    css = ORB_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    reduced = css.split("@media (prefers-reduced-motion: reduce){", 1)[1]
    still = '.orb-wrap[data-motion="still"]'
    for block, w in ((reduced, ".orb-wrap"), (css, still)):
        assert f"{w},{w} .disc,{w} .ring" in block and "animation:none !important" in block
        assert f'{w}[data-state="reading"] .scan{{opacity:.9' in block
        assert f'{w}[data-state="planning"] .nodes i:nth-child(2){{opacity:1}}' in block
    # glyphs and tints are not animations: they stay in both
    assert '[data-state="waiting"] .g-pause{display:flex}' in css
    assert '[data-state="error"] .tint{opacity:1' in css


@floating_orb_window_needs_node
def test_the_orb_page_shows_the_bridge_state_and_still_flag(tmp_path):
    script = ORB_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    js = r"""
const realSetTimeout = setTimeout;
const replies = [{state: 'reading'}, {state: 'error', still: true}, {state: 'nope'}];
const wrap = {dataset: {}};
globalThis.document = {getElementById: () => wrap, addEventListener: () => {}};
const listeners = {};
let poll = null;
globalThis.window = {addEventListener: (t, fn) => { listeners[t] = fn; }};
globalThis.setInterval = (fn) => { poll = fn; return 1; };
globalThis.pywebview = {api: {orb_state: () => Promise.resolve(replies.shift())}};
""" + script + r"""
(async () => {
  const seen = [];
  listeners['pywebviewready']();
  for (let i = 0; i < 3; i++) {
    await new Promise(r => realSetTimeout(r, 10));
    seen.push([wrap.dataset.state, wrap.dataset.motion]);
    poll();
  }
  console.log(JSON.stringify(seen));
})();
"""
    f = tmp_path / "orb_nine.js"
    f.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [["reading", "on"], ["error", "still"], ["idle", "on"]]


@floating_orb_window_needs_node
@pytest.mark.parametrize("state, texts, close_left", [
    ({"kind": "waiting", "text": "Send hi to <b>Ada</b>?", "hint": "Say yes or no."},
     [["glyph", "❚❚"], ["q", "Send hi to <b>Ada</b>?"], ["hint", "Say yes or no."]],
     False),
    ({"kind": "error", "text": "I ran out of steps before finishing."},
     [["glyph", "!"], ["q", "I ran out of steps before finishing."]], False),
    ({"kind": "answer", "text": "It says 42."}, None, True),
])
def test_the_panel_page_renders_the_status_kinds_display_only(tmp_path, state, texts, close_left):
    script = PANEL_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    js = _PANEL_DOM.replace("%STATE%", json.dumps(state)).replace("%SCRIPT%", script)
    f = tmp_path / "panel.js"
    f.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30,
                         encoding="utf-8")
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["closeLeft"] is close_left
    if texts is not None:
        assert res["texts"] == texts                       # text exactly as given, as text
        assert res["cls"] == "panel status " + state["kind"]
