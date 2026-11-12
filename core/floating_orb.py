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
"""

ORB_SIZE = 112            # window width and height, logical px
ORB_MARGIN = (24, 72)     # from the right and bottom edges of the primary screen
ORB_BACKGROUND = "#05080f"

WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000


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


def harden(hwnd, user32=None, gdi=None):
    """Exclude ``hwnd`` from capture, make it non-activating and taskbar-free,
    and cut it to a circle. Returns {"capture": "excluded" | "black" | "none",
    "noactivate": bool, "round": bool}; never raises."""
    out = {"capture": "none", "noactivate": False, "round": False}
    try:
        if user32 is None:
            import ctypes

            user32 = ctypes.windll.user32
        if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            out["capture"] = "excluded"
        elif user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR):
            out["capture"] = "black"
    except Exception:
        pass
    try:
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        out["noactivate"] = True
    except Exception:
        pass
    try:
        if gdi is None:
            import win32gui as gdi
        l, t, r, b = gdi.GetWindowRect(hwnd)
        gdi.SetWindowRgn(hwnd, gdi.CreateEllipticRgn(0, 0, r - l, b - t), True)
        out["round"] = True
    except Exception:
        pass
    return out


def _corner_position(size=ORB_SIZE):
    """(x, y) near the primary screen's bottom-right corner, or (None, None)."""
    try:
        import win32api

        w, h = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
        return max(0, w - size - ORB_MARGIN[0]), max(0, h - size - ORB_MARGIN[1])
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
