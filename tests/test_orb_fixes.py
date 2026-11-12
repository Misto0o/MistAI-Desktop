"""Fixes to the floating orb + panels prototype: real window sizes (pywebview's
min_size clamp), taskbar/Alt-Tab hiding, smooth move-only flights, the orb's
background, MISTAI_ORB_SIZES, following the task (maximized targets and
element rects), panel placement/labels/closing, MISTAI_ORB_DEBUG_CAPTURE.
Fakes only: no window is created."""

import inspect
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import core.floating_orb as fo
import core.orb_panels as op
from core import config, events
from core import orb_layout as ol
from core.agent import AgentLoop
from ui.html import ORB_HTML, PANEL_HTML


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("MISTAI_ORB_SIZES", "MISTAI_ORB_DEBUG_CAPTURE"):
        monkeypatch.delenv(name, raising=False)


# -- 0. the window is the size we ask for ---------------------------------------------------

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


class _Hook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class _Webview:
    def __init__(self):
        self.created = []

    def create_window(self, title, **kw):
        w = SimpleNamespace(title=title, kw=kw, events=SimpleNamespace(shown=_Hook()),
                            native=SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 77)))
        self.created.append(w)
        return w


def _created_min_size(make):
    wv = _Webview()
    make(wv)
    # pywebview's own default when min_size is not passed
    return wv.created[0].kw.get("min_size", (200, 100))


def test_the_orb_window_gets_the_size_it_asks_for(monkeypatch, capsys):
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


def test_the_size_check_catches_a_clamp(capsys):
    win = _ClampWin((200, 100))                              # pywebview's default
    out = fo.harden(7, win=win)
    assert out["position"][2:4] == (250, 125)                # built from the ACTUAL size
    assert ("ellipse", (0, 0, 251, 126)) in win.calls
    log = capsys.readouterr().out
    assert "requested 102,102 actual 250,125" in log
    assert "actual size differs from the requested size" in log


def test_panels_are_created_without_the_min_size_clamp_and_log_actual_sizes(monkeypatch,
                                                                           capsys):
    wv = _Webview()
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


# -- 1. taskbar and Alt-Tab -------------------------------------------------------------------

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


def test_toolwindow_is_applied_and_the_window_re_shown_so_the_taskbar_drops_it(capsys):
    win = _PanelWin()
    assert fo.hide_from_taskbar(7, win, "FloatingOrb") is True
    assert win.style & fo.WS_EX_TOOLWINDOW and win.style & fo.WS_EX_NOACTIVATE
    assert not win.style & fo.WS_EX_APPWINDOW
    shows = [c[1] for c in win.calls if c[0] == "show"]
    assert shows == [False, True]                        # hidden, then shown again
    assert win.calls.index(("set_exstyle", fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE,
                            fo.WS_EX_APPWINDOW)) < win.calls.index(("show", False))
    assert "[FloatingOrb] taskbar: hidden" in capsys.readouterr().out


def test_a_style_that_did_not_take_is_reported(capsys):
    win = _PanelWin()
    win.set_exstyle = lambda hwnd, add=0, remove=0: None       # the bits never stick
    assert fo.hide_from_taskbar(7, win, "OrbPanels") is False
    assert "[OrbPanels] taskbar: still listed" in capsys.readouterr().out


def test_every_panel_gets_the_same_extended_styles():
    win = _PanelWin()
    op.harden_panel(9, win, 190, 30, 1.0, blur=False)
    assert win.style & fo.WS_EX_TOOLWINDOW and win.style & fo.WS_EX_NOACTIVATE
    assert not win.style & fo.WS_EX_APPWINDOW
    assert [c[1] for c in win.calls if c[0] == "show"] == [False, True]


# -- 2. smooth movement -----------------------------------------------------------------------

class _CountWin(_PanelWin):
    def create_ellipse(self, box):
        self.calls.append(("ellipse", box))
        return 1


def test_frames_with_the_same_size_only_move_the_window():
    win = _CountWin()
    assert fo.apply_square(win, 7, 10, 20, 40, last_side=40) is True
    assert win.calls == [("move_only", (10, 20))]
    win.calls.clear()
    fo.apply_square(win, 7, 10, 20, 30, last_side=40)
    assert [c[0] for c in win.calls] == ["move", "ellipse"]


def test_a_flight_recuts_the_region_only_when_the_size_changes(monkeypatch):
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


def test_flight_plans_resize_before_or_after_the_move_never_during():
    shrink = ol.flight_plan((1700, 900, 82), (300, 200, 28))
    assert [seg[0][2] == seg[1][2] for seg in shrink] == [False, True]
    grow = ol.flight_plan((300, 200, 28), (1700, 900, 82))
    assert [seg[0][2] == seg[1][2] for seg in grow] == [True, False]
    assert grow[-1][1] == (1700, 900, 82) and shrink[-1][1] == (300, 200, 28)
    near = ol.flight_plan((100, 100, 82), (106, 106, 70))
    assert len(near) == 1 and near[0][3] == 0.0


def test_square_at_is_eased_and_exact_at_the_ends():
    a, b = (0, 0, 10), (100, 0, 10)
    assert ol.square_at(a, b, 0) == a and ol.square_at(a, b, 1) == b
    assert ol.square_at(a, b, 2) == b and ol.square_at(a, b, -1) == a
    assert ol.square_at(a, b, 0.1)[0] < 10             # eases in


# -- 3. background ----------------------------------------------------------------------------

def test_the_orb_page_and_window_share_the_rim_colour_and_the_disc_fills_it():
    bg = config.orb_background()
    assert bg == "#203247"
    assert f"background:{bg}" in ORB_HTML
    assert "width:100vmin;height:100vmin" in ORB_HTML
    assert "#05080f" not in ORB_HTML
    assert "background:#161c26" in PANEL_HTML and op.PANEL_BACKGROUND == "#161c26"


# -- 4. sizes -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    (None, (82, 70, 28)), ("110,94,36", (110, 94, 36)), (" 60 , 50 , 20 ", (60, 50, 20)),
    ("1,2,3", (82, 70, 28)), ("50,60,20", (82, 70, 28)), ("a,b,c", (82, 70, 28)),
    ("80,70", (82, 70, 28)), ("", (82, 70, 28)), ("500,70,28", (82, 70, 28)),
])
def test_orb_sizes_are_read_at_call_time_and_validated(monkeypatch, raw, want):
    if raw is not None:
        monkeypatch.setenv("MISTAI_ORB_SIZES", raw)
    assert config.orb_sizes() == want


@pytest.mark.parametrize("scale, big, medium, small", [
    (1.0, 82, 70, 28), (1.25, 102, 88, 35), (1.5, 123, 105, 42),
])
def test_default_sizes_are_dpi_scaled(scale, big, medium, small):
    _x, _y, side = fo.place((0, 0, 1920, 1080), scale)
    assert side == big
    assert round(fo.size_for("thinking") * scale) == medium
    assert round(fo.size_for("idle", docked=True) * scale) == small


# -- 5 / 5b. following the task -----------------------------------------------------------------

W = (0, 0, 1920, 1040)


class _FlightWin:
    def __init__(self, target, fg=55):
        self.target, self.fg, self.styles = target, fg, []

    def foreground(self):
        return self.fg

    def window_rect(self, hwnd):
        return self.target if hwnd == self.fg else (1700, 900, 1782, 982)

    def monitor_for_rect(self, rect):
        return W, 1.0

    def set_click_through(self, hwnd, on):
        self.styles.append(on)

    def is_minimized(self, hwnd):
        return True

    def window_info(self, hwnd):
        return (1234, "studio64.exe", "SunAwtFrame")

    def browser_window(self):
        return None


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
    orb = fo.FloatingOrb(api, webview=_Webview(), html="<p>")
    orb.visible, orb.hwnd, orb.scale = True, 4242, 1.0
    orb.square = orb.home = (1700, 900, 82)
    orb._win = _FlightWin(target)
    orb._adopt_drag = lambda: None                   # no user drag in these tests
    orb._acted, orb._was_busy = True, busy           # mid-task: a step has already run
    played = []
    orb._tween = SimpleNamespace(running=False, side=82,
                                 play=lambda plan, on_frame=None, done=None: played.append(plan))
    return orb, played


def test_no_target_yet_stays_where_it_is_not_in_a_corner(capsys):
    orb, played = _orb(app=None)
    orb._acted = False                               # no action has run yet
    orb.tick()
    end = played[0][-1][1]
    assert end == ol.resize_about_center(1700, 900, 82, 70)     # medium, same center
    assert "[FloatingOrb] target none (no-action-yet)" in capsys.readouterr().out


def test_a_normal_window_is_docked_beside_and_logged_as_numbers(capsys):
    orb, played = _orb()
    orb.tick()
    assert played[0][-1][1] == (1008, 108, 28)
    log = capsys.readouterr().out
    assert "[FloatingOrb] target foreground rect 100,100,1000,800" in log
    assert "studio" not in log


def test_a_maximized_target_docks_beside_the_element_inside_the_window(capsys):
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


def test_a_maximized_target_without_an_element_docks_at_its_nearest_inner_edge():
    orb, played = _orb(target=(-8, -8, 1928, 1048))
    orb.tick()
    x, y, s = played[0][-1][1]
    assert orb.dock == "inner-edge"
    assert (x, y) == (1727, 1040 - 28 - 8)            # bottom edge: nearest the orb's center


def test_a_click_point_from_before_this_task_is_ignored():
    orb, _ = _orb(target=(-8, -8, 1928, 1048))
    orb._busy_since = time.monotonic()
    orb._api.click_handler.last_element_rect = ((400, 400), orb._busy_since - 5)
    assert orb._element_rect((-8, -8, 1928, 1048), 1.0) is None
    orb._api.click_handler.last_element_rect = ((400, 400), orb._busy_since + 1)
    assert orb._element_rect((-8, -8, 1928, 1048), 1.25) == (370, 370, 430, 430)


def test_a_step_or_launch_ticks_at_once_and_polls_faster_for_a_while():
    orb, _ = _orb()
    orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "open_app"}))
    assert orb._kick.is_set() and orb._fast_until > time.monotonic()
    orb._kick.clear()
    orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": "click_on_text"}))
    assert orb._kick.is_set()


def test_clicks_and_uia_record_the_element_they_touch():
    assert "self.last_element_rect = ((x, y), time.monotonic())" in inspect.getsource(
        __import__("core.click_handler", fromlist=["x"]).ImprovedClickHandler._execute_click)
    from core.uia_nav import EnhancedUIANavigator
    assert "self.last_element_rect = ((rect.left" in inspect.getsource(
        EnhancedUIANavigator.click_element)


# -- 5b. layout: maximized targets ------------------------------------------------------------

MAXED = (-8, -8, 1928, 1048)


def test_coverage():
    assert ol.coverage(MAXED, W) == 1.0
    assert 0.24 < ol.coverage((0, 0, 960, 520), W) < 0.26


@pytest.mark.parametrize("element, where", [
    ((300, 200, 500, 240), "right"),        # more room to the right inside the window
    ((1500, 200, 1900, 240), "left"),
    ((0, 100, 1920, 140), "below"),         # full-width row: below it
    ((0, 900, 1920, 1030), "above"),
])
def test_dock_inside_beside_the_element_on_the_emptier_side(element, where):
    x, y = ol.dock_inside(MAXED, W, 28, element)
    assert not ol.overlaps((x, y, x + 28, y + 28), element)
    assert 0 <= x and x + 28 <= 1920 and 0 <= y and y + 28 <= 1040
    el, et, er, eb = element
    assert {"right": x >= er, "left": x + 28 <= el, "below": y >= eb,
            "above": y + 28 <= et}[where]


def test_dock_inside_without_an_element_hugs_the_nearest_inner_edge():
    assert ol.dock_inside(MAXED, W, 28, near=(1890, 600)) == (1884, 586)
    assert ol.dock_inside(MAXED, W, 28, near=(30, 500)) == (8, 486)
    assert ol.dock_inside(MAXED, W, 28, near=(900, 10)) == (886, 8)
    assert ol.dock_inside(MAXED, W, 28, near=(900, 1030)) == (886, 1004)


def test_dock_inside_a_window_on_a_second_monitor():
    work = (1920, -200, 3840, 880)
    x, y = ol.dock_inside((1912, -208, 3848, 888), work, 35, (2000, -100, 2300, -60))
    assert 1920 <= x and x + 35 <= 3840 and -200 <= y and y + 35 <= 880
    assert x >= 2300


def test_an_element_filling_the_window_still_keeps_the_orb_inside():
    x, y = ol.dock_inside(MAXED, W, 28, (0, 0, 1920, 1040))
    assert 0 <= x <= 1920 - 28 and 0 <= y <= 1040 - 28


# -- 6. panels ------------------------------------------------------------------------------

SIZES = [(240, 176), (280, 140)]


def test_panels_avoid_the_target_and_mistais_own_window():
    orb = (1008, 108, 28)
    target = (100, 100, 1000, 800)
    own = (1040, 0, 1600, 500)                        # MistAI's window right of the orb
    spots = ol.panel_spots(orb, SIZES, W, target, avoid=[target, own])
    for (x, y), (w, h) in zip(spots, SIZES):
        r = (x, y, x + w, y + h)
        assert not ol.overlaps(r, target) and not ol.overlaps(r, own)
        assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1040
        assert not ol.overlaps(r, (1008, 108, 1036, 136))     # never over the orb
    assert spots[0][0] >= 1036                                # still on the away side


def test_panels_keep_the_away_side_when_it_is_clear_and_fall_back_when_nothing_is():
    orb, target = (1008, 108, 28), (100, 100, 1000, 800)
    assert ol.panel_spots(orb, SIZES, W, target, [target]) == ol.panel_stack(
        orb, SIZES, W, target)
    everything = (0, 0, 1920, 1040)
    assert ol.panel_spots(orb, SIZES, W, target, [everything]) == ol.panel_stack(
        orb, SIZES, W, target)


def test_failure_pause_and_the_network_sentence_close_everything():
    m = op.PanelModel()
    m.on_event(events.Event(events.AGENT_STEP, {"action": "open_app"}))
    assert m.visible("steps", True)
    for name, data in ((events.TASK_FAILED, {}), (events.TASK_PAUSED, {}),
                       (events.AGENT_STEP, {"action": "failed"})):
        m.reset()
        m.on_event(events.Event(events.AGENT_STEP, {"action": "open_app"}))
        m.on_event(events.Event(name, data))
        assert not any(m.visible(k, True) for k in op.KINDS), name


def test_a_stop_or_failure_sends_the_orb_home_at_once():
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


def test_the_agent_signals_a_failed_call_and_the_network_sentence():
    src = inspect.getsource(AgentLoop)
    i = src.index("Network error:")
    assert 'self._publish_step("failed")' in src[i:i + 200]
    j = src.index("Agent call failed:")
    assert 'self._publish_step("failed")' in src[j:j + 200]


def test_steps_and_answer_panels_appear_through_the_real_bus(monkeypatch):
    from core.events import EventBus
    from core.tools import ToolRegistry

    monkeypatch.setattr(op, "panel_blur_enabled", lambda: False)
    bus = EventBus()
    try:
        api = SimpleNamespace(bus=bus, tools=ToolRegistry())
        wv, win = _Webview(), _PanelWin()
        panels = op.OrbPanels(api, win, webview=wv, html="<p>")
        loop = AgentLoop.__new__(AgentLoop)
        loop.api = api
        panels.update((1000, 100, 35), None, W, 1.0, busy=True)
        loop._publish_step("click_on_text")
        bus.publish(events.ACTION_EXECUTED, {"action": "click_on_text"})
        loop._publish_step("done", "All set.")
        deadline = time.monotonic() + 3
        while panels.model.answer_at is None and time.monotonic() < deadline:
            time.sleep(0.01)
        panels.update((1000, 100, 35), None, W, 1.0, busy=False)
        kinds = [w.kw["js_api"]._kind for w in wv.created]
        assert kinds == ["steps", "answer"]
        assert panels.model.state("steps")["steps"] == [{"label": "Clicking", "state": "ok"}]
        assert panels.model.state("answer")["text"] == "All set."
    finally:
        bus.stop()


# -- 7. debug capture --------------------------------------------------------------------------

def test_debug_capture_leaves_the_orb_and_panels_capturable(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_ORB_DEBUG_CAPTURE", "1")
    win = _ClampWin((1, 1))
    assert fo.harden(7, win=win)["capture"] == "debug-visible"
    assert not any(c[0] == "affinity" for c in win.calls)
    pw = _PanelWin()
    op.harden_panel(9, pw, 190, 30, 1.0, blur=False)
    assert not any(c[0] == "affinity" for c in pw.calls)
    assert "capture exclusion off (MISTAI_ORB_DEBUG_CAPTURE)" in capsys.readouterr().out


def test_debug_capture_is_ignored_in_a_built_app(monkeypatch):
    monkeypatch.setenv("MISTAI_ORB_DEBUG_CAPTURE", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.orb_debug_capture() is False
    win = _ClampWin((1, 1))
    assert fo.harden(7, win=win)["capture"] == "excluded"


# -- the general target: whatever the task brought to the front -------------------------------

class _SeqWin(_FlightWin):
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


def _task(fg=10, browser=None, app=None):
    """An orb whose task starts now, with ``fg`` in front."""
    orb, played = _orb(app=app)
    orb._win = _SeqWin(fg=fg, browser=browser)
    orb._acted, orb._was_busy = False, False
    orb._api._window_belongs_to = lambda hwnd, a: (hwnd, a) == (20, "notepad")
    orb._api._own_window_handle = lambda: 88
    orb.tick()                                       # the task starts: pre-task window
    return orb, played


def _step(orb, action, fg=None):
    if fg is not None:
        orb._win.fg = fg
    orb._on_bus(events.Event(events.ACTION_EXECUTED, {"action": action}))
    orb.tick()


def test_nothing_is_followed_before_the_first_action(capsys):
    orb, played = _task(fg=20)
    assert orb._pre_task_fg == 20 and orb._cur_target is None
    assert "target none (no-action-yet)" in capsys.readouterr().out


def test_a_search_task_follows_mistais_browser_window(capsys):
    orb, played = _task(fg=10, browser=77)
    _step(orb, "browser_search")                     # the user's editor is still in front
    assert orb._cur_target["rect"] == (200, 50, 1100, 900)
    assert played[-1][-1][1] == (1108, 58, 28)       # right of the browser window
    log = capsys.readouterr().out
    assert "[FloatingOrb] target browser rect 200,50,1100,900" in log
    assert "firefox" not in log.lower() and "code" not in log.lower()


def test_without_its_window_a_browser_step_follows_the_new_foreground(capsys):
    orb, played = _task(fg=10, browser=None)
    _step(orb, "browser_open", fg=77)                # the browser came to the front
    assert orb._cur_target["rect"] == (200, 50, 1100, 900)
    assert "target foreground rect 200,50,1100,900" in capsys.readouterr().out


def test_a_task_whose_foreground_never_changes_leaves_the_orb_put(capsys):
    orb, played = _task(fg=10)
    _step(orb, "run_command")
    _step(orb, "inspect_file")
    assert orb._cur_target is None
    assert played[-1][-1][1] == ol.resize_about_center(1700, 900, 82, 70)   # stays put
    assert "target none (unrelated-window)" in capsys.readouterr().out


def test_the_app_the_step_touched_counts_even_if_it_was_already_in_front():
    orb, played = _task(fg=20, app="notepad")
    _step(orb, "switch_app")
    assert orb._cur_target["rect"] == (100, 100, 1000, 800)


@pytest.mark.parametrize("fg", [4242, 88, 30, 31])     # orb, MistAI, taskbar, Task Manager
def test_mistai_the_orb_the_shell_and_task_manager_are_never_targets(fg, capsys):
    orb, played = _task(fg=10)
    _step(orb, "click_on_text", fg=fg)
    assert orb._cur_target is None
    assert "target none (no-window)" in capsys.readouterr().out


def test_panel_windows_are_never_targets():
    orb, played = _task(fg=10)
    orb.panels = SimpleNamespace(handles=lambda: [66], hide_all=lambda: None,
                                 update=lambda *a, **k: None)
    _step(orb, "click_on_text", fg=66)
    assert orb._cur_target is None


def test_a_maximized_browser_window_docks_inside_it(capsys):
    orb, played = _task(fg=10, browser=77)
    orb._win.RECTS = {**_SeqWin.RECTS, 77: (-8, -8, 1928, 1048)}
    _step(orb, "browser_get_text")
    x, y, s = played[-1][-1][1]
    assert orb.dock == "inner-edge" and s == 28
    assert 0 <= x and x + s <= 1920 and 0 <= y and y + s <= 1040
    assert "target maximized-edge rect -8,-8,1928,1048" in capsys.readouterr().out


def test_every_step_is_followed_by_fast_polling():
    orb, _ = _task(fg=10)
    for action in ("run_command", "browser_search", "click_on_text"):
        orb._fast_until = 0
        orb._on_bus(events.Event(events.ACTION_FAILED, {"action": action}))
        assert orb._fast_until > time.monotonic() and orb._last_action == action
