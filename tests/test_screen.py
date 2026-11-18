# ============================================================
# SCREEN AND CLICK TESTS
# ============================================================
#
# Screen reads (UIA first, vision modes, frame freshness), vision coordinates and clicking.
#
# SECTION INDEX
#   1. READ SCREEN UIA
#   2. READ SCREEN UIA API
#   3. READ SCREEN UIA HELPERS
#   4. READ SCREEN UIA WINDOW API
#   5. READ SCREEN UIA INFO
#   6. READ SCREEN UIA HELPERS (2)
#   7. READ SCREEN UIA TESTS
#   8. UIA FIRST
#   9. UIA FIRST HELPERS
#   10. UIA FIRST API
#   11. UIA FIRST LOOP
#   12. UIA FIRST TESTS
#   13. FRAME FRESHNESS
#   14. FRAME FRESHNESS BUFFER
#   15. FRAME FRESHNESS TESTS
#   16. FRAME FRESHNESS REC
#   17. FRAME FRESHNESS EXEC API
#   18. FRAME FRESHNESS TESTS (2)
#   19. VISION COORDINATES
#   20. VISION COORDINATES VISION
#   21. VISION COORDINATES HELPERS
#   22. VISION COORDINATES TESTS
#   23. VISION Y NUDGE
#   24. VISION Y NUDGE HELPERS
#   25. VISION Y NUDGE TESTS
#   26. VISION MODES AND PLANNER RULES
#   27. VISION MODES AND PLANNER RULES TESTS
#   28. VISION MODES AND PLANNER RULES LOOP API
#   29. VISION MODES AND PLANNER RULES TESTS (2)
#   30. VISION MODES AND PLANNER RULES BARE API
#   31. VISION MODES AND PLANNER RULES TESTS (3)
#   32. CLICKS
#   33. CLICKS HELPERS
#   34. CLICKS TESTS
#   35. CLICKS API
#   36. CLICKS TESTS (2)
#   37. CLICKS HELPERS (2)
#   38. CLICKS FAKE
#   39. CLICKS HANDLER
#   40. CLICKS TESTS (3)
#   41. CLICKS HELPERS (3)
#   42. CLICKS TESTS (4)
#   43. OCR COMPACT MATCH
#   44. OCR COMPACT MATCH HELPERS
#   45. OCR COMPACT MATCH DATA
#   46. OCR COMPACT MATCH HELPERS (2)
#   47. OCR COMPACT MATCH TESTS
# ============================================================

"""Screen reads (UIA first, vision modes, frame freshness), vision coordinates and clicking."""

import threading
import time
from types import SimpleNamespace
import json
import base64
import io
import subprocess
import inspect
import os
import sys
import types
import difflib

import pytest
from PIL import Image

import core.screen as uia_text
import core.screen as screen_mod
import core.screen as vision_mod
from core.api import Api
from core.agent import AgentLoop
from core.screen import TEXT_CAP, is_visual_question, text_is_sufficient, CohereVision
from core.executor import Executor, ExecutionResult
from core.tools import SPECS, ToolSpec
import core.click_handler as ch
from core.config import vision_y_nudge, lazy_vision_mode, vision_gating_enabled
from core import planner
from core.live_state import describe_open_apps
from test_agent import _result, _input_repeat_and_done_check_run as _run


# ============================================================
# READ SCREEN UIA
# ============================================================

# read_screen_text: exact accessibility-tree text beside the vision answer.


# ============================================================
# READ SCREEN UIA API
# ============================================================

def _read_screen_uia_api(vision_reply, exact=("", "", False), ready=True):
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


# ============================================================
# READ SCREEN UIA HELPERS
# ============================================================

NATIVE = ("Display is 23 | Expression is 37 × 23", "Calculator", True)


# -- Which window is read ----------------------------------------------------------------


# ============================================================
# READ SCREEN UIA WINDOW API
# ============================================================

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


# -- core/uia_text.py ---------------------------------------------------------------


# ============================================================
# READ SCREEN UIA INFO
# ============================================================

class _Info:
    def __init__(self, ctype, name, kids=(), value=None, password=False):
        self.control_type, self.name, self._kids = ctype, name, list(kids)
        props = {uia_text.UIA_VALUE_PROPERTY_ID: value,
                 uia_text.UIA_IS_PASSWORD_PROPERTY_ID: password}
        self.element = SimpleNamespace(GetCurrentPropertyValue=props.get)

    def children(self):
        return self._kids


# ============================================================
# READ SCREEN UIA HELPERS (2)
# ============================================================

def _fake_tree(monkeypatch, tree):
    import pywinauto.uia_element_info as uei

    monkeypatch.setattr(uei, "UIAElementInfo", lambda _h: tree)


# ============================================================
# READ SCREEN UIA TESTS
# ============================================================

class TestReadScreenUia:
    @pytest.fixture(autouse=True)
    def _no_trace(self, monkeypatch):
        monkeypatch.delenv("MISTAI_TRACE_READS", raising=False)
        # These pin vision beside the text (what UIA-first leaves for visual or
        # thin reads); tests/test_uia_first.py covers the default.
        monkeypatch.setenv("MISTAI_UIA_FIRST", "0")

    def test_native_window_gets_the_exact_ui_text_beside_vision(self):
        api, _, _ = _read_screen_uia_api("23", NATIVE)
        out = Api.read_screen_text(api, "what number is shown?")
        assert out.startswith("Vision: 23\n")
        assert "EXACT UI TEXT of 'Calculator' (accessibility tree, exact): Display is 23" in out
        assert "NOTE" not in out
        assert api.last_result_text == out

    def test_disagreeing_numbers_are_flagged(self):
        api, _, _ = _read_screen_uia_api("The display shows 851.", NATIVE)
        out = Api.read_screen_text(api, "what number is shown?")
        assert "NOTE: the vision answer's number(s) 851 do not appear in the exact UI text" in out
        assert "the exact UI text is what the window contains" in out

    def test_vision_not_visible_with_exact_text_does_not_send_the_agent_elsewhere(self):
        api, _, _ = _read_screen_uia_api("NOT VISIBLE ON SCREEN", NATIVE)
        out = Api.read_screen_text(api, "what number is shown?")
        assert "NOT VISIBLE ON SCREEN" not in out
        assert "Vision: vision could not see it; use the EXACT UI TEXT below" in out
        assert "Display is 23" in out

    def test_empty_uia_falls_back_to_vision_alone(self):
        api, _, _ = _read_screen_uia_api("851", ("", "Calculator", True))
        assert Api.read_screen_text(api, "what number?") == "851"

    def test_browser_gets_vision_alone_and_no_uia_walk_unless_tracing(self):
        api, _, seen = _read_screen_uia_api("Hello", ("", "Docs - Mozilla Firefox", False))
        assert Api.read_screen_text(api, "what does it say?") == "Hello"
        assert seen == [False]

    def test_vision_unavailable_still_reports_exact_ui_text(self):
        api, calls, _ = _read_screen_uia_api("unused", NATIVE, ready=False)
        out = Api.read_screen_text(api, "what number?")
        assert calls == []
        assert "Vision isn't configured" in out and "Display is 23" in out
        assert "NOTE" not in out

    def test_trace_is_off_by_default(self, capsys):
        api, _, seen = _read_screen_uia_api("851", NATIVE)
        Api.read_screen_text(api, "q")
        assert "[ReadTrace]" not in capsys.readouterr().out
        assert seen == [False]

    def test_trace_logs_both_readers_capped_and_text_only(self, monkeypatch, capsys):
        monkeypatch.setenv("MISTAI_TRACE_READS", "1")
        monkeypatch.setattr(Api, "_read_trace_warned", False)
        api, _, seen = _read_screen_uia_api("851", ("x" * 500, "Calculator", True))
        Api.read_screen_text(api, "what number?")
        Api.read_screen_text(api, "again?")
        out = capsys.readouterr().out
        assert seen == [True, True]  # browsers too, for the trace
        assert out.count("skim these logs before sharing") == 1
        line = [ln for ln in out.splitlines() if ln.startswith("[ReadTrace] query='what number?'")][0]
        assert "vision='851'" in line
        assert "uia='" + "x" * 300 + "'" in line and "x" * 301 not in line

    def test_asked_window_not_in_front_is_read_and_labelled(self, monkeypatch):
        api, _, _ = _read_screen_uia_api("12", ("Display is 851", "Calculator", True, "background", "calculator"))
        out = Api.read_screen_text(api, "what does Calculator show?")
        assert ("EXACT UI TEXT of 'Calculator' (accessibility tree, exact; this window is NOT "
                "in front, so the screenshot shows a different window): Display is 851") in out
        assert "NOTE: the vision answer's number(s) 12" in out

    def test_only_another_window_is_labelled_not_exact(self, monkeypatch):
        api, _, _ = _read_screen_uia_api("NOT VISIBLE ON SCREEN",
                         ("PS C:\> 851", "Windows PowerShell", True, "other", "calculator"))
        out = Api.read_screen_text(api, "what does Calculator show?")
        assert "EXACT" not in out and "NOTE" not in out
        assert ("UI TEXT of the foreground window 'Windows PowerShell', not the calculator "
                "window asked about (no calculator window was found): PS C:\> 851") in out
        assert out.startswith("Vision: NOT VISIBLE ON SCREEN")

    def test_reads_the_asked_app_window_even_when_another_is_in_front(self, monkeypatch):
        api, walked = _window_api(monkeypatch, 1, {1: "code", 2: "calculator"},
                                  ["code", "calculator"], target="calculator")
        text, title, native, kind, asked = Api._exact_ui_text(api, "what number is shown?")
        assert (walked, title, kind, asked) == ([2], "Calculator", "background", "calculator")

    def test_query_naming_an_open_app_picks_its_window(self, monkeypatch):
        api, walked = _window_api(monkeypatch, 1, {1: "code", 3: "notepad"}, ["code", "notepad"])
        assert Api._exact_ui_text(api, "what does Notepad say?")[3:] == ("background", "notepad")
        assert walked == [3]

    def test_target_wins_when_the_query_names_several_apps(self, monkeypatch):
        api, walked = _window_api(monkeypatch, 2, {1: "code", 2: "calculator"},
                                  ["code", "calculator"], target="calculator")
        assert Api._exact_ui_text(api, "what code is shown in calculator?")[3:] == (
            "exact", "calculator")
        assert walked == [2]

    def test_missing_asked_window_reads_the_foreground_labelled_other(self, monkeypatch):
        api, walked = _window_api(monkeypatch, 1, {1: "code"}, ["code"], target="calculator")
        assert Api._exact_ui_text(api, "what number?")[3:] == ("other", "calculator")
        assert walked == [1]

    def test_no_asked_app_reads_the_foreground(self, monkeypatch):
        api, walked = _window_api(monkeypatch, 3, {3: "notepad"}, ["notepad"])
        assert Api._exact_ui_text(api, "what is written here?")[3:] == ("exact", None)
        assert walked == [3]

    @pytest.fixture
    def fake_window(self, monkeypatch):
        """Foreground window 42 of process ``proc['name']``; slow list cleared."""
        import win32gui

        proc = {"name": "SlowApp.exe"}
        monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 42)
        monkeypatch.setattr(win32gui, "GetWindowText", lambda _h: "Some App")
        monkeypatch.setattr(uia_text, "_process_name", lambda _h: proc["name"])
        monkeypatch.setattr(uia_text, "_slow_processes", set())
        return proc

    def test_slow_process_is_skipped_for_the_rest_of_the_session(self, monkeypatch, capsys, fake_window):
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

    def test_walk_that_hits_its_deadline_marks_the_process_slow(self, monkeypatch, capsys, fake_window):
        monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: ("partial", True))
        assert uia_text.foreground_uia_text() == ("", "Some App")
        assert "SlowApp.exe" in uia_text._slow_processes
        assert "skipping SlowApp.exe" in capsys.readouterr().out

    def test_foreground_uia_text_never_raises(self, monkeypatch, fake_window):
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
    def test_secret_looking_snapshot_is_withheld(self, monkeypatch, capsys, fake_window, text):
        monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: (text, False))
        assert uia_text.foreground_uia_text() == ("", "Some App")
        out = capsys.readouterr().out
        assert "[UIA] withheld the text of the window:" in out   # the title only with the flag
        assert text not in out

    def test_ordinary_text_is_not_withheld(self, monkeypatch, fake_window):
        text = "Display is 851 | Expression is 37 × 23= | Password | Token count: 12"
        monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: (text, False))
        assert uia_text.foreground_uia_text() == (text, "Some App")

    def test_walk_collects_text_controls_dedupes_caps_and_skips_passwords(self, monkeypatch):
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

    def test_walk_reports_a_timeout_when_the_deadline_passes(self, monkeypatch):
        _fake_tree(monkeypatch, _Info("Window", "Big", [_Info("Text", "a")]))
        assert uia_text._walk_text(1, time.monotonic() - 1, 300) == ("", True)


# ============================================================
# UIA FIRST
# ============================================================

# UIA first (MISTAI_UIA_FIRST, default on): in a native window whose
# accessibility text is sufficient, read_screen_text and the per-step screen
# context answer from that text alone, with no vision call. Empty or thin text,
# browsers, visual questions and the flag off keep the vision path.


# ============================================================
# UIA FIRST HELPERS
# ============================================================

UIA_FIRST_CALC = ("Calculator | Display is 851 | Expression is 37 × 23=", "Calculator", True)


# ============================================================
# UIA FIRST API
# ============================================================

def _uia_first_api(exact=UIA_FIRST_CALC, kind="exact", vision_reply="851", ready=True):
    api = Api.__new__(Api)
    calls = []

    def call(prompt, _frame):
        calls.append(prompt)
        return vision_reply

    api.vision = SimpleNamespace(ready=ready, _call=call)
    api.screen = SimpleNamespace(frame=object())
    api.last_result_text = ""
    api._exact_ui_text = lambda query="", include_browser=False: tuple(exact) + (kind, None)
    return api, calls


# -- read_screen_text ------------------------------------------------------------


# -- the per-step screen context -------------------------------------------------


# ============================================================
# UIA FIRST LOOP
# ============================================================

def _loop(exact=UIA_FIRST_CALC, kind="exact", with_exact=True):
    understood = []
    api = SimpleNamespace(
        screen=SimpleNamespace(frame_for_read=lambda label: "img"),
        vision=SimpleNamespace(ready=True, understand_screen=lambda f, use_cache=False:
                               understood.append(f) or "A calculator window."),
        live=SimpleNamespace(snapshot={"foreground_window": "Calculator",
                                       "open_apps": ["calculator"]}),
    )
    if with_exact:
        api._exact_ui_text = lambda query="", include_browser=False: tuple(exact) + (kind, None)
    return AgentLoop(api, None), understood


# -- the general rules -------------------------------------------------------------


# ============================================================
# UIA FIRST TESTS
# ============================================================

class TestUiaFirst:
    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.delenv("MISTAI_UIA_FIRST", raising=False)
        monkeypatch.delenv("MISTAI_TRACE_READS", raising=False)

    def test_sufficient_native_text_answers_without_a_vision_call(self, capsys):
        api, calls = _uia_first_api()
        out = Api.read_screen_text(api, "what number is shown?")
        assert calls == []                                       # no vision call
        assert out.startswith("EXACT UI TEXT of 'Calculator' (accessibility tree, exact): ")
        assert "Display is 851" in out and "Vision:" not in out
        assert "No screenshot was read" in out
        # The agent's "get it from another source" note keys on this string.
        assert "NOT VISIBLE ON SCREEN" not in out
        assert api.last_result_text == out
        log = capsys.readouterr().out
        assert "answered from the accessibility text; no vision call" in log
        assert "851" not in log and "Calculator" not in log      # content-free

    def test_empty_uia_text_falls_back_to_vision(self):
        api, calls = _uia_first_api(exact=("", "Calculator", True))
        assert Api.read_screen_text(api, "what number is shown?") == "851"
        assert len(calls) == 1

    def test_browser_focus_still_uses_vision(self):
        api, calls = _uia_first_api(exact=("", "Docs - Mozilla Firefox", False), vision_reply="Hello")
        assert Api.read_screen_text(api, "what does the page say?") == "Hello"
        assert len(calls) == 1

    def test_browser_text_while_tracing_still_uses_vision(self, monkeypatch):
        monkeypatch.setenv("MISTAI_TRACE_READS", "1")
        api, calls = _uia_first_api(exact=("Some page text | More", "Docs - Mozilla Firefox", False),
                          vision_reply="Hello")
        assert Api.read_screen_text(api, "what does the page say?") == "Hello"
        assert len(calls) == 1

    @pytest.mark.parametrize("query", ["", "what colour is the button?", "is there an image?",
                                       "describe the layout", "what does the icon look like?"])
    def test_visual_or_general_questions_use_vision_beside_the_text(self, query):
        api, calls = _uia_first_api()
        out = Api.read_screen_text(api, query)
        assert len(calls) == 1
        assert out.startswith("Vision: 851\nEXACT UI TEXT of 'Calculator'")

    def test_title_only_text_is_too_thin(self):
        api, calls = _uia_first_api(exact=("Calculator", "Calculator", True))
        Api.read_screen_text(api, "what number is shown?")
        assert len(calls) == 1

    def test_text_cut_at_the_cap_uses_vision(self):
        cut = " | ".join(f"item {i}" for i in range(80))[:TEXT_CAP]
        api, calls = _uia_first_api(exact=(cut, "Notepad", True))
        Api.read_screen_text(api, "what is the last line?")
        assert len(calls) == 1

    def test_other_window_keeps_the_labelled_vision_answer(self):
        api, calls = _uia_first_api(kind="other")
        out = Api.read_screen_text(api, "what number is shown?")
        assert len(calls) == 1
        assert "not the None window asked about" in out

    def test_asked_window_behind_is_read_from_its_text_and_labelled(self):
        api, calls = _uia_first_api(kind="background")
        out = Api.read_screen_text(api, "what number is shown?")
        assert calls == []
        assert "this window is NOT in front" in out and "Display is 851" in out

    def test_flag_off_keeps_vision_beside_the_text(self, monkeypatch):
        monkeypatch.setenv("MISTAI_UIA_FIRST", "0")
        api, calls = _uia_first_api(vision_reply="The display shows 851.")
        out = Api.read_screen_text(api, "what number is shown?")
        assert len(calls) == 1
        assert out.startswith("Vision: The display shows 851.\nEXACT UI TEXT")

    def test_screen_context_in_a_native_window_uses_the_text_not_vision(self, capsys):
        loop, understood = _loop()
        ctx = loop._get_screen_context()
        assert understood == []
        assert ctx.startswith("Window: Calculator | Open apps: ")
        assert "EXACT UI TEXT: Calculator | Display is 851" in ctx
        assert "call analyze_screen or read_screen_text" in ctx
        log = capsys.readouterr().out
        assert "accessibility text, no vision" in log and "851" not in log

    @pytest.mark.parametrize("exact, kind", [
        (("", "Calculator", True), "exact"),                       # empty text
        (("Docs | Page", "Docs - Mozilla Firefox", False), "exact"),  # browser
        (UIA_FIRST_CALC, "background"),                                     # the phase's app not in front
        (UIA_FIRST_CALC, "other"),
    ])
    def test_screen_context_falls_back_to_vision(self, exact, kind):
        loop, understood = _loop(exact, kind)
        assert loop._get_screen_context() == "Window: Calculator | Vision: A calculator window."
        assert understood == ["img"]

    def test_screen_context_flag_off_or_no_reader_uses_vision(self, monkeypatch):
        loop, understood = _loop(with_exact=False)
        assert "Vision:" in loop._get_screen_context()
        monkeypatch.setenv("MISTAI_UIA_FIRST", "0")
        loop, understood = _loop()
        assert "Vision:" in loop._get_screen_context()
        assert understood == ["img"]

    def test_screen_context_reader_errors_fall_back_to_vision(self):
        loop, understood = _loop()

        def boom(query="", include_browser=False):
            raise RuntimeError("uia")

        loop.api._exact_ui_text = boom
        assert "Vision:" in loop._get_screen_context()

    def test_sufficiency_rules(self):
        assert text_is_sufficient("Display is 851", "Calculator")
        assert text_is_sufficient("Untitled - Notepad | hello", "Untitled - Notepad")
        assert not text_is_sufficient("", "Calculator")
        assert not text_is_sufficient("  ", "Calculator")
        assert not text_is_sufficient("Calculator", "Calculator")
        assert not text_is_sufficient("x" * (TEXT_CAP - 3), "Notepad")

    def test_visual_question_rules(self):
        for q in ("", "what colour is it", "Is there a red button?", "where on the page is it",
                  "describe the picture", "what font is used"):
            assert is_visual_question(q), q
        for q in ("what number is shown?", "what does the header say?", "is the file saved?"):
            assert not is_visual_question(q), q


# ============================================================
# FRAME FRESHNESS
# ============================================================

# Vision reads never use a frame captured before the last UI action.


# ============================================================
# FRAME FRESHNESS BUFFER
# ============================================================

def _buffer(frame_time, action_time):
    buf = screen_mod.ScreenBuffer.__new__(screen_mod.ScreenBuffer)
    buf._lock = threading.Lock()
    buf._pil, buf._cv, buf._time = "buffered", None, frame_time
    buf.last_action_at = action_time
    return buf


# ============================================================
# FRAME FRESHNESS TESTS
# ============================================================

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


# ============================================================
# FRAME FRESHNESS REC
# ============================================================

class _Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a))


# ============================================================
# FRAME FRESHNESS EXEC API
# ============================================================

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


# ============================================================
# FRAME FRESHNESS TESTS (2)
# ============================================================

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


# ============================================================
# VISION COORDINATES
# ============================================================

# Vision coordinates on screens wider than 1920 px: the frame is scaled to
# 1920 wide before it is sent, so the model is told the size of the image it
# gets and its answer is scaled back by (screen / image) before any click. At
# 1920x1080 the prompt and the coordinates are unchanged.


# ============================================================
# VISION COORDINATES VISION
# ============================================================

def _vision(monkeypatch, screen, answer):
    v = CohereVision.__new__(CohereVision)
    v._client = object()                       # ready, never called
    v.prompts = []

    def call(prompt, frame=None):
        v.prompts.append(prompt)
        return answer

    v._call = call
    monkeypatch.setattr(vision_mod.pyautogui, "size", lambda: screen)
    return v


# ============================================================
# VISION COORDINATES HELPERS
# ============================================================

WIDE = (2560, 1440)


# ============================================================
# VISION COORDINATES TESTS
# ============================================================

def test_sent_size():
    v = CohereVision.__new__(CohereVision)
    assert v._sent_size(Image.new("RGB", WIDE)) == (1920, 1080)
    assert v._sent_size(Image.new("RGB", (3840, 1600))) == (1920, 800)
    assert v._sent_size(Image.new("RGB", (1920, 1080))) == (1920, 1080)
    assert v._sent_size(Image.new("RGB", (1366, 768))) == (1366, 768)
    assert v._sent_size(object()) is None


def test_the_image_sent_is_the_size_the_prompt_states():
    v = CohereVision.__new__(CohereVision)
    png = base64.b64decode(v._b64(Image.new("RGB", WIDE)))
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_wide_screen_answer_is_scaled_back(monkeypatch):
    v = _vision(monkeypatch, WIDE, "960,540")
    assert v.find_element_coords("Send button", Image.new("RGB", WIDE)) == (1280, 720)
    assert "Image size: 1920x1080 pixels" in v.prompts[0]
    assert "2560" not in v.prompts[0]


def test_wide_screen_corners(monkeypatch):
    frame = Image.new("RGB", WIDE)
    for answer, expected in (("0,0", (0, 0)), ("1920,1080", (2560, 1440)),
                             ("1919, 1079", (2559, 1439))):
        v = _vision(monkeypatch, WIDE, answer)
        assert v.find_element_coords("x", frame) == expected


@pytest.mark.parametrize("answer", ["1921,500", "500,1081", "-1,5", "2000,1200", "NOT_FOUND", "abc"])
def test_out_of_range_answers_are_rejected(monkeypatch, answer):
    # In image pixels: 2000,1200 would be on a 2560x1440 screen, but not in the image sent.
    v = _vision(monkeypatch, WIDE, answer)
    assert v.find_element_coords("x", Image.new("RGB", WIDE)) is None


def test_nth_element_is_scaled_back(monkeypatch):
    v = _vision(monkeypatch, WIDE, "TITLE: A video\nCOORDS: 960,540")
    assert v.find_nth_element_coords("video", 2, Image.new("RGB", WIDE)) == ("A video", (1280, 720))
    assert "Image size: 1920x1080 pixels" in v.prompts[0]
    v = _vision(monkeypatch, WIDE, "TITLE: A video\nCOORDS: 1930,540")
    assert v.find_nth_element_coords("video", 2, Image.new("RGB", WIDE)) == ("A video", None)


def test_1920x1080_is_unchanged(monkeypatch):
    frame = Image.new("RGB", (1920, 1080))
    v = _vision(monkeypatch, (1920, 1080), "960,540")
    assert v.find_element_coords("Send button", frame) == (960, 540)
    assert v.prompts[0] == (
        "Find 'Send button' on this screen.\n"
        "Screen resolution: 1920x1080.\n"
        "Reply with ONLY: x,y (pixel coordinates of the center of that element).\n"
        "If not found, reply: NOT_FOUND\n"
        "No explanation. Just coordinates or NOT_FOUND.")
    v = _vision(monkeypatch, (1920, 1080), "1921,5")
    assert v.find_element_coords("x", frame) is None
    v = _vision(monkeypatch, (1920, 1080), "TITLE: t\nCOORDS: 100,200")
    assert v.find_nth_element_coords("video", 1, frame) == ("t", (100, 200))
    assert "Screen resolution: 1920x1080.\n" in v.prompts[0]


def test_a_frame_without_a_size_uses_the_screen(monkeypatch):
    v = _vision(monkeypatch, WIDE, "960,540")
    assert v.find_element_coords("x", object()) == (960, 540)


def test_the_click_handler_clicks_the_scaled_point(monkeypatch):
    frame = Image.new("RGB", WIDE)
    v = _vision(monkeypatch, WIDE, "960,540")
    monkeypatch.setattr(ch.pyautogui, "size", lambda: WIDE)
    h = ch.ImprovedClickHandler(SimpleNamespace(snapshot={}), v,
                                SimpleNamespace(frame_for_read=lambda _l: frame, frame=frame))
    assert h._try_vision_click("Send button") == (1280, 720)


# ============================================================
# VISION Y NUDGE
# ============================================================

# MISTAI_VISION_Y_NUDGE: the pixels _execute_click adds to y for a vision
# click with y < 80. Default 30 = unchanged behaviour; read at call time.


# ============================================================
# VISION Y NUDGE HELPERS
# ============================================================

@pytest.fixture
def vision_y_nudge_handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda *a, **k: None)
    monkeypatch.delenv("MISTAI_VISION_Y_NUDGE", raising=False)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=True),
                                screen_buffer=None)
    markers = []
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: markers.append((x, y)))
    h.clicks, h.markers = clicks, markers
    return h


def _vision_y_nudge_click(h, method, x, y):
    h.last_method = method
    assert h._execute_click((x, y)) is True
    return h.clicks[-1]


# ============================================================
# VISION Y NUDGE TESTS
# ============================================================

def test_default_is_todays_behaviour(vision_y_nudge_handler, capsys):
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 56) == (700, 86)
    assert vision_y_nudge_handler.markers[-1] == (700, 86)                 # marker and click agree
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 80) == (700, 80)  # 80 and below: untouched
    assert _vision_y_nudge_click(vision_y_nudge_handler, "OCR", 700, 56) == (700, 56)     # other methods: never
    out = capsys.readouterr().out
    assert "[Click] Vision y-nudge applied: +30px (y 56 -> 86)" in out
    assert "[Click] Vision y-nudge not applied (y=80, nudge=30px)" in out
    assert out.count("y-nudge") == 2                         # OCR prints nothing


@pytest.mark.parametrize("value, expected_y", [("0", 56), ("12", 68), ("30", 86)])
def test_the_flag_sets_the_pixels(vision_y_nudge_handler, monkeypatch, value, expected_y):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", value)
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 56) == (700, expected_y)


def test_zero_reports_not_applied(vision_y_nudge_handler, monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", "0")
    _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 56)
    assert "[Click] Vision y-nudge not applied (y=56, nudge=0px)" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", "abc", "-5", "1.5", " 30 "])
def test_invalid_values_mean_30(monkeypatch, value):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", value)
    assert vision_y_nudge() == 30


def test_read_at_call_time(vision_y_nudge_handler, monkeypatch):
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 56) == (700, 86)
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", "0")
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 56) == (700, 56)


def test_the_nudge_is_capped_near_the_bottom_as_before(vision_y_nudge_handler, monkeypatch):
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 90))
    assert _vision_y_nudge_click(vision_y_nudge_handler, "Vision", 700, 60) == (700, 70)   # min(y + 30, sh - 20)


# ============================================================
# VISION MODES AND PLANNER RULES
# ============================================================

# Vision gating default, lazy first read, live-state freshness, withheld
# wording, question routing in the planner, and no key characters in logs.


# ============================================================
# VISION MODES AND PLANNER RULES TESTS
# ============================================================

@pytest.mark.parametrize("value, gating", [(None, True), ("", True), ("1", True),
                                           ("0", False), ("off", False), ("junk", True)])
def test_vision_gating_is_on_by_default(monkeypatch, value, gating):
    if value is None:
        monkeypatch.delenv("MISTAI_VISION_GATING", raising=False)
    else:
        monkeypatch.setenv("MISTAI_VISION_GATING", value)
    assert vision_gating_enabled() is gating


@pytest.mark.parametrize("value, mode", [(None, 2), ("0", 0), ("off", 0), ("junk", 2),
                                         ("1", 1), ("on", 1), ("2", 2), (" 2 ", 2)])
def test_lazy_vision_is_mode_2_by_default(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("MISTAI_LAZY_VISION", raising=False)
    else:
        monkeypatch.setenv("MISTAI_LAZY_VISION", value)
    assert lazy_vision_mode() == mode


# -- Agent loop: refresh + lazy first read -------------------------------------------


# ============================================================
# VISION MODES AND PLANNER RULES LOOP API
# ============================================================

def _loop_api(script, snap=None):
    replies = iter(
        [json.dumps({"action": a, "parameter": p}) for a, p in script]
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    refreshes = []
    snap = snap or {"open_apps": ["code"], "foreground_hwnd": 1, "foreground_window": "Editor"}
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snap, refresh=lambda: refreshes.append(1)),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: SimpleNamespace(
                success=True, detail="", verified=True, escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    post = lambda *a, **k: SimpleNamespace(  # noqa: E731
        ok=True, status_code=200, json=lambda: {"response": next(replies)}
    )
    return api, post, refreshes


# ============================================================
# VISION MODES AND PLANNER RULES TESTS (2)
# ============================================================

def test_live_state_is_refreshed_once_at_the_start_of_each_phase(monkeypatch):
    api, post, refreshes = _loop_api([("run_command", "x")])
    monkeypatch.setattr("core.agent.requests.post", post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert refreshes == [1]


def test_lazy_mode_makes_only_the_first_read_lazy(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_LAZY_VISION", "1")
    monkeypatch.setenv("MISTAI_VISION_GATING", "1")
    api, post, _ = _loop_api([("open_app", "notepad"), ("type_text", "hi")])
    monkeypatch.setattr("core.agent.requests.post", post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    calls = []
    monkeypatch.setattr(loop, "_get_screen_context", lambda lazy=False: calls.append(lazy) or "s")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert calls == [True, False, False]  # first read lazy; UI actions re-read with vision
    assert "lazy first read: on" in capsys.readouterr().out


def test_lazy_context_uses_title_apps_and_uia_and_never_calls_vision(monkeypatch):
    vision_calls = []
    api = SimpleNamespace(
        live=SimpleNamespace(snapshot={
            "foreground_window": "Untitled - Notepad",
            "open_apps": ["notepad", "discord"], "background_apps": ["discord"],
        }),
        vision=SimpleNamespace(ready=True, understand_screen=lambda *a, **k: vision_calls.append(1)),
        screen=SimpleNamespace(frame=object()),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: "Controls: File | Edit")
    ctx = loop._get_screen_context(lazy=True)
    assert "Window: Untitled - Notepad" in ctx
    assert "Open apps: notepad, discord (background)" in ctx
    assert "Controls: File | Edit" in ctx
    assert "call analyze_screen" in ctx
    assert vision_calls == []


def test_lazy_mode_2_skips_the_uia_walk(monkeypatch):
    api = SimpleNamespace(live=SimpleNamespace(snapshot={
        "foreground_window": "Untitled - Notepad", "open_apps": ["notepad"]}))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: pytest.fail("UIA walked in mode 2"))
    ctx = loop._get_screen_context(lazy=2)
    assert "Window: Untitled - Notepad" in ctx and "Open apps: notepad" in ctx
    assert "Controls: (not read)" in ctx and "call analyze_screen" in ctx


def test_lazy_mode_1_falls_back_to_the_title_when_uia_is_slow(monkeypatch):
    release = threading.Event()
    api = SimpleNamespace(live=SimpleNamespace(snapshot={
        "foreground_window": "main.py - Visual Studio Code", "open_apps": ["code"]}))
    loop = AgentLoop(api, None)
    loop.LAZY_UIA_BUDGET = 0.05
    monkeypatch.setattr(loop, "_uia_summary", lambda: release.wait(5) and "Controls: late")
    try:
        ctx = loop._get_screen_context(lazy=1)
    finally:
        release.set()
    assert "Window: main.py - Visual Studio Code" in ctx
    assert "window too slow to scan" in ctx and "Controls: late" not in ctx


def test_lazy_off_keeps_the_original_screen_context(monkeypatch):
    api = SimpleNamespace(
        live=SimpleNamespace(snapshot={"foreground_window": "W", "open_apps": ["a"]}),
        vision=SimpleNamespace(ready=False),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: "Controls: OK")
    assert loop._get_screen_context() == "Controls: OK"
    monkeypatch.setattr(loop, "_uia_summary", lambda: "")
    assert loop._get_screen_context() == "Window: W | Apps: a"


# -- Background apps -----------------------------------------------------------------


def test_apps_without_a_visible_window_are_marked_background(monkeypatch):
    from core import live_state

    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    monkeypatch.setattr(live_state, "_visible_window_pids", lambda: {101, 103})
    monkeypatch.setattr(live_state, "_uwp_frame_apps", lambda: [])
    monkeypatch.setattr(
        live_state.psutil, "process_iter",
        lambda _attrs: [
            SimpleNamespace(info={"name": n, "pid": pid})
            for n, pid in [("Code.exe", 101), ("Notepad.exe", 102), ("Notepad.exe", 103),
                           ("Discord.exe", 104)]
        ],
    )
    tracker._update()
    snap = tracker._state
    # Win11 Notepad: one windowless broker + one windowed process = foreground app.
    assert snap["background_apps"] == ["discord"]
    assert describe_open_apps(snap) == "code, notepad, discord (background)"


def test_unknown_window_state_marks_nothing_background(monkeypatch):
    from core import live_state

    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()

    def boom():
        raise OSError("no desktop")

    monkeypatch.setattr(live_state, "_visible_window_pids", boom)
    monkeypatch.setattr(live_state, "_uwp_frame_apps", lambda: [])
    monkeypatch.setattr(live_state.psutil, "process_iter",
                        lambda _a: [SimpleNamespace(info={"name": "Discord.exe", "pid": 1})])
    tracker._update()
    assert tracker._state["background_apps"] == []


def test_prompt_explains_background_apps():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    system = AgentLoop(api, None)._build_system(
        "none", {"open_apps": ["discord"], "background_apps": ["discord"]}, "s", "", []
    )
    assert "Open apps: discord (background)" in system
    assert '"(background)" means running with no window, so use open_app' in system


# -- Withheld wording ----------------------------------------------------------------


# ============================================================
# VISION MODES AND PLANNER RULES BARE API
# ============================================================

def _bare_api():
    from core.live_state import EventBus
    from core.workspace import Workspace

    api = Api.__new__(Api)
    api.workspace = Workspace(EventBus())
    api.last_result_text = ""
    api.last_command_result = None
    api._resolve_path_placeholders = lambda p: str(p)
    return api


# ============================================================
# VISION MODES AND PLANNER RULES TESTS (3)
# ============================================================

def test_withheld_output_tells_the_model_not_to_show_or_offer_it(monkeypatch, tmp_path):
    api = _bare_api()
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "KEY=1", ""),
    )
    Api._act_run_command(api, "Get-Content .env")
    text = api.last_result_text
    assert "OUTPUT WITHHELD" in text
    assert "You cannot see or show this content" in text
    assert "do not offer to reveal it" in text

    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    Api._act_inspect_file(api, str(secret))
    assert "CONTENT WITHHELD" in api.last_result_text
    assert "do not offer to reveal it" in api.last_result_text


# -- Planner -------------------------------------------------------------------------


@pytest.mark.parametrize("goal", [
    "is Edge open?", "Are there any PDFs in my Downloads?", "what time is it in Tokyo",
    "Which app is using the most memory?", "how many files are on my desktop",
    "does my Documents folder have a resume", "do I have Python installed",
    "can you see Spotify running", "where is my latest download", "whats the weather?",
])
def test_question_goals_skip_the_planning_llm(monkeypatch, goal):
    def no_llm(*_a, **_k):
        raise AssertionError("the planner LLM must not be called for a question")

    monkeypatch.setattr(planner, "_llm", no_llm)
    plan, _ = planner.make_plan(SimpleNamespace(), goal, "m")
    assert [plan.nodes[n].desc for n in plan.order] == [goal]


@pytest.mark.parametrize("goal", [
    "is Edge open and if so close all its windows please now",
    "what is the weather, then write it into a new note on my desktop",
    "isolate the failing test in the project and fix the import errors",
])
def test_compound_or_non_question_goals_still_reach_the_planner(monkeypatch, goal):
    prompts = []
    monkeypatch.setattr(planner, "_llm", lambda _api, prompt, _m: prompts.append(prompt) or "")
    planner.make_plan(SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: "")), goal, "m")
    assert len(prompts) == 1


def test_plan_prompt_forbids_output_formats_and_same_tool_splits(monkeypatch):
    prompts = []
    monkeypatch.setattr(planner, "_llm", lambda _api, prompt, _m: prompts.append(prompt) or "")
    goal = "create a folder called notes on my desktop and then a todo.txt file inside it"
    plan, _ = planner.make_plan(
        SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: "")), goal, "m"
    )
    (prompt,) = prompts
    assert "NEVER specify an output format" in prompt
    assert '"return true/false"' in prompt
    assert "QUESTION's deliverable is the spoken answer" in prompt
    assert "creating a folder and a file inside it is ONE phase" in prompt
    assert plan.order == ["main"]  # unparseable reply -> one phase


# -- No key characters in logs --------------------------------------------------------


def test_api_key_is_never_printed(monkeypatch, capsys):
    from core.config import APIKeyManager

    monkeypatch.setenv("MISTAI_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz")
    mgr = APIKeyManager()
    mgr.set_api_key("sk-zyxwvutsrqponmlkjihgfedcba")
    out = capsys.readouterr().out
    assert "API key loaded" in out
    assert "sk-" not in out and "abcdef" not in out and "zyxw" not in out
    api = Api.__new__(Api)
    api.api_manager = mgr
    assert "sk-" not in Api.set_api_key(api, "sk-secretsecret")["message"]


# ============================================================
# CLICKS
# ============================================================

# Clicks must land inside the target app's window when one is known and in front.
#
# Merged by area (pure moves, no test changed) from: test_click_guard.py, test_own_window_mask.py, test_unconfirmed_click.py.


# ============================================================
# CLICKS HELPERS
# ============================================================

CLICKS_CALC = ((180, 300, 600, 560), "Calculator")


@pytest.fixture
def clicks_handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda: None)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=True),
                                screen_buffer=None)
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    h.clicks = clicks
    return h


def _finders(h, monkeypatch, ocr=None, uia=None, vision=None):
    monkeypatch.setattr(h, "_try_ocr_click", lambda q: ocr)
    monkeypatch.setattr(h, "_try_uia_click", lambda q: uia)
    monkeypatch.setattr(h, "_try_vision_click", lambda q: vision)


# ============================================================
# CLICKS TESTS
# ============================================================

def test_match_outside_the_target_window_is_refused_and_the_next_method_tried(
        clicks_handler, monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")   # the query and title print only with the flag
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    # The real run: OCR read the taskbar's "=n" at (544,1042); UIA found the button.
    _finders(clicks_handler, monkeypatch, ocr=(544, 1042), uia=(520, 520))
    assert clicks_handler.click_on_text("=") is True
    assert clicks_handler.clicks == [(520, 520)] and clicks_handler.last_method == "UIA"
    out = capsys.readouterr().out
    assert "REFUSED OCR click on '=' at (544, 1042)" in out
    assert "'Calculator' rect (180, 300, 600, 560) +8px" in out


def test_every_match_outside_means_no_click_and_a_reason(clicks_handler, monkeypatch):
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    # The real run: Vision put '*' at (1512,337), far right of Calculator.
    _finders(clicks_handler, monkeypatch, vision=(1512, 337))
    assert clicks_handler.click_on_text("*") is False
    assert clicks_handler.clicks == []
    assert "The click on '*' at (1512, 337) (found by Vision) was NOT made" in clicks_handler.last_refusal
    assert "outside the 'Calculator' window (180, 300, 600, 560)" in clicks_handler.last_refusal


@pytest.mark.parametrize("point", [(180, 300), (600, 560), (175, 295), (607, 567)])
def test_points_inside_the_rect_plus_margin_are_clicked(clicks_handler, monkeypatch, point):
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    _finders(clicks_handler, monkeypatch, ocr=point)
    assert clicks_handler.click_on_text("7") is True
    assert clicks_handler.clicks == [point]


def test_no_known_target_allows_taskbar_clicks(clicks_handler, monkeypatch):
    clicks_handler.target_rect_fn = lambda: None
    _finders(clicks_handler, monkeypatch, ocr=(900, 1060))
    assert clicks_handler.click_on_text("Start") is True
    assert clicks_handler.clicks == [(900, 1060)]
    clicks_handler.target_rect_fn = None
    assert clicks_handler.click_on_text("Start") is True


def test_unavailable_rect_fails_open(clicks_handler, monkeypatch, capsys):
    def boom():
        raise OSError("GetWindowRect failed")

    clicks_handler.target_rect_fn = boom
    _finders(clicks_handler, monkeypatch, ocr=(1512, 337))
    assert clicks_handler.click_on_text("x") is True
    assert "rect unavailable" in capsys.readouterr().out


def test_click_nth_is_guarded_too(clicks_handler, monkeypatch):
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    monkeypatch.setattr(clicks_handler, "_try_ocr_nth", lambda t, n: (1500, 900))
    monkeypatch.setattr(clicks_handler, "_try_uia_nth", lambda t, n: None)
    monkeypatch.setattr(clicks_handler, "_try_vision_nth", lambda t, n: None)
    assert clicks_handler.click_nth_element("result", 1) is False
    assert clicks_handler.clicks == [] and "NOT made" in clicks_handler.last_refusal


# -- Api wiring ----------------------------------------------------------------------


# ============================================================
# CLICKS API
# ============================================================

def _clicks_api(monkeypatch, target, belongs):
    import win32gui

    api = Api.__new__(Api)
    api._last_opened_app = target
    api._window_belongs_to = lambda _h, _t: belongs
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 7)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _h: CLICKS_CALC[0])
    monkeypatch.setattr(win32gui, "GetWindowText", lambda _h: "Calculator")
    return api


# ============================================================
# CLICKS TESTS (2)
# ============================================================

def test_target_rect_only_for_a_known_target_in_front(monkeypatch, capsys):
    assert Api._click_target_rect(_clicks_api(monkeypatch, None, True)) is None
    assert Api._click_target_rect(_clicks_api(monkeypatch, "calculator", True)) == CLICKS_CALC
    assert Api._click_target_rect(_clicks_api(monkeypatch, "calculator", False)) is None
    assert "calculator is not in front; window guard not applied" in capsys.readouterr().out


def test_refusal_reaches_the_result_text(clicks_handler, monkeypatch):
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    _finders(clicks_handler, monkeypatch, ocr=(544, 1042))
    monkeypatch.setattr(clicks_handler, "_try_vision_click", lambda q: None)
    api = Api.__new__(Api)
    api.click_handler = clicks_handler
    api.last_result_text = ""
    assert Api._exec_sync(api, "click_on_text", "=") is False
    assert api.last_result_text.startswith("The click on '=' at (544, 1042) (found by OCR)")


def test_click_coords_is_guarded(clicks_handler, monkeypatch):
    clicks_handler.target_rect_fn = lambda: CLICKS_CALC
    monkeypatch.setattr("core.api.pyautogui.size", lambda: (1920, 1080))
    moved = []
    monkeypatch.setattr("core.api.pyautogui.moveTo", lambda *a, **k: moved.append(a))
    monkeypatch.setattr("core.api.pyautogui.click", lambda: None)
    monkeypatch.setattr("core.api.time.sleep", lambda _s: None)
    api = Api.__new__(Api)
    api.click_handler = clicks_handler
    api.last_result_text = ""
    assert Api._act_click_coords(api, "1512,337") is False
    assert moved == [] and "NOT made" in api.last_result_text


# ==========================================================================
# moved from tests/test_own_window_mask.py
# The OCR mask finds MistAI's own window by handle (Api._own_window_handle) or
# this process's PID, never by title text.
# ==========================================================================


# ============================================================
# CLICKS HELPERS (2)
# ============================================================

OWN, OTHER = 11, 22


RECTS = {OWN: (100, 100, 500, 400), OTHER: (0, 0, 1900, 1050)}


TITLES = {OWN: "MistAI", OTHER: "api.py - MistAI Desktop - Visual Studio Code"}


# ============================================================
# CLICKS FAKE
# ============================================================

def _fake(monkeypatch, pids=None, iconic=(), visible=(OWN, OTHER), z_order=(OTHER, OWN)):
    pids = pids or {OWN: os.getpid(), OTHER: 999999}
    win32gui = types.ModuleType("win32gui")
    win32gui.EnumWindows = lambda cb, extra: [cb(h, extra) for h in z_order]
    win32gui.GetWindowText = lambda h: TITLES[h]
    win32gui.IsWindowVisible = lambda h: h in visible
    win32gui.IsIconic = lambda h: h in iconic
    win32gui.GetWindow = lambda h, rel: 0
    win32gui.GetWindowRect = lambda h: RECTS[h]
    win32process = types.ModuleType("win32process")
    win32process.GetWindowThreadProcessId = lambda h: (0, pids[h])
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process", win32process)


# ============================================================
# CLICKS HANDLER
# ============================================================

def _handler(own=None):
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=False),
                                screen_buffer=None)
    if own is not None:
        h.own_window_fn = own
    return h


# ============================================================
# CLICKS TESTS (3)
# ============================================================

def test_a_vs_code_window_titled_after_the_project_is_never_masked(monkeypatch, capsys):
    _fake(monkeypatch, z_order=(OTHER,), visible=(OTHER,))
    assert _handler(own=lambda: None)._get_mistai_window_rect() is None
    assert _handler()._get_mistai_window_rect() is None        # PID fallback: not ours
    assert "Visual Studio Code" not in capsys.readouterr().out


def test_the_own_handle_is_masked_whatever_is_in_front(monkeypatch, capsys):
    _fake(monkeypatch)
    assert _handler(own=lambda: OWN)._get_mistai_window_rect() == RECTS[OWN]
    assert "[OCR] MistAI-window mask rect: (100, 100, 500, 400)" in capsys.readouterr().out


def test_without_a_handle_this_process_s_window_is_used_not_the_title(monkeypatch):
    TITLES_BACKUP = dict(TITLES)
    try:
        TITLES[OWN] = "something unrelated"            # MistAI's window, any title
        _fake(monkeypatch)
        assert _handler()._get_mistai_window_rect() == RECTS[OWN]
    finally:
        TITLES.update(TITLES_BACKUP)


@pytest.mark.parametrize("iconic, visible", [((OWN,), (OWN, OTHER)), ((), (OTHER,))])
def test_a_minimized_or_hidden_own_window_is_not_masked(monkeypatch, iconic, visible):
    _fake(monkeypatch, iconic=iconic, visible=visible)
    assert _handler(own=lambda: OWN)._get_mistai_window_rect() is None


def test_a_failing_handle_lookup_means_no_mask(monkeypatch):
    _fake(monkeypatch)

    def boom():
        raise RuntimeError("no window yet")
    assert _handler(own=boom)._get_mistai_window_rect() is None


def test_the_implausible_rectangle_check_is_kept(capsys):
    h = _handler()
    assert h._sane_mask_rect((0, 0, 1900, 1050), (1080, 1920, 3)) is None    # 97% of the frame
    assert h._sane_mask_rect((100, 100, 500, 400), (1080, 1920, 3)) == (100, 100, 500, 400)
    assert "implausible" in capsys.readouterr().out


def test_api_passes_its_own_handle_lookup_in():
    src = inspect.getsource(Api.__init__)
    assert "self.click_handler.own_window_fn = self._own_window_handle" in src


def test_the_mask_code_never_matches_on_title_text():
    src = inspect.getsource(ch.ImprovedClickHandler._get_mistai_window_rect)
    src += inspect.getsource(ch.ImprovedClickHandler._own_process_window)
    assert "GetWindowText" not in src and '"MistAI" in' not in src


# ==========================================================================
# moved from tests/test_unconfirmed_click.py
# AgentLoop: a vision-resolved click no screen read confirmed gets one done-time
# reminder that names the action only.
# ==========================================================================


# ============================================================
# CLICKS HELPERS (3)
# ============================================================

TARGET = "Zanzibar Quillfeather"     # a contact-like click target; must never leak


DONE = {"action": "done", "speech": "Opened the chat."}


CHECK = "was resolved by vision and never confirmed"


def _clicks_click(method, confidence, verified=None, action="click_on_text"):
    r = _result(action, "clicked")
    r.method, r.confidence, r.verified = method, confidence, verified
    return r


# ============================================================
# CLICKS TESTS (4)
# ============================================================

def test_a_vision_click_then_done_gets_exactly_one_reminder(monkeypatch, capsys):
    out, _, calls, obs, spoken = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE, DONE,
    ], results={"click_on_text": _clicks_click("Vision", 0.45)})
    assert out["success"] and spoken == ["Opened the chat."]
    assert calls == [("click_on_text", TARGET)]
    checks = [o for o in obs if "Before finishing" in o]
    assert len(checks) == 1
    assert ("click_on_text was resolved by vision and never confirmed: read the screen to "
            "confirm it landed on the right item, or say you clicked but couldn't confirm"
            ) in checks[0]
    assert "Zanzibar" not in checks[0] and "Quillfeather" not in checks[0]
    log = capsys.readouterr().out
    assert "[DoneCheck] reminder sent (route notes)" in log
    assert "Zanzibar" not in log and "Quillfeather" not in log


@pytest.mark.parametrize("read", ["read_screen_text", "analyze_screen"])
def test_a_read_after_the_vision_click_means_no_reminder(monkeypatch, read):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET},
        {"action": read, "parameter": "is the chat open?"},
        DONE,
    ], results={"click_on_text": _clicks_click("Vision", 0.45),
                read: _result(read, "chat header shown")})
    assert out["success"]
    assert not any("Before finishing" in o for o in obs)
    assert len(obs) == 2          # no extra model call


def test_a_vision_click_after_the_read_still_counts(monkeypatch):
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "q"},
        {"action": "click_nth", "parameter": "2"},
        DONE, DONE,
    ], results={"click_nth": _clicks_click("Vision", 0.45, action="click_nth"),
                "read_screen_text": _result("read_screen_text", "list")})
    assert sum(CHECK in o for o in obs) == 1


@pytest.mark.parametrize("method, confidence, verified", [
    ("OCR", 0.74, None),
    ("UIA", 0.92, None),
    ("Vision", 0.93, True),      # verified: the Executor raises it to 0.93
    ("Vision", 0.45, True),
])
def test_ocr_uia_and_verified_clicks_get_no_reminder(monkeypatch, method, confidence,
                                                     verified):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE,
    ], results={"click_on_text": _clicks_click(method, confidence, verified)})
    assert out["success"] and not any("Before finishing" in o for o in obs)


def test_a_failed_vision_click_is_not_an_unconfirmed_click(monkeypatch):
    r = _clicks_click("Vision", 0.11)
    r.success = False
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE,
    ], results={"click_on_text": r})
    assert not any(CHECK in o for o in obs)


def test_the_real_executor_scores_a_vision_click_below_the_threshold():
    # The agent's rule keys on the Executor's own numbers; pin them together.
    ex = Executor.__new__(Executor)
    spec = type("S", (), {"base_confidence": 0.7})()
    conf = ex._confidence(spec, "click_on_text", "Vision", True, None)
    assert conf < Executor.CONFIDENCE_THRESHOLD
    assert ex._confidence(spec, "click_on_text", "Vision", True, True) >= \
        Executor.CONFIDENCE_THRESHOLD
    assert ExecutionResult("click_on_text", "", True, conf, method="Vision").verified is None


def test_a_vision_click_after_typing_still_counts_as_submitting_it(monkeypatch):
    # The click both submits the typing (no "never submitted" warning) and
    # stays unconfirmed until the read.
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "type_text", "parameter": "hello"},
        {"action": "click_on_text", "parameter": "Send"},
        {"action": "read_screen_text", "parameter": "sent?"},
        DONE,
    ], results={"click_on_text": _clicks_click("Vision", 0.45),
                "read_screen_text": _result("read_screen_text", "sent")})
    assert not any("Before finishing" in o for o in obs)


# ============================================================
# OCR COMPACT MATCH
# ============================================================

# OCR compact matcher: a spaced/misheard name against one OCR word or a run of
# 2-4 adjacent words on one Tesseract line (core/click_handler.py).


# ============================================================
# OCR COMPACT MATCH HELPERS
# ============================================================

FRAME = SimpleNamespace(shape=(1080, 1920, 3))


# ============================================================
# OCR COMPACT MATCH DATA
# ============================================================

def _data(words):
    """image_to_data-style dict. ``words`` = (text, left, top, width, height,
    line, word_num[, conf])."""
    d = {k: [] for k in ("text", "conf", "left", "top", "width", "height",
                         "block_num", "par_num", "line_num", "word_num")}
    for w in words:
        text, left, top, width, height, line, num = w[:7]
        d["text"].append(text)
        d["conf"].append(w[7] if len(w) > 7 else 90)
        d["left"].append(left)
        d["top"].append(top)
        d["width"].append(width)
        d["height"].append(height)
        d["block_num"].append(1)
        d["par_num"].append(1)
        d["line_num"].append(line)
        d["word_num"].append(num)
    return d


# ============================================================
# OCR COMPACT MATCH HELPERS (2)
# ============================================================

@pytest.fixture
def ocr_compact_match_handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda *a, **k: None)
    monkeypatch.setattr(ch, "OCR_AVAILABLE", True)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=False),
                                screen_buffer=None)
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    monkeypatch.setattr(h, "_get_frame_for_ocr", lambda: FRAME)
    monkeypatch.setattr(h, "_ocr_button_regions", lambda f, q: (None, "", 0, 0))
    monkeypatch.setattr(h, "_prepare_frames", lambda f: [(FRAME, 1.0)])
    monkeypatch.setattr(h, "_try_uia_click", lambda q: None)
    h.clicks = clicks
    return h


def _ocr_sees(monkeypatch, words):
    data = _data(words)
    monkeypatch.setattr(ch, "pytesseract", SimpleNamespace(
        image_to_data=lambda *a, **k: data, Output=SimpleNamespace(DICT="dict")),
        raising=False)


SPAN = [("Inbox", 100, 200, 60, 20, 1, 1),
        ("So", 300, 400, 30, 20, 2, 1), ("Can", 336, 400, 40, 20, 2, 2),
        ("Xan", 382, 400, 40, 20, 2, 3), ("online", 430, 400, 60, 20, 2, 4)]


# ============================================================
# OCR COMPACT MATCH TESTS
# ============================================================

def test_a_spaced_query_matches_a_multi_word_span_and_clicks_its_centre(ocr_compact_match_handler, monkeypatch):
    _ocr_sees(monkeypatch, SPAN)
    assert ocr_compact_match_handler.click_on_text("so can zan") is True
    # So(300) .. Xan(382+40=422): centre x 361; y 400..420: centre 410.
    assert ocr_compact_match_handler.clicks == [(361, 410)] and ocr_compact_match_handler.last_method == "OCR"
    assert ocr_compact_match_handler.last_found_text == "So Can Xan"


def test_a_spaced_query_matches_one_joined_word(ocr_compact_match_handler, monkeypatch):
    _ocr_sees(monkeypatch, [("SoCanXan", 500, 600, 100, 20, 1, 1)])
    assert ocr_compact_match_handler.click_on_text("so can zan") is True
    assert ocr_compact_match_handler.clicks == [(550, 610)]


@pytest.mark.parametrize("words", [
    [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 2, 1),
     ("Xan", 382, 400, 40, 20, 2, 2)],                       # "So" is on another line
    [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 1, 2),
     ("Xan", 382, 400, 40, 20, 1, 4)],                       # word 3 (low conf) between
])
def test_words_on_different_lines_or_not_adjacent_never_form_a_span(ocr_compact_match_handler, monkeypatch,
                                                                    words):
    _ocr_sees(monkeypatch, words)
    assert ocr_compact_match_handler.click_on_text("so can zan") is False
    assert ocr_compact_match_handler.clicks == []


def test_sam_and_pam_scores():
    # The single-word path's 65 accepts this pair (66); the compact path never
    # sees a 3-letter query, and its 80 would refuse a 66 anyway.
    assert int(difflib.SequenceMatcher(None, "pam", "sam").ratio() * 100) == 66
    assert ch._compact_score("pam", "sam") == 0
    assert ch._match_score("pam", "sam") == 66          # unchanged single-word path
    assert ch._compact_score("quillfeathr", "quillfeather") >= ch.COMPACT_MATCH_SCORE
    assert ch._compact_score("socanxan", "socanzan") == 87


def test_short_queries_do_not_use_the_compact_path(ocr_compact_match_handler, monkeypatch):
    # "ab cd" compacts to 4 chars: below COMPACT_MIN_LEN even though "Ab"+"Cd"
    # joins to an exact match.
    _ocr_sees(monkeypatch, [("Ab", 300, 400, 30, 20, 1, 1), ("Cd", 336, 400, 30, 20, 1, 2)])
    assert ocr_compact_match_handler._compact_match(ch.pytesseract.image_to_data(), "ab cd") is None
    assert ocr_compact_match_handler.click_on_text("ab cd") is False


@pytest.mark.parametrize("query, cand, length_ok", [
    ("abcdefgh", "abcdefghij", False),        # 8 chars: max(1, 1) -> 2 longer is out
    ("abcdefghijklmn", "abcdefghijklxyzw", True),   # 14 chars: 15% -> 2 allowed
])
def test_length_guard(query, cand, length_ok):
    assert (ch._compact_score(cand, query) > 0) is length_ok


def test_the_suffix_rules_hold_on_the_compact_path():
    assert ch._compact_score("subscribers", "subscribe") == 0
    assert ch._compact_score("subscriber", "subscribe") == 0      # 1 char longer, derived
    assert ch._compact_score("subscription", "subscribe") == 0
    assert ch._compact_score("subscribed", "subscribe") == 90     # toggle state
    # The single-word scorer is unchanged.
    assert ch._match_score("subscribers", "subscribe") == 0
    assert ch._match_score("subscribed", "subscribe") == 90


def test_a_compact_match_never_overrides_a_single_word_match(ocr_compact_match_handler, monkeypatch):
    # "Subscribed" wins at 90 on the single-word path; the compact span
    # "Sub scribe" (100) elsewhere is never consulted.
    _ocr_sees(monkeypatch, [("Subscribed", 700, 100, 80, 20, 1, 1),
                            ("Sub", 300, 500, 30, 20, 2, 1), ("scribe", 334, 500, 50, 20, 2, 2)])
    match, word, score, _ = ocr_compact_match_handler._ocr_pass(FRAME, "subscribe", "cfg")
    assert (match, word, score) == ((740, 110), "Subscribed", 90)


def test_the_target_window_guard_applies_to_the_span_centre(ocr_compact_match_handler, monkeypatch):
    _ocr_sees(monkeypatch, SPAN)
    ocr_compact_match_handler.target_rect_fn = lambda: ((0, 0, 340, 1080), "Chat")   # centre x 361 is outside
    assert ocr_compact_match_handler.click_on_text("so can zan") is False
    assert ocr_compact_match_handler.clicks == []
    assert "at (361, 410) (found by OCR) was NOT made" in ocr_compact_match_handler.last_refusal
    ocr_compact_match_handler.target_rect_fn = lambda: ((0, 0, 400, 1080), "Chat")
    assert ocr_compact_match_handler.click_on_text("so can zan") is True
    assert ocr_compact_match_handler.clicks == [(361, 410)]


def test_default_logs_carry_counts_scores_and_coordinates_only(ocr_compact_match_handler, monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _ocr_sees(monkeypatch, SPAN)
    ocr_compact_match_handler.click_on_text("so can zan")
    log = capsys.readouterr().out
    assert "[OCR] Compact match: 3 word(s), score=87 @ (361, 410)" in log
    for word in ("so can zan", "Xan", "Can", "online", "Inbox"):
        assert word not in log


def test_the_words_print_only_under_the_flag(ocr_compact_match_handler, monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    _ocr_sees(monkeypatch, SPAN)
    ocr_compact_match_handler.click_on_text("so can zan")
    assert "as 'So Can Xan' @ (361, 410)" in capsys.readouterr().out


def test_a_compact_score_between_65_and_80_is_not_a_match(ocr_compact_match_handler, monkeypatch):
    # Two letters off an 8-letter name scores 75: enough for the single-word
    # path's 65, not for the compact path's 80.
    assert ch._compact_score("socanxam", "socanzan") == 75
    _ocr_sees(monkeypatch, [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 1, 2),
                            ("Xam", 382, 400, 40, 20, 1, 3)])
    assert ocr_compact_match_handler.click_on_text("so can zan") is False
