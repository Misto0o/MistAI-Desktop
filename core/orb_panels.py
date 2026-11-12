"""
core/orb_panels.py — task panels beside the floating orb (Settings "Task
panels" / MISTAI_ORB_PANELS, default off; needs the floating orb shown).

Three small frameless windows built like the orb (core/floating_orb.py):
excluded from capture, never activated (WS_EX_NOACTIVATE), untitled, in
MistAI's own process (so Open apps and process lookups skip them), listed in
the orb's handles() (so the OCR mask blacks them out and input is refused
while one is in front). Each page reads its content through its own
PanelBridge: panel_state() (read-only) and dismiss() (hides that panel only).

- steps: each step as a human label from the tool's category (label_for:
  "Clicking", "Typing", "Reading the screen", ...), ticked as each one
  completes (the last one is the current step); answer: the final speech.
  They close ANSWER_S after the answer, and at once when the task fails, is
  paused or stopped, or the network sentence is spoken.

Content comes from the event bus (events.AGENT_STEP, ACTION_EXECUTED/FAILED)
and is never logged; logs carry labels and numbers only.
"""

import threading
import time

from . import events
from .config import orb_debug_capture, panel_anchor, panel_blur_enabled
from .orb_layout import anchored_panels

KINDS = ("steps", "answer")
SIZES = {"steps": (240, 176), "answer": (280, 140)}   # logical px
ANSWER_S = 8.0
MAX_STEPS = 8
CORNER = 12                  # logical px
PANEL_BACKGROUND = "#161c26"  # = the page's panel colour, so nothing else shows
MIN_SIZE = (1, 1)             # pywebview's default min_size (200, 100) clamps panels

# Tool categories (core/tools.py SPECS) -> labels, first match wins.
CATEGORY_LABELS = (
    ("perception", "Reading the screen"), ("click", "Clicking"), ("type", "Typing"),
    ("browser", "Using the browser"), ("app", "Opening an app"),
    ("window", "Arranging windows"), ("system", "Running a command"),
    ("file", "Working with files"), ("messaging", "Sending a message"),
    ("navigation", "Scrolling"), ("memory", "Remembering"), ("info", "Looking it up"),
)

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3   # acrylic


def label_for(action, categories=()):
    """A human label from the tool's categories ("click" -> "Clicking");
    "Working" when none is known. Never the action id or its parameter."""
    for cat, label in CATEGORY_LABELS:
        if cat in tuple(categories or ()):
            return label
    return "Working"


def tool_categories(api, action):
    try:
        return tuple(api.tools.get(action).categories or ())
    except Exception:
        return ()


class PanelModel:
    """What the panels show, fed by the bus (subscribers must be fast)."""

    def __init__(self, clock=time.monotonic, categories=None):
        self._lock = threading.Lock()
        self._clock = clock
        self._categories = categories or (lambda action: ())
        self.reset()

    def reset(self):
        with self._lock:
            self.steps = []           # [{"label", "action", "state": run|ok|fail}]
            self.answer = ""
            self.answer_at = None
            self.spoken_until = None  # when Mist last spoke the answer
            self.closed = set()
            self.ended = False        # failed, paused, stopped: close all now

    def on_event(self, ev):
        try:
            data = ev.data or {}
            with self._lock:
                if ev.name == events.AGENT_STEP:
                    action = str(data.get("action", ""))
                    if action == "failed":
                        self.ended = True
                    elif action == "done":
                        self.answer = str(data.get("speech", ""))
                        self.answer_at = self._clock()
                    else:
                        self.steps.append({"label": label_for(action, self._categories(action)),
                                           "action": action, "state": "run"})
                        del self.steps[:-MAX_STEPS]
                elif ev.name in (events.TASK_FAILED, events.TASK_PAUSED):
                    self.ended = True
                elif ev.name in (events.ACTION_EXECUTED, events.ACTION_FAILED):
                    action = str(data.get("action", ""))
                    for step in reversed(self.steps):
                        if step["state"] == "run" and step["action"] == action:
                            step["state"] = "ok" if ev.name == events.ACTION_EXECUTED else "fail"
                            break
        except Exception:
            pass

    def mark_speaking(self, speaking):
        """Panels close ANSWER_S after the answer's speech ENDS."""
        if speaking and self.answer_at is not None:
            self.spoken_until = self._clock()

    def expired(self):
        if self.answer_at is None:
            return False
        last = max(self.answer_at, self.spoken_until or self.answer_at)
        return self._clock() - last > ANSWER_S

    def visible(self, kind, busy):
        """Should this panel be on screen now?"""
        if self.ended or kind in self.closed or self.expired():
            return False
        if kind == "answer":
            return bool(self.answer)
        return bool(self.steps) and (busy or self.answer_at is not None)

    def state(self, kind):
        with self._lock:
            if kind == "steps":
                return {"kind": kind, "steps": [{"label": s["label"], "state": s["state"]}
                                                for s in self.steps]}
            return {"kind": kind, "text": self.answer}

    def close(self, kind):
        with self._lock:
            self.closed.add(kind)


class PanelBridge:
    """A panel page's js_api: its own content (read-only) and its own close."""

    def __init__(self, panels, kind):
        self._panels, self._kind = panels, kind

    def panel_state(self):
        st = self._panels.model.state(self._kind)
        st["side"] = self._panels.side
        return st

    def dismiss(self):
        self._panels.model.close(self._kind)
        return {"ok": True}


def harden_panel(hwnd, win, w, h, scale, blur):
    """Capture exclusion, no activation, rounded corners. Returns "blur" when
    the DWM backdrop took (Windows 11), else "plain" (a rounded region)."""
    from .floating_orb import WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR

    from .floating_orb import hide_from_taskbar

    try:
        if not orb_debug_capture() and not win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            win.set_affinity(hwnd, WDA_MONITOR)
    except Exception as e:
        print(f"[OrbPanels] hardening error ({type(e).__name__})")
    hide_from_taskbar(hwnd, win, "OrbPanels")
    if blur:
        try:
            if (win.dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW) == 0
                    and win.dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND) == 0):
                return "blur"
        except Exception:
            pass
    harden_corners(hwnd, win, w, h, scale)
    return "plain"


def harden_corners(hwnd, win, w, h, scale):
    """Cut rounded corners for a window of this actual size."""
    try:
        r = max(2, round(CORNER * 2 * scale))
        region = win.create_round_rect((0, 0, int(w) + 1, int(h) + 1, r, r))
        if region and not win.set_region(hwnd, region):
            win.delete_object(region)
    except Exception as e:
        print(f"[OrbPanels] region error ({type(e).__name__})")


class OrbPanels:
    """The three panel windows. update() is called from the orb's control
    loop with the orb square, the target rect (or None) and the work area."""

    def __init__(self, api, win, webview=None, html=None, clock=time.monotonic):
        self._api, self._win = api, win
        self._webview, self._html = webview, html
        self.model = PanelModel(clock, lambda action: tool_categories(api, action))
        self.windows = {}             # kind -> pywebview window
        self.hwnds = {}               # kind -> hwnd once shown
        self.shown = {}               # kind -> bool
        self.side = "top-right"       # the anchor in use (the page slides in from above)
        self.rects = {}               # kind -> (l, t, r, b) while shown
        self.style = None
        self.sizes = {}               # kind -> actual (w, h) the corners were cut for
        self._was_busy = False
        self._unsubs = []
        try:
            bus = api.bus
            for name in (events.AGENT_STEP, events.ACTION_EXECUTED, events.ACTION_FAILED,
                         events.TASK_FAILED, events.TASK_PAUSED):
                self._unsubs.append(bus.subscribe(name, self.model.on_event))
        except Exception:
            pass

    def handles(self):
        return [h for k, h in self.hwnds.items() if h and self.shown.get(k)]

    def _module(self):
        if self._webview is None:
            import webview

            self._webview = webview
        return self._webview

    def _create(self, kind, x, y, w, h, scale):
        html = self._html
        if html is None:
            from ui.html import PANEL_HTML as html
        window = self._module().create_window(
            "", html=html, js_api=PanelBridge(self, kind),
            width=round(w / scale), height=round(h / scale),
            x=round(x / scale), y=round(y / scale),
            min_size=MIN_SIZE,
            resizable=False, frameless=True, easy_drag=False, shadow=False,
            focus=False, on_top=True, background_color=PANEL_BACKGROUND,
        )
        self.windows[kind] = window

        def shown():
            try:
                hwnd = int(window.native.Handle.ToInt32()) or None
            except Exception:
                hwnd = None
            if not hwnd:
                return
            self.hwnds[kind] = hwnd
            self._win.move(hwnd, x, y, w, h)
            aw, ah = self._actual(hwnd, w, h, kind)
            style = harden_panel(hwnd, self._win, aw, ah, scale, panel_blur_enabled())
            self.sizes[kind] = (aw, ah)
            if self.style is None:
                self.style = style
                print(f"[OrbPanels] panel style: {style}")

        window.events.shown += shown

    def _actual(self, hwnd, w, h, kind):
        """The window's real (w, h) after a move; logged next to the request."""
        try:
            l, t, r, b = self._win.window_rect(hwnd)
            aw, ah = r - l, b - t
        except Exception:
            return w, h
        if (aw, ah) != (w, h) or kind not in self.sizes:
            print(f"[OrbPanels] {kind} requested {w},{h} actual {aw},{ah}")
        return aw, ah

    def update(self, orb, target_rect, work, scale, busy, maximized=False, ended=False,
               speaking=False):
        """Show, place or hide the panels for this tick: anchored to the top of
        ``work`` (the target's monitor, else the primary), never over the orb.
        Never raises."""
        try:
            if busy and not self._was_busy:
                self.model.reset()
            self._was_busy = busy
            if ended:
                self.model.ended = True
            self.model.mark_speaking(speaking)
            if self.model.expired() and not busy:
                self.model.reset()
            kinds = [k for k in KINDS if self.model.visible(k, busy)]
            sizes = [tuple(round(v * scale) for v in SIZES[k]) for k in kinds]
            spots, self.side = anchored_panels(sizes, work, panel_anchor(), scale,
                                               target_rect, maximized, orb)
            for kind, (x, y), (w, h) in zip(kinds, spots, sizes):
                self.rects[kind] = (x, y, x + w, y + h)
                hwnd = self.hwnds.get(kind)
                if kind not in self.windows:
                    self._create(kind, x, y, w, h, scale)
                elif hwnd:
                    if self.sizes.get(kind) == (w, h):
                        self._win.move_only(hwnd, x, y)
                    else:                     # new size (another monitor): recut corners
                        self._win.move(hwnd, x, y, w, h)
                        aw, ah = self._actual(hwnd, w, h, kind)
                        harden_corners(hwnd, self._win, aw, ah, scale)
                        self.sizes[kind] = (aw, ah)
                    if not self.shown.get(kind):
                        self._win.show(hwnd, True)
                if not self.shown.get(kind):
                    print(f"[OrbPanels] show {kind}")
                self.shown[kind] = True
            for kind in KINDS:
                if kind not in kinds and self.shown.get(kind):
                    if self.hwnds.get(kind):
                        self._win.show(self.hwnds[kind], False)
                    self.shown[kind] = False
                    self.rects.pop(kind, None)
                    print(f"[OrbPanels] hide {kind}")
        except Exception as e:
            print(f"[OrbPanels] update error ({type(e).__name__})")

    def hide_all(self):
        for kind in KINDS:
            if self.shown.get(kind) and self.hwnds.get(kind):
                try:
                    self._win.show(self.hwnds[kind], False)
                except Exception:
                    pass
            self.shown[kind] = False
