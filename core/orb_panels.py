"""
core/orb_panels.py — task panels beside the floating orb (Settings "Task
panels" / MISTAI_ORB_PANELS, default off; needs the floating orb shown).

Three small frameless windows built like the orb (core/floating_orb.py):
excluded from capture, never activated (WS_EX_NOACTIVATE), untitled, in
MistAI's own process (so Open apps and process lookups skip them), listed in
the orb's handles() (so the OCR mask blacks them out and input is refused
while one is in front). Each page reads its content through its own
PanelBridge: panel_state() (read-only) and dismiss() (hides that panel only).

- pill: the current step, a label derived from the action id ("open_app" ->
  "Open app"); steps: the actions so far, ticked as each one completes;
  answer: the final speech. They close themselves ANSWER_S after the answer.

Content comes from the event bus (events.AGENT_STEP, ACTION_EXECUTED/FAILED)
and is never logged; logs carry labels and numbers only.
"""

import threading
import time

from . import events
from .config import panel_blur_enabled
from .orb_layout import panel_stack

KINDS = ("pill", "steps", "answer")
SIZES = {"pill": (240, 44), "steps": (260, 196), "answer": (300, 150)}   # logical px
ANSWER_S = 8.0
MAX_STEPS = 8
CORNER = 14                  # logical px
PANEL_BACKGROUND = "#10141c"

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_TRANSIENTWINDOW = 3   # acrylic


def label_for(action):
    """A human label from an action id, generically: "browser_get_text" ->
    "Browser get text". Never the parameter."""
    words = str(action or "").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Working"


class PanelModel:
    """What the panels show, fed by the bus (subscribers must be fast)."""

    def __init__(self, clock=time.monotonic):
        self._lock = threading.Lock()
        self._clock = clock
        self.reset()

    def reset(self):
        with self._lock:
            self.current = ""
            self.steps = []           # [{"label", "action", "state": run|ok|fail}]
            self.answer = ""
            self.answer_at = None
            self.closed = set()

    def on_event(self, ev):
        try:
            data = ev.data or {}
            with self._lock:
                if ev.name == events.AGENT_STEP:
                    action = str(data.get("action", ""))
                    if action == "done":
                        self.answer = str(data.get("speech", ""))
                        self.answer_at = self._clock()
                        self.current = "Done"
                    else:
                        self.current = label_for(action)
                        self.steps.append({"label": self.current, "action": action,
                                           "state": "run"})
                        del self.steps[:-MAX_STEPS]
                elif ev.name in (events.ACTION_EXECUTED, events.ACTION_FAILED):
                    action = str(data.get("action", ""))
                    for step in reversed(self.steps):
                        if step["state"] == "run" and step["action"] == action:
                            step["state"] = "ok" if ev.name == events.ACTION_EXECUTED else "fail"
                            break
        except Exception:
            pass

    def expired(self):
        return self.answer_at is not None and self._clock() - self.answer_at > ANSWER_S

    def visible(self, kind, busy):
        """Should this panel be on screen now?"""
        if kind in self.closed or self.expired():
            return False
        if kind == "answer":
            return bool(self.answer)
        if kind == "steps":
            return bool(self.steps) and (busy or self.answer_at is not None)
        return busy or self.answer_at is not None

    def state(self, kind):
        with self._lock:
            if kind == "pill":
                return {"kind": kind, "label": self.current or "Working"}
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
    from .floating_orb import WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW

    try:
        if not win.set_affinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
            win.set_affinity(hwnd, WDA_MONITOR)
        win.add_exstyle(hwnd, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception as e:
        print(f"[OrbPanels] hardening error ({type(e).__name__})")
    if blur:
        try:
            if (win.dwm_set(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_TRANSIENTWINDOW) == 0
                    and win.dwm_set(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND) == 0):
                return "blur"
        except Exception:
            pass
    try:
        r = max(2, round(CORNER * 2 * scale))
        region = win.create_round_rect((0, 0, int(w) + 1, int(h) + 1, r, r))
        if region and not win.set_region(hwnd, region):
            win.delete_object(region)
    except Exception as e:
        print(f"[OrbPanels] region error ({type(e).__name__})")
    return "plain"


class OrbPanels:
    """The three panel windows. update() is called from the orb's control
    loop with the orb square, the target rect (or None) and the work area."""

    def __init__(self, api, win, webview=None, html=None, clock=time.monotonic):
        self._api, self._win = api, win
        self._webview, self._html = webview, html
        self.model = PanelModel(clock)
        self.windows = {}             # kind -> pywebview window
        self.hwnds = {}               # kind -> hwnd once shown
        self.shown = {}               # kind -> bool
        self.side = "right"
        self.style = None
        self._was_busy = False
        self._unsubs = []
        try:
            bus = api.bus
            for name in (events.AGENT_STEP, events.ACTION_EXECUTED, events.ACTION_FAILED):
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
            style = harden_panel(hwnd, self._win, w, h, scale, panel_blur_enabled())
            self._win.move(hwnd, x, y, w, h)
            if self.style is None:
                self.style = style
                print(f"[OrbPanels] panel style: {style}")

        window.events.shown += shown

    def update(self, orb, target_rect, work, scale, busy):
        """Show, place or hide the panels for this tick. Never raises."""
        try:
            if busy and not self._was_busy:
                self.model.reset()
            self._was_busy = busy
            if self.model.expired() and not busy:
                self.model.reset()
            kinds = [k for k in KINDS if self.model.visible(k, busy)]
            sizes = [tuple(round(v * scale) for v in SIZES[k]) for k in kinds]
            spots = panel_stack(orb, sizes, work, target_rect)
            ox, _oy, s = orb
            self.side = "right" if spots and spots[0][0] >= ox + s else "left"
            for kind, (x, y), (w, h) in zip(kinds, spots, sizes):
                hwnd = self.hwnds.get(kind)
                if kind not in self.windows:
                    self._create(kind, x, y, w, h, scale)
                elif hwnd:
                    self._win.move(hwnd, x, y, w, h)
                    if not self.shown.get(kind):
                        self._win.show(hwnd, True)
                self.shown[kind] = True
            for kind in KINDS:
                if kind not in kinds and self.shown.get(kind):
                    if self.hwnds.get(kind):
                        self._win.show(self.hwnds[kind], False)
                    self.shown[kind] = False
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
