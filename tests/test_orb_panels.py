"""Task panels beside the floating orb (core/orb_panels.py; Settings "Task
panels" / MISTAI_ORB_PANELS, default off). Fakes only: no window is created."""

import re
from types import SimpleNamespace

import pytest

import core.floating_orb as fo
import core.orb_panels as op
from core import events
from core.agent import AgentLoop
from core.orb_layout import overlaps
from ui.html import PANEL_HTML


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _ev(name, **data):
    return events.Event(name, data)


@pytest.mark.parametrize("action, label", [
    ("open_app", "Open app"), ("browser_get_text", "Browser get text"),
    ("press_key", "Press key"), ("", "Working"), (None, "Working"),
])
def test_labels_come_from_the_action_id_generically(action, label):
    assert op.label_for(action) == label


def test_the_model_follows_steps_ticks_and_the_answer():
    clock = _Clock()
    m = op.PanelModel(clock)
    m.on_event(_ev(events.AGENT_STEP, action="open_app"))
    assert m.state("pill")["label"] == "Open app"
    assert m.state("steps")["steps"] == [{"label": "Open app", "state": "run"}]
    m.on_event(_ev(events.ACTION_EXECUTED, action="open_app", parameter="secret"))
    m.on_event(_ev(events.AGENT_STEP, action="type_text"))
    m.on_event(_ev(events.ACTION_FAILED, action="type_text"))
    assert [s["state"] for s in m.state("steps")["steps"]] == ["ok", "fail"]
    assert m.visible("pill", True) and m.visible("steps", True)
    assert not m.visible("answer", True)
    m.on_event(_ev(events.AGENT_STEP, action="done", speech="It says 42."))
    assert m.state("answer")["text"] == "It says 42." and m.visible("answer", False)
    assert m.visible("steps", False)                  # stays with the answer
    clock.t += op.ANSWER_S + 0.1
    assert not any(m.visible(k, False) for k in op.KINDS)


def test_steps_are_capped_and_a_bad_event_is_ignored():
    m = op.PanelModel(_Clock())
    for i in range(op.MAX_STEPS + 5):
        m.on_event(_ev(events.AGENT_STEP, action=f"a{i}"))
    assert len(m.state("steps")["steps"]) == op.MAX_STEPS
    m.on_event(SimpleNamespace(name=events.AGENT_STEP, data=None))
    m.on_event(object())                              # never raises


def test_the_bridge_exposes_only_its_own_state_and_close():
    panels = SimpleNamespace(model=op.PanelModel(_Clock()), side="left")
    bridge = op.PanelBridge(panels, "answer")
    assert [n for n in dir(bridge) if not n.startswith("_")] == ["dismiss", "panel_state"]
    assert bridge.panel_state() == {"kind": "answer", "text": "", "side": "left"}
    panels.model.answer = "hi"
    bridge.dismiss()
    assert panels.model.closed == {"answer"}
    assert not panels.model.visible("answer", True) and panels.model.visible("pill", True)


# -- windows ------------------------------------------------------------------------------

class _Win:
    def __init__(self, dwm=0):
        self.calls, self.dwm = [], dwm

    def set_affinity(self, hwnd, mode):
        self.calls.append(("affinity", hwnd, mode))
        return True

    def add_exstyle(self, hwnd, bits):
        self.calls.append(("exstyle", hwnd, bits))

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
        return True

    def show(self, hwnd, on):
        self.calls.append(("show", hwnd, on))


def test_plain_style_is_a_rounded_region_and_blur_needs_the_flag_and_dwm():
    win = _Win()
    assert op.harden_panel(7, win, 300, 44, 1.25, blur=False) == "plain"
    assert ("roundrect", (0, 0, 301, 45, 35, 35)) in win.calls
    assert ("exstyle", 7, fo.WS_EX_TOOLWINDOW | fo.WS_EX_NOACTIVATE) in win.calls
    assert ("affinity", 7, fo.WDA_EXCLUDEFROMCAPTURE) in win.calls
    assert not any(c[0] == "dwm" for c in win.calls)
    assert op.harden_panel(7, _Win(dwm=0), 300, 44, 1.0, blur=True) == "blur"
    assert op.harden_panel(7, _Win(dwm=-2147024809), 300, 44, 1.0, blur=True) == "plain"


class _Hook(list):
    def __iadd__(self, fn):
        self.append(fn)
        return self


class _Webview:
    def __init__(self):
        self.created = []

    def create_window(self, title, **kw):
        n = len(self.created)
        w = SimpleNamespace(title=title, kw=kw, events=SimpleNamespace(shown=_Hook()),
                            native=SimpleNamespace(Handle=SimpleNamespace(
                                ToInt32=lambda n=n: 500 + n)))
        self.created.append(w)
        return w


class _Bus:
    def __init__(self):
        self.subs = {}

    def subscribe(self, name, fn):
        self.subs.setdefault(name, []).append(fn)
        return lambda: None

    def publish(self, name, data=None, source=""):
        for fn in self.subs.get(name, []):
            fn(events.Event(name, data or {}, source))


def _panels(monkeypatch, blur=False):
    monkeypatch.setattr(op, "panel_blur_enabled", lambda: blur)
    bus, wv, win, clock = _Bus(), _Webview(), _Win(), _Clock()
    panels = op.OrbPanels(SimpleNamespace(bus=bus), win, webview=wv, html="<p>", clock=clock)
    return panels, bus, wv, win, clock


def _show_all(wv):
    for w in wv.created:
        for fn in w.events.shown:
            fn()


WORK = (0, 0, 1920, 1040)
TARGET = (100, 100, 1000, 800)
ORB = (1008, 108, 45)


def test_panels_open_untitled_unfocused_beside_the_orb_away_from_the_target(monkeypatch,
                                                                             capsys):
    panels, bus, wv, win, clock = _panels(monkeypatch)
    panels.update(ORB, TARGET, WORK, 1.0, busy=True)
    assert len(wv.created) == 1                              # the pill: "Working"
    bus.publish(events.AGENT_STEP, {"action": "open_app"})
    panels.update(ORB, TARGET, WORK, 1.0, busy=True)
    assert len(wv.created) == 2                              # pill + steps
    for w in wv.created:
        kw = w.kw
        assert w.title == "" and kw["frameless"] and kw["on_top"] and not kw["focus"]
        assert isinstance(kw["js_api"], op.PanelBridge)
    _show_all(wv)
    assert sorted(panels.handles()) == [500, 501]
    moves = [c[2] for c in win.calls if c[0] == "move"]
    for x, y, w, h in moves:
        assert not overlaps((x, y, x + w, y + h), TARGET)
        assert x >= ORB[0] + ORB[2]                          # right of the orb
    assert panels.side == "right"
    bus.publish(events.AGENT_STEP, {"action": "done", "speech": "The total is 42."})
    panels.update(ORB, TARGET, WORK, 1.0, busy=False)
    assert len(wv.created) == 3
    _show_all(wv)
    log = capsys.readouterr().out
    assert log.count("[OrbPanels] panel style: plain") == 1
    assert "42" not in log and "open_app" not in log        # content never logged
    clock.t += op.ANSWER_S + 1
    panels.update(ORB, TARGET, WORK, 1.0, busy=False)
    assert panels.handles() == []                            # closed on their own
    assert ("show", 500, False) in win.calls


def test_a_new_task_clears_the_last_one(monkeypatch):
    panels, bus, wv, win, clock = _panels(monkeypatch)
    bus.publish(events.AGENT_STEP, {"action": "done", "speech": "old"})
    panels.update(ORB, None, WORK, 1.0, busy=False)
    panels.update(ORB, None, WORK, 1.0, busy=True)           # rising edge
    assert panels.model.answer == "" and panels.model.steps == []


def test_panels_are_part_of_the_orbs_handles_so_masks_and_input_guards_cover_them():
    orb = fo.FloatingOrb(SimpleNamespace(), webview=_Webview(), html="<p>")
    orb.visible, orb.hwnd = True, 42
    orb.panels = SimpleNamespace(handles=lambda: [500, 501], hide_all=lambda: None)
    assert orb.handles() == [42, 500, 501]
    orb.hide()
    assert orb.handles() == []


def test_the_option_off_hides_panels_and_creates_none():
    orb = fo.FloatingOrb(SimpleNamespace(orb_option=lambda n: False), webview=_Webview(),
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
