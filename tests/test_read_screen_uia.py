"""read_screen_text: exact accessibility-tree text beside the vision answer."""

import threading
import time
from types import SimpleNamespace

import pytest

import core.uia_text as uia_text
from core.api import Api


@pytest.fixture(autouse=True)
def _no_trace(monkeypatch):
    monkeypatch.delenv("MISTAI_TRACE_READS", raising=False)


def _api(vision_reply, exact=("", "", False), ready=True):
    api = Api.__new__(Api)
    calls = []

    def call(prompt, _frame):
        calls.append(prompt)
        return vision_reply

    api.vision = SimpleNamespace(ready=ready, _call=call)
    api.screen = SimpleNamespace(frame=object())
    api.last_result_text = ""
    seen = []

    if len(exact) == 3:
        exact = tuple(exact) + ("exact", None)

    def exact_ui_text(query="", include_browser=False):
        seen.append(include_browser)
        return exact

    api._exact_ui_text = exact_ui_text
    return api, calls, seen


NATIVE = ("Display is 23 | Expression is 37 × 23", "Calculator", True)


def test_native_window_gets_the_exact_ui_text_beside_vision():
    api, _, _ = _api("23", NATIVE)
    out = Api.read_screen_text(api, "what number is shown?")
    assert out.startswith("Vision: 23\n")
    assert "EXACT UI TEXT of 'Calculator' (accessibility tree, exact): Display is 23" in out
    assert "NOTE" not in out
    assert api.last_result_text == out


def test_disagreeing_numbers_are_flagged():
    api, _, _ = _api("The display shows 851.", NATIVE)
    out = Api.read_screen_text(api, "what number is shown?")
    assert "NOTE: the vision answer's number(s) 851 do not appear in the exact UI text" in out
    assert "the exact UI text is what the window contains" in out


def test_vision_not_visible_with_exact_text_does_not_send_the_agent_elsewhere():
    api, _, _ = _api("NOT VISIBLE ON SCREEN", NATIVE)
    out = Api.read_screen_text(api, "what number is shown?")
    assert "NOT VISIBLE ON SCREEN" not in out
    assert "Vision: vision could not see it; use the EXACT UI TEXT below" in out
    assert "Display is 23" in out


def test_empty_uia_falls_back_to_vision_alone():
    api, _, _ = _api("851", ("", "Calculator", True))
    assert Api.read_screen_text(api, "what number?") == "851"


def test_browser_gets_vision_alone_and_no_uia_walk_unless_tracing():
    api, _, seen = _api("Hello", ("", "Docs - Mozilla Firefox", False))
    assert Api.read_screen_text(api, "what does it say?") == "Hello"
    assert seen == [False]


def test_vision_unavailable_still_reports_exact_ui_text():
    api, calls, _ = _api("unused", NATIVE, ready=False)
    out = Api.read_screen_text(api, "what number?")
    assert calls == []
    assert "Vision isn't configured" in out and "Display is 23" in out
    assert "NOTE" not in out


def test_trace_is_off_by_default(capsys):
    api, _, seen = _api("851", NATIVE)
    Api.read_screen_text(api, "q")
    assert "[ReadTrace]" not in capsys.readouterr().out
    assert seen == [False]


def test_trace_logs_both_readers_capped_and_text_only(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_TRACE_READS", "1")
    monkeypatch.setattr(Api, "_read_trace_warned", False)
    api, _, seen = _api("851", ("x" * 500, "Calculator", True))
    Api.read_screen_text(api, "what number?")
    Api.read_screen_text(api, "again?")
    out = capsys.readouterr().out
    assert seen == [True, True]  # browsers too, for the trace
    assert out.count("skim these logs before sharing") == 1
    line = [ln for ln in out.splitlines() if ln.startswith("[ReadTrace] query='what number?'")][0]
    assert "vision='851'" in line
    assert "uia='" + "x" * 300 + "'" in line and "x" * 301 not in line


def test_asked_window_not_in_front_is_read_and_labelled(monkeypatch):
    api, _, _ = _api("12", ("Display is 851", "Calculator", True, "background", "calculator"))
    out = Api.read_screen_text(api, "what does Calculator show?")
    assert ("EXACT UI TEXT of 'Calculator' (accessibility tree, exact; this window is NOT "
            "in front, so the screenshot shows a different window): Display is 851") in out
    assert "NOTE: the vision answer's number(s) 12" in out


def test_only_another_window_is_labelled_not_exact(monkeypatch):
    api, _, _ = _api("NOT VISIBLE ON SCREEN",
                     ("PS C:\> 851", "Windows PowerShell", True, "other", "calculator"))
    out = Api.read_screen_text(api, "what does Calculator show?")
    assert "EXACT" not in out and "NOTE" not in out
    assert ("UI TEXT of the foreground window 'Windows PowerShell', not the calculator "
            "window asked about (no calculator window was found): PS C:\> 851") in out
    assert out.startswith("Vision: NOT VISIBLE ON SCREEN")


# -- Which window is read ----------------------------------------------------------------


def _window_api(monkeypatch, foreground, owners, open_apps, target=None, query_hwnd=None):
    """owners: {hwnd: app}; the asked window lookup returns query_hwnd."""
    import sys

    titles = {1: "main.py - Visual Studio Code", 2: "Calculator", 3: "Untitled - Notepad"}
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: foreground, GetWindowText=lambda h: titles[h]))
    monkeypatch.setitem(sys.modules, "win32process",
                        SimpleNamespace(GetWindowThreadProcessId=lambda h: (0, h)))
    monkeypatch.setattr("core.api.psutil.Process",
                        lambda pid: SimpleNamespace(name=lambda: "app.exe"))
    walked = []
    monkeypatch.setattr(uia_text, "foreground_uia_text",
                        lambda hwnd=None, **k: walked.append(hwnd) or (f"text of {hwnd}", ""))
    api = Api.__new__(Api)
    api.live = SimpleNamespace(snapshot={"open_apps": open_apps})
    api._last_opened_app = target
    api._window_belongs_to = lambda h, app: owners.get(h) == app
    api._find_hwnd_by_process_name = lambda app: next(
        (h for h, a in owners.items() if a == app), None)
    return api, walked


def test_reads_the_asked_app_window_even_when_another_is_in_front(monkeypatch):
    api, walked = _window_api(monkeypatch, 1, {1: "code", 2: "calculator"},
                              ["code", "calculator"], target="calculator")
    text, title, native, kind, asked = Api._exact_ui_text(api, "what number is shown?")
    assert (walked, title, kind, asked) == ([2], "Calculator", "background", "calculator")


def test_query_naming_an_open_app_picks_its_window(monkeypatch):
    api, walked = _window_api(monkeypatch, 1, {1: "code", 3: "notepad"}, ["code", "notepad"])
    assert Api._exact_ui_text(api, "what does Notepad say?")[3:] == ("background", "notepad")
    assert walked == [3]


def test_target_wins_when_the_query_names_several_apps(monkeypatch):
    api, walked = _window_api(monkeypatch, 2, {1: "code", 2: "calculator"},
                              ["code", "calculator"], target="calculator")
    assert Api._exact_ui_text(api, "what code is shown in calculator?")[3:] == (
        "exact", "calculator")
    assert walked == [2]


def test_missing_asked_window_reads_the_foreground_labelled_other(monkeypatch):
    api, walked = _window_api(monkeypatch, 1, {1: "code"}, ["code"], target="calculator")
    assert Api._exact_ui_text(api, "what number?")[3:] == ("other", "calculator")
    assert walked == [1]


def test_no_asked_app_reads_the_foreground(monkeypatch):
    api, walked = _window_api(monkeypatch, 3, {3: "notepad"}, ["notepad"])
    assert Api._exact_ui_text(api, "what is written here?")[3:] == ("exact", None)
    assert walked == [3]


# -- core/uia_text.py ---------------------------------------------------------------


@pytest.fixture
def fake_window(monkeypatch):
    """Foreground window 42 of process ``proc['name']``; slow list cleared."""
    import win32gui

    proc = {"name": "SlowApp.exe"}
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 42)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda _h: "Some App")
    monkeypatch.setattr(uia_text, "_process_name", lambda _h: proc["name"])
    monkeypatch.setattr(uia_text, "_slow_processes", set())
    return proc


def test_slow_process_is_skipped_for_the_rest_of_the_session(monkeypatch, capsys, fake_window):
    release = threading.Event()
    walks = []

    def slow_walk(_hwnd, _deadline, _cap):
        walks.append(1)
        release.wait(5)
        return "too late", False

    monkeypatch.setattr(uia_text, "_walk_text", slow_walk)
    t0 = time.monotonic()
    assert uia_text.foreground_uia_text(budget=0.1) == ("", "Some App")
    assert time.monotonic() - t0 < 1.0
    assert "[UIA] skipping SlowApp.exe: too slow" in capsys.readouterr().out
    # Second read: no walk at all, no repeated log line.
    assert uia_text.foreground_uia_text(budget=0.1) == ("", "Some App")
    assert len(walks) == 1
    assert "skipping" not in capsys.readouterr().out
    release.set()
    # Other processes are still read.
    fake_window["name"] = "CalculatorApp.exe"
    monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: ("Display is 23", False))
    assert uia_text.foreground_uia_text() == ("Display is 23", "Some App")


def test_walk_that_hits_its_deadline_marks_the_process_slow(monkeypatch, capsys, fake_window):
    monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: ("partial", True))
    assert uia_text.foreground_uia_text() == ("", "Some App")
    assert "SlowApp.exe" in uia_text._slow_processes
    assert "skipping SlowApp.exe" in capsys.readouterr().out


def test_foreground_uia_text_never_raises(monkeypatch, fake_window):
    def boom(*_a):
        raise RuntimeError("COM error")

    monkeypatch.setattr(uia_text, "_walk_text", boom)
    assert uia_text.foreground_uia_text() == ("", "Some App")


@pytest.mark.parametrize("text", [
    "-----BEGIN RSA PRIVATE KEY----- MIIEpAIB",
    "Key: sk-abcdefghijklmnopqrstuvwxyz123456",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAABCDEFGHIJKLMNOP",
    "MISTAI_API_KEY=abcdef123456",
    "password: hunter2hunter2",
])
def test_secret_looking_snapshot_is_withheld(monkeypatch, capsys, fake_window, text):
    monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: (text, False))
    assert uia_text.foreground_uia_text() == ("", "Some App")
    out = capsys.readouterr().out
    assert "[UIA] withheld the text of 'Some App'" in out
    assert text not in out


def test_ordinary_text_is_not_withheld(monkeypatch, fake_window):
    text = "Display is 851 | Expression is 37 × 23= | Password | Token count: 12"
    monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: (text, False))
    assert uia_text.foreground_uia_text() == (text, "Some App")


class _Info:
    def __init__(self, ctype, name, kids=(), value=None, password=False):
        self.control_type, self.name, self._kids = ctype, name, list(kids)
        props = {uia_text.UIA_VALUE_PROPERTY_ID: value,
                 uia_text.UIA_IS_PASSWORD_PROPERTY_ID: password}
        self.element = SimpleNamespace(GetCurrentPropertyValue=props.get)

    def children(self):
        return self._kids


def _fake_tree(monkeypatch, tree):
    import pywinauto.uia_element_info as uei

    monkeypatch.setattr(uei, "UIAElementInfo", lambda _h: tree)


def test_walk_collects_text_controls_dedupes_caps_and_skips_passwords(monkeypatch):
    _fake_tree(monkeypatch, _Info("Window", "Calculator", [
        _Info("Button", "Equals"),
        _Info("Text", "Display is 23"),
        _Info("Text", "Display is 23"),
        _Info("Edit", "Search", value="hello   world"),
        _Info("Edit", "PIN", value="123456", password=True),
    ]))
    text, timed_out = uia_text._walk_text(1, time.monotonic() + 5, 300)
    assert text == "Display is 23 | Search | hello world"
    assert timed_out is False
    assert len(uia_text._walk_text(1, time.monotonic() + 5, 10)[0]) <= 10


def test_walk_reports_a_timeout_when_the_deadline_passes(monkeypatch):
    _fake_tree(monkeypatch, _Info("Window", "Big", [_Info("Text", "a")]))
    assert uia_text._walk_text(1, time.monotonic() - 1, 300) == ("", True)
