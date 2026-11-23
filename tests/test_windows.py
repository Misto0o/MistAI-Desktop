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
# ============================================================

"""Windows and input targets, restore after a task, clipboard reliability."""

import sys
from types import SimpleNamespace
import os
import inspect
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
    api = Api.__new__(Api)
    api._BROWSER_APP_NAMES = set()
    api.app_window_state = lambda _n: ("not_running", "")
    api._resolve_app_exe = lambda _n: None
    api._resolve_app_id = lambda _n: "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    api.is_app_running = lambda _n: True
    waits, launched, keys = [], [], []
    api._wait_for_app_window = lambda _a, timeout: waits.append(timeout) or False
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: launched.append(args))
    monkeypatch.setattr("core.api.pyautogui.press", lambda k: keys.append(k))
    monkeypatch.setattr("core.api.pyautogui.write", lambda *a, **k: keys.append(a))

    assert Api._act_open_app(api, "calculator") is False
    assert launched == [["explorer.exe",
                         "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"]]
    assert waits == [5.0] and keys == []
    assert "no window appeared within 5s" in api.last_result_text


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
