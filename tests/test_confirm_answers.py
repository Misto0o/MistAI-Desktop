"""Confirmation answers are "yes", "no" or "unclear" (config.confirmation_answer).

- yes runs the action; an explicit no keeps today's decline path and wording.
- unclear (empty, unrecognized, or neither a yes nor a no word): in voice mode
  ask once more ("Sorry, yes or no?"); still unclear -> the action is not
  run, the user hears "I didn't hear a yes, so I didn't <verb> that.", the
  model is told the answer was not understood, and no decline signature is
  recorded. Typed mode: unclear the same way, without the retry.
- logs carry "[Confirm] answer=yes|no|unclear" and "(retry)", never the words.
- planner._confirm uses the same helper.
"""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import (CONFIRM_RETRY_QUESTION, UNCLEAR_OBSERVATION, AgentLoop,
                        unclear_answer_speech)
from core.config import confirmation_answer
from core.planner import PlanRunner
from core.tools import SPECS

CONTACT = "Joanne Quillfeather"
MESSAGE = "meet me at the lighthouse pier"
SEND = {"action": "quick_message", "parameter": f"Discord|{CONTACT}|{MESSAGE}"}
DONE = {"action": "done", "speech": "ok"}
MUMBLE = "marblewood cobblestone"        # an answer with neither a yes nor a no
CONTENT_WORDS = ("joanne", "quillfeather", "lighthouse", "pier", "marblewood",
                 "cobblestone", "maybe")


def _no_content(text):
    low = text.lower()
    assert [w for w in CONTENT_WORDS if w in low] == []


@pytest.mark.parametrize("answer, verdict", [
    ("yes", "yes"), ("Yes please.", "yes"), ("go ahead", "yes"), ("do it", "yes"),
    ("sure", "yes"), ("no", "no"), ("nope", "no"), ("not sure", "no"),
    ("No, sure", "no"), ("cancel", "no"), ("never mind", "no"),
    ("", "unclear"), (None, "unclear"), ("   ", "unclear"), ("maybe", "unclear"),
    (MUMBLE, "unclear"), ("yesterday", "unclear"), ("continue", "unclear"),
])
def test_the_helper(answer, verdict):
    assert confirmation_answer(answer) == verdict


def test_extra_yes_words_are_whole_words():
    assert confirmation_answer("continue", extra_yes=("continue",)) == "yes"
    assert confirmation_answer("discontinued", extra_yes=("continue",)) == "unclear"


@pytest.mark.parametrize("answer", ["", None, "maybe", MUMBLE, "no", "not sure"])
def test_is_confirmation_stays_the_strict_yes_test(answer):
    assert AgentLoop._is_confirmation(answer) is False
    assert AgentLoop._is_confirmation("yes") is True


# -- the agent loop ---------------------------------------------------------------------

def _run(monkeypatch, replies, answers, mode="voice", confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop. Returns a namespace with the
    executed calls, spoken lines, asked questions, stored rows, the requests
    sent to the model and the result."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    rec = SimpleNamespace(calls=[], spoken=[], asked=[], rows=[], requests=[])

    def answer(question):
        rec.asked.append(question)
        return answers.pop(0) if answers else None

    def run(a, p):
        rec.calls.append((a, p))
        return SimpleNamespace(action=a, parameter="", success=True, detail="", verified=None,
                               escalate_to_user=False, reliability_note=lambda: "",
                               escalated_from="")

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
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: rec.rows.append(a)),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda t, **_k: rec.spoken.append(t),
        tts=SimpleNamespace(speaking=False),
        _listen_once=lambda timeout=10: answer(rec.spoken[-1] if rec.spoken else ""),
        _get_text_answer=lambda q, timeout=60: answer(q),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        stop_current_task=lambda: api._stop_requested.set(),
        _last_opened_app="",
    )

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", mode, None)
    return rec


def _observations(rec):
    return [m["content"] for r in rec.requests for m in r.get("messages", [])
            if m.get("role") == "user" and "[OBSERVATION]" in str(m.get("content"))]


def test_unclear_then_yes_runs(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "yes"])
    assert rec.calls == [("quick_message", SEND["parameter"])]
    assert rec.asked[1] == CONFIRM_RETRY_QUESTION and len(rec.asked) == 2
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear\n" in log and "[Confirm] answer=yes (retry)" in log
    _no_content(log)


def test_unclear_twice_runs_nothing_and_says_so(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], [MUMBLE, ""])
    assert rec.calls == []
    assert "I didn't hear a yes, so I didn't send that." in rec.spoken
    assert any(UNCLEAR_OBSERVATION in o for o in _observations(rec))
    assert rec.rows == []                                 # no confirm_declined row
    assert rec.result["success"] is True                  # the model's done ends it
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear\n" in log and "[Confirm] answer=unclear (retry)" in log
    _no_content(log)
    _no_content(repr(rec.rows))


def test_unclear_records_no_decline_so_the_model_may_ask_again(monkeypatch):
    rec = _run(monkeypatch, [SEND, SEND, DONE], [MUMBLE, MUMBLE, "yes"])
    assert rec.calls == [("quick_message", SEND["parameter"])]
    assert not any("REFUSED" in o for o in _observations(rec))


def test_other_actions_name_a_generic_verb(monkeypatch):
    rec = _run(monkeypatch, [{"action": "window_close", "parameter": "Notepad"}, DONE],
               ["maybe", "maybe"])
    assert rec.calls == []
    assert "I didn't hear a yes, so I didn't do that." in rec.spoken
    assert unclear_answer_speech("quick_message") == "I didn't hear a yes, so I didn't send that."


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_no_is_unchanged(monkeypatch, capsys, mode):
    rec = _run(monkeypatch, [SEND, {**SEND, "parameter": "Discord|Ada|hi"}], ["no", "nope"],
               mode=mode)
    assert rec.calls == []
    assert len(rec.asked) == 2                            # no retry after a no
    assert any("User explicitly declined this action" in o for o in _observations(rec))
    assert rec.rows == [("confirm_declined", False)]
    assert rec.result == {"success": False, "error": "User declined the requested action.",
                          "speech": "Okay, I won't do that."}
    assert "I didn't hear a yes" not in " ".join(rec.spoken)
    assert capsys.readouterr().out.count("[Confirm] answer=no") == 2


def test_typed_maybe_is_unclear_without_a_retry(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe"], mode="text")
    assert rec.calls == []
    assert len(rec.asked) == 1
    assert "I didn't hear a yes, so I didn't send that." in rec.spoken
    assert any(UNCLEAR_OBSERVATION in o for o in _observations(rec))
    assert rec.rows == []
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear" in log and "(retry)" not in log
    _no_content(log)


def test_typed_yes_runs(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["Yes, go ahead"], mode="text")
    assert rec.calls == [("quick_message", SEND["parameter"])]


def test_a_voice_stop_on_the_retry_is_still_a_stop(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "stop"])
    assert rec.calls == []
    assert rec.result == {"success": False, "error": "Stopped by user"}


# -- the quick_message question ---------------------------------------------------------

def _question(action, param):
    return AgentLoop(SimpleNamespace(_resolve_path_placeholders=lambda p: p),
                     None)._confirmation_question(action, param)


def test_the_quick_message_question_names_message_contact_and_app():
    assert _question("quick_message", f"Discord|{CONTACT}|{MESSAGE}") == (
        f"Send '{MESSAGE}' to {CONTACT} on Discord?")


def test_a_long_message_is_capped_with_an_ellipsis():
    long = "word " * 30
    q = _question("quick_message", f"Discord|Ada|{long}")
    quoted = q[len("Send '"):q.index("' to Ada")]
    assert len(quoted) <= 60 and quoted.endswith("…")
    assert long.startswith(quoted[:-1])
    exact = "x" * 60
    assert _question("quick_message", f"Discord|Ada|{exact}") == f"Send '{exact}' to Ada on Discord?"


@pytest.mark.parametrize("action, param, expected", [
    ("quick_message", "Discord|Ada", "This will run: quick_message(Discord|Ada) -- "
                                     "this can't easily be undone. Proceed?"),
    ("window_close", "Notepad", "This will run: window_close(Notepad) -- "
                                "this can't easily be undone. Proceed?"),
])
def test_other_questions_are_unchanged(action, param, expected):
    assert _question(action, param) == expected


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_the_question_is_asked_but_never_logged(monkeypatch, capsys, mode):
    rec = _run(monkeypatch, [SEND, DONE], ["yes"], mode=mode)
    assert rec.asked == [f"Send '{MESSAGE}' to {CONTACT} on Discord?"]
    _no_content(capsys.readouterr().out)


# -- planner._confirm --------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["voice", "text"])
@pytest.mark.parametrize("answer, expected, verdict", [
    ("continue", True, "yes"), ("yes please", True, "yes"), ("no", False, "no"),
    (f"maybe {MUMBLE}", False, "unclear"), (None, False, "unclear")])
def test_planner_confirm_uses_the_helper(capsys, mode, answer, expected, verdict):
    api = SimpleNamespace(speak=lambda _t: None, tts=SimpleNamespace(speaking=False),
                          _listen_once=lambda timeout=10: answer,
                          _get_text_answer=lambda _q, timeout=60: answer,
                          _stop_requested=threading.Event())
    runner = PlanRunner.__new__(PlanRunner)
    runner.api = api
    assert runner._confirm("Continue with the independent phases?", mode) is expected
    log = capsys.readouterr().out
    assert f"[Confirm] answer={verdict}" in log
    _no_content(log)
