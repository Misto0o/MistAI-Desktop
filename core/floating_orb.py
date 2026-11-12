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
talks to Python only through OrbBridge.orb_state(), never the full Api.

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

ORB_SIZE = 112            # window width and height, logical px (96 DPI)
ORB_MARGIN = 16           # logical px from the work area's right and bottom edges
ORB_BACKGROUND = "#05080f"

WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
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
    """The orb page's js_api: one read-only method, so the floating window
    cannot reach the rest of the Api."""

    def __init__(self, api):
        self._api = api

    def orb_state(self):
        return {"state": orb_state(self._api)}


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
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self._RECT = wintypes.RECT

    def last_error(self):
        return ctypes.get_last_error()

    def set_affinity(self, hwnd, mode):
        return bool(self.user32.SetWindowDisplayAffinity(hwnd, mode))

    def add_exstyle(self, hwnd, bits):
        style = self.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | bits)

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
        return int(x / scale), int(y / scale)
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

    def _on_shown(self):
        try:
            self.hwnd = int(self.window.native.Handle.ToInt32()) or None
        except Exception:
            self.hwnd = None
        if self.hwnd:
            self.hardening = harden(self.hwnd)
            print(f"[FloatingOrb] shown | capture: {self.hardening['capture']} | "
                  f"no-activate: {self.hardening['noactivate']} | round: {self.hardening['round']}")

    def handles(self):
        """The orb's window handle while it is shown (for the own-window mask)."""
        return [self.hwnd] if self.visible and self.hwnd else []
