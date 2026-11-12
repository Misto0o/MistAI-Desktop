"""
core/floating_orb.py — the optional floating orb (Settings > "Floating orb", off
by default): a small frameless always-on-top window that shows only the orb,
so Mist's state stays visible while the main window is minimized for a task.

Safety: the window is excluded from screen capture where Windows supports it
(SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+; older
builds get WDA_MONITOR, a black box) and from the OCR search (the own-window
mask, by handle). It never activates (WS_EX_NOACTIVATE) and stays out of the
taskbar and Alt-Tab (WS_EX_TOOLWINDOW). Its title is empty, so title lookups
(window_list, window_activate) never return it; it lives in MistAI's own
process, so Open apps and process lookups already skip it (is_self_process).
Input actions refuse to type into it (Api._ensure_input_target). Its page
talks to Python only through OrbBridge (orb_state, talk), never the full Api.
A WM_MOUSEACTIVATE subclass answers MA_NOACTIVATE so a click never activates
it. A control thread (tick, 4 Hz) resizes it about its center per state
(config ORB_SIZE_BIG/MEDIUM/SMALL), flies it beside the task's window when
"Orb follows the task" is on (core/orb_layout.py), and drives the task
panels (core/orb_panels.py).

The window is opaque and cut to a circle with a window region rather than
using pywebview's transparent mode, which is unreliable with WebView2.

Placement and the region are done in PHYSICAL pixels once the window is shown
(pywebview makes the process DPI-aware, and it scales create-time x/y by the
display scale itself): the primary monitor's work area (above the taskbar),
its scale from GetDpiForMonitor, a square of ORB_SIZE * scale placed
ORB_MARGIN * scale from the right and bottom edges, clamped inside the area.
Logs carry numbers and Win32 error codes only.
"""

import ctypes
import threading

from .config import ORB_SIZE_BIG, ORB_SIZE_MEDIUM, ORB_SIZE_SMALL
from .orb_layout import dock_position, dock_side, frames, resize_about_center

ORB_SIZE = ORB_SIZE_BIG   # window width and height at home, logical px (96 DPI)
ORB_MARGIN = 16           # logical px from the work area's right and bottom edges
ORB_BACKGROUND = "#05080f"

WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020     # with LAYERED: clicks pass through
LWA_ALPHA = 0x2
MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010


def orb_state(api):
    """The orb's state from Python's own facts, with the page's priority
    (ui/html.py orbState): the push-to-talk mic, then Mist's voice, then a
    running task, else idle."""
    try:
        if api._ptt_wants_mic():
            return "listening"
    except Exception:
        pass
    try:
        if getattr(getattr(api, "tts", None), "speaking", False):
            return "speaking"
    except Exception:
        pass
    try:
        if api._agent_lock.locked():
            return "thinking"
    except Exception:
        pass
    return "idle"


class OrbBridge:
    """The orb page's js_api: the read-only state and talk(), nothing else,
    so the floating window cannot reach the rest of the Api."""

    _TALK = ("start", "stop", "toggle")

    def __init__(self, api):
        self._api = api

    def orb_state(self):
        return {"state": orb_state(self._api)}

    def talk(self, kind):
        """Push-to-talk from the orb: "start" (a hold began; only when not
        already listening), "stop" (the hold ended; sends, only while
        listening), "toggle" (a tap: start, or send a latched listen). The
        same Api.push_to_talk as the Talk button, with its refusals."""
        if kind not in self._TALK:
            return {"success": False}
        try:
            listening = self._api._ptt_wants_mic()
            if (kind == "start" and listening) or (kind == "stop" and not listening):
                return {"success": True}
            r = self._api.push_to_talk() or {}
            return {"success": bool(r.get("success")), "message": str(r.get("message", ""))}
        except Exception as e:
            print(f"[FloatingOrb] talk failed ({type(e).__name__})")
            return {"success": False}


# -- geometry (pure) ------------------------------------------------------------

def place(work, scale, size=ORB_SIZE, margin=ORB_MARGIN):
    """(x, y, side) in physical px: a square of ``size * scale`` with
    ``margin * scale`` to the right and bottom edges of ``work`` (l, t, r, b),
    shrunk and clamped so it always lies fully inside the work area."""
    l, t, r, b = (int(v) for v in work)
    scale = scale if scale and scale > 0 else 1.0
    avail_w, avail_h = max(1, r - l), max(1, b - t)
    side = max(1, min(int(round(size * scale)), avail_w, avail_h))
    gap = max(0, min(int(round(margin * scale)), avail_w - side, avail_h - side))
    return max(l, r - gap - side), max(t, b - gap - side), side


def ellipse_box(width, height):
    """CreateEllipticRgn's box for a window of this size: relative to the
    window's top-left, right/bottom exclusive (hence the +1)."""
    return 0, 0, int(width) + 1, int(height) + 1


def size_for(state, docked=False):
    """Logical side for a state: small while docked beside the task's window,
    medium while thinking or speaking, big when idle or listening."""
    if docked:
        return ORB_SIZE_SMALL
    return ORB_SIZE_MEDIUM if state in ("thinking", "speaking") else ORB_SIZE_BIG


# -- Win32 (ctypes; tests pass a fake with the same methods) --------------------

class Win32:
    """The few Win32 calls the orb needs, with last-error capture. pywin32's
    win32gui has no CreateEllipticRgn, so the region calls go through ctypes."""

    def __init__(self):
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             wintypes.UINT]
        self.user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        self.gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
        self.gdi32.CreateEllipticRgn.restype = wintypes.HANDLE
        self.gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        self.gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self._RECT = wintypes.RECT

    def last_error(self):
        return ctypes.get_last_error()

    def set_affinity(self, hwnd, mode):
        return bool(self.user32.SetWindowDisplayAffinity(hwnd, mode))

    def add_exstyle(self, hwnd, bits):
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | bits)

    def set_click_through(self, hwnd, on):
        """WS_EX_LAYERED|WS_EX_TRANSPARENT on (fully opaque) or both off."""
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        bits = WS_EX_LAYERED | WS_EX_TRANSPARENT
        if on:
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | bits)
            self.user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)
        else:
            self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style & ~bits)

    def show(self, hwnd, on):
        """ShowWindow: SW_SHOWNOACTIVATE (4) or SW_HIDE (0); never activates."""
        return bool(self.user32.ShowWindow(hwnd, 4 if on else 0))

    def dwm_set(self, hwnd, attribute, value):
        """DwmSetWindowAttribute with an int value; the HRESULT (0 = ok)."""
        v = ctypes.c_int(int(value))
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(v),
                                                          ctypes.sizeof(v))

    def create_round_rect(self, box):
        return self.gdi32.CreateRoundRectRgn(*box)

    def exstyle(self, hwnd):
        return self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)

    def foreground(self):
        return self.user32.GetForegroundWindow()

    def monitor_for_rect(self, rect):
        """(work area, scale) of the monitor nearest ``rect`` (l, t, r, b)."""
        import win32api

        mon = win32api.MonitorFromRect(tuple(int(v) for v in rect), MONITOR_DEFAULTTONEAREST)
        return tuple(win32api.GetMonitorInfo(mon)["Work"]), self.monitor_scale(mon)

    def primary_work_area(self):
        """(monitor handle, (l, t, r, b)): the primary monitor's work area,
        i.e. without the taskbar, in this process's DPI context."""
        import win32api

        mon = win32api.MonitorFromPoint((0, 0), MONITOR_DEFAULTTOPRIMARY)
        return mon, tuple(win32api.GetMonitorInfo(mon)["Work"])

    def monitor_scale(self, monitor, hwnd=None):
        """Effective DPI / 96 of ``monitor`` (GetDpiForMonitor), else of the
        window (GetDpiForWindow), else 1.0. A DPI-unaware process gets 96."""
        try:
            x, y = ctypes.c_uint(), ctypes.c_uint()
            if ctypes.windll.shcore.GetDpiForMonitor(int(monitor), 0, ctypes.byref(x),
                                                     ctypes.byref(y)) == 0 and x.value:
                return x.value / 96.0
        except Exception:
            pass
        try:
            dpi = self.user32.GetDpiForWindow(hwnd) if hwnd else 0
            if dpi:
                return dpi / 96.0
        except Exception:
            pass
        return 1.0

    def window_rect(self, hwnd):
        rect = self._RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def move(self, hwnd, x, y, w, h):
        return bool(self.user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE))

    def create_ellipse(self, box):
        return self.gdi32.CreateEllipticRgn(*box)

    def set_region(self, hwnd, region):
        return bool(self.user32.SetWindowRgn(hwnd, region, True))

    def delete_object(self, handle):
        self.gdi32.DeleteObject(handle)


def apply_square(win, hwnd, x, y, side):
    """Move/resize the window to the square and cut a new circle for it.
    True when both applied; never raises, never logs (callers log once)."""
    try:
        ok = win.move(hwnd, x, y, side, side)
        region = win.create_ellipse(ellipse_box(side, side))
        if not region:
            return False
        if not win.set_region(hwnd, region):
            win.delete_object(region)
            return False
        return bool(ok)
    except Exception:
        return False


class Tween:
    """Plays a list of squares on a worker thread, one per frame. A new
    play() (or cancel()) stops the one before at its next frame."""

    FRAME_S = 0.016

    def __init__(self, win, hwnd):
        self._win, self._hwnd = win, hwnd
        self._stop = None
        self._thread = None

    def play(self, squares, on_frame=None, done=None):
        self.cancel()
        stop = self._stop = threading.Event()

        def run():
            failed = 0
            for sq in squares:
                if stop.is_set():
                    return
                if not apply_square(self._win, self._hwnd, *sq):
                    failed += 1
                if on_frame:
                    on_frame(sq)
                stop.wait(self.FRAME_S)
            if failed:
                print(f"[FloatingOrb] tween: {failed} of {len(squares)} frames failed")
            if done and not stop.is_set():
                done()

        t = self._thread = threading.Thread(target=run, daemon=True, name="OrbTween")
        t.start()
        return t

    def cancel(self):
        if self._stop is not None:
            self._stop.set()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()


WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3


def mouse_activate_answer(msg):
    """The subclass's answer: MA_NOACTIVATE to WM_MOUSEACTIVATE, else None
    (pass the message on)."""
    return MA_NOACTIVATE if msg == WM_MOUSEACTIVATE else None


_SUBCLASS_PROCS = []      # keeps the ctypes callbacks alive


def guard_mouse_activate(hwnd, run_on_ui=None):
    """Subclass the window (comctl32 SetWindowSubclass) so a click answers
    WM_MOUSEACTIVATE with MA_NOACTIVATE: a click never activates the orb,
    on top of WS_EX_NOACTIVATE. SetWindowSubclass works only on the
    window's own thread, so a failed direct call is retried through
    ``run_on_ui`` (pywebview's WinForms Invoke). True when installed."""
    try:
        from ctypes import wintypes

        comctl = ctypes.WinDLL("comctl32")
        lresult = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                   wintypes.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
        comctl.DefSubclassProc.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        comctl.DefSubclassProc.restype = lresult
        comctl.SetWindowSubclass.argtypes = [wintypes.HWND, proto, ctypes.c_size_t,
                                             ctypes.c_size_t]

        def proc(h, msg, wp, lp, _uid, _data):
            answer = mouse_activate_answer(msg)
            return answer if answer is not None else comctl.DefSubclassProc(h, msg, wp, lp)

        cb = proto(proc)
        _SUBCLASS_PROCS.append(cb)
        done = {"ok": bool(comctl.SetWindowSubclass(hwnd, cb, 0x4D49, 0))}
        if not done["ok"] and run_on_ui is not None:
            run_on_ui(lambda: done.update(ok=bool(comctl.SetWindowSubclass(hwnd, cb, 0x4D49, 0))))
        return done["ok"]
    except Exception:
        return False


def _fail(call, win):
    """Log a failed Win32 call by name and GetLastError() code only."""
    try:
        code = int(win.last_error())
    except Exception:
        code = -1
    print(f"[FloatingOrb] {call} failed (error {code})")


def harden(hwnd, win=None):
    """Exclude ``hwnd`` from capture, make it non-activating and taskbar-free,
    place it inside the primary work area and cut it to a circle. Returns
    {"capture": "excluded" | "black" | "none", "noactivate": bool,
    "round": bool, "position": (x, y, w, h, scale) | None}; never raises."""
    out = {"capture": "none", "noactivate": False, "round": False, "position": None}
    try:
        win = win or Win32()
    except Exception as e:
        print(f"[FloatingOrb] Win32 unavailable ({type(e).__name__})")
        return out
    try:
        if win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            out["capture"] = "excluded"
        elif win.set_affinity(hwnd, WDA_MONITOR):
            out["capture"] = "black"
        else:
            _fail("SetWindowDisplayAffinity", win)
    except Exception as e:
        print(f"[FloatingOrb] SetWindowDisplayAffinity error ({type(e).__name__})")
    try:
        win.add_exstyle(hwnd, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        out["noactivate"] = True
    except Exception as e:
        print(f"[FloatingOrb] SetWindowLongW error ({type(e).__name__})")
    scale = 1.0
    try:
        monitor, work = win.primary_work_area()
        scale = win.monitor_scale(monitor, hwnd) or 1.0
        x, y, side = place(work, scale)
        if not win.move(hwnd, x, y, side, side):
            _fail("SetWindowPos", win)
    except Exception as e:
        print(f"[FloatingOrb] placement error ({type(e).__name__})")
    try:
        rect = win.window_rect(hwnd)       # the real size after the move
        if rect is None:
            _fail("GetWindowRect", win)
            return out
        l, t, r, b = rect
        out["position"] = (l, t, r - l, b - t, round(scale, 2))
        print(f"[FloatingOrb] at {l},{t} size {r - l},{b - t} monitor-scale {scale:.2f}")
        region = win.create_ellipse(ellipse_box(r - l, b - t))
        if not region:
            _fail("CreateEllipticRgn", win)
        elif win.set_region(hwnd, region):
            out["round"] = True             # the system owns the region now
        else:
            _fail("SetWindowRgn", win)
            win.delete_object(region)
    except Exception as e:
        print(f"[FloatingOrb] region error ({type(e).__name__})")
    return out


def _corner_position(size=ORB_SIZE, win=None):
    """Create-time (x, y) in LOGICAL px near the work area's bottom-right
    corner. pywebview multiplies create-time x/y by the display scale, so the
    physical spot from place() is divided by it; in a DPI-unaware context the
    scale reads 1 and the work area is already logical. (None, None) on any
    error. harden() places the window exactly once it is shown."""
    try:
        win = win or Win32()
        monitor, work = win.primary_work_area()
        scale = win.monitor_scale(monitor) or 1.0
        x, y, _side = place(work, scale, size)
        return int(round(x / scale)), int(round(y / scale))
    except Exception:
        return None, None


class FloatingOrb:
    """Creates the orb window on first show; hide() keeps it for the next show."""

    def __init__(self, api, webview=None, html=None):
        self._api = api
        self._webview = webview
        self._html = html
        self.window = None
        self.hwnd = None
        self.hardening = None
        self.visible = False
        self.square = None        # (x, y, side) physical px, as last applied
        self.home = None          # the big square at home
        self.scale = 1.0
        self._win = None
        self._tween = None
        self._loop_stop = threading.Event()
        self.dock = None          # side docked on ("right", ...) or None
        self._logged_dock = None
        self.click_through = False
        self._cur_target = None   # this tick's (rect, work, scale) or None
        self.panels = None        # core.orb_panels.OrbPanels once used

    TICK_S = 0.25
    TWEEN_FRAMES = 8
    FLIGHT_FRAMES = 22

    def _flight_on(self):
        try:
            return bool(self._api.orb_option("orb_flight"))
        except Exception:
            return False

    def _busy(self):
        try:
            return self._api._agent_lock.locked()
        except Exception:
            return False

    def _target(self):
        """(rect, work area, scale) of the window this phase opened or
        switched to, while it is in front; else None. Like
        Api._click_target_rect, but quiet (it runs four times a second)."""
        app = getattr(self._api, "_last_opened_app", None)
        if not app:
            return None
        hwnd = self._win.foreground()
        if not hwnd or hwnd in self.handles() or not self._api._window_belongs_to(hwnd, app):
            return None
        rect = self._win.window_rect(hwnd)
        if not rect:
            return None
        work, scale = self._win.monitor_for_rect(rect)
        return rect, work, scale or 1.0

    def _here_scale(self):
        try:
            x, y, s = self.square
            return self._win.monitor_for_rect((x, y, x + s, y + s))[1] or self.scale
        except Exception:
            return self.scale

    def _goal(self, state):
        """(x, y, side) the orb should have now. Without flight: at home,
        resized about its center for the state. With flight (Settings "Orb
        follows the task"): docked small beside the task's window while one
        is known and in front, medium where it is while working or speaking,
        else big at home."""
        x, y, side = self.square
        self.dock = None
        if not self._flight_on():
            return resize_about_center(x, y, side, round(size_for(state) * self.scale))
        busy = self._busy()
        target = self._cur_target
        if target:
            rect, work, scale = target
            small = max(1, round(ORB_SIZE_SMALL * scale))
            self.dock = dock_side(rect, work, small, 8)
            return dock_position(rect, work, small) + (small,)
        if busy or state == "speaking":
            return resize_about_center(x, y, side, round(ORB_SIZE_MEDIUM * self._here_scale()))
        return self.home

    def _sync_panels(self, busy):
        """Task panels (Settings "Task panels"): created on first use, placed
        beside the orb away from the target, hidden when the option is off."""
        try:
            on = bool(self._api.orb_option("orb_panels"))
        except Exception:
            on = False
        if not on:
            if self.panels is not None:
                self.panels.hide_all()
            return
        if self.panels is None:
            from .orb_panels import OrbPanels

            self.panels = OrbPanels(self._api, self._win)
        if self._cur_target:
            rect, work, scale = self._cur_target
        else:
            x, y, s = self.square
            rect = None
            work, scale = self._win.monitor_for_rect((x, y, x + s, y + s))
        self.panels.update(self.square, rect, work, scale or self.scale, busy)

    def _sync_click_through(self):
        """Click-through while flight is on and the agent acts, so the orb can
        never swallow one of its clicks; restored afterwards."""
        want = self._flight_on() and self._busy()
        if want != self.click_through:
            try:
                self._win.set_click_through(self.hwnd, want)
                self.click_through = want
                print(f"[FloatingOrb] click-through {'on' if want else 'off'}")
            except Exception as e:
                print(f"[FloatingOrb] click-through error ({type(e).__name__})")

    def _adopt_drag(self):
        """The user dragged the orb: take the real rect as the square, and as
        the new home while the orb is at home."""
        try:
            rect = self._win.window_rect(self.hwnd)
        except Exception:
            rect = None
        if not rect:
            return
        l, t, r, b = rect
        real = (l, t, r - l)
        if real != self.square:
            at_home = self.square == self.home
            self.square = real
            if at_home and not self._busy():
                self.home = real

    def tick(self):
        """One controller step: tween towards the goal for the current state.
        Never raises."""
        try:
            if not (self.visible and self.square and self._tween):
                return
            self._sync_click_through()
            busy = self._busy()
            try:
                self._cur_target = self._target() if busy else None
            except Exception:
                self._cur_target = None
            self._sync_panels(busy)
            if self._tween.running:
                return
            self._adopt_drag()
            goal = self._goal(orb_state(self._api))
            if self.dock != self._logged_dock:
                self._logged_dock = self.dock
                print(f"[FloatingOrb] dock {self.dock or 'none'} size {goal[2]}")
            if max(abs(a - b) for a, b in zip(goal, self.square)) <= 2:
                return
            (x0, y0, s0), (x1, y1, s1) = self.square, goal
            far = abs((x1 + s1 / 2) - (x0 + s0 / 2)) + abs((y1 + s1 / 2) - (y0 + s0 / 2)) > s0
            path = frames(self.square, goal, self.FLIGHT_FRAMES if far else self.TWEEN_FRAMES,
                          bow=0.3 if far else 0.0)
            self._tween.play(path, on_frame=self._set_square)
        except Exception as e:
            print(f"[FloatingOrb] tick error ({type(e).__name__})")

    def _set_square(self, sq):
        self.square = sq

    def _run(self):
        while not self._loop_stop.wait(self.TICK_S):
            self.tick()

    def _module(self):
        if self._webview is None:
            import webview

            self._webview = webview
        return self._webview

    def show(self):
        """Show the orb (create it the first time). Returns True on success."""
        try:
            if self.window is None:
                html = self._html
                if html is None:
                    from ui.html import ORB_HTML as html
                x, y = _corner_position()
                self.window = self._module().create_window(
                    "", html=html, js_api=OrbBridge(self._api),
                    width=ORB_SIZE, height=ORB_SIZE, x=x, y=y,
                    resizable=False, frameless=True, easy_drag=True, shadow=False,
                    focus=False, on_top=True, background_color=ORB_BACKGROUND,
                )
                self.window.events.shown += self._on_shown
            else:
                self.window.show()
            self.visible = True
            return True
        except Exception as e:
            print(f"[FloatingOrb] could not show the orb ({type(e).__name__})")
            self.visible = False
            return False

    def hide(self):
        if self.window is not None:
            try:
                self.window.hide()
            except Exception:
                pass
        self.visible = False
        if self.panels is not None:
            self.panels.hide_all()

    def _on_shown(self):
        try:
            self.hwnd = int(self.window.native.Handle.ToInt32()) or None
        except Exception:
            self.hwnd = None
        if self.hwnd:
            self.hardening = harden(self.hwnd)
            print(f"[FloatingOrb] shown | capture: {self.hardening['capture']} | "
                  f"no-activate: {self.hardening['noactivate']} | round: {self.hardening['round']}")
            self._guard_clicks()
            pos = self.hardening.get("position")
            if pos and self._tween is None:
                x, y, w, h, scale = pos
                self.square = self.home = (x, y, w)
                self.scale = scale or 1.0
                try:
                    self._win = Win32()
                except Exception:
                    return
                self._tween = Tween(self._win, self.hwnd)
                threading.Thread(target=self._run, daemon=True, name="OrbControl").start()

    def _guard_clicks(self):
        """Verify WS_EX_NOACTIVATE took and install the WM_MOUSEACTIVATE
        guard; logs labels only."""
        verified = False
        try:
            verified = bool(Win32().exstyle(self.hwnd) & WS_EX_NOACTIVATE)
        except Exception:
            pass

        def run_on_ui(fn):
            from System import Action  # pythonnet, as pywebview uses on Windows

            self.window.native.Invoke(Action(fn))

        guarded = guard_mouse_activate(self.hwnd, run_on_ui)
        print(f"[FloatingOrb] no-activate verified: {verified} | mouse-activate guard: "
              f"{'on' if guarded else 'off'}")

    def handles(self):
        """The orb's and its panels' window handles while shown (for the
        own-window OCR mask and the input-target refusal)."""
        if not (self.visible and self.hwnd):
            return []
        return [self.hwnd] + (self.panels.handles() if self.panels is not None else [])
