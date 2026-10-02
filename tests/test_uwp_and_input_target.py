"""Store/UWP app windows (ApplicationFrameHost), open_app's fail-fast AUMID
path, and type_text/keys reporting which window actually got the input."""

import sys
from types import SimpleNamespace

import pytest

from core.api import Api

PROCS = {1: "ApplicationFrameHost.exe", 2: "CalculatorApp.exe", 3: "firefox.exe",
         4: "Notepad.exe", 5: "Code.exe"}


def _fake_desktop(monkeypatch, windows, foreground=None):
    """windows: {hwnd: (pid, class, title, [child pids])}."""
    win32gui = SimpleNamespace(
        IsWindowVisible=lambda h: True,
        GetWindowText=lambda h: windows[h][2],
        GetClassName=lambda h: windows[h][1],
        GetForegroundWindow=lambda: foreground,
        EnumWindows=lambda cb, extra: [cb(h, extra) for h in windows],
        EnumChildWindows=lambda h, cb, extra: [cb(("child", h, p), extra) for p in windows[h][3]],
        ShowWindow=lambda *a: None,
        SetForegroundWindow=lambda h: None,
    )

    def thread_pid(h):
        return (0, h[2]) if isinstance(h, tuple) else (0, windows[h][0])

    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process",
                        SimpleNamespace(GetWindowThreadProcessId=thread_pid))
    monkeypatch.setattr("core.api.psutil.Process",
                        lambda pid: SimpleNamespace(name=lambda: PROCS[pid]))


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


def _input_api(monkeypatch, target, foreground_hwnd):
    _fake_desktop(monkeypatch, {
        20: (4, "Notepad", "Untitled - Notepad", []),
        21: (5, "Chrome_WidgetWin_1", "main.py - Visual Studio Code", []),
    }, foreground=foreground_hwnd)
    api = Api.__new__(Api)
    api.live = SimpleNamespace(snapshot={"foreground_window": "stale"})
    api._last_opened_app = target
    api.switch_to_app = lambda _a: False  # no Notepad window to switch to
    api.get_clipboard = lambda: "old"
    api.set_clipboard = lambda _t: True
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


def test_type_text_off_target_warns_and_never_clears_the_wrong_window(monkeypatch):
    api, events = _input_api(monkeypatch, "notepad", 21)
    assert Api._act_type_text(api, "hello") is True
    text = api.last_result_text
    assert text.startswith("WARNING: the text went to 'main.py - Visual Studio Code', NOT to notepad")
    assert "NOT cleared" in text
    assert events == [("ctrl", "v")]  # no Ctrl+A / Delete in VS Code


@pytest.mark.parametrize("action, param", [("press_key", "enter"), ("hotkey", "ctrl+s")])
def test_keys_report_the_receiving_window_and_warn_off_target(monkeypatch, action, param):
    api, _ = _input_api(monkeypatch, "notepad", 21)
    Api._exec_sync(api, action, param)
    assert api.last_result_text.startswith("WARNING:")
    assert "Visual Studio Code" in api.last_result_text
    api, _ = _input_api(monkeypatch, "notepad", 20)
    Api._exec_sync(api, action, param)
    kind = "hotkey" if action == "hotkey" else "key"
    assert api.last_result_text == f"The {kind} '{param}' went to 'Untitled - Notepad'."


def test_failed_switch_app_still_records_the_intended_target(monkeypatch):
    api, _ = _input_api(monkeypatch, None, 21)
    assert Api._exec_sync(api, "switch_app", "Notepad") is False
    assert api._last_opened_app == "notepad"
    Api._act_press_key(api, "a")
    assert "NOT to notepad" in api.last_result_text
