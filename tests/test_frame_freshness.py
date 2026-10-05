"""Vision reads never use a frame captured before the last UI action."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

import core.screen as screen_mod
from core.agent import AgentLoop
from core.api import Api
from core.executor import Executor
from core.tools import SPECS, ToolSpec


def _buffer(frame_time, action_time):
    buf = screen_mod.ScreenBuffer.__new__(screen_mod.ScreenBuffer)
    buf._lock = threading.Lock()
    buf._pil, buf._cv, buf._time = "buffered", None, frame_time
    buf.last_action_at = action_time
    return buf


def test_buffered_frame_newer_than_the_last_action_is_used(monkeypatch, capsys):
    grabs = []
    monkeypatch.setattr(screen_mod.ImageGrab, "grab", lambda: grabs.append(1) or "fresh")
    now = time.monotonic()
    assert _buffer(now - 0.05, now - 1.0).frame_for_read("read_screen_text") == "buffered"
    assert grabs == []
    out = capsys.readouterr().out
    assert "[Frame] read_screen_text: buffered frame 0.0" in out
    assert "newer than the last action" in out


def test_frame_older_than_the_last_action_is_replaced_by_a_fresh_capture(monkeypatch, capsys):
    monkeypatch.setattr(screen_mod.ImageGrab, "grab", lambda: "fresh")
    now = time.monotonic()
    buf = _buffer(now - 8.0, now - 0.5)  # e.g. the previous task's last screen
    assert buf.frame_for_read("screen context") == "fresh"
    out = capsys.readouterr().out
    assert "[Frame] screen context: fresh capture (" in out
    assert "buffered frame 8.0" in out and "older than the last action" in out


def test_mark_action_makes_the_current_buffer_stale(monkeypatch):
    monkeypatch.setattr(screen_mod.ImageGrab, "grab", lambda: "fresh")
    buf = _buffer(time.monotonic(), 0.0)
    assert buf.frame_for_read() == "buffered"
    buf.mark_action()
    assert buf.frame_for_read() == "fresh"


def test_failed_fresh_capture_never_falls_back_to_the_stale_frame(monkeypatch, capsys):
    def boom():
        raise OSError("screen grab failed")

    monkeypatch.setattr(screen_mod.ImageGrab, "grab", boom)
    now = time.monotonic()
    assert _buffer(now - 5.0, now - 1.0).frame_for_read("read_screen_text") is None
    assert "fresh capture failed (screen grab failed)" in capsys.readouterr().out


# -- Who marks ------------------------------------------------------------------------


class _Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a))


def _exec_api(action):
    marks = []
    api = Api.__new__(Api)
    api.last_result_text = ""
    api.tools = SimpleNamespace(get=lambda n: SPECS.get(n, ToolSpec(name=n)),
                                settle=lambda n: 0, is_destructive=lambda *a, **k: False)
    api.memory = api.task_memory = api.workspace = api.bus = _Rec()
    api.uia_nav = SimpleNamespace(invalidate_cache=lambda: None)
    api.click_handler = SimpleNamespace(last_method=None)
    api.live = SimpleNamespace(snapshot={"foreground_window": "Calculator"})
    api.screen = SimpleNamespace(mark_action=lambda: marks.append(action))
    api._exec_sync = lambda a, p: True
    return api, marks


@pytest.mark.parametrize("action, marked", [
    ("press_key", True), ("open_app", True), ("run_command", True),
    ("read_screen_text", False), ("analyze_screen", False),
])
def test_executor_marks_after_actions_that_can_change_the_screen(monkeypatch, action, marked):
    monkeypatch.setattr("core.executor.time.sleep", lambda _s: None)
    monkeypatch.setattr("core.executor.verify.run", lambda *a, **k: (None, ""))
    api, marks = _exec_api(action)
    Executor(api).run(action, "x")
    assert bool(marks) is marked


def test_each_phase_starts_by_marking(monkeypatch):
    marks = []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _s: None,
        screen=SimpleNamespace(mark_action=lambda: marks.append(1)),
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200,
        json=lambda: {"response": json.dumps({"action": "done", "speech": "ok"})}))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    loop._loop("task", "m", "none", "text", None)
    assert marks == [1]


def test_read_screen_text_and_the_screen_context_use_frame_for_read(monkeypatch):
    labels = []
    screen = SimpleNamespace(frame_for_read=lambda label: labels.append(label) or "img",
                             frame="STALE")
    api = Api.__new__(Api)
    api.vision = SimpleNamespace(ready=True, _call=lambda p, f: f"saw {f}")
    api.screen = screen
    assert Api._vision_read(api, "q") == ("saw img", True)

    loop_api = SimpleNamespace(
        screen=screen,
        vision=SimpleNamespace(ready=True,
                               understand_screen=lambda f, use_cache=False: f"described {f} ok"),
        live=SimpleNamespace(snapshot={"foreground_window": "W"}),
    )
    loop = AgentLoop(loop_api, None)
    assert loop._get_screen_context() == "Window: W | Vision: described img ok"
    assert labels == ["read_screen_text", "screen context"]
