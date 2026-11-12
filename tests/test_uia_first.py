"""UIA first (MISTAI_UIA_FIRST, default on): in a native window whose
accessibility text is sufficient, read_screen_text and the per-step screen
context answer from that text alone, with no vision call. Empty or thin text,
browsers, visual questions and the flag off keep the vision path."""

from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.api import Api
from core.uia_text import TEXT_CAP, is_visual_question, text_is_sufficient

CALC = ("Calculator | Display is 851 | Expression is 37 × 23=", "Calculator", True)


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.delenv("MISTAI_UIA_FIRST", raising=False)
    monkeypatch.delenv("MISTAI_TRACE_READS", raising=False)


def _api(exact=CALC, kind="exact", vision_reply="851", ready=True):
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

def test_sufficient_native_text_answers_without_a_vision_call(capsys):
    api, calls = _api()
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


def test_empty_uia_text_falls_back_to_vision():
    api, calls = _api(exact=("", "Calculator", True))
    assert Api.read_screen_text(api, "what number is shown?") == "851"
    assert len(calls) == 1


def test_browser_focus_still_uses_vision():
    api, calls = _api(exact=("", "Docs - Mozilla Firefox", False), vision_reply="Hello")
    assert Api.read_screen_text(api, "what does the page say?") == "Hello"
    assert len(calls) == 1


def test_browser_text_while_tracing_still_uses_vision(monkeypatch):
    monkeypatch.setenv("MISTAI_TRACE_READS", "1")
    api, calls = _api(exact=("Some page text | More", "Docs - Mozilla Firefox", False),
                      vision_reply="Hello")
    assert Api.read_screen_text(api, "what does the page say?") == "Hello"
    assert len(calls) == 1


@pytest.mark.parametrize("query", ["", "what colour is the button?", "is there an image?",
                                   "describe the layout", "what does the icon look like?"])
def test_visual_or_general_questions_use_vision_beside_the_text(query):
    api, calls = _api()
    out = Api.read_screen_text(api, query)
    assert len(calls) == 1
    assert out.startswith("Vision: 851\nEXACT UI TEXT of 'Calculator'")


def test_title_only_text_is_too_thin():
    api, calls = _api(exact=("Calculator", "Calculator", True))
    Api.read_screen_text(api, "what number is shown?")
    assert len(calls) == 1


def test_text_cut_at_the_cap_uses_vision():
    cut = " | ".join(f"item {i}" for i in range(80))[:TEXT_CAP]
    api, calls = _api(exact=(cut, "Notepad", True))
    Api.read_screen_text(api, "what is the last line?")
    assert len(calls) == 1


def test_other_window_keeps_the_labelled_vision_answer():
    api, calls = _api(kind="other")
    out = Api.read_screen_text(api, "what number is shown?")
    assert len(calls) == 1
    assert "not the None window asked about" in out


def test_asked_window_behind_is_read_from_its_text_and_labelled():
    api, calls = _api(kind="background")
    out = Api.read_screen_text(api, "what number is shown?")
    assert calls == []
    assert "this window is NOT in front" in out and "Display is 851" in out


def test_flag_off_keeps_vision_beside_the_text(monkeypatch):
    monkeypatch.setenv("MISTAI_UIA_FIRST", "0")
    api, calls = _api(vision_reply="The display shows 851.")
    out = Api.read_screen_text(api, "what number is shown?")
    assert len(calls) == 1
    assert out.startswith("Vision: The display shows 851.\nEXACT UI TEXT")


# -- the per-step screen context -------------------------------------------------

def _loop(exact=CALC, kind="exact", with_exact=True):
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


def test_screen_context_in_a_native_window_uses_the_text_not_vision(capsys):
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
    (CALC, "background"),                                     # the phase's app not in front
    (CALC, "other"),
])
def test_screen_context_falls_back_to_vision(exact, kind):
    loop, understood = _loop(exact, kind)
    assert loop._get_screen_context() == "Window: Calculator | Vision: A calculator window."
    assert understood == ["img"]


def test_screen_context_flag_off_or_no_reader_uses_vision(monkeypatch):
    loop, understood = _loop(with_exact=False)
    assert "Vision:" in loop._get_screen_context()
    monkeypatch.setenv("MISTAI_UIA_FIRST", "0")
    loop, understood = _loop()
    assert "Vision:" in loop._get_screen_context()
    assert understood == ["img"]


def test_screen_context_reader_errors_fall_back_to_vision():
    loop, understood = _loop()

    def boom(query="", include_browser=False):
        raise RuntimeError("uia")

    loop.api._exact_ui_text = boom
    assert "Vision:" in loop._get_screen_context()


# -- the general rules -------------------------------------------------------------

def test_sufficiency_rules():
    assert text_is_sufficient("Display is 851", "Calculator")
    assert text_is_sufficient("Untitled - Notepad | hello", "Untitled - Notepad")
    assert not text_is_sufficient("", "Calculator")
    assert not text_is_sufficient("  ", "Calculator")
    assert not text_is_sufficient("Calculator", "Calculator")
    assert not text_is_sufficient("x" * (TEXT_CAP - 3), "Notepad")


def test_visual_question_rules():
    for q in ("", "what colour is it", "Is there a red button?", "where on the page is it",
              "describe the picture", "what font is used"):
        assert is_visual_question(q), q
    for q in ("what number is shown?", "what does the header say?", "is the file saved?"):
        assert not is_visual_question(q), q
