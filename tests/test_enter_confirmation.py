"""press_key('enter') confirmation: skipped only for a safe, known, in-front target."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.api import Api
from core.tools import SPECS, ToolRegistry


def _registry():
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.specs = dict(SPECS)
    return reg


# -- ToolRegistry ---------------------------------------------------------------------


@pytest.mark.parametrize("key", ["enter", "Enter", "return"])
def test_enter_asks_unless_the_target_is_safe(key):
    reg = _registry()
    assert reg.requires_confirmation("press_key", key) is True
    assert reg.requires_confirmation("press_key", key, enter_safe=False) is True
    assert reg.requires_confirmation("press_key", key, enter_safe=True) is False


def test_delete_and_browser_enter_always_ask_and_plain_keys_never_do():
    reg = _registry()
    assert reg.requires_confirmation("press_key", "delete", enter_safe=True) is True
    assert reg.requires_confirmation("browser_press_key", "enter", enter_safe=True) is True
    for key in ("=", "3", "escape", "tab"):
        assert reg.requires_confirmation("press_key", key) is False


# -- Api.enter_target_is_safe -----------------------------------------------------------

PROCS = {1: "ApplicationFrameHost.exe", 2: "CalculatorApp.exe", 3: "firefox.exe",
         4: "Notepad.exe", 5: "Discord.exe", 6: "powershell.exe", 7: "msedgewebview2.exe"}
WINDOWS = {
    10: (1, "ApplicationFrameWindow", "Calculator", [1, 2]),
    11: (4, "Notepad", "Untitled - Notepad", []),
    12: (3, "MozillaWindowClass", "Docs - Mozilla Firefox", []),
    13: (5, "Chrome_WidgetWin_1", "#general - Discord", []),
    14: (6, "ConsoleWindowClass", "Windows PowerShell", []),
    15: (7, "Chrome_WidgetWin_1", "MistAI", []),
    16: (1, "ApplicationFrameWindow", "Mail", [1]),
    17: (4, "Notepad", "edge cases.txt - Notepad", []),
}


def _api(monkeypatch, target, fg):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: fg,
        GetWindowText=lambda h: WINDOWS[h][2],
        GetClassName=lambda h: WINDOWS[h][1],
        EnumChildWindows=lambda h, cb, extra: [cb(("child", p), extra) for p in WINDOWS[h][3]],
    ))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (0, h[1]) if isinstance(h, tuple) else (0, WINDOWS[h][0])))
    monkeypatch.setattr("core.api.psutil.Process", lambda pid: SimpleNamespace(name=lambda: PROCS[pid]))
    api = Api.__new__(Api)
    api._last_opened_app = target
    return api


@pytest.mark.parametrize("target, fg", [("calculator", 10), ("notepad", 11)])
def test_safe_target_in_front_skips_the_prompt(monkeypatch, target, fg):
    assert _api(monkeypatch, target, fg).enter_target_is_safe() is True


@pytest.mark.parametrize("target, fg, why", [
    (None, 10, "no target this phase"),
    ("calculator", 11, "target not in front"),
    ("firefox", 12, "browser"),
    ("discord", 13, "messaging"),
    ("powershell", 14, "terminal"),
    ("mistai", 15, "MistAI itself"),
    ("mail", 16, "mail (UWP frame)"),
    ("notepad", 17, "title looks like a browser"),
])
def test_unknown_or_risky_targets_keep_the_prompt(monkeypatch, target, fg, why):
    assert _api(monkeypatch, target, fg).enter_target_is_safe() is False, why


# -- AgentLoop ------------------------------------------------------------------------


def _loop(monkeypatch, safe, replies):
    replies = iter(json.dumps(r) for r in replies)
    calls, asked = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=_registry(),
        executor=SimpleNamespace(run=lambda a, p: calls.append((a, p)) or SimpleNamespace(
            action=a, parameter=p, success=True, detail="", verified=None,
            escalate_to_user=False, reliability_note=lambda: "", escalated_from="")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
        _get_text_answer=lambda q, timeout=60: asked.append(q) or "no",
        enter_target_is_safe=lambda: safe,
    )
    monkeypatch.setattr("core.agent.requests.post",
                        lambda *a, **k: SimpleNamespace(ok=True, status_code=200,
                                                        json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "W"))
    loop._loop("task", "m", "none", "text", None)
    return calls, asked


STEPS = [{"action": "press_key", "parameter": "enter"}, {"action": "done", "speech": "ok"}]


def test_loop_presses_enter_without_asking_in_a_safe_target(monkeypatch):
    calls, asked = _loop(monkeypatch, True, STEPS)
    assert calls == [("press_key", "enter")] and asked == []


def test_loop_asks_before_enter_otherwise(monkeypatch):
    calls, asked = _loop(monkeypatch, False, STEPS)
    assert calls == [] and len(asked) == 1  # harness-style "no" declines it


def test_loop_never_asks_for_plain_keys(monkeypatch):
    calls, asked = _loop(monkeypatch, False, [{"action": "press_key", "parameter": "="},
                                              {"action": "done", "speech": "ok"}])
    assert calls == [("press_key", "=")] and asked == []
