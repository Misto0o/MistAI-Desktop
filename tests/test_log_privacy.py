"""The agent's logs never carry content (stage 1).

The step log prints a parameter only for app names, scroll/brightness values
and named keys or modifier combinations; anything else is a length. ask_user
logs "asked" / "answered (N words)". A declined confirmation stores only the
action name. Message text, contact names, typed text and heard answers must
never appear in captured output or in the actions table.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop, step_param_for_log
from core.tools import SPECS

# Invented content, each word distinctive enough to search for.
CONTACT = "Joanne Quillfeather"
MESSAGE = "meet me at the lighthouse pier at six"
TYPED = "my confidential diary entry"
QUESTION = "Which Quillfeather did you mean, the cousin or the colleague?"
HEARD = "the colleague from marblewood"
CONTENT_WORDS = ("joanne", "quillfeather", "lighthouse", "pier", "confidential",
                 "diary", "cousin", "colleague", "marblewood")


def _result(action):
    return SimpleNamespace(action=action, parameter="", success=True, detail="",
                           verified=None, escalate_to_user=False,
                           reliability_note=lambda: "", escalated_from="")


def _run(monkeypatch, replies, answers=(), mode="voice", confirm=("quick_message",)):
    """One phase through the real AgentLoop, scripted model replies, stubbed
    executor and listen. Returns (result, add_action rows)."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    rows = []
    listen = lambda timeout=10: answers.pop(0) if answers else None   # noqa: E731
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a in confirm),
        executor=SimpleNamespace(run=lambda a, p: _result(a)),
        memory=SimpleNamespace(add_action=lambda *a: rows.append(a)),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _listen_once=listen,
        _get_text_answer=lambda _q, timeout=60: listen(),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        _last_opened_app="",
    )

    def fake_post(*_a, json=None, **_k):
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    return loop._loop("send it", "m", "none", mode, None), rows


def _no_content(text):
    low = text.lower()
    leaked = [w for w in CONTENT_WORDS if w in low]
    assert leaked == [], f"content in the log: {leaked}"


DONE = {"action": "done", "speech": ""}


# -- the step log ----------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["voice", "text"])
def test_a_confirmed_message_and_typed_text_never_reach_the_log(monkeypatch, capsys, mode):
    msg = f"Discord|{CONTACT}|{MESSAGE}"
    out, rows = _run(monkeypatch, [
        {"action": "quick_message", "parameter": msg},
        {"action": "type_text", "parameter": TYPED},
        DONE,
    ], answers=["yes"], mode=mode)
    log = capsys.readouterr().out
    _no_content(log)
    assert f"Step 1: quick_message(<{len(msg)} chars>)" in log
    assert f"Step 2: type_text(<{len(TYPED)} chars>)" in log
    _no_content(repr(rows))


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_ask_user_logs_asked_and_a_word_count_only(monkeypatch, capsys, mode):
    _run(monkeypatch, [
        {"action": "ask_user", "parameter": QUESTION},
        DONE,
    ], answers=[HEARD], mode=mode)
    log = capsys.readouterr().out
    _no_content(log)
    assert "[AskUser] asked" in log
    assert f"[AskUser] answered ({len(HEARD.split())} words)" in log


def test_a_declined_message_stores_only_the_action_name(monkeypatch, capsys):
    msg = f"Discord|{CONTACT}|{MESSAGE}"
    _out, rows = _run(monkeypatch, [
        {"action": "quick_message", "parameter": msg},
        DONE,
    ], answers=["no"])
    assert ("confirm_declined", "quick_message", False) in rows
    _no_content(repr(rows))
    _no_content(capsys.readouterr().out)


def test_the_done_speech_still_logs_as_a_word_count(monkeypatch, capsys):
    _run(monkeypatch, [{"action": "done", "speech": f"Sent it to {CONTACT}."}], confirm=())
    log = capsys.readouterr().out
    assert "Step 1: done('') speech: 5 words" in log
    _no_content(log)


# -- which parameters may print ------------------------------------------------------

@pytest.mark.parametrize("action, param, shown", [
    ("open_app", "notepad", "'notepad'"),
    ("switch_app", "discord", "'discord'"),
    ("scroll", "down 3", "'down 3'"),
    ("brightness_set", "60", "'60'"),
    ("press_key", "enter", "'enter'"),
    ("press_key", "Escape", "'Escape'"),
    ("press_key", "F5", "'F5'"),
    ("browser_press_key", "ArrowDown", "'ArrowDown'"),
    ("hotkey", "ctrl+a", "'ctrl+a'"),
    ("hotkey", "ctrl+shift+t", "'ctrl+shift+t'"),
    ("hotkey", "win+up", "'win+up'"),
    ("press_key", "a", "<1 chars>"),                 # a letter: could be typing
    ("press_key", "7", "<1 chars>"),
    ("hotkey", "a+b", "<3 chars>"),                  # no modifier
    ("hotkey", "ctrl+hello", "<10 chars>"),          # not a key
    ("type_text", TYPED, f"<{len(TYPED)} chars>"),
    ("click_on_text", CONTACT, f"<{len(CONTACT)} chars>"),
    ("window_activate", f"{CONTACT} - Discord", f"<{len(CONTACT) + 10} chars>"),
    ("browser_navigate", "https://example.com/?q=pier", "<27 chars>"),
    ("read_screen_text", "what does it say", "<16 chars>"),
    ("done", "", "''"),
    ("type_text", None, "''"),
])
def test_the_step_log_parameter_view(action, param, shown):
    assert step_param_for_log(action, param) == shown


def test_a_logged_parameter_is_capped():
    assert step_param_for_log("open_app", "x" * 100) == repr("x" * 60)
