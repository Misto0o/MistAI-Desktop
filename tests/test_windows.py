# ============================================================
# WINDOW TESTS
# ============================================================
#
# Windows and input targets, restore after a task, clipboard reliability.
#
# SECTION INDEX
#   1. WINDOWS AND INPUT
#   2. WINDOWS AND INPUT HELPERS
#   3. WINDOWS AND INPUT REFUSED
#   4. WINDOWS AND INPUT FAKE DESKTOP
#   5. WINDOWS AND INPUT TESTS
#   6. WINDOWS AND INPUT INPUT API
#   7. WINDOWS AND INPUT TESTS (2)
#   8. NO TARGET API
#   9. WINDOWS AND INPUT TESTS (3)
#   10. WINDOWS AND INPUT HELPERS (2)
#   11. WINDOWS AND INPUT TESTS (4)
#   12. WINDOWS AND INPUT HELPERS (3)
#   13. WINDOWS AND INPUT ACTIVATE API
#   14. WINDOWS AND INPUT TESTS (5)
#   15. RESTORE AFTER TASK
#   16. RESTORE AFTER TASK HELPERS
#   17. RESTORE AFTER TASK WIN32
#   18. RESTORE AFTER TASK WINDOW
#   19. RESTORE AFTER TASK HELPERS (2)
#   20. RESTORE AFTER TASK API
#   21. RESTORE AFTER TASK HELPERS (3)
#   22. NO FOCUS CHANGE
#   23. RESTORE AFTER TASK TESTS
#   24. RESTORE AFTER TASK BARE
#   25. RESTORE AFTER TASK TESTS (2)
#   26. CLIPBOARD RELIABILITY
#   27. CLIPBOARD RELIABILITY HELPERS
#   28. CLIPBOARD RELIABILITY BUSY
#   29. CLIPBOARD RELIABILITY TESTS
#   30. CLIPBOARD RELIABILITY FAKE WIN32
#   31. CLIPBOARD RELIABILITY TESTS (2)
#   32. WINDOW EVENTS
#   33. WINDOW EVENTS HELPERS
#   34. WINDOW EVENTS TESTS
#   35. SAVE AS DIALOG
#   36. SAVE AS DIALOG HELPERS
#   37. SAVE AS DIALOG TESTS
#   38. CLIPBOARD WIN32 CALLS
#   39. CLIPBOARD WIN32 CALLS TESTS
#   40. CLIPBOARD WRITE BLOCK
#   41. CLIPBOARD WRITE BLOCK HELPERS
#   42. CLIPBOARD WRITE BLOCK TESTS
# ============================================================

"""Windows and input targets, restore after a task, clipboard reliability."""

import sys
from types import SimpleNamespace
import os
import inspect
import re
import time
import threading

import pytest

from core.api import Api
import core.api as api_mod
import core.enhanced_utils as eu
from core import executor as verify, config, live_state as clip
from core.config import normalize_window_title
from test_planner import CHAT, _routing_api


# ============================================================
# WINDOWS AND INPUT
# ============================================================

# Store/UWP app windows (ApplicationFrameHost), open_app's fail-fast AUMID
# path, and type_text/keys reporting which window actually got the input.
#
# Merged by area (pure moves, no test changed) from: test_uwp_and_input_target.py, test_window_focus.py.


# ============================================================
# WINDOWS AND INPUT HELPERS
# ============================================================

PROCS = {1: "ApplicationFrameHost.exe", 2: "CalculatorApp.exe", 3: "firefox.exe",
         4: "Notepad.exe", 5: "Code.exe"}


# ============================================================
# WINDOWS AND INPUT REFUSED
# ============================================================

class _Refused(Exception):
    """Stands in for pywintypes.error: pywin32 raises when Windows refuses."""


# ============================================================
# WINDOWS AND INPUT FAKE DESKTOP
# ============================================================

def _fake_desktop(monkeypatch, windows, foreground=None, refuse_focus=False,
                  iconic=(), parents=None, bring_to_top_raises=False):
    """windows: {hwnd: (pid, class, title, [child pids])}. Returns the fake's
    state: the current foreground ("fg") and the calls made ("calls").
    A window's thread id is 1000 + hwnd; MistAI's own thread id is 1.
    parents: {pid: [parent pids]} for psutil.Process(pid).parents()."""
    state = {"fg": foreground, "calls": []}

    def set_foreground(h):
        state["calls"].append(("SetForegroundWindow", h))
        if refuse_focus:
            raise _Refused(0, "SetForegroundWindow", "No error message is available")
        state["fg"] = h

    def bring_to_top(h):
        state["calls"].append(("BringWindowToTop", h))
        if bring_to_top_raises:
            raise _Refused(5, "BringWindowToTop", "Access is denied.")

    win32gui = SimpleNamespace(
        IsWindowVisible=lambda h: True,
        GetWindowText=lambda h: windows[h][2],
        GetClassName=lambda h: windows[h][1],
        GetForegroundWindow=lambda: state["fg"],
        EnumWindows=lambda cb, extra: [cb(h, extra) for h in windows],
        EnumChildWindows=lambda h, cb, extra: [cb(("child", h, p), extra) for p in windows[h][3]],
        ShowWindow=lambda h, cmd: state["calls"].append(("ShowWindow", h, cmd)),
        SetForegroundWindow=set_foreground,
        IsIconic=lambda h: h in iconic,
        BringWindowToTop=bring_to_top,
    )

    def thread_pid(h):
        return (0, h[2]) if isinstance(h, tuple) else (1000 + h, windows[h][0])

    def attach(own, other, on):
        state["calls"].append(("AttachThreadInput", own, other, on))

    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process",
                        SimpleNamespace(GetWindowThreadProcessId=thread_pid,
                                        AttachThreadInput=attach))
    monkeypatch.setitem(sys.modules, "win32api",
                        SimpleNamespace(GetCurrentThreadId=lambda: 1))
    monkeypatch.setattr("core.api.psutil.Process", lambda pid: SimpleNamespace(
        name=lambda: PROCS[pid],
        parents=lambda: [SimpleNamespace(pid=p) for p in (parents or {}).get(pid, [])],
    ))
    return state


# -- UWP window ownership -------------------------------------------------------------


# ============================================================
# WINDOWS AND INPUT TESTS
# ============================================================

def test_running_calculator_frame_is_found_through_its_child_window(monkeypatch):
    _fake_desktop(monkeypatch, {10: (1, "ApplicationFrameWindow", "Calculator", [1, 2])})
    assert Api._window_belongs_to(10, "calculator")
    assert Api.__new__(Api)._find_hwnd_by_process_name("calculator") == 10


def test_suspended_uwp_frame_is_matched_by_title_only_for_frame_windows(monkeypatch):
    _fake_desktop(monkeypatch, {
        10: (1, "ApplicationFrameWindow", "Calculator", [1]),  # child detached
        11: (1, "ApplicationFrameWindow", "Settings", [1]),
        12: (3, "MozillaWindowClass", "Calculator - Search", []),
    })
    assert Api._window_belongs_to(10, "calculator")
    assert not Api._window_belongs_to(11, "calculator")
    assert not Api._window_belongs_to(12, "calculator")  # a browser tab is not the app


def test_app_window_state_sees_a_foreground_uwp_app(monkeypatch):
    _fake_desktop(monkeypatch, {10: (1, "ApplicationFrameWindow", "Calculator", [1, 2])},
                  foreground=10)
    api = Api.__new__(Api)
    api.is_app_running = lambda _n: True
    assert api.app_window_state("calculator") == ("foreground", "Calculator")


def test_open_app_by_aumid_fails_fast_without_start_menu_typing(monkeypatch):
    from core import start_apps as sa
    api = Api.__new__(Api)
    api._BROWSER_APP_NAMES = set()
    api.app_window_state = lambda _n: ("not_running", "")
    api._resolve_app_exe = lambda _n: None
    api.start_apps = sa.StartAppsCache(loader=lambda: [
        ("Calculator", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")])
    api.is_app_running = lambda _n: True
    waits, launched, keys = [], [], []
    clock = [0.0]
    monkeypatch.setattr("core.api.time.perf_counter", lambda: clock[0])

    def wait(_a, timeout):
        waits.append(timeout)
        clock[0] += timeout
        return False

    api._wait_for_app_window = wait
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: launched.append(args))
    monkeypatch.setattr("core.api.os.startfile", lambda t: launched.append(["startfile", t]),
                        raising=False)
    monkeypatch.setattr("core.api.pyautogui.press", lambda k: keys.append(k))
    monkeypatch.setattr("core.api.pyautogui.write", lambda *a, **k: keys.append(a))

    assert Api._act_open_app(api, "calculator") is False
    target = "shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert launched == [["explorer.exe", target], ["startfile", target]]   # two shell routes
    assert waits == [3.0, 4.0] and sum(waits) <= Api.OPEN_APP_BUDGET_S and keys == []
    assert "showed no window within 7s" in api.last_result_text


# -- Input target reporting ----------------------------------------------------------


# ============================================================
# WINDOWS AND INPUT INPUT API
# ============================================================

def _input_api(monkeypatch, target, foreground_hwnd, clipboard="old"):
    _fake_desktop(monkeypatch, {
        20: (4, "Notepad", "Untitled - Notepad", []),
        21: (5, "Chrome_WidgetWin_1", "main.py - Visual Studio Code", []),
    }, foreground=foreground_hwnd)
    api = Api.__new__(Api)
    api.live = SimpleNamespace(snapshot={"foreground_window": "stale"})
    api._last_opened_app = target
    api.switch_to_app = lambda _a: False  # no Notepad window to switch to
    api.get_clipboard = lambda: "old"
    api._clipboard_text_full = lambda: clipboard
    clip_sets = []
    api.set_clipboard = lambda t: clip_sets.append(t) or True
    api.clip_sets = clip_sets
    api.last_result_text = ""
    events = []
    monkeypatch.setattr("core.api.time.sleep", lambda _s: None)
    monkeypatch.setattr("core.api.pyautogui.hotkey", lambda *k: events.append(k))
    monkeypatch.setattr("core.api.pyautogui.press", lambda k: events.append(k))
    return api, events


# ============================================================
# WINDOWS AND INPUT TESTS (2)
# ============================================================

def test_type_text_on_target_reports_the_window_and_clears_the_field(monkeypatch):
    api, events = _input_api(monkeypatch, "notepad", 20)
    assert Api._act_type_text(api, "hello") is True
    assert api.last_result_text == "The text went to 'Untitled - Notepad'."
    assert events == [("ctrl", "a"), "delete", ("ctrl", "v")]


def test_type_text_restores_the_whole_clipboard_not_a_truncated_copy(monkeypatch):
    long_clip = "x" * 50_000
    api, _ = _input_api(monkeypatch, "notepad", 20, clipboard=long_clip)
    assert Api._act_type_text(api, "hello") is True
    assert api.clip_sets == ["hello", long_clip]


def test_type_text_off_target_sends_nothing_and_fails(monkeypatch):
    api, events = _input_api(monkeypatch, "notepad", 21)
    switched = []
    api.switch_to_app = lambda a: switched.append(a) or False
    assert Api._act_type_text(api, "hello") is False
    text = api.last_result_text
    assert text.startswith("NOT SENT: the text was not sent because notepad is not in front")
    assert "'main.py - Visual Studio Code' is" in text and "Nothing was typed" in text
    assert events == [] and api.clip_sets == []  # no keys, clipboard untouched
    assert switched == ["notepad"]  # one switch attempt first


@pytest.mark.parametrize("action, param", [("press_key", "enter"), ("hotkey", "ctrl+s")])
def test_keys_off_target_send_nothing_and_on_target_report_the_window(
        monkeypatch, action, param):
    api, events = _input_api(monkeypatch, "notepad", 21)
    assert Api._exec_sync(api, action, param) is False
    assert api.last_result_text.startswith("NOT SENT:")
    assert "Visual Studio Code" in api.last_result_text
    assert events == []
    api, events = _input_api(monkeypatch, "notepad", 20)
    assert Api._exec_sync(api, action, param) is True
    kind = "hotkey" if action == "hotkey" else "key"
    assert api.last_result_text == f"The {kind} '{param}' went to 'Untitled - Notepad'."
    assert events


@pytest.mark.parametrize("action, param", [("type_text", "hi"), ("press_key", "a"),
                                           ("hotkey", "ctrl+s")])
def test_no_known_target_does_not_interfere(monkeypatch, action, param):
    api, events = _input_api(monkeypatch, None, 21)
    assert Api._exec_sync(api, action, param) is True
    assert events
    assert "NOT SENT" not in api.last_result_text


@pytest.mark.parametrize("param, hint", [
    ("ctrl+s", "is a key combination. Use hotkey('ctrl+s')."),
    ("Ctrl+Shift+S", "is a key combination. Use hotkey('Ctrl+Shift+S')."),
    ("page down", "is not a key name"),
    ("savefile", "is not a key name"),
])
def test_press_key_refuses_a_combo_or_unknown_name_and_sends_nothing(monkeypatch, param, hint):
    """pyautogui.press ignores names it does not know: rt-notepad-save's
    press_key('ctrl+s') pressed nothing and reported success."""
    api, events = _input_api(monkeypatch, "notepad", 20)
    assert Api._exec_sync(api, "press_key", param) is False
    assert api.last_result_text.startswith("NOT SENT: press_key presses ONE key;")
    assert hint in api.last_result_text and "Nothing was pressed." in api.last_result_text
    assert events == []


@pytest.mark.parametrize("param", ["+", "A", "a", "Enter", "esc", "f5", "pagedown", "tab"])
def test_press_key_still_presses_real_keys(monkeypatch, param):
    api, events = _input_api(monkeypatch, "notepad", 20)
    assert Api._exec_sync(api, "press_key", param) is True
    assert events == [param]


def test_failed_switch_app_still_records_the_intended_target(monkeypatch):
    api, events = _input_api(monkeypatch, None, 21)
    assert Api._exec_sync(api, "switch_app", "Notepad") is False
    assert api._last_opened_app == "notepad"
    assert Api._act_press_key(api, "a") is False
    assert "notepad is not in front" in api.last_result_text
    assert events == []


def test_no_fallback_can_resend_refused_input():
    """A refused type_text/press_key/hotkey must not be retried by the
    Executor through some other tool that sends keys anyway."""
    from core.tools import SPECS

    key_senders = {"type_text", "type_search", "press_key", "hotkey", "quick_message"}
    for name in ("type_text", "press_key", "hotkey"):
        assert SPECS[name].fallback_tools == (), name
    for name, spec in SPECS.items():
        assert not set(spec.fallback_tools) & key_senders, name


# -- Open apps: Store/UWP frames ------------------------------------------------------


def test_leftover_uwp_frame_counts_as_an_open_app(monkeypatch):
    """A visible Calculator frame whose app process is gone (child detached)
    was missing from Open apps; the frame title now puts it there."""
    import threading

    from core import live_state

    _fake_desktop(monkeypatch, {
        10: (1, "ApplicationFrameWindow", "Calculator", [1]),
        11: (1, "ApplicationFrameWindow", "", [1]),            # untitled: ignored
        12: (3, "MozillaWindowClass", "Calculator - Search", []),
    })
    monkeypatch.setattr(live_state.psutil, "process_iter",
                        lambda _a: [SimpleNamespace(info={"name": "firefox.exe", "pid": 3})])
    monkeypatch.setattr(live_state, "_visible_window_pids", lambda: {1, 3})
    assert live_state._uwp_frame_apps() == ["calculator"]

    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    tracker._update()
    assert tracker._state["open_apps"] == ["firefox", "calculator"]
    assert tracker._state["background_apps"] == []


def test_running_uwp_app_is_not_listed_twice(monkeypatch):
    import threading

    from core import live_state

    _fake_desktop(monkeypatch, {10: (1, "ApplicationFrameWindow", "Calculator", [1, 2])})
    monkeypatch.setattr(live_state.psutil, "process_iter",
                        lambda _a: [SimpleNamespace(info={"name": "CalculatorApp.exe", "pid": 2})])
    monkeypatch.setattr(live_state, "_visible_window_pids", lambda: {1, 2})
    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    tracker._update()
    assert tracker._state["open_apps"] == ["calculatorapp"]


# -- No established target: append only, never into a terminal -------------------------


# ============================================================
# NO TARGET API
# ============================================================

def _no_target_api(monkeypatch, fg, console_hwnd=0, target=None):
    import ctypes

    monkeypatch.setitem(PROCS, 6, "powershell.exe")
    monkeypatch.setitem(PROCS, 7, "WindowsTerminal.exe")
    monkeypatch.setitem(PROCS, 8, "python.exe")
    windows = {
        20: (4, "Notepad", "Untitled - Notepad", []),
        30: (6, "ConsoleWindowClass", "Administrator: Windows PowerShell", []),
        31: (7, "CASCADIA_HOSTING_WINDOW_CLASS", "PowerShell", []),
        32: (8, "PseudoConsoleWindow", "C:\Python313\python.exe", []),
    }
    _fake_desktop(monkeypatch, windows, foreground=fg)
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleWindow", lambda: console_hwnd)
    api = Api.__new__(Api)
    api.live = SimpleNamespace(snapshot={"foreground_window": "stale"})
    api._last_opened_app = target
    api.switch_to_app = lambda _a: False
    api.get_clipboard = lambda: "old"
    api._clipboard_text_full = lambda: "old"
    api.set_clipboard = lambda _t: True
    api.last_result_text = ""
    events = []
    monkeypatch.setattr("core.api.time.sleep", lambda _s: None)
    monkeypatch.setattr("core.api.pyautogui.hotkey", lambda *k: events.append(k))
    monkeypatch.setattr("core.api.pyautogui.press", lambda k: events.append(k))
    return api, events


# ============================================================
# WINDOWS AND INPUT TESTS (3)
# ============================================================

def test_no_target_appends_and_never_clears(monkeypatch):
    api, events = _no_target_api(monkeypatch, 20)
    assert Api._act_type_text(api, "hello") is True
    assert events == [("ctrl", "v")]  # no Ctrl+A / Delete
    assert "appended; nothing was cleared" in api.last_result_text


def test_type_text_right_after_ctrl_a_says_it_replaced_the_selection(monkeypatch):
    api, events = _no_target_api(monkeypatch, 20)
    api.tools = SimpleNamespace(get=lambda n: SimpleNamespace(
        categories=["perception"] if n == "read_screen_text" else ["input"]))
    api._ensure_input_target = lambda what: (f"The {what} went to 'Untitled - Notepad'.", True)
    api._act_read_screen_text = lambda p: True
    monkeypatch.setattr(api, "_act_type_text", lambda p: Api._act_type_text(api, p), raising=False)
    assert api._exec_sync("hotkey", "ctrl+a") is True
    assert api._exec_sync("type_text", "hello") is True
    assert "replaced the selected text" in api.last_result_text
    assert "appended" not in api.last_result_text
    assert events == [("ctrl", "a"), ("ctrl", "v")]     # still no Delete of its own
    # A read between keeps the selection; any other action ends it.
    api._exec_sync("hotkey", "ctrl+a")
    api._exec_sync("read_screen_text", "")
    api._exec_sync("type_text", "again")
    assert "replaced the selected text" in api.last_result_text
    api._exec_sync("hotkey", "ctrl+a")
    api._exec_sync("press_key", "end")
    api._exec_sync("type_text", "more")
    assert "appended; nothing was cleared" in api.last_result_text


def test_ctrl_a_in_another_window_still_reports_append(monkeypatch):
    api, events = _no_target_api(monkeypatch, 20)
    api._selection_before = 999                     # the select-all went to another window
    assert Api._act_type_text(api, "hello") is True
    assert "appended; nothing was cleared" in api.last_result_text
    api._selection_before = None                    # nothing was selected
    assert Api._act_type_text(api, "hello") is True
    assert "appended; nothing was cleared" in api.last_result_text


@pytest.mark.parametrize("fg, console", [(30, 0), (31, 0), (32, 32)])
@pytest.mark.parametrize("action, param", [("type_text", "Get-ChildItem"),
                                           ("press_key", "enter"), ("hotkey", "ctrl+v")])
def test_no_target_never_sends_input_to_a_terminal(monkeypatch, fg, console, action, param):
    api, events = _no_target_api(monkeypatch, fg, console_hwnd=console)
    assert Api._exec_sync(api, action, param) is False
    assert events == []
    assert api.last_result_text.startswith("NOT SENT:")
    assert "is a terminal" in api.last_result_text and "run_command" in api.last_result_text


def test_a_terminal_the_phase_opened_still_gets_input(monkeypatch):
    api, events = _no_target_api(monkeypatch, 30, target="powershell")
    assert Api._exec_sync(api, "type_text", "Get-Date") is True
    assert events == [("ctrl", "a"), "delete", ("ctrl", "v")]


def test_type_text_menu_says_it_does_not_submit():
    from core.agent import AgentLoop

    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    system = AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])
    line = next(l for l in system.splitlines() if "- type_text " in l)
    assert "does NOT submit" in line and "press_key('enter')" in line
    assert "only appends" in line


# ==========================================================================
# moved from tests/test_window_focus.py
# Bringing windows to the front: the shared _focus_hwnd helper, honest
# switch_app / window_activate results, and title matching that survives a
# cut-off title quoted back with '-' for its em dash.
# ==========================================================================


# ============================================================
# WINDOWS AND INPUT HELPERS (2)
# ============================================================

ME = os.getpid()


FIREFOX_TITLE = "MistAI multi-laptop setup and workflow — Mozilla Firefox"


QUOTED = "MistAI multi-laptop setup and workflow -"   # 40 chars, as in the real log


@pytest.fixture
def desk(monkeypatch):
    """A Firefox window (10), MistAI's own browser window (11, its process a
    child of this one), MistAI's UI (12, this process) and Notepad (13)."""
    monkeypatch.setitem(PROCS, ME, "python.exe")
    monkeypatch.setitem(PROCS, 9, "firefox.exe")
    keys = []
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(api_mod.subprocess, "run", lambda *a, **k: None)  # AppActivate

    def make(foreground=12, **kw):
        windows = {
            10: (3, "MozillaWindowClass", FIREFOX_TITLE, []),
            11: (9, "MozillaWindowClass", "about:blank — Nightly", []),
            12: (ME, "WindowsForms10", "mistai v2.0", []),
            13: (4, "Notepad", "Untitled - Notepad", []),
        }
        state = _fake_desktop(monkeypatch, windows, foreground=foreground,
                              parents={9: [500, ME]}, **kw)
        state["keys"] = keys
        api = Api.__new__(Api)
        api.last_result_text = ""
        return api, state

    return make


def _calls(state, name):
    return [c for c in state["calls"] if c[0] == name]


# -- _focus_hwnd --------------------------------------------------------------


# ============================================================
# WINDOWS AND INPUT TESTS (4)
# ============================================================

def test_focus_succeeds_first_try_without_attach_or_alt(desk):
    api, state = desk()
    assert api._focus_hwnd(13) == (True, "")
    assert _calls(state, "SetForegroundWindow") == [("SetForegroundWindow", 13)]
    assert not _calls(state, "AttachThreadInput") and state["keys"] == []
    assert not _calls(state, "ShowWindow")      # not minimized: never restored


def test_minimized_window_is_restored_first(desk):
    api, state = desk(iconic={13})
    assert api._focus_hwnd(13)[0] is True
    assert state["calls"][0] == ("ShowWindow", 13, 9)


def test_refused_focus_tries_every_tier_and_names_the_window_in_front(desk):
    api, state = desk(refuse_focus=True)
    ok, reason = api._focus_hwnd(10)
    assert ok is False
    assert len(_calls(state, "SetForegroundWindow")) == 3      # plain, attached, after ALT
    assert _calls(state, "AttachThreadInput") == [
        ("AttachThreadInput", 1, 1012, True), ("AttachThreadInput", 1, 1012, False)]
    assert state["keys"] == ["alt"]
    assert reason == ("Windows refused focus; the window in front belongs to python.exe "
                      "(MistAI's own window, not minimized)")


def test_detach_always_runs_even_when_bring_to_top_fails(desk):
    api, state = desk(refuse_focus=True, bring_to_top_raises=True, iconic={12})
    ok, reason = api._focus_hwnd(10)
    assert ok is False and "minimized)" in reason and "not minimized" not in reason
    assert _calls(state, "AttachThreadInput")[-1] == ("AttachThreadInput", 1, 1012, False)


# -- switch_to_app ------------------------------------------------------------

def test_switch_reports_the_match_count_and_mistais_own_browser(desk, capsys):
    api, _ = desk()
    assert api.switch_to_app("firefox") is True
    assert api.last_result_text == (
        "Switched to 'firefox' (window 'MistAI multi-laptop setup and workflow —') "
        "[1 of 2 matching windows; another matching window is MistAI's own browser]")


def test_switch_says_found_but_refused_not_no_window(desk, capsys):
    api, _ = desk(refuse_focus=True)
    assert api.switch_to_app("firefox") is False
    text = api.last_result_text
    assert text.startswith("'firefox' has a window ('MistAI multi-laptop")
    assert "could not be brought to the front: Windows refused focus" in text
    assert "MistAI's own window" in text
    out = capsys.readouterr().out
    assert "exists but refused focus" in out and "No window found" not in out


def test_switch_with_no_window_says_so(desk, capsys):
    api, _ = desk()
    assert api.switch_to_app("calculator") is False
    assert api.last_result_text == "No window found for 'calculator'"
    assert "No window found for process 'calculator'" in capsys.readouterr().out


def test_switch_flags_when_the_chosen_window_is_mistais_own_browser(desk, monkeypatch):
    api, _ = desk()
    monkeypatch.setattr(api, "_find_hwnds_by_process_name", lambda _a: [11])
    assert api.switch_to_app("firefox") is True
    assert "this window is MistAI's own browser" in api.last_result_text


# -- window_activate ----------------------------------------------------------


# ============================================================
# WINDOWS AND INPUT HELPERS (3)
# ============================================================

@pytest.fixture
def gw_windows(monkeypatch):
    """pygetwindow's real matching rule (case-insensitive substring) over the
    desk's titles."""
    def make(titles):
        wins = [SimpleNamespace(title=t, _hWnd=h) for h, t in titles.items()]
        monkeypatch.setattr(eu, "WINDOW_MGMT_AVAILABLE", True)
        monkeypatch.setattr(eu, "gw", SimpleNamespace(
            getWindowsWithTitle=lambda q: [w for w in wins if q.upper() in w.title.upper()],
            getAllWindows=lambda: wins,
        ), raising=False)
    return make


# ============================================================
# WINDOWS AND INPUT ACTIVATE API
# ============================================================

def _activate_api(api):
    api.enhanced_available = True
    api.window_mgr = eu.WindowManager
    api.spoken = []
    api.speak = api.spoken.append
    return api


# ============================================================
# WINDOWS AND INPUT TESTS (5)
# ============================================================

def test_window_activate_finds_a_cut_title_with_a_hyphen_for_the_em_dash(desk, gw_windows):
    api, state = desk()
    gw_windows({10: FIREFOX_TITLE, 12: "mistai v2.0"})
    assert Api._exec_sync(_activate_api(api), "window_activate", QUOTED) is True
    assert state["fg"] == 10
    assert api.last_result_text.startswith("Activate Window: MistAI multi-laptop")


def test_window_activate_found_but_refused_is_not_reported_as_not_found(desk, gw_windows):
    api, _ = desk(refuse_focus=True)
    gw_windows({10: FIREFOX_TITLE})
    assert Api._exec_sync(_activate_api(api), "window_activate", QUOTED) is False
    assert api.last_result_text.startswith("Found window 'MistAI multi-laptop")
    assert "could not bring it to the front: Windows refused focus" in api.last_result_text
    assert "Could not find window" not in api.last_result_text


def test_window_activate_not_found(desk, gw_windows):
    api, state = desk()
    gw_windows({10: FIREFOX_TITLE})
    assert Api._exec_sync(_activate_api(api), "window_activate", "Spotify Premium") is False
    assert api.last_result_text == "Could not find window: Spotify Premium"
    assert not _calls(state, "SetForegroundWindow")


# -- title normalization ------------------------------------------------------

@pytest.mark.parametrize("raw, norm", [
    (QUOTED, "MistAI multi-laptop setup and workflow"),
    ("Page — ", "Page"),
    ("a – b  —  c", "a - b - c"),
    ("Inbox |", "Inbox"),
    ("", ""),
    (" - ", ""),
])
def test_normalize_window_title(raw, norm):
    assert normalize_window_title(raw) == norm


def test_focus_verifier_accepts_the_quoted_title_with_a_hyphen():
    api = SimpleNamespace(live=SimpleNamespace(snapshot={"foreground_window": FIREFOX_TITLE}))
    ok, _ = verify._window_focused(api, "window_activate", QUOTED, None)
    assert ok is True
    api.live.snapshot["foreground_window"] = "Untitled - Notepad"
    assert verify._window_focused(api, "window_activate", QUOTED, None)[0] is False


# ============================================================
# RESTORE AFTER TASK
# ============================================================

# Restore MistAI's window after a task (Api._restore_after_task).
#
# ask_mistai minimizes MistAI before the agent runs; when the run REALLY ends
# (the agent thread's done/error callback, after the gate is released) the
# window comes back WITHOUT focus: ShowWindowAsync(SW_SHOWNOACTIVATE), then an
# async, no-activate HWND_TOPMOST / HWND_NOTOPMOST pair that lifts it to the top
# of the normal band and always ends non-topmost. Never SetForegroundWindow,
# never window.restore(). win32gui / win32process / ShowWindowAsync are stubbed.


# ============================================================
# RESTORE AFTER TASK HELPERS
# ============================================================

HWND = 0x1234
SW_SHOWNOACTIVATE = 4
SWP_FLAGS = 0x4000 | 0x0010 | 0x0002 | 0x0001   # ASYNC | NOACTIVATE | NOMOVE | NOSIZE
SWP_ASYNC, SWP_NOACTIVATE = 0x4000, 0x0010
SWP_FORBIDDEN = 0x0040 | 0x0004 | 0x0020          # SHOWWINDOW | NOZORDER | FRAMECHANGED
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
WS_EX_TOPMOST = 0x8


# ============================================================
# RESTORE AFTER TASK WIN32
# ============================================================

class _Win32:
    """win32gui + win32process stand-ins that record every call."""

    def __init__(self, windows=None, iconic=True):
        self.calls = []
        self.iconic = iconic
        self.exstyle = 0              # WS_EX_TOPMOST tracked like Windows does
        self.raise_on = set()         # insert-after values whose call raises
        # hwnd -> (pid, owner, title): EnumWindows' top-level windows.
        self.windows = windows or {}

    def __getattr__(self, name):            # anything unexpected is recorded
        def call(*a):
            self.calls.append((name,) + a)
        return call

    def IsWindow(self, h):
        return True

    def IsIconic(self, h):
        self.calls.append(("IsIconic", h))
        return self.iconic

    def GetWindowLong(self, h, index):
        self.calls.append(("GetWindowLong", h, index))
        return self.exstyle

    def SetWindowPos(self, h, after, *rest):
        self.calls.append(("SetWindowPos", h, after) + rest)
        if after in self.raise_on:
            raise OSError("SetWindowPos failed")
        if after == HWND_TOPMOST:
            self.exstyle |= WS_EX_TOPMOST
        elif after == HWND_NOTOPMOST:
            self.exstyle &= ~WS_EX_TOPMOST

    def SetForegroundWindow(self, h):
        self.calls.append(("SetForegroundWindow", h))

    def EnumWindows(self, cb, extra):
        for h in self.windows:
            cb(h, extra)

    def GetWindowThreadProcessId(self, h):
        return (1, self.windows[h][0])

    def GetWindow(self, h, kind):
        return self.windows[h][1]

    def GetWindowText(self, h):
        return self.windows[h][2]


# ============================================================
# RESTORE AFTER TASK WINDOW
# ============================================================

class _Window:
    title = "MistAI v9"

    def __init__(self, native=True):
        self.calls = []
        self.native = (SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: HWND))
                       if native else None)

    def restore(self):
        self.calls.append("restore")

    def minimize(self):
        self.calls.append("minimize")


# ============================================================
# RESTORE AFTER TASK HELPERS (2)
# ============================================================

@pytest.fixture
def win(monkeypatch):
    w = _Win32()
    monkeypatch.setitem(sys.modules, "win32gui", w)
    monkeypatch.setitem(sys.modules, "win32process", w)
    monkeypatch.setattr(api_mod, "_show_window_async",
                        lambda h, cmd: w.calls.append(("ShowWindowAsync", h, cmd)))
    monkeypatch.delenv("MISTAI_RESTORE_AFTER_TASK", raising=False)
    return w


RESTORE = [("IsIconic", HWND), ("ShowWindowAsync", HWND, SW_SHOWNOACTIVATE),
           ("GetWindowLong", HWND, -20),
           ("SetWindowPos", HWND, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS),
           ("SetWindowPos", HWND, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)]


def _restores(w):
    return [c for c in w.calls if c[0] == "ShowWindowAsync"]


# ============================================================
# RESTORE AFTER TASK API
# ============================================================

def _api(monkeypatch, run=None, llm_reply=""):
    """The conversation tests' bare Api with a window. `run(callbacks)` is
    what the agent thread does (default: nothing yet)."""
    api = _routing_api(monkeypatch, llm_reply)
    api._window = _Window()
    api._awaiting_text_answer = threading.Event()
    agent_run = api.agent.run_project

    def run_project(*a, **k):
        agent_run(*a, **k)
        if run:
            run(k)

    api.agent.run_project = run_project
    return api


# ============================================================
# RESTORE AFTER TASK HELPERS (3)
# ============================================================

def _positions(w):
    return [c for c in w.calls if c[0] == "SetWindowPos"]


# ============================================================
# NO FOCUS CHANGE
# ============================================================

def _no_focus_change(api, w):
    focus = {"SetForegroundWindow", "BringWindowToTop", "SetActiveWindow", "SetFocus"}
    assert not any(c[0] in focus for c in w.calls)
    assert api._window.calls == []                  # no window.restore()
    for c in _positions(w):                         # never activates, always async
        flags = c[-1]
        assert flags & SWP_NOACTIVATE and flags & SWP_ASYNC
        assert not flags & SWP_FORBIDDEN
    if _positions(w):                               # always ends non-topmost
        assert _positions(w)[-1][2] == HWND_NOTOPMOST
    assert not w.exstyle & WS_EX_TOPMOST


# -- one no-activate restore per finished task -----------------------------------------


# ============================================================
# RESTORE AFTER TASK TESTS
# ============================================================

@pytest.mark.parametrize("finish", [
    lambda cb: cb["on_done"]({"success": True, "steps": 2, "speech": "Done."}),
    lambda cb: cb["on_done"]({"success": False, "error": "no answer", "steps": 3}),
    lambda cb: cb["on_done"]({"success": False, "error": "Stopped by user"}),
    lambda cb: cb["on_error"]("boom"),
], ids=["success", "failure", "stop", "error"])
def test_a_finished_task_restores_once_without_focus(monkeypatch, win, finish):
    api = _api(monkeypatch, run=finish)
    Api.ask_mistai(api, "open notepad")
    assert api.rec["minimized"] == 1
    assert win.calls == RESTORE
    assert not api._agent_lock.locked()          # restored after the release
    _no_focus_change(api, win)


def test_a_window_the_user_already_restored_is_left_alone(monkeypatch, win):
    win.iconic = False
    api = _api(monkeypatch, run=lambda cb: cb["on_done"]({"success": True}))
    Api.ask_mistai(api, "open notepad")
    assert win.calls == [("IsIconic", HWND)] and _positions(win) == []


# -- never for chat, filler or busy replies ---------------------------------------------

def test_chat_filler_and_busy_replies_never_restore(monkeypatch, win):
    api = _api(monkeypatch, llm_reply=CHAT)
    assert Api.ask_mistai(api, "tell me a joke")["command"]["speech"] == "Ha."
    assert Api.ask_mistai(api, "thanks")["command"]["speech"] == "You're welcome."
    api._agent_lock.acquire()                    # another task holds the gate
    api._agent_task = "earlier task"
    assert Api.ask_mistai(api, "open notepad")["error"] == "busy"
    assert api.rec["minimized"] == 0 and win.calls == [] and api._window.calls == []


# -- skipped when the run is no longer the latest -----------------------------------------

def test_a_stale_callback_after_a_new_run_started_does_not_restore(monkeypatch, win):
    api = _api(monkeypatch)
    assert Api.ask_mistai(api, "open notepad")["error"] == "still_running"
    callbacks = api.agent.callbacks
    with api._agent_state_lock:
        api._agent_generation += 1               # what a newer run's gate does
    callbacks["on_done"]({"success": True})
    assert win.calls == []


def test_a_stale_callback_after_a_newer_run_finished_does_not_restore(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 5                    # run 4 is old; run 5 already ended
    api._restore_after_task(4)
    assert win.calls == []
    api._restore_after_task(5)
    assert win.calls == RESTORE


def test_no_restore_while_another_run_holds_the_gate(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 4
    api._agent_lock.acquire()
    api._restore_after_task(4)
    assert _restores(win) == []


def test_no_restore_while_a_text_prompt_is_open(monkeypatch, win):
    api = _api(monkeypatch)
    Api.ask_mistai(api, "open notepad")
    api._awaiting_text_answer.set()
    api.agent.callbacks["on_done"]({"success": True})
    assert win.calls == []


def test_the_setting_off_skips_it_and_is_read_at_call_time(monkeypatch, win):
    api = _api(monkeypatch)
    Api.ask_mistai(api, "open notepad")          # setting on when the task started
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "0")
    assert config.restore_after_task_enabled() is False
    api.agent.callbacks["on_done"]({"success": True})
    assert win.calls == []
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "off")
    assert config.restore_after_task_enabled() is False
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "")
    assert config.restore_after_task_enabled() is True       # default on


# -- a run that outlasts the timeout restores when it really ends ----------------------------

def test_a_run_past_the_timeout_restores_when_it_really_ends(monkeypatch, win):
    api = _api(monkeypatch)                      # AGENT_HARD_TIMEOUT is 0.01s
    assert Api.ask_mistai(api, "long task")["error"] == "still_running"
    assert win.calls == []                       # still running: stays minimized
    api.agent.callbacks["on_done"]({"success": True, "steps": 9})
    assert win.calls == RESTORE
    _no_focus_change(api, win)


# -- the except branch -------------------------------------------------------------------

def test_an_exception_after_minimizing_restores(monkeypatch, win):
    def boom(_cb):
        raise RuntimeError("thread could not start")
    api = _api(monkeypatch, run=boom)
    assert Api.ask_mistai(api, "open notepad") == {"success": False,
                                                    "error": "thread could not start"}
    assert api.rec["minimized"] == 1 and not api._agent_lock.locked()
    assert win.calls == RESTORE
    _no_focus_change(api, win)


def test_an_exception_before_minimizing_does_not_restore(monkeypatch, win):
    api = _api(monkeypatch)
    api._task_needs_project_workspace = lambda _m: (_ for _ in ()).throw(ValueError("x"))
    assert Api.ask_mistai(api, "open notepad")["success"] is False
    assert api.rec["minimized"] == 0 and win.calls == []


# -- the handle, and bare objects ---------------------------------------------------------

def test_a_bare_object_with_no_window_skips_it(win):
    api = Api.__new__(Api)
    api._restore_after_task(0)                   # no _window, no locks: no error
    assert win.calls == []


def test_the_handle_falls_back_to_this_process_top_level_window(win):
    pid = os.getpid()
    win.windows = {
        0x10: (pid + 1, 0, "MistAI v9"),          # another process
        0x20: (pid, 0x99, "MistAI v9"),           # owned (a dialog)
        0x30: (pid, 0, "Captions"),               # the caption window
        0x40: (pid, 0, "MistAI v9"),              # MistAI itself
    }
    api = Api.__new__(Api)
    api._window = _Window(native=False)
    assert api._own_window_handle() == 0x40


def test_the_restore_path_never_focuses_or_calls_window_restore():
    src = inspect.getsource(Api._restore_after_task) + inspect.getsource(Api._own_window_handle)
    for name in ("SetForegroundWindow", "BringWindowToTop", "SetActiveWindow", "SetFocus",
                 ".restore()"):
        assert name not in src, name
    # The existing ask-the-user restore is unchanged.
    assert "self._window.restore()" in inspect.getsource(Api._get_text_answer)


# -- the raise: topmost then not-topmost, always ending non-topmost ------------------------


# ============================================================
# RESTORE AFTER TASK BARE
# ============================================================

def _bare(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 1
    return api


# ============================================================
# RESTORE AFTER TASK TESTS (2)
# ============================================================

def test_the_raise_logs_its_line(win, capsys):
    api = _bare(win)
    api._restore_after_task(1)
    assert win.calls == RESTORE
    assert "[Window] Restored and raised after the task (not activated)" in capsys.readouterr().out
    _no_focus_change(api, win)


def test_a_failed_topmost_call_still_clears_topmost(win, capsys):
    win.raise_on = {HWND_TOPMOST}
    api = _bare(win)
    api._restore_after_task(1)
    assert [c[2] for c in _positions(win)] == [HWND_TOPMOST, HWND_NOTOPMOST]
    assert "Restore after the task skipped: OSError" in capsys.readouterr().out
    _no_focus_change(api, win)


def test_a_failed_notopmost_call_is_logged_by_type_only(win, capsys):
    win.raise_on = {HWND_NOTOPMOST}
    api = _bare(win)
    api._restore_after_task(1)                   # no crash
    out = capsys.readouterr().out
    assert "[Window] Could not clear topmost: OSError" in out
    assert "SetWindowPos failed" not in out      # the type, never the message
    assert [c[2] for c in _positions(win)] == [HWND_TOPMOST, HWND_NOTOPMOST]


def test_an_already_topmost_window_is_left_alone(win):
    win.exstyle = WS_EX_TOPMOST
    api = _bare(win)
    api._restore_after_task(1)
    assert _positions(win) == []
    assert win.exstyle & WS_EX_TOPMOST           # its topmost state is kept
    assert ("ShowWindowAsync", HWND, SW_SHOWNOACTIVATE) in win.calls


@pytest.mark.parametrize("skip", ["stale", "gate", "prompt", "off", "shown"])
def test_every_skip_makes_no_position_call(monkeypatch, win, skip):
    api = _bare(win)
    api._awaiting_text_answer = threading.Event()
    gen = 1
    if skip == "stale":
        gen = 0
    elif skip == "gate":
        api._agent_lock.acquire()
    elif skip == "prompt":
        api._awaiting_text_answer.set()
    elif skip == "off":
        monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "0")
    elif skip == "shown":
        win.iconic = False
    api._restore_after_task(gen)
    assert _positions(win) == [] and _restores(win) == []


# ============================================================
# CLIPBOARD RELIABILITY
# ============================================================

# Clipboard reliability for type_text: retries, key-event fallback, guards,
# content-free logging, watcher sequence gating and clipboard restore.


# ============================================================
# CLIPBOARD RELIABILITY HELPERS
# ============================================================

@pytest.fixture
def keys(monkeypatch):
    """Record SendInput fallback calls instead of sending real keystrokes."""
    sent = []
    monkeypatch.setattr(clip, "send_unicode", lambda t: sent.append(t) or True)
    return sent


# ============================================================
# CLIPBOARD RELIABILITY BUSY
# ============================================================

def _busy(api, results):
    results = list(results)
    api.sets = []

    def set_clipboard(text):
        api.sets.append(text)
        return results.pop(0) if results else False

    api.set_clipboard = set_clipboard


# ============================================================
# CLIPBOARD RELIABILITY TESTS
# ============================================================

def test_busy_then_available_retries_and_pastes(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [False, False, True, True])
    assert Api._act_type_text(api, "hello") is True
    assert ("ctrl", "v") in events and keys == []
    assert api.sets[-1] == "old"                 # user's clipboard restored


def test_permanently_busy_short_text_uses_keys_and_says_so(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    assert Api._act_type_text(api, "37*23") is True
    assert keys == ["37*23"]
    assert ("ctrl", "v") not in events and "enter" not in [str(e).lower() for e in events]
    assert "keyboard events" in api.last_result_text
    assert set(api.sets) == {"37*23"}            # nothing staged -> nothing to restore


@pytest.mark.parametrize("text", ["x" * 201, "line one\nline two"])
def test_long_or_multiline_text_fails_safely(monkeypatch, keys, text):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    assert Api._act_type_text(api, text) is False
    assert keys == [] and events == []           # field untouched
    assert "left unchanged" in api.last_result_text


def test_keyboard_fallback_respects_the_input_target_guard(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 21)   # VS Code is in front
    _busy(api, [])
    assert Api._act_type_text(api, "hi") is False
    assert keys == [] and events == []
    assert "NOT SENT" in api.last_result_text


def test_retry_delays_are_short_jittered_and_capped():
    for _ in range(20):
        d = clip.retry_delays()
        assert d and sum(d) <= 1.5 and max(d) < 0.5
    assert len({tuple(clip.retry_delays()) for _ in range(5)}) > 1   # jitter


# ============================================================
# CLIPBOARD RELIABILITY FAKE WIN32
# ============================================================

def _fake_win32(monkeypatch, fail_open=False):
    calls = []

    def open_():
        calls.append("open")
        if fail_open:
            raise RuntimeError("SECRET-HOLDER-NAME")
    fake = SimpleNamespace(OpenClipboard=open_, CloseClipboard=lambda: calls.append("close"),
                           EmptyClipboard=lambda: None, SetClipboardData=lambda *a: None,
                           IsClipboardFormatAvailable=lambda f: True,
                           GetClipboardData=lambda f: "clip text", CF_UNICODETEXT=13)
    monkeypatch.setitem(sys.modules, "win32clipboard", fake)
    # The write is our own ctypes block now; the fake's SetClipboardData
    # (looked up per call, so a test can replace it) stands in for it.
    monkeypatch.setattr(clip, "_write_unicode", lambda text: sys.modules["win32clipboard"]
                        .SetClipboardData(13, text))
    return calls


# ============================================================
# CLIPBOARD RELIABILITY TESTS (2)
# ============================================================

@pytest.mark.parametrize("holder", ["self", "other", "unknown"])
def test_busy_log_has_only_the_holder_label(monkeypatch, capsys, holder):
    _fake_win32(monkeypatch, fail_open=True)
    monkeypatch.setattr(clip, "holder", lambda: holder)
    assert clip.set_text("my private text") is False
    out = capsys.readouterr().out
    assert out.strip() == f"[TypeText] clipboard busy holder={holder}"
    assert "private" not in out and "SECRET" not in out


def test_a_refused_write_after_open_is_logged_as_denied_with_its_code(monkeypatch, capsys):
    _fake_win32(monkeypatch)
    err = OSError("SECRET policy text")
    err.winerror = 5

    def refuse(*_a):
        raise err
    sys.modules["win32clipboard"].SetClipboardData = refuse
    monkeypatch.setattr(clip, "holder", lambda: "unknown")
    assert clip.set_text("my private text") is False
    out = capsys.readouterr().out
    assert out.strip() == "[TypeText] clipboard denied holder=unknown code=5"
    assert "private" not in out and "SECRET" not in out


def test_breaker_opens_after_two_failures_within_the_window(capsys):
    b = clip.Breaker()
    b.record(False, now=0)
    assert not b.is_open(now=1)
    b.record(False, now=30)
    assert b.is_open(now=31) and b.is_open(now=89)
    assert not b.is_open(now=90)                     # 60 s later it closes by itself
    assert capsys.readouterr().out.splitlines() == [
        "[TypeText] clipboard breaker open", "[TypeText] clipboard breaker closed"]


def test_breaker_needs_consecutive_failures_inside_60_s():
    b = clip.Breaker()
    b.record(False, now=0)
    b.record(False, now=61)                          # the first one is too old
    assert not b.is_open(now=62)
    b.record(True, now=63)
    b.record(False, now=64)                          # a success reset the count
    assert not b.is_open(now=65)


def test_a_successful_write_closes_the_breaker(capsys):
    b = clip.Breaker()
    b.record(False, now=0)
    b.record(False, now=1)
    b.record(True, now=2)
    assert not b.is_open(now=3)
    assert capsys.readouterr().out.splitlines()[-1] == "[TypeText] clipboard breaker closed"


def test_open_breaker_sends_short_text_as_keys_without_touching_the_clipboard(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    assert Api._act_type_text(api, "one") is True
    assert Api._act_type_text(api, "two") is True    # second exhausted call opens it
    tries = len(api.sets)
    assert Api._act_type_text(api, "three") is True
    assert len(api.sets) == tries                    # no clipboard attempt at all
    assert keys == ["one", "two", "three"]
    assert "keyboard events" in api.last_result_text


def test_open_breaker_keeps_the_guards(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    Api._act_type_text(api, "one")
    Api._act_type_text(api, "two")
    events.clear()
    keys.clear()
    # Long text still tries the clipboard and fails closed with the field untouched.
    assert Api._act_type_text(api, "x" * 201) is False
    assert keys == [] and events == [] and "left unchanged" in api.last_result_text
    # The input-target guard still refuses when the target is not in front.
    api2, events2 = _input_api(monkeypatch, "notepad", 21)
    api2._clip_breaker = api._clip_breaker
    _busy(api2, [])
    assert Api._act_type_text(api2, "hi") is False and keys == []
    assert "NOT SENT" in api2.last_result_text


def test_breaker_is_per_session_and_closed_at_start(monkeypatch, keys):
    api, _ = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [True, True])
    assert Api._act_type_text(api, "hello") is True and keys == []


def test_holder_is_self_while_we_have_it_open(monkeypatch):
    seen = []
    fake = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_text_of", lambda w: seen.append(clip.holder()) or "")
    clip.read_text()
    assert seen == ["self"] and fake == ["open", "close"]


def test_watcher_does_not_open_the_clipboard_when_the_sequence_is_unchanged(monkeypatch):
    calls = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_seq", lambda: 41)
    clip._cache_seq = None
    assert clip.peek_text() == "clip text" and calls == ["open", "close"]
    assert clip.peek_text() == "clip text" and calls == ["open", "close"]   # no 2nd open
    monkeypatch.setattr(clip, "_seq", lambda: 42)
    clip.peek_text()
    assert calls == ["open", "close"] * 2


def test_watcher_keeps_the_old_value_when_the_clipboard_is_busy(monkeypatch):
    clip._cache, clip._cache_seq = "kept", 1
    _fake_win32(monkeypatch, fail_open=True)
    monkeypatch.setattr(clip, "_seq", lambda: 2)
    assert clip.peek_text() == "kept"


def test_own_write_does_not_trigger_a_watcher_reread(monkeypatch):
    calls = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_seq", lambda: 7)
    assert clip.set_text("staged") is True
    calls.clear()
    assert clip.peek_text() == "staged" and calls == []


def test_previous_clipboard_is_restored_after_the_paste(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20, clipboard="user's plain text")
    assert Api._act_type_text(api, "hello") is True
    assert api.clip_sets == ["hello", "user's plain text"]


def test_previous_clipboard_is_restored_even_if_the_paste_fails(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20, clipboard="keep me")
    monkeypatch.setattr("core.api.pyautogui.hotkey",
                        lambda *k: (_ for _ in ()).throw(RuntimeError("x")))
    assert Api._act_type_text(api, "hello") is False
    assert api.clip_sets[-1] == "keep me"


# ============================================================
# WINDOW EVENTS
# ============================================================

# core/live_state.py WINDOW EVENTS / WINEVENT HOOK / WINDOW SETTLE: a
# WinEvent hook feeds WindowEvents; settles end early only after their floor,
# with the expected window in front and the desktop quiet, else wait the old
# cap; any failure (no hook, dead thread, stale events, MISTAI_WINDOW_EVENTS=0)
# means the old fixed waits. The real hook is only installed by the
# Windows-only test.


# ============================================================
# WINDOW EVENTS HELPERS
# ============================================================

def _healthy_events(quiet=True):
    ev = clip.WindowEvents(own_pid=1)
    ev.installed = True
    ev.alive = lambda: True
    ev.record("show", 10, 2)
    if quiet:
        ev.last_relevant_at = time.monotonic() - 10
    return ev


def _settle_line(out):
    return [l for l in out.splitlines() if l.startswith("[WindowWatch]")]


def _later(seconds, fn):
    t = threading.Timer(seconds, fn)
    t.daemon = True
    t.start()
    return t


# ============================================================
# WINDOW EVENTS TESTS
# ============================================================

def test_wait_for_returns_early_on_a_matching_event():
    ev = _healthy_events()
    _later(0.05, lambda: ev.record("foreground", 42, 2))
    t0 = time.monotonic()
    assert ev.wait_for(ev.foreground_changed(0), 2.0) is True
    assert time.monotonic() - t0 < 1.0
    assert ev.foreground == (42, 2)


def test_wait_for_times_out_when_nothing_matches():
    ev = _healthy_events()
    t0 = time.monotonic()
    assert ev.wait_for(lambda: False, 0.2) is False
    assert 0.19 <= time.monotonic() - t0 < 1.0


def test_settle_returns_early_but_never_before_the_floor(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_WINDOW_EVENTS", raising=False)
    ev = _healthy_events()
    waited, reason = clip.settle(ev, 1.5, 0.3, lambda: True, "window+quiet")
    assert 0.3 <= waited < 1.0 and reason == "window+quiet"
    line = _settle_line(capsys.readouterr().out)
    assert len(line) == 1
    assert re.fullmatch(r"\[WindowWatch\] settle 0\.\d\ds \(cap 1\.50s, window\+quiet\)", line[0])


def test_settle_waits_the_old_cap_when_the_window_never_appears(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_WINDOW_EVENTS", raising=False)
    ev = _healthy_events()
    waited, reason = clip.settle(ev, 0.4, 0.1, lambda: False, "window+quiet")
    assert waited == 0.4 and reason == "no early signal"
    assert "(cap 0.40s, no early signal)" in capsys.readouterr().out


def test_settle_waits_the_cap_while_events_keep_arriving(monkeypatch):
    monkeypatch.delenv("MISTAI_WINDOW_EVENTS", raising=False)
    ev = _healthy_events(quiet=False)
    stop = threading.Event()

    def busy():
        while not stop.is_set():
            ev.record("name", 11, 2)
            time.sleep(0.03)

    threading.Thread(target=busy, daemon=True).start()
    try:
        waited, reason = clip.settle(ev, 0.4, 0.1, lambda: True)
    finally:
        stop.set()
    assert waited == 0.4 and reason == "no early signal"


def test_own_process_events_do_not_break_the_quiet():
    ev = _healthy_events()
    ev.record("show", 5, 1)                    # MistAI's own orb / caption window
    assert ev.quiet_for(0.15)
    ev.record("show", 5, 2)
    assert not ev.quiet_for(0.15)


@pytest.mark.parametrize("break_it", ["not installed", "dead thread", "no events", "stale",
                                      "env off", "no floor", "floor >= cap", "not real"])
def test_any_failure_means_the_old_fixed_wait(monkeypatch, capsys, break_it):
    monkeypatch.delenv("MISTAI_WINDOW_EVENTS", raising=False)
    ev, floor, cap = _healthy_events(), 0.05, 0.3
    if break_it == "not installed":
        ev.installed = False
    elif break_it == "dead thread":
        ev.alive = lambda: False
    elif break_it == "no events":
        ev = clip.WindowEvents(own_pid=1)
        ev.installed, ev.alive = True, lambda: True
    elif break_it == "stale":
        ev.check_poll(777)
        ev.check_poll(777)
        assert ev.stale
    elif break_it == "env off":
        monkeypatch.setenv("MISTAI_WINDOW_EVENTS", "0")
    elif break_it == "no floor":
        floor = None
    elif break_it == "floor >= cap":
        floor = cap
    elif break_it == "not real":
        ev = SimpleNamespace(healthy=lambda: True, quiet_for=lambda p: True,
                             wait_for=lambda c, t: True)
    slept = []
    assert clip.settle(ev, cap, floor, lambda: True, sleep=slept.append) == (cap, "fixed")
    assert slept == [cap]


def test_failures_are_logged_once_without_content(capsys):
    ev = _healthy_events()
    ev.alive = lambda: False
    assert not ev.healthy() and not ev.healthy()
    ev.alive = lambda: True
    for _ in range(4):
        ev.check_poll(999)
    ev.record("foreground", 999, 2)
    ev.check_poll(999)
    assert not ev.stale
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "[WindowWatch] window event thread stopped -- fixed waits"
    assert re.fullmatch(r"\[WindowWatch\] window events stopped arriving -- fixed waits "
                        r"\(last event \d+\.\ds ago; poll saw the foreground move to another "
                        r"window with no event, 2 polls\)", lines[1])
    assert lines[2:] == ["[WindowWatch] window events arriving again"]


# -- stale detection: quiet periods, missed changes, recovery ----------------------

def _poll_events():
    ev = _healthy_events()
    ev.record("foreground", 50, 2)
    return ev


def test_a_quiet_desktop_never_declares_stale(capsys):
    ev = _poll_events()
    ev.last_event_at = ev.last_relevant_at = time.monotonic() - 3600   # an idle hour
    for _ in range(20):
        ev.check_poll(50, 2)                      # the poll agrees with the hook
    assert not ev.stale and ev.healthy()
    assert "[WindowWatch]" not in capsys.readouterr().out


def test_a_change_the_hook_missed_declares_stale_with_numbers(capsys):
    ev = _poll_events()
    ev.check_poll(60, 3)
    assert not ev.stale                          # one poll is not enough
    ev.check_poll(60, 3)
    assert ev.stale and not ev.healthy()
    out = capsys.readouterr().out
    assert "stopped arriving -- fixed waits (last event " in out
    assert "move to another process with no event, 2 polls)" in out


def test_an_event_between_polls_restarts_the_count():
    ev = _poll_events()
    ev.check_poll(60, 2)
    ev.record("foreground", 61, 2)               # the hook is live (names another hwnd)
    ev.check_poll(60, 2)
    assert not ev.stale
    ev.check_poll(60, 2)                         # then nothing for the change it shows
    assert ev.stale


def test_recovery_needs_an_event_and_does_not_flap(capsys):
    ev = _poll_events()
    ev.check_poll(60, 2)
    ev.check_poll(60, 2)
    assert ev.stale
    ev.check_poll(50, 2)                         # back on the last reported window, no event
    assert ev.stale
    ev.record("foreground", 70, 2)
    ev.check_poll(70, 2)
    assert not ev.stale and ev.healthy()
    ev.check_poll(70, 2)
    assert not ev.stale
    lines = [l for l in capsys.readouterr().out.splitlines() if "[WindowWatch]" in l]
    assert len(lines) == 2 and lines[1] == "[WindowWatch] window events arriving again"


def test_stale_transitions_are_logged_a_bounded_number_of_times(capsys):
    ev = _poll_events()
    for i in range(10):
        ev.check_poll(100 + i)
        ev.check_poll(100 + i)
        ev.record("foreground", 100 + i, 2)
        ev.check_poll(100 + i)
    lines = [l for l in capsys.readouterr().out.splitlines() if "[WindowWatch]" in l]
    assert len(lines) == clip.WATCH_STATE_LOGS


def test_a_failed_hook_install_falls_back_and_says_so_once(monkeypatch, capsys):
    ev = clip.WindowEvents()
    hook = clip.WinEventHook(ev, start_timeout=2.0)
    fake = SimpleNamespace(SetWinEventHook=lambda *a: 0, UnhookWinEvent=lambda h: None)
    monkeypatch.setattr(hook, "_api", lambda: (fake, lambda fn: fn))
    monkeypatch.setattr(clip.ctypes, "get_last_error", lambda: 5)
    assert hook.start() is False
    hook._thread.join(2)
    assert not ev.installed and not ev.healthy()
    out = capsys.readouterr().out
    assert "[WindowWatch] window events unavailable (hook install failed, error 5) -- fixed waits" in out
    slept = []
    assert clip.settle(ev, 1.5, 0.4, lambda: True, sleep=slept.append) == (1.5, "fixed")


def test_the_callback_filters_and_never_prints(capsys):
    ev = clip.WindowEvents(own_pid=1)
    hook = clip.WinEventHook(ev)
    roots = {100: 100, 200: 100}                        # 200 is a child of 100

    def pid_of(hwnd, out):
        out._obj.value = 7

    hook._user32 = SimpleNamespace(GetAncestor=lambda h, f: roots.get(h, 0),
                                   GetWindowThreadProcessId=pid_of)
    cb = hook._callback
    cb(0, clip.EVENT_OBJECT_SHOW, 100, 0, 0, 0, 0)       # top-level: kept
    cb(0, clip.EVENT_OBJECT_SHOW, 200, 0, 0, 0, 0)       # child window: dropped
    cb(0, clip.EVENT_OBJECT_SHOW, 100, -4, 0, 0, 0)      # not OBJID_WINDOW: dropped
    cb(0, clip.EVENT_OBJECT_NAMECHANGE, 100, 0, 3, 0, 0)  # a child element: dropped
    cb(0, 0x8005, 100, 0, 0, 0, 0)                       # not a hooked event: dropped
    cb(0, clip.EVENT_OBJECT_DESTROY, 300, 0, 0, 0, 0)    # never seen top-level: dropped
    cb(0, clip.EVENT_OBJECT_DESTROY, 100, 0, 0, 0, 0)    # seen top-level: kept
    cb(0, clip.EVENT_SYSTEM_FOREGROUND, 100, 0, 0, 0, 0)
    assert [(k, h, p) for _t, k, h, p in ev.ring] == [("show", 100, 7), ("destroy", 100, 7),
                                                      ("foreground", 100, 7)]
    assert capsys.readouterr().out == ""


def test_window_exists_compares_titles_in_memory_only(monkeypatch, capsys):
    import win32gui
    import win32process

    wins = {1: ("Secret Budget.xlsx - Excel", 50), 2: ("Untitled - Notepad", 60)}
    monkeypatch.setattr(win32gui, "EnumWindows", lambda fn, x: [fn(h, x) for h in wins])
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda h: True)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: wins[h][0])
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda h: (0, wins[h][1]))
    ev = clip.WindowEvents()
    ev._names = {50: "excel", 60: "notepad"}
    assert ev.window_exists(title="budget")()
    assert ev.window_exists(process="notepad")()
    assert not ev.window_exists(title="calculator")()
    assert capsys.readouterr().out == ""


def test_the_executor_settles_with_the_expected_window(monkeypatch):
    calls = []
    monkeypatch.setattr(verify.events, "settle",
                        lambda w, cap, floor, expect, label: calls.append((cap, floor, label)))
    ev = _healthy_events()
    api = SimpleNamespace(live=SimpleNamespace(window_events=ev),
                          _window_belongs_to=lambda h, app: True)
    from core.tools import SPECS

    for action, param in (("open_app", "notepad"), ("browser_navigate", "x.org"),
                          ("click_on_text", "OK")):
        verify.settle_after(api, action, param, SPECS[action], SPECS[action].settle)
    assert calls == [(1.5, 0.4, "window+quiet"), (1.0, 0.2, "browser window+quiet"),
                     (0.3, 0.15, "quiet")]


@pytest.mark.parametrize("live", [None, SimpleNamespace(), SimpleNamespace(window_events=object())])
def test_the_executor_keeps_the_fixed_settle_without_window_events(monkeypatch, live):
    slept = []
    monkeypatch.setattr(verify.time, "sleep", slept.append)
    from core.tools import SPECS

    api = SimpleNamespace(live=live) if live is not None else SimpleNamespace()
    verify.settle_after(api, "open_app", "notepad", SPECS["open_app"], 1.5)
    verify.settle_after(api, "run_command", "dir", SPECS["run_command"], 0.15)
    assert slept == [1.5, 0.15]


def test_floors_never_exceed_the_settle_and_risky_actions_have_none():
    from core.tools import SPECS, SETTLE_FLOORS

    for name, spec in SPECS.items():
        if name in SETTLE_FLOORS:
            assert 0 < spec.settle_floor <= spec.settle, name
        else:
            assert spec.settle_floor is None, name
    for name in ("quick_message", "open_url", "check_email", "run_command", "write_file"):
        assert name not in SETTLE_FLOORS


def test_the_step_pause_and_the_window_poll_use_window_events():
    from core.agent import AgentLoop

    src = inspect.getsource(AgentLoop._loop_steps)
    i = src.index("window_events.settle(watched, self.STEP_SETTLE_S, self.STEP_SETTLE_FLOOR")
    assert "time.sleep(self.STEP_SETTLE_S)" in src[i:i + 400]
    assert AgentLoop.STEP_SETTLE_S == 0.2 and AgentLoop.STEP_SETTLE_FLOOR == 0.05
    wait = inspect.getsource(Api._wait_for_app_window)
    assert "watched.healthy()" in wait and "time.sleep(0.4)" in wait


def test_the_open_app_window_poll_wakes_on_an_event():
    ev = _healthy_events()
    api = Api.__new__(Api)
    api.live = SimpleNamespace(window_events=ev)
    states = iter(["missing", "missing", "foreground"])
    api.app_window_state = lambda app: (next(states), "")
    _later(0.05, lambda: ev.record("show", 99, 2))
    _later(0.10, lambda: ev.record("foreground", 99, 2))
    t0 = time.monotonic()
    assert Api._wait_for_app_window(api, "notepad", timeout=5.0) is True
    assert time.monotonic() - t0 < 0.5                   # not 2 x 0.4 s


def test_a_tracker_poll_cross_checks_the_hook(monkeypatch):
    import win32gui

    from core import live_state

    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 55)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: "x")
    monkeypatch.setattr(win32gui, "EnumWindows", lambda fn, x: None)
    monkeypatch.setattr(live_state, "_visible_window_pids", lambda: set())
    monkeypatch.setattr(live_state, "_uwp_frame_apps", lambda: [])
    monkeypatch.setattr(live_state.psutil, "process_iter", lambda _a: [])
    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    tracker.window_events = _healthy_events()
    tracker.window_events.foreground = (54, 2)
    tracker._update()
    tracker._update()
    assert tracker.window_events.stale                   # the hook never saw window 55
    tracker.window_events.record("foreground", 55, 2)
    tracker._update()
    assert not tracker.window_events.stale


@pytest.mark.skipif(sys.platform != "win32", reason="real WinEvent hook: Windows only")
def test_the_real_hook_installs_and_stops_without_hanging():
    ev = clip.WindowEvents()
    hook = clip.WinEventHook(ev, start_timeout=3.0)
    assert hook.start() is True and ev.installed
    assert hook._thread.daemon                           # never blocks app exit
    assert ev.foreground[0]                              # seeded at install
    t0 = time.monotonic()
    hook.stop()
    hook._thread.join(3.0)
    assert not hook._thread.is_alive() and time.monotonic() - t0 < 3.0
    assert not ev.installed and not ev.healthy()


# ============================================================
# SAVE AS DIALOG
# ============================================================

# save_file_as: the keyboard route through a Save As dialog, against a fake
# desktop (windows, keys, the UIA tree walked by handle) and a real file.


# ============================================================
# SAVE AS DIALOG HELPERS
# ============================================================

from core.click_handler import EnhancedUIANavigator


class _SaveWorld:
    """Notepad (hwnd 1, pid 50) and what Ctrl+S / Enter / the prompt's
    buttons do. ``name_box``: (name, automation id) of the file-name Edit."""

    DOC, DIALOG, PROMPT, OTHER = 1, 7, 9, 30

    def __init__(self, path, dialog_on_save=True, prompt=False, other_app=False,
                 name_box=("File name:", "1001"), autocomplete="", content="milk eggs bread",
                 box_after=0):
        self.path, self.content = path, content
        # The file-name box joins the UIA tree only after this many looks at it.
        self.box_after, self.box_looks = box_after, 0
        self.dialog_on_save, self.prompt, self.other_app = dialog_on_save, prompt, other_app
        self.name_box, self.autocomplete = name_box, autocomplete
        self.windows = {self.DOC: [50, "Notepad", "*Untitled - Notepad", True]}
        self.fg = self.DOC
        self.field, self.selected = "Untitled.txt", False
        self.keys, self.typed, self.invoked = [], [], []

    # -- the app's behaviour ------------------------------------------------------
    def open(self, hwnd, pid, cls, title):
        self.windows[hwnd] = [pid, cls, title, True]
        self.fg = hwnd

    def close(self, hwnd):
        self.windows.pop(hwnd, None)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(self.content)
        self.close(self.DIALOG)
        self.windows[self.DOC][2] = os.path.basename(self.path) + " - Notepad"
        self.fg = self.DOC

    def hotkey(self, *keys):
        self.keys.append("+".join(keys))
        if keys == ("ctrl", "s") and self.other_app:
            self.open(self.OTHER, 77, "Chrome_WidgetWin_1", "Something else")
        elif keys == ("ctrl", "s") and self.dialog_on_save:
            self.open(self.DIALOG, 50, "#32770", "Save as")
        elif keys == ("ctrl", "a"):
            self.selected = True

    def press(self, key):
        self.keys.append(key)
        if key == "enter" and self.fg == self.DIALOG:
            if self.prompt:
                self.open(self.PROMPT, 50, "#32770", "Confirm Save As")
            else:
                self.save()
        elif key == "escape" and self.fg == self.PROMPT:
            self.close(self.PROMPT)
            self.fg = self.DIALOG

    def type(self, text):
        self.typed.append(text)
        self.field = (text if self.selected else self.field + text) + self.autocomplete
        self.selected = False
        return True

    # -- the UIA tree -------------------------------------------------------------
    def element(self, ctype, name, aid, on_invoke=None):
        world = self

        def invoke():
            world.invoked.append(name)
            if on_invoke:
                on_invoke()

        return SimpleNamespace(
            window_text=lambda: name,
            element_info=SimpleNamespace(automation_id=aid, control_type=ctype),
            is_enabled=lambda: True,
            set_focus=lambda: None,
            get_value=lambda: world.field,
            invoke=invoke,
        )

    def descendants(self, hwnd, control_type):
        if hwnd not in self.windows:
            raise RuntimeError("gone")
        if hwnd == self.DIALOG:
            if control_type == "Edit":
                self.box_looks += 1
            items = [self.element("Edit", "Search", ""),
                     self.element("Button", "Save", "1")]
            if self.box_looks > self.box_after:
                items.insert(1, self.element("Edit", *self.name_box))
        elif hwnd == self.PROMPT:
            def yes():
                self.close(self.PROMPT)
                self.save()

            def no():
                self.close(self.PROMPT)
                self.fg = self.DIALOG
            items = [self.element("Button", "Yes", "6", yes),
                     self.element("Button", "No", "7", no)]
        else:
            items = []
        return [e for e in items if e.element_info.control_type == control_type]


def _save_api(monkeypatch, world, on_target=True, events=None):
    win = world.windows
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        IsWindow=lambda h: h in win,
        IsWindowVisible=lambda h: h in win and win[h][3],
        GetWindowText=lambda h: win[h][2],
        GetClassName=lambda h: win[h][1],
        GetForegroundWindow=lambda: world.fg,
        GetWindow=lambda h, cmd: world.DOC if h in (world.DIALOG, world.PROMPT) else 0,
    ))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (0, win[h][0])))
    monkeypatch.setattr(api_mod, "pyautogui", SimpleNamespace(hotkey=world.hotkey, press=world.press))
    monkeypatch.setattr(clip, "send_unicode", world.type)
    monkeypatch.setattr("core.api.time.sleep", lambda _s: None)

    class FakeDesktop:
        def __init__(self, backend):
            assert backend == "uia"

        def window(self, handle):
            return SimpleNamespace(wrapper_object=lambda: SimpleNamespace(
                descendants=lambda control_type: world.descendants(handle, control_type)))

    monkeypatch.setattr("core.click_handler.Desktop", FakeDesktop, raising=False)
    nav = EnhancedUIANavigator.__new__(EnhancedUIANavigator)
    nav.available = True
    nav.click_element = lambda el: False
    api = Api.__new__(Api)
    api.uia_nav = nav
    api.live = SimpleNamespace(window_events=events, snapshot={})
    api._last_opened_app = "notepad"
    api._resolve_path_placeholders = lambda p: p
    api.last_result_text = ""

    def ensure(what):
        if on_target:
            return f"The {what} went to 'x'.", True
        return f"NOT SENT: the {what} was not sent because notepad is not in front.", False

    api._ensure_input_target = ensure
    for name in ("SAVE_DIALOG_WAIT_S", "SAVE_CLOSE_WAIT_S", "SAVE_FILE_WAIT_S", "SAVE_TITLE_WAIT_S",
                 "SAVE_NAME_BOX_WAIT_S"):
        setattr(api, name, 0.05)
    return api


# ============================================================
# SAVE AS DIALOG TESTS
# ============================================================

def test_save_file_as_runs_the_dialog_flow_and_checks_the_file(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path)
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is True
    # Ctrl+S, select all in the name box, the path typed ONCE, Enter -- nothing else.
    assert world.keys == ["ctrl+s", "ctrl+a", "enter"]
    assert world.typed == [path] and world.field == path       # replaced, not appended
    assert open(path, encoding="utf-8").read() == "milk eggs bread"
    assert api.last_result_text.startswith(f"Saved to {path} (checked on disk: 15 bytes")
    assert "the window title is now 'groceries.txt - Notepad'" in api.last_result_text
    log = capsys.readouterr().out
    assert log.splitlines() == [
        "[SaveAs] dialog appeared",
        "[SaveAs] file-name box found by name",
        f"[SaveAs] typed {len(path)} chars, read back: match",
        "[SaveAs] saved (file checked, title changed: yes)",
    ]
    for content in ("groceries", str(tmp_path), "Notepad", "Save as", "Untitled"):
        assert content not in log                                 # content-free


def test_save_file_as_finds_the_name_box_by_prefix_never_by_its_value():
    nav = EnhancedUIANavigator
    assert nav.name_has_prefix("File name:", "file name")
    assert nav.name_has_prefix("  File  Name : ", "File name:")
    # The real runs asked for 'File name: reminder.txt': that never matches.
    assert not nav.name_has_prefix("File name:", "File name: reminder.txt")
    assert not nav.name_has_prefix("Save as type:", "file name")
    assert not nav.name_has_prefix("anything", "")


def test_save_file_as_falls_back_to_the_automation_id(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "notes.txt")
    world = _SaveWorld(path, name_box=("Dateiname:", "1001"))   # a localized name
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is True
    assert "[SaveAs] file-name box found by automation id" in capsys.readouterr().out


def test_save_file_as_stops_when_no_dialog_appears(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, dialog_on_save=False)
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is False
    assert world.keys == ["ctrl+s"] and world.typed == []       # never types into the document
    assert not os.path.exists(path)
    assert api.last_result_text.startswith(
        "NOT SAVED: no Save As dialog appeared after Ctrl+S, so nothing was typed.")
    assert "call save_file_as again" in api.last_result_text
    assert capsys.readouterr().out.splitlines() == ["[SaveAs] stopped: no dialog"]


def test_save_file_as_stops_when_another_app_comes_to_the_front(monkeypatch, tmp_path):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, other_app=True)
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is False
    assert world.typed == [] and world.keys == ["ctrl+s"]
    assert "a window of another app came to the front" in api.last_result_text


def test_save_file_as_uses_a_dialog_that_is_already_open(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path)
    world.open(world.DIALOG, 50, "#32770", "Save as")             # an earlier Ctrl+S
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is True
    assert world.keys == ["ctrl+a", "enter"] and world.typed == [path]
    assert "title changed: yes" in capsys.readouterr().out


def test_save_file_as_never_presses_enter_on_a_field_it_could_not_set(monkeypatch, tmp_path):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, autocomplete=".bak")                 # the box holds something else
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is False
    assert world.keys == ["ctrl+s", "ctrl+a"] and world.typed == [path]
    assert "Enter was not pressed" in api.last_result_text and not os.path.exists(path)


def test_save_file_as_without_a_name_box_types_nothing(monkeypatch, tmp_path):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, name_box=("Search", "x"))
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is False
    assert world.typed == [] and world.keys == ["ctrl+s"]
    assert "no file-name box was found" in api.last_result_text


def test_save_file_as_answers_no_to_an_unapproved_replace_prompt(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, prompt=True)                         # the file was not there before
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(path) is False
    assert world.invoked == ["No"] and not os.path.exists(path)
    assert "it was NOT replaced and nothing was overwritten" in api.last_result_text
    log = capsys.readouterr().out
    assert "[SaveAs] replace prompt: not approved, answered no" in log
    assert "[SaveAs] stopped: not replaced" in log


def test_save_file_as_replaces_only_a_file_the_user_approved(monkeypatch, tmp_path, capsys):
    from core.tools import ToolRegistry

    path = tmp_path / "groceries.txt"
    path.write_text("old", encoding="utf-8")
    world = _SaveWorld(str(path), prompt=True)
    api = _save_api(monkeypatch, world)
    # An existing target asks first: the agent's confirmation gate.
    reg = ToolRegistry(db_path=str(tmp_path / "t.db"))
    assert reg.requires_confirmation("save_file_as", str(path))
    assert reg.requires_confirmation("save_file_as", f'"{path}"')
    assert not reg.requires_confirmation("save_file_as", str(tmp_path / "new.txt"))
    reg._conn and reg._conn.close()
    assert api._act_save_file_as(str(path)) is True              # runs only after a yes
    assert world.invoked == ["Yes"] and path.read_text(encoding="utf-8") == "milk eggs bread"
    assert "It replaced the existing file, as approved." in api.last_result_text
    assert "[SaveAs] replace prompt: approved, answered yes" in capsys.readouterr().out


@pytest.mark.parametrize("param, says", [
    ("", "no path was given"),
    ("groceries.txt", "must be a full path"),
    ("{tmp}", "that path is a folder"),
    ("{tmp}\\missing\\g.txt", "its folder does not exist"),
    ("{tmp}\\a\nb.txt", "not one line"),
])
def test_save_file_as_refuses_a_bad_path_before_pressing_anything(monkeypatch, tmp_path, param, says):
    world = _SaveWorld(str(tmp_path / "g.txt"))
    api = _save_api(monkeypatch, world)
    assert api._act_save_file_as(param.replace("{tmp}", str(tmp_path))) is False
    assert says in api.last_result_text and "Nothing was pressed." in api.last_result_text
    assert world.keys == [] and world.typed == []


def test_save_file_as_sends_nothing_when_the_app_is_not_in_front(monkeypatch, tmp_path):
    world = _SaveWorld(str(tmp_path / "g.txt"))
    api = _save_api(monkeypatch, world, on_target=False)
    assert api._act_save_file_as(str(tmp_path / "g.txt")) is False
    assert api.last_result_text.startswith("NOT SENT")
    assert world.keys == [] and world.typed == []


def test_save_file_as_waits_on_window_events_when_they_are_healthy(monkeypatch, tmp_path):
    ev = _healthy_events()
    waits = []
    real = ev.wait_for

    def spy(condition, timeout, poll=0.05):
        waits.append(timeout)
        return real(condition, timeout, poll)

    monkeypatch.setattr(ev, "wait_for", spy)
    path = str(tmp_path / "g.txt")
    world = _SaveWorld(path)
    api = _save_api(monkeypatch, world, events=ev)
    assert api._act_save_file_as(path) is True
    assert len(waits) >= 2                                        # the dialog, then its close


def test_save_file_as_is_registered_dispatched_and_on_the_menu():
    from core.tools import SPECS, action_menu

    spec = SPECS["save_file_as"]
    assert spec.args == "full file path" and "file" in spec.categories
    assert ("- save_file_as <full file path>: Save the document in front via its Save As "
            "dialog (checked on disk)") in action_menu()
    src = inspect.getsource(Api._exec_sync)
    assert 'elif action == "save_file_as":' in src and "self._act_save_file_as(param)" in src


def _save_clock(monkeypatch):
    """A fake clock for core.api: sleep advances it (no real waiting)."""
    clock = {"now": 1000.0, "sleeps": []}

    def sleep(s):
        clock["sleeps"].append(s)
        clock["now"] += s

    monkeypatch.setattr(api_mod, "time", SimpleNamespace(
        time=time.time, sleep=sleep, monotonic=lambda: clock["now"]))
    return clock


def test_save_file_as_waits_for_a_name_box_that_joins_the_tree_late(monkeypatch, tmp_path, capsys):
    # Real runs: "dialog appeared", then "no file-name box" on the one look.
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, box_after=2)
    api = _save_api(monkeypatch, world)
    clock = _save_clock(monkeypatch)
    api.SAVE_NAME_BOX_WAIT_S = config.SAVE_NAME_BOX_WAIT_S        # the default, 3.0 s
    assert api._act_save_file_as(path) is True
    assert world.box_looks == 3 and world.typed == [path]
    assert clock["sleeps"][:2] == [config.SAVE_BOX_POLL_S] * 2     # short sleeps between looks
    log = capsys.readouterr().out.splitlines()
    assert log[:3] == ["[SaveAs] dialog appeared",
                       "[SaveAs] file-name box ready after 0.20s (3 looks)",
                       "[SaveAs] file-name box found by name"]


def test_save_file_as_gives_up_on_the_name_box_after_the_timeout(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path, box_after=10 ** 6)                     # never appears
    api = _save_api(monkeypatch, world)
    clock = _save_clock(monkeypatch)
    api.SAVE_NAME_BOX_WAIT_S = config.SAVE_NAME_BOX_WAIT_S
    t0 = clock["now"]
    assert api._act_save_file_as(path) is False
    waited = clock["now"] - t0
    assert config.SAVE_NAME_BOX_WAIT_S <= waited <= config.SAVE_NAME_BOX_WAIT_S + 0.2  # bounded
    assert 25 <= world.box_looks <= 35 and max(clock["sleeps"]) <= config.SAVE_BOX_POLL_S
    assert world.typed == [] and world.keys == ["ctrl+s"]
    assert api.last_result_text.startswith("NOT SAVED: a dialog opened, but no file-name box")
    (line,) = [l for l in capsys.readouterr().out.splitlines() if "stopped" in l]
    assert re.fullmatch(r"\[SaveAs\] stopped: no file-name box \(waited 3\.\d\ds, \d+ looks\)", line)


def test_save_file_as_adds_no_wait_when_the_box_is_there_at_once(monkeypatch, tmp_path, capsys):
    path = str(tmp_path / "groceries.txt")
    world = _SaveWorld(path)
    api = _save_api(monkeypatch, world)
    clock = _save_clock(monkeypatch)
    api.SAVE_NAME_BOX_WAIT_S = config.SAVE_NAME_BOX_WAIT_S
    assert api._act_save_file_as(path) is True
    assert world.box_looks == 1
    # Ctrl+S -> focus: no poll sleep before the fixed 0.1 s focus pause.
    assert clock["sleeps"][0] == 0.1 and world.keys[:2] == ["ctrl+s", "ctrl+a"]
    assert "ready after" not in capsys.readouterr().out


def test_save_file_as_name_box_wait_wakes_on_window_events(monkeypatch, tmp_path, capsys):
    ev = _healthy_events()
    polls = []
    real = ev.wait_for

    def spy(condition, timeout, poll=0.05):
        polls.append((timeout, poll))
        return real(condition, timeout, poll)

    monkeypatch.setattr(ev, "wait_for", spy)
    path = str(tmp_path / "g.txt")
    world = _SaveWorld(path, box_after=1)
    api = _save_api(monkeypatch, world, events=ev)
    api.SAVE_NAME_BOX_WAIT_S = 1.0
    assert api._act_save_file_as(path) is True
    assert (1.0, config.SAVE_BOX_POLL_S) in polls and world.box_looks == 2
    assert "ready after" in capsys.readouterr().out


# ============================================================
# CLIPBOARD WIN32 CALLS
# ============================================================

# The clipboard helpers' own Win32 calls: typed on a private user32 (64-bit
# handles), OpenClipboard tried again briefly while another process holds it,
# and the clipboard closed on every path once it was opened. No test here
# touches the real clipboard.


# ============================================================
# CLIPBOARD WIN32 CALLS TESTS
# ============================================================

import ctypes


def test_the_clipboard_helpers_use_a_private_typed_user32(monkeypatch):
    from ctypes import wintypes

    monkeypatch.setattr(clip, "_user32_typed", None)
    u = clip._user32()
    assert u is not ctypes.windll.user32 and clip._user32() is u      # private, one instance
    assert u.GetOpenClipboardWindow.restype is wintypes.HWND and u.GetOpenClipboardWindow.argtypes == []
    assert u.GetClipboardSequenceNumber.restype is wintypes.DWORD
    assert u.GetWindowThreadProcessId.argtypes == [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    assert u.GetWindowThreadProcessId.restype is wintypes.DWORD
    # The shared loader every other library uses is left alone.
    assert ctypes.windll.user32.GetOpenClipboardWindow.restype is not wintypes.HWND


def test_holder_reads_the_owner_through_the_typed_calls(monkeypatch):
    def thread_pid(hwnd, pid_ref):
        pid_ref._obj.value = 4242 if hwnd == 0x7FFF00001234 else 0   # a 64-bit handle
        return 1

    monkeypatch.setattr(clip, "_user32", lambda: SimpleNamespace(
        GetOpenClipboardWindow=lambda: 0x7FFF00001234, GetWindowThreadProcessId=thread_pid))
    monkeypatch.setattr(clip.os, "getpid", lambda: 4242)
    assert clip.holder() == "self"
    monkeypatch.setattr(clip.os, "getpid", lambda: 1)
    assert clip.holder() == "other"


def test_open_is_retried_briefly_while_another_process_holds_it(monkeypatch, capsys):
    calls = _fake_win32(monkeypatch)
    left = {"busy": 2}
    win = sys.modules["win32clipboard"]

    def open_():
        calls.append("open")
        if left["busy"]:
            left["busy"] -= 1
            raise OSError("held")
    win.OpenClipboard = open_
    sleeps = []
    monkeypatch.setattr("core.live_state.time.sleep", sleeps.append)
    assert clip.set_text("x") is True
    assert calls == ["open", "open", "open", "close"]
    assert sleeps == list(clip.OPEN_RETRY_DELAYS[:2])
    assert capsys.readouterr().out == ""                       # no busy line: it opened


def test_open_gives_up_after_the_short_retries_and_never_closes_what_it_did_not_open(
        monkeypatch, capsys):
    calls = _fake_win32(monkeypatch, fail_open=True)
    sleeps = []
    monkeypatch.setattr("core.live_state.time.sleep", sleeps.append)
    monkeypatch.setattr(clip, "holder", lambda: "other")
    assert clip.set_text("x") is False
    assert calls == ["open"] * (len(clip.OPEN_RETRY_DELAYS) + 1)
    assert sleeps == list(clip.OPEN_RETRY_DELAYS) and sum(sleeps) < 0.1
    assert capsys.readouterr().out.strip() == "[TypeText] clipboard busy holder=other"


@pytest.mark.parametrize("failing", ["EmptyClipboard", "SetClipboardData"])
def test_a_failed_write_always_closes_the_clipboard(monkeypatch, failing):
    calls = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "holder", lambda: "unknown")

    def boom(*_a):
        raise OSError("refused")
    setattr(sys.modules["win32clipboard"], failing, boom)
    assert clip.set_text("x") is False
    assert calls == ["open", "close"] and clip._ours_open == 0


def test_a_failed_read_always_closes_the_clipboard(monkeypatch):
    calls = _fake_win32(monkeypatch)

    def boom(_f):
        raise OSError("gone")
    sys.modules["win32clipboard"].GetClipboardData = boom
    assert clip.read_text() is None
    assert calls == ["open", "close"] and clip._ours_open == 0


# ============================================================
# CLIPBOARD WRITE BLOCK
# ============================================================

# The clipboard write uses our own moveable memory block through a private,
# fully typed kernel32/user32. pywin32 311's SetClipboardData took a str that
# int() accepts ("37") as a HANDLE: typing digits into Calculator handed
# Windows handle 37, and the next write freed it (0xC0000374 in _set).


# ============================================================
# CLIPBOARD WRITE BLOCK HELPERS
# ============================================================

_REAL_WRITE_UNICODE = clip._write_unicode    # before any test patches it

class _FakeGlobalMemory:
    """kernel32/user32 stand-ins over one real ctypes buffer (so memmove is
    real); records the calls in order."""

    HANDLE = 0x7FFF00002000                   # a 64-bit handle value

    def __init__(self, lock_ok=True, set_ok=True):
        self.calls, self.buf, self.size = [], None, 0
        self.lock_ok, self.set_ok = lock_ok, set_ok

    def GlobalAlloc(self, flags, size):
        self.calls.append(("alloc", flags, size))
        self.size, self.buf = size, ctypes.create_string_buffer(size)
        return self.HANDLE

    def GlobalLock(self, h):
        self.calls.append(("lock", h))
        return ctypes.addressof(self.buf) if self.lock_ok else None

    def GlobalUnlock(self, h):
        self.calls.append(("unlock", h))
        return 0

    def GlobalFree(self, h):
        self.calls.append(("free", h))
        return None

    def SetClipboardData(self, fmt, h):
        self.calls.append(("set", fmt, h))
        return h if self.set_ok else None

    def written(self):
        return self.buf.raw[:self.size]


def _fake_memory(monkeypatch, **kw):
    mem = _FakeGlobalMemory(**kw)
    monkeypatch.setattr(clip, "_kernel32", lambda: mem)
    monkeypatch.setattr(clip, "_user32", lambda: mem)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 1418)
    return mem


# ============================================================
# CLIPBOARD WRITE BLOCK TESTS
# ============================================================

def test_the_write_uses_a_private_kernel32_with_64_bit_signatures(monkeypatch):
    from ctypes import wintypes

    monkeypatch.setattr(clip, "_kernel32_typed", None)
    monkeypatch.setattr(clip, "_user32_typed", None)
    k, u = clip._kernel32(), clip._user32()
    assert k is not ctypes.windll.kernel32 and clip._kernel32() is k
    assert (k.GlobalAlloc.argtypes, k.GlobalAlloc.restype) == (
        [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL)
    assert (k.GlobalLock.argtypes, k.GlobalLock.restype) == ([wintypes.HGLOBAL], wintypes.LPVOID)
    assert (k.GlobalUnlock.argtypes, k.GlobalUnlock.restype) == ([wintypes.HGLOBAL], wintypes.BOOL)
    assert (k.GlobalFree.argtypes, k.GlobalFree.restype) == ([wintypes.HGLOBAL], wintypes.HGLOBAL)
    assert (u.SetClipboardData.argtypes, u.SetClipboardData.restype) == (
        [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE)
    for t in (wintypes.HGLOBAL, wintypes.LPVOID, wintypes.HANDLE):   # pointer-sized
        assert ctypes.sizeof(t) == ctypes.sizeof(ctypes.c_void_p)


@pytest.mark.parametrize("text, units", [
    ("37", 2), ("3", 1), ("", 0), ("hello", 5), ("a🙂", 3), ("x" * 10_000, 10_000),
    ("line1\r\nline2", 12),
])
def test_the_block_holds_the_text_and_its_nul_and_the_clipboard_owns_it(monkeypatch, text, units):
    mem = _fake_memory(monkeypatch)
    clip._write_unicode(text)
    size = (units + 1) * 2
    assert mem.calls == [("alloc", clip.GMEM_MOVEABLE, size), ("lock", mem.HANDLE),
                         ("unlock", mem.HANDLE), ("set", 13, mem.HANDLE)]    # never freed
    assert mem.written() == text.encode("utf-16-le") + b"\x00\x00"


def test_a_digit_string_is_text_never_a_handle(monkeypatch):
    # The Calculator crash: "37" must reach SetClipboardData as our block's handle.
    calls = _fake_win32(monkeypatch)
    mem = _fake_memory(monkeypatch)
    monkeypatch.setattr(clip, "_write_unicode", _REAL_WRITE_UNICODE)
    for digits in ("37", "3", "007", " 37", "12", "-5"):
        mem.calls.clear()
        assert clip.set_text(digits) is True
        assert ("set", 13, mem.HANDLE) in mem.calls and ("free", mem.HANDLE) not in mem.calls
        assert mem.written() == digits.encode("utf-16-le") + b"\x00\x00"
    assert calls.count("open") == calls.count("close") == 6


@pytest.mark.parametrize("fail", ["lock", "set"])
def test_the_block_is_freed_only_when_the_clipboard_did_not_take_it(monkeypatch, fail):
    mem = _fake_memory(monkeypatch, lock_ok=fail != "lock", set_ok=fail != "set")
    with pytest.raises(OSError) as e:
        clip._write_unicode("37")
    assert e.value.winerror == 1418
    assert mem.calls[-1] == ("free", mem.HANDLE)
    assert [c[0] for c in mem.calls].count("free") == 1
    if fail == "lock":
        assert not any(c[0] in ("unlock", "set") for c in mem.calls)    # nothing to unlock or set
    else:
        assert [c[0] for c in mem.calls] == ["alloc", "lock", "unlock", "set", "free"]


def test_a_refused_block_is_logged_as_denied_and_the_clipboard_closed(monkeypatch, capsys):
    calls = _fake_win32(monkeypatch)
    _fake_memory(monkeypatch, set_ok=False)
    monkeypatch.setattr(clip, "_write_unicode", _REAL_WRITE_UNICODE)
    monkeypatch.setattr(clip, "holder", lambda: "self")
    assert clip.set_text("37") is False
    assert calls == ["open", "close"]
    assert capsys.readouterr().out.strip() == "[TypeText] clipboard denied holder=self code=1418"


def test_nothing_hands_a_str_to_pywin32s_set_clipboard_data():
    import inspect

    src = inspect.getsource(clip)
    assert "w.SetClipboardData(" not in src and "win32clipboard.SetClipboardData(" not in src
    assert "SetClipboardText(" not in src

