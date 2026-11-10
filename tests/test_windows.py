"""Store/UWP app windows (ApplicationFrameHost), open_app's fail-fast AUMID
path, and type_text/keys reporting which window actually got the input.

Merged by area (pure moves, no test changed) from: test_uwp_and_input_target.py, test_window_focus.py."""

import sys
from types import SimpleNamespace
import pytest
from core.api import Api
import os
import core.api as api_mod
import core.enhanced_utils as eu
from core import verify
from core.config import normalize_window_title



# ==========================================================================
# moved from tests/test_uwp_and_input_target.py
# ==========================================================================

PROCS = {1: "ApplicationFrameHost.exe", 2: "CalculatorApp.exe", 3: "firefox.exe",
         4: "Notepad.exe", 5: "Code.exe"}


class _Refused(Exception):
    """Stands in for pywintypes.error: pywin32 raises when Windows refuses."""


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


def _activate_api(api):
    api.enhanced_available = True
    api.window_mgr = eu.WindowManager
    api.spoken = []
    api.speak = api.spoken.append
    return api


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
