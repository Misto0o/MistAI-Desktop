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
import sys
import threading
import time

from . import events
from .config import orb_background, orb_debug_capture, orb_frame_stats, orb_motion, orb_sizes
from .orb_layout import (RESIZE_S, TOUR_REJECTIONS, answer_spot, classify_page_rects, coverage, dock_inside,
                         dock_position, dock_side, flight_plan, margin_spot, resize_about_center,
                         square_at, tour_plan)
from .orb_motion import Flight, frame_stats, stats_line, valid_refresh

ORB_MARGIN = 16           # logical px from the work area's right and bottom edges
MIN_SIZE = (1, 1)         # pywebview's default min_size (200, 100) clamps every resize
MAXIMIZED = 0.9           # a target covering this much of its work area is "maximized"
ELEMENT_PAD = 24          # logical px around a click point used as its element rect
# Windows the orb never follows: the taskbar/desktop shell and Task Manager
# (MistAI's own windows, the orb and its panels are excluded by handle/PID).
SHELL_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                           "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
                           "Windows.UI.Core.CoreWindow", "XamlExplorerHostIslandWindow"})
IGNORED_PROCESSES = ("taskmgr",)
# MistAI's dedicated browser is Playwright's Firefox, run from its own folder.
PLAYWRIGHT_PATH_HINTS = ("ms-playwright", ".local-browsers")
# Organic result title links: DuckDuckGo's normal page (current and older
# markup), its HTML endpoint, Google. Ads are flagged in the page and skipped.
RESULT_LINKS = ('a[data-testid="result-title-a"], article[data-testid="result"] h2 a, '
                'li[data-layout="organic"] h2 a, a.result__a, .result__title a, '
                '#search a:has(h3), #rso a:has(h3)')
TOUR_STOPS = 4
TOUR_CANDIDATES = 12      # measured per read; the first TOUR_STOPS usable ones are kept
TOUR_RETRY_S = 0.4        # fewer than TOUR_STOPS found: the page may still be rendering
TOUR_DWELL_S = 0.7
SPEECH_START_S = 1.0      # the voice may start a moment after the answer arrives
WORDS_PER_S = 2.5         # speech-length estimate when the TTS has no speaking flag
SPEECH_CAP_S = 20.0

WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020     # with LAYERED: clicks pass through
LWA_ALPHA = 0x2
MONITOR_DEFAULTTONEAREST = 2
MONITOR_DEFAULTTOPRIMARY = 1
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOOWNERZORDER = 0x0200
SWP_ASYNCWINDOWPOS = 0x4000
# One flight frame: position only. ASYNCWINDOWPOS because the window belongs
# to the WinForms UI thread: a synchronous cross-thread SetWindowPos would
# block the animation thread whenever that thread is busy (each post is an
# absolute position, so a late one is harmless). No SWP_NOCOPYBITS: it would
# make WebView2 repaint on every move.
SWP_FRAME = SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_ASYNCWINDOWPOS
SPI_GETCLIENTAREAANIMATION = 0x1042
ENUM_CURRENT_SETTINGS = -1
THREAD_PRIORITY_ABOVE_NORMAL = 1
CREATE_WAITABLE_TIMER_HIGH_RESOLUTION = 0x2
TIMER_ALL_ACCESS = 0x1F0003
FLIGHT_SWITCH_INTERVAL = 0.001     # s, sys.setswitchinterval during a flight


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

def place(work, scale, size=None, margin=ORB_MARGIN):
    """(x, y, side) in physical px: a square of ``size * scale`` with
    ``margin * scale`` to the right and bottom edges of ``work`` (l, t, r, b),
    shrunk and clamped so it always lies fully inside the work area."""
    l, t, r, b = (int(v) for v in work)
    scale = scale if scale and scale > 0 else 1.0
    size = orb_sizes()[0] if size is None else size
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
    big, medium, small = orb_sizes()
    if docked:
        return small
    return medium if state in ("thinking", "speaking") else big


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

    def set_exstyle(self, hwnd, add=0, remove=0):
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, (style | add) & ~remove)

    def move_only(self, hwnd, x, y):
        """Position only: no resize, no z-order change, no activation, and
        posted to the window's own thread (SWP_FRAME)."""
        return bool(self.user32.SetWindowPos(hwnd, None, x, y, 0, 0, SWP_FRAME))

    def animations_enabled(self):
        """SPI_GETCLIENTAREAANIMATION (Settings > Accessibility > Animation
        effects); True when it can't be read."""
        from ctypes import wintypes

        v = wintypes.BOOL(1)
        if not self.user32.SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(v), 0):
            return True
        return bool(v.value)

    def refresh_rate(self, hwnd):
        """The refresh rate (Hz) of the monitor the window is on, else 60."""
        try:
            import win32api

            mon = win32api.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            device = win32api.GetMonitorInfo(mon)["Device"]
            mode = win32api.EnumDisplaySettings(device, ENUM_CURRENT_SETTINGS)
            return valid_refresh(mode.DisplayFrequency)
        except Exception:
            return 60

    def make_pacer(self, hz):
        return FramePacer(hz)

    def _kernel32(self):
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentThread.restype = wintypes.HANDLE
        k.GetThreadPriority.argtypes = [wintypes.HANDLE]
        k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        return k

    def boost_thread(self):
        """Raise the calling thread to above-normal; its old priority, or None."""
        k = self._kernel32()
        h = k.GetCurrentThread()
        prev = k.GetThreadPriority(h)
        return prev if k.SetThreadPriority(h, THREAD_PRIORITY_ABOVE_NORMAL) else None

    def restore_thread(self, prev):
        k = self._kernel32()
        k.SetThreadPriority(k.GetCurrentThread(), int(prev))

    def foreground(self):
        return self.user32.GetForegroundWindow()

    def is_minimized(self, hwnd):
        return bool(self.user32.IsIconic(hwnd))

    def window_info(self, hwnd):
        """(pid, process name, window class) of a window; never raises."""
        try:
            import psutil
            import win32gui
            import win32process

            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            return pid, psutil.Process(pid).name(), win32gui.GetClassName(hwnd)
        except Exception:
            return 0, "", ""

    def browser_window(self):
        """The visible top-level window of MistAI's Playwright Firefox, or None."""
        try:
            import psutil
            import win32gui
            import win32process

            found = []

            def visit(hwnd, _):
                if found or not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
                    return True
                try:
                    exe = psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).exe()
                except Exception:
                    return True
                if any(h in exe.lower() for h in PLAYWRIGHT_PATH_HINTS):
                    found.append(hwnd)
                return True

            win32gui.EnumWindows(visit, None)
            return found[0] if found else None
        except Exception:
            return None

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


def apply_square(win, hwnd, x, y, side, last_side=None):
    """Put the window on the square. Same side as ``last_side``: a plain
    move (SetWindowPos SWP_NOSIZE), no region work. A new side: resize, then
    cut a circle for that size. True when applied; never raises or logs."""
    try:
        if last_side is not None and side == last_side:
            return bool(win.move_only(hwnd, x, y))
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


class FramePacer:
    """Waits one display frame per wait(): DwmFlush() (returns after the next
    DWM composition, i.e. vsync), else a high-resolution waitable timer
    (CreateWaitableTimerExW, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION) on
    deadlines one refresh apart, else timeBeginPeriod(1) and time.sleep.
    DwmFlush falls back for the rest of the flight when it fails or returns
    in under 30% of a frame three times running (nothing to compose: it would
    spin). ``kind`` is "dwm", "timer" or "sleep"; close() frees the timer and
    ends the timer period. Never raises."""

    FAST_SHARE = 0.3
    FAST_LIMIT = 3

    def __init__(self, hz, clock=time.perf_counter):
        self.interval = 1.0 / valid_refresh(hz)
        self._clock = clock
        self._fast = 0
        self._timer = None
        self._period = False
        self._k = None
        self._next = clock() + self.interval
        self.kind = "dwm"
        try:
            self._dwm = ctypes.WinDLL("dwmapi").DwmFlush
            self._dwm.restype = ctypes.c_long
        except Exception:
            self._dwm = None
            self._to_timer()

    def _to_timer(self):
        from ctypes import wintypes

        self.kind = "timer"
        try:
            k = ctypes.WinDLL("kernel32")
            k.CreateWaitableTimerExW.restype = wintypes.HANDLE
            k.CreateWaitableTimerExW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                                 wintypes.DWORD, wintypes.DWORD]
            k.SetWaitableTimer.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong),
                                           ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.BOOL]
            k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            self._k = k
            self._timer = k.CreateWaitableTimerExW(None, None,
                                                   CREATE_WAITABLE_TIMER_HIGH_RESOLUTION,
                                                   TIMER_ALL_ACCESS) or None
        except Exception:
            self._timer = None
        if self._timer is None:
            self.kind = "sleep"
            try:
                ctypes.WinDLL("winmm").timeBeginPeriod(1)
                self._period = True
            except Exception:
                pass
        self._next = self._clock() + self.interval

    def wait(self):
        try:
            if self.kind == "dwm":
                t = self._clock()
                hr = self._dwm()
                if hr != 0:
                    self._to_timer()
                elif self._clock() - t < self.FAST_SHARE * self.interval:
                    self._fast += 1
                    if self._fast < self.FAST_LIMIT:
                        return
                    self._to_timer()
                else:
                    self._fast = 0
                    return
            now = self._clock()
            self._next += self.interval
            if self._next <= now:                    # a late frame: skip ahead, no catch-up
                self._next = now + self.interval
            delay = self._next - now
            if self._timer is not None:
                due = ctypes.c_longlong(-max(1, int(delay * 1e7)))     # relative, 100 ns units
                if self._k.SetWaitableTimer(self._timer, ctypes.byref(due), 0, None, None, False):
                    self._k.WaitForSingleObject(self._timer, int(delay * 1000) + 50)
                    return
            time.sleep(delay)
        except Exception:
            time.sleep(self.interval)

    def close(self):
        try:
            if self._timer is not None:
                self._k.CloseHandle(self._timer)
                self._timer = None
            if self._period:
                ctypes.WinDLL("winmm").timeEndPeriod(1)
                self._period = False
        except Exception:
            pass


class _EventPacer:
    """For a Win32 stand-in without make_pacer (tests): Event.wait(FRAME_S)."""

    kind = "sleep"

    def __init__(self, stop, seconds):
        self._stop, self._s = stop, seconds

    def wait(self):
        if self._s > 0:
            self._stop.wait(self._s)

    def close(self):
        pass


_BOOST_LOCK = threading.Lock()
_BOOST = {"n": 0, "interval": None}


def _boost_enter(win):
    """Above-normal priority for this thread and a 1 ms GIL switch interval
    for the flight (ref-counted, so overlapping flights restore it once).
    The thread's old priority, or None."""
    prev = None
    try:
        prev = win.boost_thread() if hasattr(win, "boost_thread") else None
    except Exception:
        prev = None
    with _BOOST_LOCK:
        if _BOOST["n"] == 0:
            _BOOST["interval"] = sys.getswitchinterval()
            sys.setswitchinterval(FLIGHT_SWITCH_INTERVAL)
        _BOOST["n"] += 1
    return prev


def _boost_exit(win, prev):
    try:
        if prev is not None and hasattr(win, "restore_thread"):
            win.restore_thread(prev)
    except Exception:
        pass
    with _BOOST_LOCK:
        _BOOST["n"] = max(0, _BOOST["n"] - 1)
        if _BOOST["n"] == 0 and _BOOST["interval"] is not None:
            sys.setswitchinterval(_BOOST["interval"])
            _BOOST["interval"] = None


class Tween:
    """Plays flight segments [(from, to, seconds, bow), ...] on a worker
    thread, one frame per display refresh (FramePacer). A segment that keeps
    the size is a spring move (core/orb_motion.Flight, MISTAI_ORB_MOTION read
    per play): one SetWindowPos per frame and nothing else; its ``seconds`` is
    only the caller's estimate. A segment that changes the size is a short
    timed resize step (smoothstep), the region recut per frame. Positions come
    from the clock, never from a frame count. retarget() redirects a spring
    move in flight without a jump; a new play() (or cancel()) stops the one
    before. With Windows animations off every segment ends at once.
    MISTAI_ORB_FRAME_STATS=1 logs one numbers-only line per play."""

    FRAME_S = 1 / 60          # only for a Win32 stand-in without make_pacer
    JOIN_S = 0.05
    _logged_animate = True    # log "animations off" once per change, not per flight

    def __init__(self, win, hwnd, clock=time.perf_counter):
        self._win, self._hwnd, self._clock = win, hwnd, clock
        self._stop = None
        self._thread = None
        self._leg = None          # the spring Flight being flown, else None
        self._pending = None      # (landing, tail segments) from retarget()
        self.side = None          # the side last applied (physical px)
        self.goal = None          # where the current play ends
        self.last_stats = None

    def _ask(self, name, default):
        fn = getattr(self._win, name, None)
        if fn is None:
            return default
        try:
            return fn(self._hwnd) if name == "refresh_rate" else fn()
        except Exception:
            return default

    def play(self, segments, on_frame=None, done=None):
        self.cancel()
        old = self._thread
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(self.JOIN_S)                    # never two threads moving the window
        segments = [tuple(s) for s in segments]
        stop = self._stop = threading.Event()
        self._leg, self._pending = None, None
        self.goal = tuple(segments[-1][1]) if segments else None
        opts = {"motion": orb_motion(), "stats": orb_frame_stats(),
                "animate": bool(self._ask("animations_enabled", True)),
                "hz": valid_refresh(self._ask("refresh_rate", 60))}
        if opts["animate"] != Tween._logged_animate:
            Tween._logged_animate = opts["animate"]
            if not opts["animate"]:
                print("[FloatingOrb] Windows animation effects are off: the orb jumps")
        t = self._thread = threading.Thread(target=self._run,
                                            args=(stop, segments, on_frame, done, opts),
                                            daemon=True, name="OrbTween")
        t.start()
        return t

    def _apply(self, sq, on_frame, counts):
        counts[0] += 1
        if not apply_square(self._win, self._hwnd, *sq, last_side=self.side):
            counts[1] += 1
        self.side = sq[2]
        if on_frame:
            on_frame(sq)

    def _run(self, stop, segments, on_frame, done, opts):
        counts = [0, 0]                              # frames applied, failed
        times = []
        pacer = None
        if not opts["animate"]:                      # Windows animations off: jump
            for _start, end, _s, _bow in segments:
                if stop.is_set():
                    return
                self._apply(tuple(end), on_frame, counts)
        else:
            pacer = (self._win.make_pacer(opts["hz"]) if hasattr(self._win, "make_pacer")
                     else _EventPacer(stop, self.FRAME_S))
            prev = _boost_enter(self._win)
            try:
                self._fly(stop, segments, on_frame, opts, counts, times, pacer)
            finally:
                _boost_exit(self._win, prev)
                pacer.close()
                self._leg, self._pending = None, None
        if counts[1]:
            print(f"[FloatingOrb] tween: {counts[1]} of {counts[0]} frames failed")
        if opts["stats"] and times:
            self.last_stats = frame_stats(times, opts["hz"])
            print(stats_line(self.last_stats, pacer.kind if pacer else "none", opts["motion"]))
        if done and not stop.is_set():
            done()

    def _fly(self, stop, segments, on_frame, opts, counts, times, pacer):
        """The frame loop: one clock read per frame, no logging, no locks."""
        queue = list(segments)
        while queue and not stop.is_set():
            start, end, seconds, bow = queue.pop(0)
            last = tuple(start)                      # already there: no frame for it
            now = t0 = self._clock()
            side = end[2]
            leg = None
            if start[2] == side:                     # a move: the spring
                leg = self._leg = Flight(start[:2], end[:2], now, opts["motion"], bow)
            while not stop.is_set():
                if opts["stats"]:
                    times.append(now)
                if leg is not None:
                    pend = self._pending
                    if pend is not None:
                        self._pending = None
                        leg.retarget(now, pend[0][:2])
                        queue = list(pend[1])
                    x, y, finished = leg.at(now)
                    sq = (int(round(x)), int(round(y)), side)
                else:                                # a resize step
                    t = 1.0 if seconds <= 0 else (now - t0) / seconds
                    sq = square_at(start, end, t, bow)
                    finished = t >= 1.0
                if sq != last:
                    self._apply(sq, on_frame, counts)
                    last = sq
                if finished:
                    break
                pacer.wait()
                now = self._clock()
            self._leg, self._pending = None, None

    @property
    def in_flight(self):
        """A spring move is flying (retarget() can redirect it)."""
        return self._leg is not None and self.running

    def retarget(self, goal):
        """Redirect the spring move in flight to ``goal`` (x, y, side),
        keeping its position and velocity: it lands at the current size on
        the goal's centre, then resizes there. False when no move is flying."""
        if not self.in_flight or self.side is None:
            return False
        goal = tuple(int(v) for v in goal)
        side = self.side
        land = goal if goal[2] == side else resize_about_center(goal[0], goal[1], goal[2], side)
        tail = [] if land == goal else [(land, goal, RESIZE_S, 0.0)]
        self.goal = goal
        self._pending = (land, tail)
        return True

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


def hide_from_taskbar(hwnd, win, tag):
    """WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE on, WS_EX_APPWINDOW off, then hide
    and re-show without activating: the taskbar and Alt-Tab only re-read a
    window's styles when it is shown, so bits set on a visible WinForms form
    (ShowInTaskbar is True by default) leave its button in place. Returns
    True when the read-back has both bits and no APPWINDOW."""
    try:
        win.set_exstyle(hwnd, add=WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, remove=WS_EX_APPWINDOW)
        win.show(hwnd, False)
        win.show(hwnd, True)
        style = win.exstyle(hwnd)
        ok = bool(style & WS_EX_TOOLWINDOW and style & WS_EX_NOACTIVATE
                  and not style & WS_EX_APPWINDOW)
        print(f"[{tag}] taskbar: {'hidden' if ok else 'still listed'}")
        return ok
    except Exception as e:
        print(f"[{tag}] SetWindowLongW error ({type(e).__name__})")
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
        if orb_debug_capture():
            out["capture"] = "debug-visible"
            print("[FloatingOrb] capture exclusion off (MISTAI_ORB_DEBUG_CAPTURE)")
        elif win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            out["capture"] = "excluded"
        elif win.set_affinity(hwnd, WDA_MONITOR):
            out["capture"] = "black"
        else:
            _fail("SetWindowDisplayAffinity", win)
    except Exception as e:
        print(f"[FloatingOrb] SetWindowDisplayAffinity error ({type(e).__name__})")
    out["noactivate"] = hide_from_taskbar(hwnd, win, "FloatingOrb")
    scale = 1.0
    side = None
    try:
        monitor, work = win.primary_work_area()
        scale = win.monitor_scale(monitor, hwnd) or 1.0
        x, y, side = place(work, scale)
        if not win.move(hwnd, x, y, side, side):
            _fail("SetWindowPos", win)
    except Exception as e:
        print(f"[FloatingOrb] placement error ({type(e).__name__})")
    try:
        rect = win.window_rect(hwnd)       # the ACTUAL size after the move
        if rect is None:
            _fail("GetWindowRect", win)
            return out
        l, t, r, b = rect
        out["position"] = (l, t, r - l, b - t, round(scale, 2))
        req = f"{side},{side}" if side else "none"
        print(f"[FloatingOrb] at {l},{t} requested {req} actual {r - l},{b - t} "
              f"monitor-scale {scale:.2f}")
        if side and (r - l, b - t) != (side, side):
            print("[FloatingOrb] actual size differs from the requested size")
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


def _corner_position(size=None, win=None):
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
        self._cur_target = None   # this tick's target dict or None
        self._logged_target = None
        self.panels = None        # core.orb_panels.OrbPanels once used
        self.aborted = False      # the task failed, paused or was stopped
        self._was_busy = False
        self._busy_since = 0.0
        self._kick = threading.Event()     # tick now (a step just ran)
        self._fast_until = 0.0             # poll faster after a step
        self._acted = False                # the task has run an action
        self._last_action = ""             # its most recent action id
        self._pre_task_fg = None           # the window in front when it started
        self._dwell_until = 0.0            # hold a dock until then (minimum dwell)
        self._tour = None                  # {"stops": [...], "i", "next_at"} while touring
        self._tour_gen = 0                 # bumped by every step: stale tours are dropped
        self._answer = None                # {"at", "words"} from the final AgentStep
        self._clock = time.monotonic
        self._sleep = time.sleep           # the tour's one re-read (tests: no-op)

    TICK_S = 0.25
    DWELL_S = 1.5             # a docked arrival stays put this long
    FAST_TICK_S = 0.1
    LAUNCH_POLL_S = 2.0

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

    def _element_rect(self, window_rect, scale):
        """The rect the current task's last click touched (a UIA control's
        rect, else a box around the click point), if it was made during this
        task and lies in ``window_rect``; else None."""
        best = None
        for owner in (getattr(self._api, "click_handler", None),
                      getattr(self._api, "uia_nav", None)):
            got = getattr(owner, "last_element_rect", None)
            if got and (best is None or got[1] > best[1]):
                best = got
        if not best or best[1] < self._busy_since:
            return None
        rect = tuple(int(v) for v in best[0])
        if len(rect) == 2:                           # a click point
            pad = round(ELEMENT_PAD * scale)
            rect = (rect[0] - pad, rect[1] - pad, rect[0] + pad, rect[1] + pad)
        wl, wt, wr, wb = window_rect
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        return rect if wl <= cx <= wr and wt <= cy <= wb else None

    def _ignored(self, hwnd):
        """MistAI's own windows (main window, orb, panels, its PID), the
        taskbar/desktop shell and Task Manager are never a target."""
        if not hwnd or hwnd in self.handles():
            return True
        try:
            if hwnd == self._api._own_window_handle():
                return True
        except Exception:
            pass
        pid, name, cls = self._win.window_info(hwnd)
        if cls in SHELL_CLASSES:
            return True
        from .config import is_self_process, process_matches

        try:
            if is_self_process(name, pid):
                return True
        except Exception:
            pass
        return any(process_matches(p, name) for p in IGNORED_PROCESSES)

    def _target(self):
        """(target dict or None, reason label). Only after the task's first
        action. browser_* steps: MistAI's own browser window ("browser").
        Otherwise the window in front ("foreground"), unless it is one of
        ours, the shell or Task Manager ("no-window"), or it is the same
        window that was in front before the task and not the app the step
        opened or switched to ("unrelated-window": the user's own window).
        {"rect", "work", "scale", "inside", "element"}; a window covering
        MAXIMIZED of its work area is docked inside, beside the element the
        step touched. Quiet: it runs up to ten times a second."""
        if not self._acted:
            return None, "no-action-yet"
        why = "foreground"
        hwnd = None
        if self._last_action.startswith("browser_"):
            hwnd = self._win.browser_window()
            why = "browser"
        if not hwnd:
            why = "foreground"
            hwnd = self._win.foreground()
            if self._ignored(hwnd):
                return None, "no-window"
            app = getattr(self._api, "_last_opened_app", None)
            touched = False
            if app:
                try:
                    touched = bool(self._api._window_belongs_to(hwnd, app))
                except Exception:
                    touched = False
            if hwnd == self._pre_task_fg and not touched:
                return None, "unrelated-window"
        rect = self._win.window_rect(hwnd)
        if not rect:
            return None, "no-rect"
        work, scale = self._win.monitor_for_rect(rect)
        scale = scale or 1.0
        target = {"rect": tuple(rect), "work": tuple(work), "scale": scale,
                  "inside": coverage(rect, work) >= MAXIMIZED, "element": None}
        if not target["inside"]:
            return target, why
        target["element"] = self._element_rect(rect, scale)
        return target, ("maximized-element" if target["element"] else "maximized-edge")

    def _log_target(self, target, why):
        key = (why, target and target["rect"], target and target["element"])
        if key != self._logged_target:
            self._logged_target = key
            if target:
                l, t, r, b = target["rect"]
                el = target["element"]
                el_txt = f" element {el[0]},{el[1]},{el[2]},{el[3]}" if el else ""
                print(f"[FloatingOrb] target {why} rect {l},{t},{r},{b}{el_txt}")
            else:
                print(f"[FloatingOrb] target none ({why})")

    def _here_scale(self):
        try:
            x, y, s = self.square
            return self._win.monitor_for_rect((x, y, x + s, y + s))[1] or self.scale
        except Exception:
            return self.scale

    def _goal(self, state, from_square=None):
        """(x, y, side) the orb should have now. Without flight: at home,
        resized about its center for the state. With flight (Settings "Orb
        follows the task"): docked small beside the task's window while one
        is known and in front, medium where it is while working or speaking,
        else big at home. ``from_square``: judge from there instead of the
        current square (a flight's destination, so a goal re-checked in
        flight doesn't wander with the orb)."""
        x, y, side = from_square or self.square
        self.dock = None
        if not self._flight_on():
            return resize_about_center(x, y, side, round(size_for(state) * self.scale))
        if self.aborted or self._stop_requested():
            self._answer = None
            return self.home                         # failed, paused or stopped: home now
        if self._answer_speaking():
            panel = self.panels.rects.get("answer") if self.panels is not None else None
            medium = round(orb_sizes()[1] * self._here_scale())
            if panel:                                # beside the answer panel, speaking
                self.dock = "answer"
                work, _scale = self._win.monitor_for_rect(panel)
                return answer_spot(panel, medium, work) + (medium,)
            return resize_about_center(x, y, side, medium)
        busy = self._busy()
        target = self._cur_target
        _big, medium, small_l = orb_sizes()
        if target:
            rect, work, scale = target["rect"], target["work"], target["scale"]
            small = max(1, round(small_l * scale))
            if target["inside"]:
                self.dock = "element" if target["element"] else "inner-edge"
                pos = dock_inside(rect, work, small, target["element"],
                                  near=(x + side / 2, y + side / 2))
            else:
                self.dock = dock_side(rect, work, small, 8)
                pos = dock_position(rect, work, small)
            return pos + (small,)
        if busy or state == "speaking":
            # No target yet (or it left the front): stay where it is.
            return resize_about_center(x, y, side, round(medium * self._here_scale()))
        return self.home

    def _answer_speaking(self):
        """Is the final answer being spoken? The TTS's own flag when it has
        one (plus a moment before the voice starts), else an estimate from
        the word count, capped. Clears the answer once it is over."""
        a = self._answer
        if a is None:
            return False
        elapsed = self._clock() - a["at"]
        tts = getattr(self._api, "tts", None)
        if tts is not None and hasattr(tts, "speaking"):
            on = bool(tts.speaking) or elapsed < SPEECH_START_S
        else:
            on = elapsed < min(SPEECH_CAP_S, max(SPEECH_START_S, a["words"] / WORDS_PER_S))
        if not on:
            self._answer = None
        return on

    def _stop_requested(self):
        try:
            return self._api._stop_requested.is_set()
        except Exception:
            return False

    def _on_bus(self, ev):
        """Bus subscriber (fast): a step ran -> tick now (and poll faster
        after a launch); a task failed or paused, or the network sentence ->
        go home."""
        try:
            name, data = ev.name, ev.data or {}
            if name in (events.ACTION_EXECUTED, events.ACTION_FAILED):
                # After every step: re-check now, then every 0.1 s for a while
                # (the foreground can change a moment later, e.g. a launch).
                self._acted = True
                self._last_action = str(data.get("action", ""))
                self._fast_until = time.monotonic() + self.LAUNCH_POLL_S
            if name == events.AGENT_STEP:
                self._cancel_tour()                  # the next real action or "done"
                if data.get("action") == "done" and data.get("speech"):
                    # Only the length is kept (for the estimate), never the text.
                    self._answer = {"at": self._clock(),
                                    "words": len(str(data["speech"]).split())}
            if (name == events.ACTION_EXECUTED and data.get("action") == "browser_search"
                    and self._flight_on()):
                gen = self._tour_gen
                threading.Thread(target=self._prepare_tour, args=(gen,), daemon=True,
                                 name="OrbTour").start()
            if name in (events.TASK_FAILED, events.TASK_PAUSED) or (
                    name == events.AGENT_STEP and data.get("action") == "failed"):
                self.aborted = True
            self._kick.set()
        except Exception:
            pass

    # -- the search tour ---------------------------------------------------------
    # Choreography only: while the agent waits on its next model call after a
    # browser_search, the orb hops down the first results. It does NOT mean the
    # results were read: it adds no step, tick or event, never delays an action,
    # and the next AgentStep (a real action or "done") cancels it.

    def _cancel_tour(self):
        self._tour_gen += 1
        if self._tour is not None:
            self._tour = None
            try:
                if not getattr(self._tween, "in_flight", False):
                    self._tween.cancel()             # in flight: tick() retargets it instead
            except Exception:
                pass

    def _read_tour_rects(self):
        """(screen rects or None, counts or None, browser window rect)."""
        try:
            info = self._api.browser.link_boxes(RESULT_LINKS, TOUR_CANDIDATES)
            hwnd = self._win.browser_window()
            window = self._win.window_rect(hwnd) if hwnd else None
            if not (info and window):
                return None, None, window
            rects, counts = classify_page_rects(info, window, TOUR_STOPS)
            return rects, counts, window
        except Exception:
            return None, None, None

    def _prepare_tour(self, gen):
        """Worker thread: read the result link boxes and build the stops; no
        usable rect skips the tour (the normal docking stays). Fewer results
        than stops found: the page may still be rendering, so it reads once
        more after TOUR_RETRY_S. Logs counts and fixed labels only."""
        rects, counts, window = self._read_tour_rects()
        if counts and counts["found"] < TOUR_STOPS and gen == self._tour_gen:
            self._sleep(TOUR_RETRY_S)
            if gen == self._tour_gen:
                again = self._read_tour_rects()
                if again[1] and again[1]["found"] >= counts["found"]:
                    rects, counts, window = again
        if counts:
            why = ", ".join(f"{k} {counts[k]}" for k in TOUR_REJECTIONS if counts.get(k))
            print(f"[FloatingOrb] tour candidates found {counts['found']} measured "
                  f"{counts['measured']} accepted {counts['accepted']} rejected "
                  f"{counts['rejected']}" + (f" ({why})" if why else ""))
        if gen != self._tour_gen:
            return                                   # a step arrived meanwhile
        if not rects:
            print("[FloatingOrb] tour skipped (no-rects)")
            return
        work, scale = self._win.monitor_for_rect(window)
        _big, medium, small = orb_sizes()
        small, medium = round(small * (scale or 1.0)), round(medium * (scale or 1.0))
        area = (max(window[0], work[0]), max(window[1], work[1]),
                min(window[2], work[2]), min(window[3], work[3]))
        stops = [margin_spot(r, area, small) + (small,) for r in rects]
        self._tour = {"stops": stops, "i": 0, "next_at": 0.0, "medium": medium}
        print(f"[FloatingOrb] tour stops {len(stops)}")
        self._kick.set()

    def _tour_step(self):
        """Play the next hop when the last one has dwelt; True while touring."""
        tour = self._tour
        if tour is None:
            return False
        if self._clock() < tour["next_at"]:
            return True
        if tour["i"] >= len(tour["stops"]):
            self._tour = None
            return False
        stop = tour["stops"][tour["i"]]
        plan = tour_plan(self.square, stop, tour["medium"])
        tour["i"] += 1
        tour["next_at"] = self._clock() + sum(s[2] for s in plan) + TOUR_DWELL_S   # estimate
        self.dock = "tour"
        self._tween.play(plan, on_frame=self._set_square,
                         done=lambda: self._hop_landed(tour))
        return True

    def _hop_landed(self, tour):
        """Tween thread: a hop has landed; dwell from now."""
        if self._tour is tour:
            tour["next_at"] = self._clock() + TOUR_DWELL_S

    def _landed(self):
        """Tween thread: a flight has landed; a dock dwells from now (finish,
        then dwell)."""
        if self.dock and self.dock != "tour" and not (self.aborted or self._stop_requested()):
            self._dwell_until = self._clock() + self.DWELL_S

    def _follow_in_flight(self):
        """In flight and the goal moved (a new dock, the tour cancelled):
        retarget the spring, keeping its position and velocity, instead of
        waiting for it to land. The goal is judged from the destination."""
        tw = self._tween
        if self._tour is not None or not getattr(tw, "in_flight", False):
            return
        dest = getattr(tw, "goal", None)
        if not dest:
            return
        goal = self._goal(orb_state(self._api), from_square=dest)
        if max(abs(a - b) for a, b in zip(goal, dest)) <= 2:
            return
        if tw.retarget(goal) and self.dock != self._logged_dock:
            self._logged_dock = self.dock
            print(f"[FloatingOrb] dock {self.dock or 'none'} size {goal[2]} (retarget)")

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
        if self._cur_target:                          # the target's monitor
            t = self._cur_target
            rect, work, scale, maximized = t["rect"], t["work"], t["scale"], t["inside"]
        else:                                         # else the primary monitor
            rect, maximized = None, False
            monitor, work = self._win.primary_work_area()
            scale = self._win.monitor_scale(monitor)
        speaking = bool(getattr(getattr(self._api, "tts", None), "speaking", False))
        self.panels.update(self.square, rect, work, scale or self.scale, busy,
                           maximized=maximized, ended=self.aborted or self._stop_requested(),
                           speaking=speaking)

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
            busy = self._busy()
            if busy and not self._was_busy:          # a new task
                self.aborted = False
                self._answer = None
                self._busy_since = time.monotonic()
                self._acted, self._last_action = False, ""
                try:
                    self._pre_task_fg = self._win.foreground()
                except Exception:
                    self._pre_task_fg = None
            self._was_busy = busy
            self._sync_click_through()
            try:
                self._cur_target, why = self._target() if busy else (None, "idle")
            except Exception:
                self._cur_target, why = None, "error"
            if busy and self._flight_on():
                self._log_target(self._cur_target, why)
            self._sync_panels(busy)
            if self._tween.running:
                self._follow_in_flight()
                return
            if busy and self._tour_step():
                return                               # touring the results
            if self._clock() < self._dwell_until and not (self.aborted
                                                          or self._stop_requested()):
                return                               # minimum dwell at a dock
            self._adopt_drag()
            goal = self._goal(orb_state(self._api))
            if self.dock != self._logged_dock:
                self._logged_dock = self.dock
                print(f"[FloatingOrb] dock {self.dock or 'none'} size {goal[2]}")
            if max(abs(a - b) for a, b in zip(goal, self.square)) <= 2:
                return
            plan = flight_plan(self.square, goal)
            if self.dock:                            # a flight to a dock: finish, then dwell
                # (an estimate now; _landed() restarts the dwell at the landing)
                self._dwell_until = self._clock() + sum(s[2] for s in plan) + self.DWELL_S
            self._tween.play(plan, on_frame=self._set_square, done=self._landed)
        except Exception as e:
            print(f"[FloatingOrb] tick error ({type(e).__name__})")

    def _set_square(self, sq):
        self.square = sq

    def _run(self):
        while not self._loop_stop.is_set():
            fast = time.monotonic() < self._fast_until
            self._kick.wait(self.FAST_TICK_S if fast else self.TICK_S)
            self._kick.clear()
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
                    width=orb_sizes()[0], height=orb_sizes()[0], x=x, y=y,
                    min_size=MIN_SIZE,
                    resizable=False, frameless=True, easy_drag=True, shadow=False,
                    focus=False, on_top=True, background_color=orb_background(),
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
                self._tween.side = w
                try:
                    for name in (events.ACTION_EXECUTED, events.ACTION_FAILED,
                                 events.TASK_FAILED,
                                 events.TASK_PAUSED, events.AGENT_STEP):
                        self._api.bus.subscribe(name, self._on_bus)
                except Exception:
                    pass
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
