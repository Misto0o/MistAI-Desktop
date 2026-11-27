# ============================================================
# CONFIRMATION TESTS
# ============================================================
#
# Confirmations (typed, spoken, buttons), ask before sending, remembered approvals and quick_message checks.
#
# SECTION INDEX
#   1. CONFIRMATION ANSWERS
#   2. CONFIRMATION ANSWERS HELPERS
#   3. CONFIRMATION ANSWERS TESTS
#   4. CONFIRMATION ANSWERS RUN
#   5. CONFIRMATION ANSWERS HELPERS (2)
#   6. CONFIRMATION ANSWERS TESTS (2)
#   7. CONFIRMATION ANSWERS HELPERS (3)
#   8. CONFIRMATION ANSWERS TESTS (3)
#   9. CONFIRMATION ANSWERS HELPERS (4)
#   10. CONFIRMATION ANSWERS TESTS (4)
#   11. CONFIRMATION ANSWERS MIC
#   12. CONFIRMATION ANSWERS REC
#   13. CONFIRMATION ANSWERS HELPERS (5)
#   14. CONFIRMATION ANSWERS TESTS (5)
#   15. CONFIRMATION ANSWERS CONFIRM API
#   16. CONFIRMATION ANSWERS TESTS (6)
#   17. CONFIRMATION ANSWERS HELPERS (6)
#   18. CONFIRMATION ANSWERS TESTS (7)
#   19. CONFIRMATION ANSWERS HELPERS (7)
#   20. CONFIRMATION ANSWERS API
#   21. CONFIRMATION ANSWERS TESTS (8)
#   22. CONFIRMATION ANSWERS LOOP
#   23. CONFIRMATION ANSWERS HELPERS (8)
#   24. CONFIRMATION ANSWERS TESTS (9)
#   25. CONFIRMATION BUTTONS
#   26. CONFIRMATION BUTTONS API
#   27. CLICK DURING LISTEN
#   28. CONFIRMATION BUTTONS TESTS
#   29. CONFIRMATION BUTTONS LOOP
#   30. CONFIRMATION BUTTONS HELPERS
#   31. CONFIRMATION BUTTONS TESTS (2)
#   32. CONFIRMATION BUTTONS STREAM
#   33. CONFIRMATION BUTTONS MIC
#   34. CONFIRMATION BUTTONS LISTEN API
#   35. CONFIRMATION BUTTONS TESTS (3)
#   36. CONFIRMATION BUTTONS PAGE HARNESS
#   37. CONFIRMATION BUTTONS HELPERS (2)
#   38. RUN CONFIRM PAGE
#   39. CONFIRMATION BUTTONS HELPERS (3)
#   40. CONFIRMATION BUTTONS TESTS (4)
#   41. ASK BEFORE SENDING
#   42. ASK BEFORE SENDING HELPERS
#   43. ASK BEFORE SENDING MEMORY
#   44. ASK BEFORE SENDING RUN
#   45. ASK BEFORE SENDING HELPERS (2)
#   46. ASK BEFORE SENDING TESTS
#   47. ASK BEFORE SENDING HELPERS (3)
#   48. ASK BEFORE SENDING TESTS (2)
#   49. ASK BEFORE SENDING HELPERS (4)
#   50. ASK BEFORE SENDING RUN PAGE
#   51. ASK BEFORE SENDING TESTS (3)
#   52. REMEMBERED APPROVALS
#   53. REMEMBERED APPROVALS HELPERS
#   54. REMEMBERED APPROVALS API
#   55. REMEMBERED APPROVALS RUN
#   56. REMEMBERED APPROVALS HELPERS (2)
#   57. REMEMBERED APPROVALS PROFILE ROWS
#   58. REMEMBERED APPROVALS TESTS
#   59. REMEMBERED APPROVALS HELPERS (3)
#   60. REMEMBERED APPROVALS TESTS (2)
#   61. REMEMBERED APPROVALS HELPERS (4)
#   62. REMEMBERED APPROVALS TESTS (3)
#   63. QUICK MESSAGE CHECKS
#   64. QUICK MESSAGE CHECKS HELPERS
#   65. QUICK MESSAGE CHECKS API
#   66. QUICK MESSAGE CHECKS HELPERS (2)
#   67. QUICK MESSAGE CHECKS TESTS
#   68. OWN DIALOG KEYS
#   69. OWN DIALOG KEYS HELPERS
#   70. OWN DIALOG KEYS TESTS
#   71. DECLINE GUARD
# ============================================================

"""Confirmations (typed, spoken, buttons), ask before sending, remembered approvals and quick_message checks."""

import json
import threading
from types import SimpleNamespace
import sys
import re
import subprocess
import shutil
import sqlite3

import pytest
from ui.html import HTML

from confirm_helpers import bind_confirmation
from core.agent import (
    CONFIRM_RETRY_QUESTION,
    NOT_CONFIRMED_ERROR,
    AgentLoop,
    unclear_answer_speech,
)
from core.config import (
    confirmation_answer,
    confirm_listen_profile,
    ASK_BEFORE_SENDING_CHOICES,
    ASK_BEFORE_SENDING_DEFAULT,
    ASK_BEFORE_SENDING_HELP,
    ASK_BEFORE_SENDING_LABELS,
    send_is_grounded,
    recipient_match,
)
from core.planner import PlanRunner
from core.tools import SPECS, ToolRegistry
import core.api as api_mod
from core.api import (
    Api,
    APPROVED_PREFIX,
    QM_HEADER_QUESTION,
    QM_SWITCHER_QUESTION,
    QM_UNCONFIRMED_REASON,
)
from test_api import _HARNESS, _real_outputs, _script, needs_node
from core.memory import Memory


# ============================================================
# CONFIRMATION ANSWERS
# ============================================================

# Confirmation answers are "yes", "no" or "unclear" (config.confirmation_answer).
#
# - yes runs the action; an explicit no keeps today's decline path and wording.
# - unclear (empty, unrecognized, or neither a yes nor a no word): ask once
#   more ("Sorry, yes or no?"), voice and typed alike; still unclear -> the
#   phase fails closed: the user hears "I didn't hear a yes, so I didn't
#   <verb> that.", the result is success False / "not confirmed", and no model
#   call, executor action or other route follows. No decline signature.
# - logs carry "[Confirm] answer=yes|no|unclear (button|voice|typed)" and
#   "(retry)", never the words.
# - planner._confirm uses the same helper.
#
# Merged by area (pure moves, no test changed) from: test_confirm_answers.py, test_confirm_listen_profiles.py, test_enter_confirmation.py.


# ============================================================
# CONFIRMATION ANSWERS HELPERS
# ============================================================

CONFIRMATION_ANSWERS_CONTACT = "Joanne Quillfeather"


CONFIRMATION_ANSWERS_MESSAGE = "meet me at the lighthouse pier"


CONFIRMATION_ANSWERS_SEND = {"action": "quick_message", "parameter": f"Discord|{CONFIRMATION_ANSWERS_CONTACT}|{CONFIRMATION_ANSWERS_MESSAGE}"}


CONFIRMATION_ANSWERS_DONE = {"action": "done", "speech": "ok"}


MUMBLE = "marblewood cobblestone"        # an answer with neither a yes nor a no


CONTENT_WORDS = ("joanne", "quillfeather", "lighthouse", "pier", "marblewood",
                 "cobblestone", "maybe")


def _no_content(text):
    low = text.lower()
    assert [w for w in CONTENT_WORDS if w in low] == []


# ============================================================
# CONFIRMATION ANSWERS TESTS
# ============================================================

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


# ============================================================
# CONFIRMATION ANSWERS RUN
# ============================================================

def _confirmation_answers_run(monkeypatch, replies, answers, mode="voice", confirm=("quick_message", "window_close")):
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
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", mode, None)
    return rec


# ============================================================
# CONFIRMATION ANSWERS HELPERS (2)
# ============================================================

def _observations(rec):
    return [m["content"] for r in rec.requests for m in r.get("messages", [])
            if m.get("role") == "user" and "[OBSERVATION]" in str(m.get("content"))]


# ============================================================
# CONFIRMATION ANSWERS TESTS (2)
# ============================================================

def test_unclear_then_yes_runs(monkeypatch, capsys):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["maybe", "yes"])
    assert rec.calls == [("quick_message", CONFIRMATION_ANSWERS_SEND["parameter"])]
    assert rec.asked[1] == CONFIRM_RETRY_QUESTION and len(rec.asked) == 2
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear (voice)\n" in log
    assert "[Confirm] answer=yes (voice) (retry)" in log
    _no_content(log)


# ============================================================
# CONFIRMATION ANSWERS HELPERS (3)
# ============================================================

UNCLEAR_SEND = {"success": False, "error": "not confirmed",
                "speech": "I didn't hear a yes, so I didn't send that.", "steps": 1}


# ============================================================
# CONFIRMATION ANSWERS TESTS (3)
# ============================================================

@pytest.mark.parametrize("mode", ["voice", "text"])
def test_unclear_twice_fails_closed(monkeypatch, capsys, mode):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], [MUMBLE, ""], mode=mode)
    assert rec.calls == []                                # the executor never ran
    assert rec.result == UNCLEAR_SEND and NOT_CONFIRMED_ERROR == "not confirmed"
    assert rec.spoken[-1] == "I didn't hear a yes, so I didn't send that."
    assert len(rec.asked) == 2 and rec.asked[1] == CONFIRM_RETRY_QUESTION   # one retry
    assert len(rec.requests) == 1                         # no model call after the answer
    assert rec.rows == []                                 # no confirm_declined row
    log = capsys.readouterr().out
    src = "voice" if mode == "voice" else "typed"
    assert f"[Confirm] answer=unclear ({src})\n" in log
    assert f"[Confirm] answer=unclear ({src}) (retry)" in log
    assert "[Confirm] not confirmed; the phase ends" in log
    _no_content(log)
    _no_content(repr(rec.rows))


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_unclear_cannot_lead_to_another_model_call_or_route(monkeypatch, mode):
    # The model has a manual route queued; none of it may run.
    manual = [{"action": "click_on_text", "parameter": CONFIRMATION_ANSWERS_CONTACT},
              {"action": "type_text", "parameter": CONFIRMATION_ANSWERS_MESSAGE},
              {"action": "press_key", "parameter": "enter"}]
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, *manual, CONFIRMATION_ANSWERS_DONE], [MUMBLE, MUMBLE], mode=mode)
    assert rec.calls == [] and len(rec.requests) == 1
    assert rec.result["error"] == "not confirmed"
    assert not any("REFUSED" in o or "declined" in o for o in _observations(rec))


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_enter_in_a_messaging_app_fails_closed_too(monkeypatch, mode):
    enter = {"action": "press_key", "parameter": "enter"}
    rec = _confirmation_answers_run(monkeypatch, [enter, CONFIRMATION_ANSWERS_DONE], ["maybe", "hmm"], mode=mode,
               confirm=("press_key",))
    assert rec.calls == [] and len(rec.requests) == 1
    assert rec.result == {"success": False, "error": "not confirmed",
                          "speech": "I didn't hear a yes, so I didn't do that.", "steps": 1}


def test_other_actions_name_a_generic_verb(monkeypatch):
    rec = _confirmation_answers_run(monkeypatch, [{"action": "window_close", "parameter": "Notepad"}, CONFIRMATION_ANSWERS_DONE],
               ["maybe", "maybe"])
    assert rec.calls == []
    assert "I didn't hear a yes, so I didn't do that." in rec.spoken
    assert unclear_answer_speech("quick_message") == "I didn't hear a yes, so I didn't send that."


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_no_is_unchanged(monkeypatch, capsys, mode):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, {**CONFIRMATION_ANSWERS_SEND, "parameter": "Discord|Ada|hi"}], ["no", "nope"],
               mode=mode)
    assert rec.calls == []
    assert len(rec.asked) == 2                            # no retry after a no
    assert any("User explicitly declined this action" in o for o in _observations(rec))
    assert rec.rows == [("confirm_declined", False)]
    assert {k: v for k, v in rec.result.items() if k != "steps"} == {
        "success": False, "error": "User declined the requested action.",
        "speech": "I stopped there: you said no twice, so I didn't do what you declined. "
                  "I didn't change anything."}
    assert "I didn't hear a yes" not in " ".join(rec.spoken)
    assert capsys.readouterr().out.count("[Confirm] answer=no") == 2


def test_typed_maybe_gets_the_one_retry_then_yes_runs(monkeypatch, capsys):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["maybe", "yes"], mode="text")
    assert rec.calls == [("quick_message", CONFIRMATION_ANSWERS_SEND["parameter"])]
    assert rec.asked[1] == CONFIRM_RETRY_QUESTION and len(rec.asked) == 2
    assert rec.result["success"] is True
    assert "[Confirm] answer=yes (typed) (retry)" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_an_explicit_no_on_the_retry_is_still_the_decline_path(monkeypatch, mode):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["maybe", "no"], mode=mode)
    assert rec.calls == []
    assert rec.rows == [("confirm_declined", False)]
    assert any("User explicitly declined this action" in o for o in _observations(rec))
    assert rec.result["success"] is True                  # the model's done ends it


def test_typed_yes_runs(monkeypatch):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["Yes, go ahead"], mode="text")
    assert rec.calls == [("quick_message", CONFIRMATION_ANSWERS_SEND["parameter"])]


def test_a_voice_stop_on_the_retry_is_still_a_stop(monkeypatch):
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["maybe", "stop"])
    assert rec.calls == []
    assert rec.result == {"success": False, "error": "Stopped by user"}


# -- the quick_message question ---------------------------------------------------------


# ============================================================
# CONFIRMATION ANSWERS HELPERS (4)
# ============================================================

def _question(action, param):
    return AgentLoop(SimpleNamespace(_resolve_path_placeholders=lambda p: p),
                     None)._confirmation_question(action, param)


# ============================================================
# CONFIRMATION ANSWERS TESTS (4)
# ============================================================

def test_the_quick_message_question_names_message_contact_and_app():
    assert _question("quick_message", f"Discord|{CONFIRMATION_ANSWERS_CONTACT}|{CONFIRMATION_ANSWERS_MESSAGE}") == (
        f"Send '{CONFIRMATION_ANSWERS_MESSAGE}' to {CONFIRMATION_ANSWERS_CONTACT} on Discord?")


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
    rec = _confirmation_answers_run(monkeypatch, [CONFIRMATION_ANSWERS_SEND, CONFIRMATION_ANSWERS_DONE], ["yes"], mode=mode)
    assert rec.asked == [f"Send '{CONFIRMATION_ANSWERS_MESSAGE}' to {CONFIRMATION_ANSWERS_CONTACT} on Discord?"]
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
    bind_confirmation(api)
    runner = PlanRunner.__new__(PlanRunner)
    runner.api = api
    assert runner._confirm("Continue with the independent phases?", mode) is expected
    log = capsys.readouterr().out
    assert f"[Confirm] answer={verdict} ({'voice' if mode == 'voice' else 'typed'})" in log
    assert api.notified[-2:] == ["showConfirmPrompt", "closeConfirmPrompt"]
    _no_content(log)


# ==========================================================================
# moved from tests/test_confirm_listen_profiles.py
# MISTAI_CONFIRM_LISTEN = shared (default) | ptt | lead: an off-by-default A/B
# for voice confirmation and ask_user listens. The default is unchanged; the
# other profiles use their own recognizer and only READ push-to-talk's stored
# noise threshold.
# ==========================================================================


# ============================================================
# CONFIRMATION ANSWERS MIC
# ============================================================

class _ConfirmationAnswersMic:
    SAMPLE_RATE, SAMPLE_WIDTH = 16000, 2
    stream = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# CONFIRMATION ANSWERS REC
# ============================================================

def _rec(calls, name, threshold=300):
    class Rec:
        energy_threshold = threshold
        pause_threshold = 0.8
        non_speaking_duration = 0.5

        def adjust_for_ambient_noise(self, src, duration=1):
            calls.append((name, "adjust", duration))
            self.energy_threshold = 999

        def listen(self, src, timeout=None, phrase_time_limit=None):
            calls.append((name, "listen", timeout, phrase_time_limit, self.energy_threshold,
                          self.pause_threshold))
            return object()

        def recognize_google(self, audio):
            calls.append((name, "recognize"))
            return "yes"
    return Rec()


# ============================================================
# CONFIRMATION ANSWERS HELPERS (5)
# ============================================================

@pytest.fixture
def api(monkeypatch):
    now = [5000.0]
    sleeps = []
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: sleeps.append(round(s, 2)))
    a = Api.__new__(Api)
    a._mic_lock, a._listen_waiting = threading.Lock(), threading.Event()
    a.microphone = _ConfirmationAnswersMic()
    a.calls, a.sleeps, a.now = [], sleeps, now
    a.recognizer = _rec(a.calls, "shared")
    a.ptt_recognizer = _rec(a.calls, "pttrec", threshold=420)
    a.confirm_recognizer = _rec(a.calls, "confirm")
    a._ptt_calibrated_at = now[0] - 60          # measured a minute ago: fresh
    a.tts = SimpleNamespace(speaking=False, finished_at=now[0] - 0.1)
    monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    monkeypatch.delenv("MISTAI_VOICE_DIAG", raising=False)
    return a


# ============================================================
# CONFIRMATION ANSWERS TESTS (5)
# ============================================================

def test_profile_setting_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    assert confirm_listen_profile() == "shared"
    for value, expected in (("ptt", "ptt"), (" LEAD ", "lead"), ("shared", "shared"),
                            ("fast", "shared"), ("", "shared")):
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
        assert confirm_listen_profile() == expected       # read at call time


def test_default_listen_is_pinned_and_identical(api, capsys):
    assert Api._listen_once(api, timeout=10) == "yes"
    assert api.calls == [("shared", "adjust", 0.3), ("shared", "listen", 10, 15, 999, 1.0),
                         ("shared", "recognize")]
    assert api.recognizer.non_speaking_duration == 0.5
    assert api.sleeps == [] and "[Confirm] listen profile" not in capsys.readouterr().out


def test_ptt_profile_reuses_the_fresh_threshold_with_its_own_settings(api, capsys):
    assert Api._listen_once(api, timeout=6, profile="ptt") == "yes"
    assert api.calls == [("confirm", "listen", 10, 8, 420, 1.0), ("confirm", "recognize")]
    assert "[Confirm] listen profile=ptt" in capsys.readouterr().out
    # Nothing else changed: the shared and push-to-talk recognizers.
    assert api.recognizer.energy_threshold == 300 and api.recognizer.pause_threshold == 0.8
    assert api.ptt_recognizer.energy_threshold == 420 and api.ptt_recognizer.pause_threshold == 0.8


def test_ptt_profile_adjusts_when_the_threshold_is_stale_or_missing(api):
    api._ptt_calibrated_at = api.now[0] - 600         # 10 minutes: not fresh
    Api._listen_once(api, timeout=10, profile="ptt")
    assert api.calls[0] == ("confirm", "adjust", 0.3)
    api.calls.clear()
    api._ptt_calibrated_at = None
    Api._listen_once(api, timeout=10, profile="ptt")
    assert api.calls[0] == ("confirm", "adjust", 0.3)


def test_lead_profile_waits_after_tts_and_reuses_a_fresh_threshold(api, capsys):
    assert Api._listen_once(api, timeout=10, profile="lead") == "yes"
    assert api.sleeps == [0.3]                        # 0.4 s after TTS ended 0.1 s ago
    assert api.calls == [("confirm", "listen", 10, 15, 420, 1.0), ("confirm", "recognize")]
    assert "[Confirm] listen profile=lead" in capsys.readouterr().out


def test_lead_profile_waits_for_speech_to_end_first(api):
    api.tts = SimpleNamespace(speaking=True, finished_at=None)
    seen = []

    def sleep(s):
        seen.append(round(s, 2))
        api.tts.speaking, api.tts.finished_at = False, api.now[0]
    api_mod.time.sleep = sleep
    Api._listen_once(api, timeout=10, profile="lead")
    assert seen == [0.05, 0.4]


def test_lead_profile_does_not_wait_when_tts_ended_long_ago(api):
    api.tts.finished_at = api.now[0] - 3
    Api._listen_once(api, timeout=10, profile="lead")
    assert api.sleeps == []


def test_lead_without_a_fresh_threshold_adjusts_like_shared(api):
    api._ptt_calibrated_at = None
    Api._listen_once(api, timeout=10, profile="lead")
    assert api.calls[:2] == [("confirm", "adjust", 0.3), ("confirm", "listen", 10, 15, 999, 1.0)]


def test_push_to_talk_listens_are_unchanged_by_the_setting(api, monkeypatch):
    monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", "ptt")
    api._voice_mark = lambda *a, **k: None
    Api._listen_once(api, timeout=6, ptt=True)
    from core.config import PTT_PHRASE_TIME_LIMIT
    assert api.calls[0] == ("pttrec", "listen", 6, PTT_PHRASE_TIME_LIMIT, 420, 2.0)


@pytest.mark.parametrize("profile", ["ptt", "lead"])
def test_voice_diag_names_the_profile(api, monkeypatch, capsys, profile):
    monkeypatch.setenv("MISTAI_VOICE_DIAG", "1")
    Api._listen_once(api, timeout=10, profile=profile)
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[VoiceDiag]")][0]
    assert f"rec={profile} " in line


# -- the callers -----------------------------------------------------------------------------


# ============================================================
# CONFIRMATION ANSWERS CONFIRM API
# ============================================================

def _confirm_api(listens):
    a = Api.__new__(Api)
    a._answer_lock, a._answer_event = threading.Lock(), threading.Event()
    a._awaiting_text_answer = threading.Event()
    a._pending_answer = a._pending_source = None
    a._notify_js = lambda js: None
    a._window = None
    a.speak = lambda t: None
    a.tts = SimpleNamespace(speaking=False)
    a._voice_start = a._voice_mark = lambda *x, **k: None
    a._listen_once = lambda **kw: listens.append(kw) or "yes"
    return a


# ============================================================
# CONFIRMATION ANSWERS TESTS (6)
# ============================================================

@pytest.mark.parametrize("value, extra", [(None, {}), ("shared", {}), ("bogus", {}),
                                          ("ptt", {"profile": "ptt"}),
                                          ("lead", {"profile": "lead"})])
def test_confirmations_pass_a_profile_only_when_one_is_chosen(monkeypatch, value, extra):
    if value is None:
        monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
    listens = []
    Api.ask_confirmation(_confirm_api(listens), "q?", "voice")
    assert len(listens) == 1
    kw = listens[0]
    assert {k: v for k, v in kw.items() if k == "profile"} == extra
    assert kw["timeout"] == 10


@pytest.mark.parametrize("value, extra", [(None, {}), ("ptt", {"profile": "ptt"})])
def test_ask_user_passes_a_profile_only_when_one_is_chosen(monkeypatch, value, extra):
    from core.agent import AgentLoop
    from core.tools import SPECS
    if value is None:
        monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
    listens = []
    replies = iter(json.dumps(r) for r in [{"action": "ask_user", "parameter": "Which one?"},
                                           {"action": "done", "speech": "ok"}])
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u",
                                    get_fallback_url=lambda: "f"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: None),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _t: None,
        tts=SimpleNamespace(speaking=False),
        _listen_once=lambda **kw: listens.append(kw) or "the first one",
        _normalize_utterance=lambda t: str(t).lower(), _VOICE_STOP_WORDS={"stop"},
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    loop._loop("t", "m", "none", "voice", None)
    assert listens == [{"timeout": 10, **extra}]


# -- A question no voice could speak (the TTS dropped it) --------------------
# It stays on screen and its wait is the typed answer's full time, never cut
# short because nothing was said.

class _ClickLater:
    """An answer event whose wait() is a button click arriving at once."""

    def __init__(self, api, answer):
        self.api, self.answer, self.waits, self.flag = api, answer, [], False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def clear(self):
        self.flag = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.api._pending_answer, self.api._pending_source = self.answer, "button"
        self.flag = True
        return True


def _unheard_confirm_api(listens, spoken_ok):
    a = _confirm_api(listens)
    a.tts = SimpleNamespace(speaking=False, dropped=0)
    a.speak = (lambda t: None) if spoken_ok else (lambda t: setattr(a.tts, "dropped", 1))
    a._listen_once = lambda **kw: listens.append(kw) or None
    a._answer_event = _ClickLater(a, "yes")
    return a


def test_an_unspoken_confirmation_waits_the_full_time_for_a_button():
    listens = []
    a = _unheard_confirm_api(listens, spoken_ok=False)
    assert Api.ask_confirmation(a, "Delete it?", "voice") == ("yes", "button")
    assert len(listens) == 1                                  # a spoken answer still counts
    assert a._answer_event.waits and a._answer_event.waits[0] > Api.CONFIRM_TEXT_TIMEOUT - 5


def test_a_spoken_confirmation_keeps_its_short_listen():
    listens = []
    a = _unheard_confirm_api(listens, spoken_ok=True)
    assert Api.ask_confirmation(a, "Delete it?", "voice") == (None, "voice")
    assert a._answer_event.waits == [] and len(listens) == 1


def _unheard_ask_api(heard):
    a = _confirm_api([])
    shown = []
    a._notify_js = shown.append
    a._listen_once = lambda **kw: heard
    a._answer_event = _ClickLater(a, "Documents")
    return a, shown


def test_an_unspoken_ask_user_question_is_shown_and_waits_for_a_typed_answer(capsys):
    a, shown = _unheard_ask_api(heard=None)
    assert Api._answer_unheard_question(a, "Which folder?", timeout=60) == "Documents"
    assert any(js.startswith('showAskUserPrompt("Which folder?"') for js in shown)
    assert a._answer_event.waits[0] > 55 and not a._awaiting_text_answer.is_set()
    assert "Which folder" not in capsys.readouterr().out      # never logged
    a, _ = _unheard_ask_api(heard="the downloads one")
    assert Api._answer_unheard_question(a, "Which folder?") == "the downloads one"
    assert a._answer_event.waits == []


def test_the_agent_hands_an_unspoken_question_to_the_screen_prompt(monkeypatch):
    from core.agent import AgentLoop
    from core.tools import SPECS
    monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    listens, unheard = [], []
    replies = iter(json.dumps(r) for r in [{"action": "ask_user", "parameter": "Which one?"},
                                           {"action": "done", "speech": "ok"}])
    tts = SimpleNamespace(speaking=False, dropped=0)
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u",
                                    get_fallback_url=lambda: "f"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: None),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _t: setattr(tts, "dropped", tts.dropped + 1),   # no voice works
        tts=tts,
        _listen_once=lambda **kw: listens.append(kw) or "x",
        _answer_unheard_question=lambda q, **kw: unheard.append((q, kw)) or "the first one",
        _normalize_utterance=lambda t: str(t).lower(), _VOICE_STOP_WORDS={"stop"},
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    loop._loop("t", "m", "none", "voice", None)
    assert listens == [] and unheard == [("Which one?", {"timeout": 60, "listen_timeout": 10})]


# ==========================================================================
# moved from tests/test_enter_confirmation.py
# press_key('enter') confirmation: skipped only for a safe, known, in-front target.
# ==========================================================================


# ============================================================
# CONFIRMATION ANSWERS HELPERS (6)
# ============================================================

def _registry():
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.specs = dict(SPECS)
    return reg


# -- ToolRegistry ---------------------------------------------------------------------


# ============================================================
# CONFIRMATION ANSWERS TESTS (7)
# ============================================================

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


# ============================================================
# CONFIRMATION ANSWERS HELPERS (7)
# ============================================================

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


# ============================================================
# CONFIRMATION ANSWERS API
# ============================================================

def _confirmation_answers_api(monkeypatch, target, fg):
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


# ============================================================
# CONFIRMATION ANSWERS TESTS (8)
# ============================================================

@pytest.mark.parametrize("target, fg", [("calculator", 10), ("notepad", 11)])
def test_safe_target_in_front_skips_the_prompt(monkeypatch, target, fg):
    assert _confirmation_answers_api(monkeypatch, target, fg).enter_target_is_safe() is True


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
    assert _confirmation_answers_api(monkeypatch, target, fg).enter_target_is_safe() is False, why


# -- AgentLoop ------------------------------------------------------------------------


# ============================================================
# CONFIRMATION ANSWERS LOOP
# ============================================================

def _confirmation_answers_loop(monkeypatch, safe, replies):
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
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "W"))
    loop._loop("task", "m", "none", "text", None)
    return calls, asked


# ============================================================
# CONFIRMATION ANSWERS HELPERS (8)
# ============================================================

STEPS = [{"action": "press_key", "parameter": "enter"}, {"action": "done", "speech": "ok"}]


# ============================================================
# CONFIRMATION ANSWERS TESTS (9)
# ============================================================

def test_loop_presses_enter_without_asking_in_a_safe_target(monkeypatch):
    calls, asked = _confirmation_answers_loop(monkeypatch, True, STEPS)
    assert calls == [("press_key", "enter")] and asked == []


def test_loop_asks_before_enter_otherwise(monkeypatch):
    calls, asked = _confirmation_answers_loop(monkeypatch, False, STEPS)
    assert calls == [] and len(asked) == 1  # harness-style "no" declines it


def test_loop_never_asks_for_plain_keys(monkeypatch):
    calls, asked = _confirmation_answers_loop(monkeypatch, False, [{"action": "press_key", "parameter": "="},
                                              {"action": "done", "speech": "ok"}])
    assert calls == [("press_key", "=")] and asked == []


# ============================================================
# CONFIRMATION BUTTONS
# ============================================================

# Yes/No buttons for confirmations (Api.ask_confirmation + the page).
#
# - Voice: the question is spoken and shown with buttons; the listen gets the
#   answer event as its stop_event, so a click ends it and wins over speech.
# - Typed: the buttons or the text box.
# - The event is cleared before asking and after answering; a stop request ends
#   the wait; MistAI's window is shown while pending and minimized again after
#   (MISTAI_CONFIRM_REMINIMIZE, default on).
# - _listen_once without stop_event is unchanged.


# ============================================================
# CONFIRMATION BUTTONS API
# ============================================================

def _confirmation_buttons_api(listen=None, speaking=False):
    """A bare Api with the confirmation plumbing and recorders."""
    api = Api.__new__(Api)
    api._answer_lock = threading.Lock()
    api._answer_event = threading.Event()
    api._awaiting_text_answer = threading.Event()
    api._pending_answer = api._pending_source = None
    api._stop_requested = threading.Event()
    api._agent_lock = threading.Lock()
    api._agent_state_lock = threading.Lock()
    api.rec = {"js": [], "spoken": [], "listens": [], "window": []}
    api._notify_js = lambda js: api.rec["js"].append(js)
    api.speak = lambda text: api.rec["spoken"].append(text)
    api.tts = SimpleNamespace(speaking=speaking, interrupt=lambda: None)
    api._window = SimpleNamespace(restore=lambda: api.rec["window"].append("restore"))
    api.minimize_window = lambda: api.rec["window"].append("minimize")
    api.CONFIRM_TEXT_TIMEOUT = 0.5
    api._voice_start = lambda *_a, **_k: None
    api._voice_mark = lambda *_a, **_k: None

    def default_listen(timeout=6, stop_event=None, **_k):
        api.rec["listens"].append((timeout, stop_event is api._answer_event))
        return listen() if callable(listen) else listen

    api._listen_once = default_listen
    return api


# ============================================================
# CLICK DURING LISTEN
# ============================================================

def _click_during_listen(api, answer, then_heard="no"):
    """A listen that is still running when the user clicks: it waits until the
    stop_event is set (as the real stream proxy ends listen()) and reports
    what speech it would have returned."""
    def listen(timeout=6, stop_event=None, **_k):
        api.rec["listens"].append((timeout, stop_event is api._answer_event))
        threading.Timer(0.05, lambda: Api.submit_answer(api, answer, "button")).start()
        assert stop_event.wait(2)
        return None if stop_event.is_set() else then_heard
    api._listen_once = listen


# -- Api.ask_confirmation ------------------------------------------------------------------


# ============================================================
# CONFIRMATION BUTTONS TESTS
# ============================================================

def test_a_click_answers_while_a_listen_is_pending_and_stops_it(capsys):
    api = _confirmation_buttons_api()
    _click_during_listen(api, "yes")
    assert Api.ask_confirmation(api, "Send hi to Ada?", "voice") == ("yes", "button")
    assert api.rec["listens"] == [(10, True)]          # the answer event is the stop_event
    assert api.rec["spoken"] == ["Send hi to Ada?"]
    assert api.rec["js"] == ['showConfirmPrompt("Send hi to Ada?")', "closeConfirmPrompt()"]
    assert not api._answer_event.is_set() and not api._awaiting_text_answer.is_set()
    assert "Ada" not in capsys.readouterr().out


def test_no_click_falls_back_to_speech():
    api = _confirmation_buttons_api(listen="yes please")
    assert Api.ask_confirmation(api, "q?", "voice") == ("yes please", "voice")


def test_a_click_wins_over_speech_heard_in_the_same_listen():
    api = _confirmation_buttons_api()

    def listen(timeout=6, stop_event=None, **_k):
        Api.submit_answer(api, "no", "button")       # clicked as the listen returned
        return "yes"
    api._listen_once = listen
    assert Api.ask_confirmation(api, "q?", "voice") == ("no", "button")


def test_a_click_while_the_question_is_still_spoken_skips_the_listen():
    api = _confirmation_buttons_api()
    api.tts = SimpleNamespace(speaking=True)
    api.speak = lambda _t: Api.submit_answer(api, "yes", "button")
    assert Api.ask_confirmation(api, "q?", "voice") == ("yes", "button")
    assert api.rec["listens"] == []


def test_typed_mode_takes_a_button_or_the_text_box():
    api = _confirmation_buttons_api()
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, "yes", "button")
    assert Api.ask_confirmation(api, "q?", "text") == ("yes", "button")
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, "Yes, go")
    assert Api.ask_confirmation(api, "q?", "text") == ("Yes, go", "typed")
    assert api.rec["listens"] == []                  # typed mode never listens


def test_typed_mode_with_no_answer_times_out_as_none():
    api = _confirmation_buttons_api()
    api.CONFIRM_TEXT_TIMEOUT = 0.01
    assert Api.ask_confirmation(api, "q?", "text") == (None, "typed")


def test_a_stop_request_ends_the_wait(monkeypatch):
    api = _confirmation_buttons_api()
    api._agent_lock.acquire()
    api.CONFIRM_TEXT_TIMEOUT = 5
    threading.Timer(0.05, lambda: Api.stop_current_task(api)).start()
    answer, _source = Api.ask_confirmation(api, "q?", "text")
    assert answer == "__mistai_stop_requested__" and api._stop_requested.is_set()


def test_a_stop_request_also_ends_a_pending_listen():
    api = _confirmation_buttons_api()
    api._agent_lock.acquire()

    def listen(timeout=6, stop_event=None, **_k):
        threading.Timer(0.05, lambda: Api.stop_current_task(api)).start()
        assert stop_event.wait(2)
        return None
    api._listen_once = listen
    answer, _ = Api.ask_confirmation(api, "q?", "voice")
    assert answer == "__mistai_stop_requested__"


def test_no_stale_answer_leaks_into_the_next_confirmation():
    api = _confirmation_buttons_api(listen="maybe")
    Api.submit_answer(api, "yes", "button")          # a late click from an old prompt
    assert Api.ask_confirmation(api, "q?", "voice") == ("maybe", "voice")
    _click_during_listen(api, "no")
    assert Api.ask_confirmation(api, "q?", "voice") == ("no", "button")
    Api.submit_answer(api, "yes", "button")          # a click after it was answered
    api._listen_once = lambda timeout=6, stop_event=None, **_k: "nope"
    assert Api.ask_confirmation(api, "q?", "voice") == ("nope", "voice")


@pytest.mark.parametrize("flag, expected", [(None, ["restore", "minimize"]),
                                            ("1", ["restore", "minimize"]),
                                            ("0", ["restore"])])
def test_the_window_is_shown_then_minimized_again(monkeypatch, flag, expected):
    if flag is None:
        monkeypatch.delenv("MISTAI_CONFIRM_REMINIMIZE", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_REMINIMIZE", flag)
    api = _confirmation_buttons_api(listen="yes")
    Api.ask_confirmation(api, "q?", "voice")
    assert api.rec["window"] == expected


def test_ask_user_still_uses_the_text_prompt_and_never_minimizes():
    api = _confirmation_buttons_api()
    api._notify_js = lambda js: (api.rec["js"].append(js), Api.submit_answer(api, "Documents"))
    assert Api._get_text_answer(api, "Which folder?", timeout=1) == "Documents"
    assert api.rec["js"] == ['showAskUserPrompt("Which folder?")']
    assert api.rec["window"] == ["restore"]


# -- the agent gate and planner._confirm ----------------------------------------------------


# ============================================================
# CONFIRMATION BUTTONS LOOP
# ============================================================

def _confirmation_buttons_loop(monkeypatch, api, replies, mode="voice"):
    from core.agent import AgentLoop
    from core.tools import SPECS
    replies = iter(json.dumps(r) for r in replies)
    calls = []
    api.task_memory = SimpleNamespace(get_hints_for_task=lambda _t: [])
    api.caption_window, api.captions_enabled = None, False
    api.live = SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)})
    api.api_manager = SimpleNamespace(get_headers=lambda: {},
                                      get_api_url=lambda: "https://example.invalid/a",
                                      get_fallback_url=lambda: "https://example.invalid/b")
    api.tools = SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                                requires_confirmation=lambda a, *_x, **_k: a == "quick_message")
    api.executor = SimpleNamespace(run=lambda a, p: calls.append(a) or SimpleNamespace(
        action=a, parameter=p, success=True, detail="", verified=None,
        escalate_to_user=False, reliability_note=lambda: "", escalated_from=""))
    api.memory = SimpleNamespace(add_action=lambda *a: None)
    api._resolve_path_placeholders = lambda p: p
    api._last_opened_app = ""
    monkeypatch.setattr("core.agent.requests.post", lambda *_a, **_k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    return loop._loop("send it", "m", "none", mode, None), calls


# ============================================================
# CONFIRMATION BUTTONS HELPERS
# ============================================================

CONFIRMATION_BUTTONS_SEND = {"action": "quick_message", "parameter": "Discord|Ada|hi"}
CONFIRMATION_BUTTONS_DONE = {"action": "done", "speech": "ok"}


# ============================================================
# CONFIRMATION BUTTONS TESTS (2)
# ============================================================

def test_a_click_after_an_unclear_first_listen_answers(monkeypatch, capsys):
    api = _confirmation_buttons_api()
    first = [True]

    def listen(timeout=6, stop_event=None, **_k):
        if first.pop() if first else False:
            return "marblewood"                      # unclear speech
        threading.Timer(0.05, lambda: Api.submit_answer(api, "yes", "button")).start()
        stop_event.wait(2)
        return None
    api._listen_once = listen
    result, calls = _confirmation_buttons_loop(monkeypatch, api, [CONFIRMATION_BUTTONS_SEND, CONFIRMATION_BUTTONS_DONE])
    assert calls == ["quick_message"] and result["success"]
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear (voice)" in log
    assert "[Confirm] answer=yes (button) (retry)" in log
    assert "marblewood" not in log and "Ada" not in log


@pytest.mark.parametrize("answer, ran", [("yes", True), ("no", False)])
def test_a_button_answers_the_agent_gate_in_typed_mode(monkeypatch, capsys, answer, ran):
    api = _confirmation_buttons_api()
    api._notify_js = lambda js: js.startswith("show") and Api.submit_answer(api, answer, "button")
    _result, calls = _confirmation_buttons_loop(monkeypatch, api, [CONFIRMATION_BUTTONS_SEND, CONFIRMATION_BUTTONS_DONE, CONFIRMATION_BUTTONS_DONE], mode="text")
    assert (calls == ["quick_message"]) is ran
    assert f"[Confirm] answer={answer} (button)" in capsys.readouterr().out


def test_planner_confirm_takes_a_button(capsys):
    api = _confirmation_buttons_api()
    _click_during_listen(api, "yes")
    runner = PlanRunner.__new__(PlanRunner)
    runner.api = api
    assert runner._confirm("Continue with the independent phases?", "voice") is True
    assert "[Confirm] answer=yes (button)" in capsys.readouterr().out


# -- _listen_once with a stop_event --------------------------------------------------------


# ============================================================
# CONFIRMATION BUTTONS STREAM
# ============================================================

class _Stream:
    def __init__(self):
        self.reads = 0

    def read(self, size):
        self.reads += 1
        return b"\x10\x00" * 8

    def close(self):
        pass


# ============================================================
# CONFIRMATION BUTTONS MIC
# ============================================================

class _ConfirmationButtonsMic:
    SAMPLE_RATE, SAMPLE_WIDTH, CHUNK = 16000, 2, 8

    def __init__(self):
        self.stream = _Stream()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ============================================================
# CONFIRMATION BUTTONS LISTEN API
# ============================================================

def _listen_api(stop_after=None):
    api = Api.__new__(Api)
    api._mic_lock = threading.Lock()
    api._listen_waiting = threading.Event()
    api.microphone = _ConfirmationButtonsMic()
    calls = []

    class Rec:
        energy_threshold = 300

        def adjust_for_ambient_noise(self, src, duration=1):
            calls.append(("adjust", duration))

        def listen(self, src, timeout=None, phrase_time_limit=None):
            calls.append(("listen", timeout, phrase_time_limit, type(src.stream).__name__))
            reads = 0
            while src.stream.read(8):                # ends when the proxy returns b""
                reads += 1
                if stop_after is not None and reads == stop_after[0]:
                    stop_after[1].set()
                if reads > 50:
                    break
            calls.append(("reads", reads))
            return object()

        def recognize_google(self, audio):
            calls.append(("recognize",))
            return "yes"

    api.recognizer = Rec()
    api.calls = calls
    return api


# ============================================================
# CONFIRMATION BUTTONS TESTS (3)
# ============================================================

def test_listen_once_defaults_are_unchanged():
    api = _listen_api()
    assert Api._listen_once(api, timeout=10) == "yes"
    assert api.calls[0] == ("adjust", 0.3)
    assert api.calls[1] == ("listen", 10, 15, "_Stream")         # no proxy without stop_event
    assert api.calls[-1] == ("recognize",)
    assert (api.recognizer.pause_threshold, api.recognizer.non_speaking_duration) == (1.0, 0.5)


def test_a_stop_event_ends_the_listen_within_one_chunk_and_skips_recognition():
    stop = threading.Event()
    api = _listen_api(stop_after=(3, stop))
    assert Api._listen_once(api, timeout=10, stop_event=stop) is None
    assert ("listen", 10, 15, "_StoppableStream") in api.calls
    assert ("reads", 3) in api.calls                              # the next read ended it
    assert ("recognize",) not in api.calls


def test_an_unset_stop_event_changes_nothing_else():
    api = _listen_api()
    assert Api._listen_once(api, timeout=10, stop_event=threading.Event()) == "yes"
    assert api.calls[0] == ("adjust", 0.3) and api.calls[-1] == ("recognize",)


# -- the page (Node, the whole script) ------------------------------------------------------


# ============================================================
# CONFIRMATION BUTTONS PAGE HARNESS
# ============================================================

def _page_harness():
    prefix = _HARNESS.split("%PAGE%", 1)[0]
    prefix = prefix.replace(
        "appendChild(c) { this.children.push(c); return c; }, remove() {},",
        "appendChild(c) { c.parent = this; this.children.push(c); return c; }, "
        "remove() { if (this.parent) this.parent.children = "
        "this.parent.children.filter(x => x !== this); this.parent = null; },")
    assert "c.parent = this" in prefix
    prefix = prefix.replace(
        "  ask_mistai: api('ask_mistai', () => (CONFIG.ask || []).shift()),",
        "  ask_mistai: api('ask_mistai', () => (CONFIG.ask || []).shift()),\n"
        "  submit_answer: (...args) => { submitted.push(args); return Promise.resolve({success: true}); },")
    return "const submitted = [];\n" + prefix


# ============================================================
# CONFIRMATION BUTTONS HELPERS (2)
# ============================================================

_DRIVER = r"""
const settle = () => new Promise(r => realSetTimeout(r, 20));
function confirmBox() { return chat.children.filter(m => m.className.includes('confirm')).pop(); }
function view() {
  const box = confirmBox();
  return {
    question: box ? box.children[0].textContent : null,
    buttons: box && box.children[1] && box.children[1].parent === box
      ? box.children[1].children.map(b => [b.textContent, b.className]) : [],
    answering: document.getElementById('inp').dataset.answering || '',
    inputDisabled: document.getElementById('inp').disabled,
    submitted: submitted.slice(),
    lastUser: (chat.children.filter(m => m.className === 'msg user').pop() || {}).textContent || null,
  };
}
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const out = {};
  for (const step of CONFIG.steps) {
    if (step.js) eval(step.js);
    if (step.click !== undefined) {
      const b = confirmBox().children[1].children[step.click];
      for (let i = 0; i < (step.times || 1); i++) await b.onclick();
    }
    if (step.type) { document.getElementById('inp').value = step.type; await send(); }
    await settle();
    out[step.name] = view();
  }
  out.consoleErrors = consoleErrors;
  console.log(JSON.stringify(out));
})();
"""


# ============================================================
# RUN CONFIRM PAGE
# ============================================================

def _run_confirm_page(tmp_path, steps):
    set_gender = _real_outputs()
    cfg = {"gender": "none", "set_gender": set_gender, "flags": {"is_dev": False},
           "steps": steps}
    script = tmp_path / "confirm.js"
    script.write_text(_page_harness().replace("%CONFIG%", json.dumps(cfg))
                      + _script() + _DRIVER, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ============================================================
# CONFIRMATION BUTTONS HELPERS (3)
# ============================================================

QUESTION = 'Send "<b>hi</b>" to Ada on Discord?'


# ============================================================
# CONFIRMATION BUTTONS TESTS (4)
# ============================================================

@needs_node
def test_page_shows_text_and_buttons_and_one_click_sends_one_answer(tmp_path):
    r = _run_confirm_page(tmp_path, [
        {"name": "shown", "js": f"showConfirmPrompt({json.dumps(QUESTION)})"},
        {"name": "clicked", "click": 0, "times": 2},           # a double click
        {"name": "closed", "js": "closeConfirmPrompt()"},     # Python closes it after
    ])
    assert r["shown"]["question"] == QUESTION                  # text, not markup
    assert r["shown"]["buttons"] == [["Yes", "btn-save"], ["No", "btn-cancel"]]
    assert r["shown"]["answering"] == "true" and r["shown"]["inputDisabled"] is False
    assert r["clicked"]["submitted"] == [["yes", "button"]]     # exactly one submit
    assert r["clicked"]["buttons"] == [] and r["clicked"]["lastUser"] == "Yes"
    assert r["clicked"]["answering"] == "false"
    assert r["closed"]["submitted"] == [["yes", "button"]]
    assert r["consoleErrors"] == []


@needs_node
def test_page_buttons_go_away_after_a_voice_or_typed_answer(tmp_path):
    r = _run_confirm_page(tmp_path, [
        {"name": "shown", "js": "showConfirmPrompt('Proceed?')"},
        {"name": "voice", "js": "closeConfirmPrompt()"},      # answered by speech
        {"name": "again", "js": "showConfirmPrompt('Sorry, yes or no?')"},
        {"name": "typed", "type": "no"},
    ])
    assert r["voice"]["buttons"] == [] and r["voice"]["answering"] == "false"
    assert r["voice"]["submitted"] == []
    assert r["again"]["buttons"] == [["Yes", "btn-save"], ["No", "btn-cancel"]]
    assert r["typed"]["buttons"] == [] and r["typed"]["submitted"] == [["no"]]


def test_page_builds_the_prompt_with_text_only_and_no_storage():
    js = _script()
    body = js.split("function showConfirmPrompt(question){", 1)[1].split("\nfunction openAPIModal", 1)[0]
    assert "innerHTML" not in body
    assert "q.textContent=String(question||'');" in body
    assert "pywebview.api.submit_answer(answer,'button')" in body
    assert not re.search(r"localstorage|sessionstorage|indexeddb", js, re.I)


# ============================================================
# ASK BEFORE SENDING
# ============================================================

# Ask before sending (quick_message only), profile key "ask_before_sending":
# "always" asks up front as before; "never" doesn't ask; "when_unsure" (the
# default) doesn't ask only when the recipient AND the message are the user's
# own words (Api._agent_task, the original request) AND the Stage A checks
# matched the recipient exactly; otherwise it asks (up front when not
# grounded, inside the action when the match was only close). Everything else
# that needs confirmation is unchanged in every mode. Logs carry labels only.


# ============================================================
# ASK BEFORE SENDING HELPERS
# ============================================================

ASK_BEFORE_SENDING_CONTACT = "Zorblax Quindlewhip"
ASK_BEFORE_SENDING_MESSAGE = "the pelican lands at noon"
ASK_BEFORE_SENDING_SEND = {"action": "quick_message", "parameter": f"discord|{ASK_BEFORE_SENDING_CONTACT}|{ASK_BEFORE_SENDING_MESSAGE}"}
ASK_BEFORE_SENDING_DONE = {"action": "done", "speech": "ok"}
GOAL = f"tell {ASK_BEFORE_SENDING_CONTACT} on Discord: The pelican lands at noon!"
ASK_BEFORE_SENDING_CONTENT = ("zorblax", "quindlewhip", "pelican", "noon")


# ============================================================
# ASK BEFORE SENDING MEMORY
# ============================================================

class _Memory:
    def __init__(self, stored=None):
        self.profile = {} if stored is None else {"ask_before_sending": stored}
        self.rows = []

    def get_profile(self, key, default=None):
        return self.profile.get(key, default)

    def set_profile(self, key, value):
        self.profile[key] = value

    def add_action(self, *a):
        self.rows.append(a)


# ============================================================
# ASK BEFORE SENDING RUN
# ============================================================

def _ask_before_sending_run(monkeypatch, replies, answers, *, mode=None, goal=GOAL, seen=None,
         confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop; quick_message runs the REAL
    Api._act_quick_message (vision answers `seen`, default exact), typed
    confirmations answer from `answers`."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    seen = list(seen if seen is not None else [ASK_BEFORE_SENDING_CONTACT] * 3)
    rec = SimpleNamespace(calls=[], keys=[], clips=[], asked=[], requests=[])
    memory = _Memory(mode)

    def answer(question):
        rec.asked.append(question)
        return answers.pop(0) if answers else None

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
        memory=memory,
        _resolve_path_placeholders=lambda p: p,
        speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _get_text_answer=lambda q, timeout=60: answer(q),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        stop_current_task=lambda: api._stop_requested.set(),
        _last_opened_app="",
        _agent_task=goal, _agent_generation=1,
        # what _act_quick_message needs
        vision=SimpleNamespace(ready=True),
        screen=SimpleNamespace(mark_action=lambda: None),
        is_app_running=lambda _a: True, switch_to_app=lambda _a: True,
        _foreground_process_matches=lambda _a: True,
        _get_app_search_hotkey=lambda _a: "ctrl+k",
        set_clipboard=lambda t: rec.clips.append(t) or True,
        _vision_read=lambda q: ((seen.pop(0), True) if seen else ("", False)),
        last_result_text="",
    )
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    api._qm_read_name = lambda *a, **k: Api._qm_read_name(api, *a, **k)

    def run(a, p):
        rec.calls.append((a, p))
        ok = Api._act_quick_message(api, p) if a == "quick_message" else True
        return SimpleNamespace(action=a, parameter=p, success=ok, detail=api.last_result_text,
                               verified=None, escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="")

    api.executor = SimpleNamespace(run=run)

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: rec.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: rec.keys.append("+".join(k)))
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", "text", None)
    rec.api, rec.loop = api, loop
    return rec


# ============================================================
# ASK BEFORE SENDING HELPERS (2)
# ============================================================

def _ask_before_sending_sent(rec):
    return ASK_BEFORE_SENDING_MESSAGE in rec.clips and rec.keys.count("enter") == 2


# -- the setting -----------------------------------------------------------------------


# ============================================================
# ASK BEFORE SENDING TESTS
# ============================================================

def test_default_is_when_unsure():
    assert ASK_BEFORE_SENDING_DEFAULT == "when_unsure"
    api = SimpleNamespace(memory=_Memory())
    assert Api.ask_before_sending_mode(api) == "when_unsure"


@pytest.mark.parametrize("stored", ["", "bogus", "ALWAYS ", None, 3])
def test_a_bad_stored_value_falls_back_to_the_default(stored):
    api = SimpleNamespace(memory=_Memory(stored))
    expected = "always" if stored == "ALWAYS " else "when_unsure"
    assert Api.ask_before_sending_mode(api) == expected


def test_an_unreadable_profile_gives_the_default():
    def broken(*_a):
        raise OSError("locked")
    api = SimpleNamespace(memory=SimpleNamespace(get_profile=broken))
    assert Api.ask_before_sending_mode(api) == "when_unsure"


def test_set_validates_in_python_and_saves_the_profile_key():
    api = SimpleNamespace(memory=_Memory())
    for bad in ("sometimes", "", None, "always; drop table"):
        r = Api.set_ask_before_sending(api, bad)
        assert r["success"] is False and "Choose one of" in r["error"]
        assert api.memory.profile == {}
    r = Api.set_ask_before_sending(api, " Never ")
    assert r == {"success": True, "current": "never", "label": ASK_BEFORE_SENDING_LABELS["never"],
                 "help": ASK_BEFORE_SENDING_HELP["never"]}
    assert api.memory.profile == {"ask_before_sending": "never"}


def test_options_come_from_config():
    api = SimpleNamespace(memory=_Memory("always"))
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    o = Api.get_ask_before_sending_options(api)
    assert [c["id"] for c in o["choices"]] == list(ASK_BEFORE_SENDING_CHOICES)
    assert all(c["label"] == ASK_BEFORE_SENDING_LABELS[c["id"]] and c["help"] for c in o["choices"])
    assert o["current"] == "always"


@pytest.mark.parametrize("param, goal, grounded", [
    (f"discord|{ASK_BEFORE_SENDING_CONTACT}|{ASK_BEFORE_SENDING_MESSAGE}", GOAL, True),
    (f"discord|{ASK_BEFORE_SENDING_CONTACT}|{ASK_BEFORE_SENDING_MESSAGE}", GOAL.upper(), True),
    (f"discord|Zorblax|{ASK_BEFORE_SENDING_MESSAGE}", GOAL, True),             # the user's words, shorter
    (f"discord|Zorb|{ASK_BEFORE_SENDING_MESSAGE}", GOAL, False),               # not a whole word
    (f"discord|Someone Else|{ASK_BEFORE_SENDING_MESSAGE}", GOAL, False),
    (f"discord|{ASK_BEFORE_SENDING_CONTACT}|the pelican lands at 12", GOAL, False),
    (f"discord|{ASK_BEFORE_SENDING_CONTACT}|", GOAL, False),
    ("discord|only two", GOAL, False),
    (f"discord|{ASK_BEFORE_SENDING_CONTACT}|{ASK_BEFORE_SENDING_MESSAGE}", "", False),
])
def test_send_is_grounded(param, goal, grounded):
    assert send_is_grounded(param, goal) is grounded


# -- the gate --------------------------------------------------------------------------

def test_grounded_and_exact_sends_without_asking(monkeypatch, capsys):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [])
    assert rec.asked == [] and _ask_before_sending_sent(rec)
    out = capsys.readouterr().out
    assert "[Confirm] grounded" in out and "[Confirm] exact" in out


def test_a_model_invented_recipient_asks_up_front(monkeypatch, capsys):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["no"], goal=f"tell my friend: {ASK_BEFORE_SENDING_MESSAGE}")
    assert len(rec.asked) == 1 and rec.calls == []          # asked before anything ran
    assert "[Confirm] not grounded" in capsys.readouterr().out


def test_a_model_edited_message_asks(monkeypatch):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["yes"], goal=f"tell {ASK_BEFORE_SENDING_CONTACT} the bird arrives at 12")
    assert len(rec.asked) == 1 and _ask_before_sending_sent(rec)


def test_a_phase_brief_does_not_ground_the_send(monkeypatch):
    # The task text the loop gets (here "send it") is not the user's request.
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["no"], goal="send it")
    assert len(rec.asked) == 1 and rec.calls == []


def test_a_close_match_asks_inside_the_action_and_a_yes_sends(monkeypatch, capsys):
    close = "Zorblax Quindlewip"
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["yes"], seen=[close, close, close])
    assert len(rec.asked) == 1 and _ask_before_sending_sent(rec)
    assert "[Confirm] not exact" in capsys.readouterr().out


def test_a_close_match_and_a_no_types_and_sends_nothing(monkeypatch):
    close = "Zorblax Quindlewip"
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["no"], seen=[close, close])
    assert len(rec.asked) == 1
    assert ASK_BEFORE_SENDING_MESSAGE not in rec.clips and rec.keys.count("enter") == 1
    assert rec.result["success"] is True                    # the model's done after the decline
    assert any("User explicitly declined" in m["content"]
               for m in rec.requests[-1]["messages"] if m["role"] == "user")


def test_a_close_match_and_an_unclear_answer_ends_the_phase(monkeypatch):
    close = "Zorblax Quindlewip"
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["maybe", "hmm"], seen=[close, close])
    assert len(rec.asked) == 2 and ASK_BEFORE_SENDING_MESSAGE not in rec.clips
    assert rec.result["error"] == "not confirmed"


def test_always_asks_even_when_grounded_and_exact(monkeypatch):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["yes"], mode="always")
    assert len(rec.asked) == 1 and _ask_before_sending_sent(rec)


def test_never_does_not_ask(monkeypatch, capsys):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [], mode="never", goal="something else entirely")
    assert rec.asked == [] and _ask_before_sending_sent(rec)
    assert "[Confirm] skipped (setting)" in capsys.readouterr().out


def test_never_still_never_sends_to_a_mismatched_chat(monkeypatch):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [], mode="never", seen=[ASK_BEFORE_SENDING_CONTACT, ASK_BEFORE_SENDING_CONTACT, "Someone Else"])
    assert rec.keys.count("enter") == 1


def test_the_setting_is_read_at_call_time(monkeypatch):
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [], mode="never")
    assert rec.asked == []
    rec.api.memory.profile["ask_before_sending"] = "always"
    assert rec.loop._send_gate(ASK_BEFORE_SENDING_SEND["parameter"], "text", None, 1)[:2] == (True, None)


@pytest.mark.parametrize("mode", ASK_BEFORE_SENDING_CHOICES)
def test_destructive_actions_ask_in_every_mode(monkeypatch, mode):
    close = {"action": "window_close", "parameter": "Discord"}
    rec = _ask_before_sending_run(monkeypatch, [close, ASK_BEFORE_SENDING_DONE], ["no"], mode=mode)
    assert len(rec.asked) == 1 and rec.calls == []


def test_an_approved_send_is_not_asked_again_in_the_same_task(monkeypatch):
    # The first try is approved, then fails its final check; the identical
    # retry is not asked again.
    rec = _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["yes"], goal="message my friend",
               seen=[ASK_BEFORE_SENDING_CONTACT, ASK_BEFORE_SENDING_CONTACT, "Someone Else", ASK_BEFORE_SENDING_CONTACT, ASK_BEFORE_SENDING_CONTACT, ASK_BEFORE_SENDING_CONTACT])
    assert len(rec.asked) == 1
    assert len(rec.calls) == 2 and rec.keys.count("enter") == 3   # 2 opens + 1 send


def test_logs_carry_labels_only(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    close = "Zorblax Quindlewip"
    _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [])
    _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["yes"], seen=[close, close, close])
    _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], ["no"], goal="tell my friend")
    _ask_before_sending_run(monkeypatch, [ASK_BEFORE_SENDING_SEND, ASK_BEFORE_SENDING_DONE], [], mode="never")
    out = capsys.readouterr()
    low = (out.out + out.err).lower()
    assert [w for w in ASK_BEFORE_SENDING_CONTENT if w in low] == []


# -- the Settings dialog ---------------------------------------------------------------


# ============================================================
# ASK BEFORE SENDING HELPERS (3)
# ============================================================

def _ask_before_sending_script():
    return HTML.split("<script>", 1)[1].split("</script>", 1)[0]


# ============================================================
# ASK BEFORE SENDING TESTS (2)
# ============================================================

def test_page_hardcodes_no_choice_id_label_or_help():
    for c in ASK_BEFORE_SENDING_CHOICES:            # ids, as a JS/HTML literal
        assert f"'{c}'" not in HTML and f'"{c}"' not in HTML, c
    assert "when_unsure" not in HTML
    for word in (*ASK_BEFORE_SENDING_LABELS.values(), *ASK_BEFORE_SENDING_HELP.values()):
        assert word not in HTML, word
    m = re.search(r'<select id="sel-send">(.*?)</select>', HTML, re.S)
    assert m and m.group(1).strip() == ""
    js = _ask_before_sending_script()
    assert "opt.value=String(c.id);opt.textContent=String(c.label||c.id);" in js
    assert "pywebview.api.set_ask_before_sending(e.target.value)" in js
    assert "innerHTML=" not in js.split("async function loadSendSetting", 1)[1].split(
        "selSend.innerHTML=''", 1)[1].split("selSend.onchange", 1)[0]


# ============================================================
# ASK BEFORE SENDING HELPERS (4)
# ============================================================

_ASK_BEFORE_SENDING_HARNESS = r"""
const CONFIG = %CONFIG%;
const realSetTimeout = setTimeout;
globalThis.setInterval = () => 0;
const errors = [];
console.error = (...a) => errors.push(a.map(x => String(x && x.message || x)).join(' '));
function makeEl(tag) {
  const classes = new Set();
  return {tag, children: [], style: {}, dataset: {}, textContent: '', value: '', className: '',
    classList: {add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, f) { (f === undefined ? !classes.has(c) : f) ? classes.add(c) : classes.delete(c); },
      contains(c) { return classes.has(c); }},
    appendChild(c) { this.children.push(c); return c; }, remove() {}, focus() {},
    setAttribute() {}, addEventListener() {}, querySelector() { return makeEl('x'); },
    set innerHTML(_v) { this.children = []; }, get innerHTML() { return ''; }};
}
const els = {};
globalThis.document = {getElementById(id) { return els[id] || (els[id] = makeEl('#' + id)); },
                       createElement(t) { return makeEl(t); }};
globalThis.window = globalThis;
const listeners = {};
window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
const calls = [];
const impl = {
  get_ask_before_sending_options: () => CONFIG.options,
  set_ask_before_sending: (v) => CONFIG.set[v],
};
globalThis.pywebview = {api: new Proxy({}, {get: (_t, name) => (...args) => {
  calls.push(name);
  return Promise.resolve(impl[name] ? impl[name](...args) : {});
}})};
%PAGE%
const settle = () => new Promise(r => realSetTimeout(r, 30));
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const sel = document.getElementById('sel-send'), note = document.getElementById('send-note');
  const snap = () => ({options: sel.children.map(o => [o.value, o.textContent]), value: sel.value,
                       note: [note.textContent, note.className]});
  const before = {calls: calls.filter(c => c.includes('sending')), ...snap()};
  openAPIModal();
  await settle();
  const opened = snap();
  const steps = [];
  for (const v of CONFIG.change) { await sel.onchange({target: {value: v}}); await settle(); steps.push(snap()); }
  console.log(JSON.stringify({before, opened, steps, errors: errors.filter(e => e.includes('[send]')),
                              calls: calls.filter(c => c.includes('sending'))}));
})();
"""


# ============================================================
# ASK BEFORE SENDING RUN PAGE
# ============================================================

def _run_page(tmp_path, change):
    api = SimpleNamespace(memory=_Memory())
    api.ask_before_sending_mode = lambda: Api.ask_before_sending_mode(api)
    options = Api.get_ask_before_sending_options(api)
    sets = {v: Api.set_ask_before_sending(api, v) for v in change}
    script = tmp_path / "page.js"
    script.write_text(_ASK_BEFORE_SENDING_HARNESS.replace("%CONFIG%", json.dumps(
        {"options": options, "set": sets, "change": change})).replace("%PAGE%", _ask_before_sending_script()),
        encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# ============================================================
# ASK BEFORE SENDING TESTS (3)
# ============================================================

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_whole_page_dropdown_renders_python_options_and_saves_ids(tmp_path):
    r = _run_page(tmp_path, ["never", "bogus"])
    assert r["before"]["options"] == [] and r["before"]["calls"] == []   # not loaded at startup
    assert r["opened"]["options"] == [[c, ASK_BEFORE_SENDING_LABELS[c]]
                                      for c in ASK_BEFORE_SENDING_CHOICES]
    assert r["opened"]["value"] == "when_unsure"
    assert r["opened"]["note"] == [ASK_BEFORE_SENDING_HELP["when_unsure"], "card-note"]
    assert r["steps"][0]["note"] == [ASK_BEFORE_SENDING_HELP["never"], "card-note"]
    # A refused id shows Python's error and reloads the saved choice.
    assert r["steps"][1]["note"][1] == "card-note error"
    assert "Choose one of" in r["steps"][1]["note"][0]
    assert r["calls"] == ["get_ask_before_sending_options", "set_ask_before_sending",
                          "set_ask_before_sending", "get_ask_before_sending_options"]
    assert r["errors"] == []


# ============================================================
# REMEMBERED APPROVALS
# ============================================================

# "Always for this person" (quick_message, setting "when_unsure" only): a
# third button on the Yes/No prompt answers yes and remembers the app + person
# as HMAC-SHA256(per-install random key, app|recipient) in user_profile
# ("approval_key", "approved:<hmac>"); no name is stored. A remembered person
# skips the question only when the recipient checks matched EXACTLY. Settings
# shows only a count and a Forget all that removes only those rows.


# ============================================================
# REMEMBERED APPROVALS HELPERS
# ============================================================

REMEMBERED_APPROVALS_CONTACT = "Zorblax Quindlewhip"
REMEMBERED_APPROVALS_MESSAGE = "the pelican lands at noon"
REMEMBERED_APPROVALS_SEND = {"action": "quick_message", "parameter": f"discord|{REMEMBERED_APPROVALS_CONTACT}|{REMEMBERED_APPROVALS_MESSAGE}"}
REMEMBERED_APPROVALS_DONE = {"action": "done", "speech": "ok"}
NOT_GROUNDED = "message my friend about lunch"
REMEMBERED_APPROVALS_CONTENT = ("zorblax", "quindlewhip", "pelican", "noon")
CLICK = ("remember", "button")


@pytest.fixture
def memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "memory.db"))
    m = Memory()
    m.db_file = tmp_path / "memory.db"
    return m


# ============================================================
# REMEMBERED APPROVALS API
# ============================================================

def _remembered_approvals_api(memory, mode="when_unsure", seen=None):
    api = SimpleNamespace(memory=memory)
    memory.set_profile("ask_before_sending", mode)
    for name in ("ask_before_sending_mode", "_approval_id", "remember_send_approval",
                 "is_send_approved", "get_remembered_approvals_count",
                 "forget_remembered_approvals", "_qm_read_name"):
        setattr(api, name, getattr(Api, name).__get__(api))
    return api


# ============================================================
# REMEMBERED APPROVALS RUN
# ============================================================

def _remembered_approvals_run(monkeypatch, memory, replies, answers, *, mode="when_unsure", goal=NOT_GROUNDED,
         seen=None, gen=1, confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop with the real quick_message
    handler. answers: (answer, source) per confirmation, in order."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    seen = list(seen if seen is not None else [REMEMBERED_APPROVALS_CONTACT] * 3)
    rec = SimpleNamespace(calls=[], keys=[], clips=[], offers=[])
    api = _remembered_approvals_api(memory, mode)

    def ask_confirmation(question, input_mode, offer_remember=False):
        rec.offers.append(offer_remember)
        return answers.pop(0) if answers else (None, "typed")

    def run(a, p):
        rec.calls.append((a, p))
        ok = Api._act_quick_message(api, p) if a == "quick_message" else True
        return SimpleNamespace(action=a, parameter=p, success=ok, detail=api.last_result_text,
                               verified=None, escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="")

    api.__dict__.update(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a in confirm),
        executor=SimpleNamespace(run=run), ask_confirmation=ask_confirmation,
        _resolve_path_placeholders=lambda p: p, speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"}, stop_current_task=lambda: None, _last_opened_app="",
        _agent_task=goal, _agent_generation=gen,
        vision=SimpleNamespace(ready=True), screen=SimpleNamespace(mark_action=lambda: None),
        is_app_running=lambda _a: True, switch_to_app=lambda _a: True,
        _foreground_process_matches=lambda _a: True, _get_app_search_hotkey=lambda _a: "ctrl+k",
        set_clipboard=lambda t: rec.clips.append(t) or True,
        _vision_read=lambda q: ((seen.pop(0), True) if seen else ("", False)),
        last_result_text="",
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *_a, json=None, **_k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: rec.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: rec.keys.append("+".join(k)))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", "text", None)
    rec.api = api
    return rec


# ============================================================
# REMEMBERED APPROVALS HELPERS (2)
# ============================================================

def _remembered_approvals_sent(rec):
    return REMEMBERED_APPROVALS_MESSAGE in rec.clips and rec.keys.count("enter") == 2


# ============================================================
# REMEMBERED APPROVALS PROFILE ROWS
# ============================================================

def _profile_rows(memory):
    con = sqlite3.connect(memory.db_file)
    try:
        return con.execute("SELECT key, value FROM user_profile").fetchall()
    finally:
        con.close()


# -- when the button is offered -----------------------------------------------------------


# ============================================================
# REMEMBERED APPROVALS TESTS
# ============================================================

def test_offered_up_front_in_when_unsure(monkeypatch, memory):
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("no", "button")])
    assert rec.offers == [True]


def test_offered_in_the_mid_action_question(monkeypatch, memory):
    close = "Zorblax Quindlewip"
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("yes", "button")],
               goal=f"tell {REMEMBERED_APPROVALS_CONTACT}: {REMEMBERED_APPROVALS_MESSAGE}", seen=[close] * 3)
    assert rec.offers == [True] and _remembered_approvals_sent(rec)


def test_not_offered_in_always(monkeypatch, memory):
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("yes", "button")], mode="always")
    assert rec.offers == [False]


def test_never_offered_for_another_action(monkeypatch, memory):
    close = {"action": "window_close", "parameter": "Discord"}
    rec = _remembered_approvals_run(monkeypatch, memory, [close, REMEMBERED_APPROVALS_DONE], [CLICK, ("no", "button")])
    assert rec.offers == [False, False]
    assert rec.api.get_remembered_approvals_count() == 0


def test_the_page_call_carries_the_offer_only_when_asked():
    sent = []
    api = SimpleNamespace(_answer_lock=threading.Lock(), _answer_event=threading.Event(),
                          _awaiting_text_answer=threading.Event(), _pending_answer=None,
                          _pending_source=None, _window=None, CONFIRM_TEXT_TIMEOUT=0)
    api._notify_js = sent.append
    Api.ask_confirmation(api, "Send it?", "text")
    Api.ask_confirmation(api, "Send it?", "text", offer_remember=True)
    assert sent == ['showConfirmPrompt("Send it?")', "closeConfirmPrompt()",
                    'showConfirmPrompt("Send it?",true)', "closeConfirmPrompt()"]


# -- remembering and using it ---------------------------------------------------------------

def test_a_click_sends_and_remembers_without_storing_a_name(monkeypatch, memory, capsys):
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK])
    assert _remembered_approvals_sent(rec)
    assert rec.api.get_remembered_approvals_count() == 1
    rows = dict(_profile_rows(memory))
    approved = [k for k in rows if k.startswith(APPROVED_PREFIX)]
    assert len(approved) == 1 and len(approved[0]) == len(APPROVED_PREFIX) + 64
    assert len(rows["approval_key"]) == 64
    dump = json.dumps(_profile_rows(memory)).lower()
    assert [w for w in REMEMBERED_APPROVALS_CONTENT + ("discord|",) if w in dump] == []
    assert "[Confirm] approval remembered" in capsys.readouterr().out


def test_a_remembered_person_and_an_exact_match_skips_the_question(monkeypatch, memory, capsys):
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    other = {"action": "quick_message", "parameter": f"Discord|{REMEMBERED_APPROVALS_CONTACT.lower()}|another thing"}
    rec = _remembered_approvals_run(monkeypatch, memory, [other, REMEMBERED_APPROVALS_DONE], [], gen=2)    # a new task, any message
    assert rec.offers == [] and rec.keys.count("enter") == 2
    assert "[Confirm] remembered approval used" in capsys.readouterr().out


def test_a_remembered_person_and_a_close_match_still_asks(monkeypatch, memory):
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    close = "Zorblax Quindlewip"
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("no", "button")], gen=2, seen=[close] * 3)
    assert rec.offers == [True] and REMEMBERED_APPROVALS_MESSAGE not in rec.clips


@pytest.mark.parametrize("param", [f"slack|{REMEMBERED_APPROVALS_CONTACT}|{REMEMBERED_APPROVALS_MESSAGE}", f"discord|Zorblax Other|{REMEMBERED_APPROVALS_MESSAGE}"])
def test_another_app_or_person_asks(monkeypatch, memory, param):
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    rec = _remembered_approvals_run(monkeypatch, memory, [{"action": "quick_message", "parameter": param}, REMEMBERED_APPROVALS_DONE],
               [("no", "button")], gen=2)
    assert rec.offers == [True] and rec.calls == []


def test_a_typed_or_spoken_remember_is_not_a_click(monkeypatch, memory):
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("remember", "typed"), ("remember", "voice")])
    assert rec.result["error"] == "not confirmed"
    assert rec.api.get_remembered_approvals_count() == 0


def test_always_ignores_remembered_approvals(monkeypatch, memory):
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    rec = _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [("yes", "button")], gen=2, mode="always")
    assert rec.offers == [False]


def test_forget_removes_only_the_approvals(monkeypatch, memory):
    for key, value in (("model", "mistral"), ("user_gender", "none"), ("hotkey_discord", "ctrl+k"),
                       ("approvedish", "x"), ("my_approved:1", "y")):
        memory.set_profile(key, value)
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    api = _remembered_approvals_api(memory)
    api.remember_send_approval("slack", "Someone")
    assert api.get_remembered_approvals_count() == 2
    before = {k: v for k, v in _profile_rows(memory) if not k.startswith(APPROVED_PREFIX)}
    assert api.forget_remembered_approvals() == {"success": True, "removed": 2}
    assert api.get_remembered_approvals_count() == 0
    assert dict(_profile_rows(memory)) == before          # every other key untouched
    assert not api.is_send_approved("discord", REMEMBERED_APPROVALS_CONTACT)


def test_matching_is_normalized_and_checking_creates_no_key(memory):
    api = _remembered_approvals_api(memory)
    assert api.is_send_approved("discord", REMEMBERED_APPROVALS_CONTACT) is False
    assert memory.get_profile("approval_key") is None     # a check never creates the key
    assert api.remember_send_approval("Discord", "  zorblax   QUINDLEWHIP. ") is True
    assert api.is_send_approved("discord", REMEMBERED_APPROVALS_CONTACT) is True
    assert api.remember_send_approval("discord", "  ") is False


def test_logs_name_no_person(monkeypatch, memory, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [CLICK], gen=1)
    _remembered_approvals_run(monkeypatch, memory, [REMEMBERED_APPROVALS_SEND, REMEMBERED_APPROVALS_DONE], [], gen=2)
    _remembered_approvals_api(memory).forget_remembered_approvals()
    out = capsys.readouterr()
    low = (out.out + out.err).lower()
    assert [w for w in REMEMBERED_APPROVALS_CONTENT if w in low] == []


# -- the page ------------------------------------------------------------------------------------


# ============================================================
# REMEMBERED APPROVALS HELPERS (3)
# ============================================================

def _remembered_approvals_script():
    return HTML.split("<script>", 1)[1].split("</script>", 1)[0]


# ============================================================
# REMEMBERED APPROVALS TESTS (2)
# ============================================================

def test_page_strings():
    js = _remembered_approvals_script()
    assert "if(offerRemember)buttons.push(['remember','Always for this person','btn-cancel']);" in js
    assert "const offerRemember=arguments[1]===true;" in js
    assert "approvalsNote.textContent='Remembered approvals: '+n;" in js
    assert '<button class="btn-cancel" id="btn-forget-approvals"' in HTML


# ============================================================
# REMEMBERED APPROVALS HELPERS (4)
# ============================================================

_REMEMBERED_APPROVALS_HARNESS = r"""
const CONFIG = %CONFIG%;
const realSetTimeout = setTimeout;
globalThis.setInterval = () => 0;
let timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = () => {};
const errors = [];
console.error = (...a) => errors.push(a.map(x => String(x && x.message || x)).join(' '));
function makeEl(tag) {
  const classes = new Set();
  const el = {tag, children: [], style: {}, dataset: {}, textContent: '', value: '', className: '',
    classList: {add(c) { classes.add(c); }, remove(c) { classes.delete(c); },
      toggle(c, f) { (f === undefined ? !classes.has(c) : f) ? classes.add(c) : classes.delete(c); },
      contains(c) { return classes.has(c); }},
    appendChild(c) { c.parent = el; this.children.push(c); return c; },
    remove() { if (el.parent) el.parent.children = el.parent.children.filter(x => x !== el); },
    focus() {}, setAttribute() {}, addEventListener() {}, querySelector() { return makeEl('x'); },
    set innerHTML(_v) { this.children = []; }, get innerHTML() { return ''; }};
  return el;
}
const els = {};
globalThis.document = {getElementById(id) { return els[id] || (els[id] = makeEl('#' + id)); },
                       createElement(t) { return makeEl(t); }};
globalThis.window = globalThis;
const listeners = {};
window.addEventListener = (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); };
const calls = [];
let count = CONFIG.count;
const impl = {
  get_remembered_approvals_count: () => count,
  forget_remembered_approvals: () => { count = 0; return {success: true, removed: 2}; },
  submit_answer: (...a) => { submitted.push(a); return {success: true}; },
};
const submitted = [];
globalThis.pywebview = {api: new Proxy({}, {get: (_t, name) => (...args) => {
  calls.push(name);
  return Promise.resolve(impl[name] ? impl[name](...args) : {});
}})};
%PAGE%
const settle = () => new Promise(r => realSetTimeout(r, 30));
const buttons = () => {
  const all = document.getElementById('chat').children.flatMap(m => m.children)
    .filter(c => c.className === 'confirm-buttons');
  return all.flatMap(r => r.children.map(b => b.textContent));
};
(async () => {
  for (const fn of listeners['pywebviewready'] || []) fn();
  await settle();
  const out = {};
  showConfirmPrompt('Send it?'); out.plain = buttons(); closeConfirmPrompt();
  showConfirmPrompt('Send it?', 'yes'); out.notTrue = buttons(); closeConfirmPrompt();
  showConfirmPrompt('Send it?', true); out.offered = buttons();
  const row = document.getElementById('chat').children.flatMap(m => m.children)
    .find(c => c.className === 'confirm-buttons');
  await row.children[2].onclick(); await settle();
  out.submitted = submitted.slice(); out.afterClick = buttons();
  openAPIModal(); await settle();
  const note = document.getElementById('approvals-note'), btn = document.getElementById('btn-forget-approvals');
  out.note = note.textContent;
  await btn.onclick(); await settle();
  out.armed = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length];
  await btn.onclick(); await settle();
  out.forgot = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length, note.textContent];
  await btn.onclick(); timers.forEach(f => f()); await settle();
  out.disarmed = [btn.textContent, calls.filter(c => c === 'forget_remembered_approvals').length];
  out.errors = errors.filter(e => e.includes('[approvals]'));
  console.log(JSON.stringify(out));
})();
"""


# ============================================================
# REMEMBERED APPROVALS TESTS (3)
# ============================================================

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_whole_page_third_button_and_forget_all(tmp_path):
    script = tmp_path / "page.js"
    script.write_text(_REMEMBERED_APPROVALS_HARNESS.replace("%CONFIG%", json.dumps({"count": 2}))
                      .replace("%PAGE%", _remembered_approvals_script()), encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["plain"] == ["Yes", "No"] and r["notTrue"] == ["Yes", "No"]
    assert r["offered"] == ["Yes", "No", "Always for this person"]
    assert r["submitted"] == [["remember", "button"]] and r["afterClick"] == []
    assert r["note"] == "Remembered approvals: 2"
    assert r["armed"] == ["Click again to forget all", 0]            # the first click only asks
    assert r["forgot"] == ["Forget all", 1, "Remembered approvals: 0"]
    assert r["disarmed"] == ["Forget all", 1]                        # timed out: nothing forgotten
    assert r["errors"] == []


# ============================================================
# QUICK MESSAGE CHECKS
# ============================================================

# quick_message recipient checks: vision is asked what the switcher / chat
# header shows (never told the expected name or the message), the answer is
# compared locally (config.recipient_match), and the header is read once more
# immediately before the final Enter, which is never pressed unless it matches.


# ============================================================
# QUICK MESSAGE CHECKS HELPERS
# ============================================================

RECIPIENT = "Zorblax Quindlewhip"
QUICK_MESSAGE_CHECKS_MESSAGE = "canary pelican rendezvous"


# ============================================================
# QUICK MESSAGE CHECKS API
# ============================================================

def _quick_message_checks_api(monkeypatch, answers):
    """A stub Api whose vision reads return `answers` in order (a string, or
    None for a failed read). Records keys, clipboard contents and prompts."""
    api = Api.__new__(Api)
    api.vision = SimpleNamespace(ready=True)
    api.screen = SimpleNamespace(marks=0)
    api.screen.mark_action = lambda: setattr(api.screen, "marks", api.screen.marks + 1)
    api.is_app_running = lambda _a: True
    api.switch_to_app = lambda _a: True
    api._get_app_search_hotkey = lambda _a: "ctrl+k"
    api.last_result_text = ""
    api.keys, api.clips, api.prompts = [], [], []
    api.set_clipboard = lambda t: api.clips.append(t) or True
    queue = list(answers)

    def vision_read(q):
        api.prompts.append(q)
        a = queue.pop(0) if queue else None
        return (a, True) if a is not None else ("Could not read anything meaningful.", False)

    api._vision_read = vision_read
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: api.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: api.keys.append("+".join(k)))
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    return api


# ============================================================
# QUICK MESSAGE CHECKS HELPERS (2)
# ============================================================

def _send(api):
    return Api._act_quick_message(api, f"discord|{RECIPIENT}|{QUICK_MESSAGE_CHECKS_MESSAGE}")


# ============================================================
# QUICK MESSAGE CHECKS TESTS
# ============================================================

def test_happy_path_sends_with_three_checks_and_neutral_prompts(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, RECIPIENT, RECIPIENT])
    assert _send(api) is True
    assert api.last_result_text.startswith("Sent to")
    assert api.prompts == [QM_SWITCHER_QUESTION, QM_HEADER_QUESTION, QM_HEADER_QUESTION]
    assert api.clips == [RECIPIENT, QUICK_MESSAGE_CHECKS_MESSAGE]
    assert api.keys.count("enter") == 2          # open the chat, then send
    assert api.keys[-1] == "enter"
    assert api._last_send_exact is True
    assert api.screen.marks == 3                 # each check reads a frame after the keys


def test_no_vision_prompt_contains_the_recipient_or_the_message(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, RECIPIENT, RECIPIENT])
    _send(api)
    for prompt in api.prompts:
        low = prompt.lower()
        assert "expected" not in low
        for word in RECIPIENT.lower().split() + QUICK_MESSAGE_CHECKS_MESSAGE.lower().split():
            assert word not in low, prompt


def test_switcher_mismatch_types_and_presses_nothing(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, ["Zorblax Quindlewhip Fan Club"])
    assert _send(api) is False
    assert "enter" not in api.keys
    assert QUICK_MESSAGE_CHECKS_MESSAGE not in api.clips
    assert api.keys[-1] == "escape"
    assert len(api.prompts) == 1


def test_header_mismatch_types_nothing_and_never_sends(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, "Someone Else"])
    assert _send(api) is False
    assert QUICK_MESSAGE_CHECKS_MESSAGE not in api.clips
    assert api.keys.count("enter") == 1          # only the switcher's Enter
    assert "NOTHING was typed or sent" in api.last_result_text


def test_final_check_mismatch_never_presses_enter(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, RECIPIENT, "Someone Else"])
    assert _send(api) is False
    assert api.keys.count("enter") == 1
    assert api.keys[-1] == "ctrl+v"              # the paste was the last key; no Enter after it
    assert api.last_result_text.startswith(QM_UNCONFIRMED_REASON)
    assert "Zorblax" not in api.last_result_text and "Someone" not in api.last_result_text
    assert len(api.prompts) == 3                 # a mismatch is not retried


def test_final_check_unreadable_twice_fails_closed(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, RECIPIENT, None, "NOT VISIBLE ON SCREEN"])
    assert _send(api) is False
    assert api.keys.count("enter") == 1
    assert len(api.prompts) == 4
    assert api.last_result_text.startswith(QM_UNCONFIRMED_REASON)


def test_final_check_unreadable_once_then_readable_sends(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, RECIPIENT, None, RECIPIENT])
    assert _send(api) is True
    assert api.keys.count("enter") == 2
    assert len(api.prompts) == 4


def test_unreadable_header_before_typing_is_not_retried(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, [RECIPIENT, None, RECIPIENT])
    assert _send(api) is False
    assert QUICK_MESSAGE_CHECKS_MESSAGE not in api.clips
    assert len(api.prompts) == 2


def test_a_close_match_sends_but_is_not_exact(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, ["Zorblax Quindlewhip.", "Zorblax Quindlewip", "Zorblax Quindlewip"])
    assert _send(api) is True
    assert api._last_send_exact is False


def test_quotes_and_case_still_exact(monkeypatch):
    api = _quick_message_checks_api(monkeypatch, ['"zorblax quindlewhip"', "ZORBLAX  QUINDLEWHIP", RECIPIENT])
    assert _send(api) is True
    assert api._last_send_exact is True


@pytest.mark.parametrize("seen, expected, matched, exact", [
    ("Anna Bell", "anna bell", True, True),
    ("anna-bell!", "Anna Bell", True, False),    # punctuation removed, then compact-equal
    ("AnnaBell", "Anna Bell", True, False),      # compact-equal, not exact
    ("Jonathon Smith", "Jonathan Smith", True, False),   # ratio 0.92, long enough
    ("Kirsten", "Kristen", True, False),         # ratio 0.857 >= 0.85
    ("Markus", "Marcus", False, False),          # ratio 0.833 < 0.85
    ("Michelle", "Michael", False, False),       # ratio 0.80
    ("Anne", "Anna", False, False),              # compact length 4 < 5: no fuzzy accept
    ("Ann", "Anna", False, False),
    ("", "Anna", False, False),
    ("Anna", "", False, False),
    ("José", "jose", False, False),             # accents are letters, not punctuation; short
    ("Zoë Kravitz", "zoë kravitz", True, True),
])
def test_recipient_match_thresholds(seen, expected, matched, exact):
    assert recipient_match(seen, expected) == (matched, exact)


def test_logs_name_no_person_and_no_message(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    for answers in ([RECIPIENT, RECIPIENT, RECIPIENT], [RECIPIENT, RECIPIENT, "Zorblax Q"],
                    [RECIPIENT, "Quindlewhip Fan"], [RECIPIENT, RECIPIENT, None, None]):
        _send(_quick_message_checks_api(monkeypatch, answers))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "[QuickMessage] final check: exact" in text
    assert "[QuickMessage] final check failed; Enter not pressed" in text
    for word in ("Zorblax", "Quindlewhip", "pelican", "rendezvous"):
        assert word not in text


# ============================================================
# OWN DIALOG KEYS
# ============================================================

# Enter, Escape and Delete pressed inside the file dialog the agent's own
# dialog key opened (same process, the dialog window class) are part of that
# flow; everywhere else the confirmation gate is exactly as before.


# ============================================================
# OWN DIALOG KEYS HELPERS
# ============================================================

# hwnd -> (pid, class, thread id): Notepad, its Save As dialog, the replace
# prompt, a window of another app, a non-dialog window of Notepad.
_OWN_DIALOG_WINDOWS = {1: (50, "Notepad", 500), 7: (50, "#32770", 500),
                       9: (50, "#32770", 500), 30: (77, "#32770", 900),
                       11: (50, "NotepadPopup", 500)}
_DOC, _SAVE_AS = (1, "*milk - Notepad"), (7, "Save as")


def _own_dialog_run(monkeypatch, tmp_path, keys, foreground, answers=(), focus_class="Edit",
                    enter_safe=False):
    """hotkey ``keys``, then press_key Enter, Delete, Escape, done."""
    from core.agent import AgentLoop
    from test_agent import _batched_steps_run

    monkeypatch.setattr(AgentLoop, "_window_facts",
                        staticmethod(lambda h: _OWN_DIALOG_WINDOWS.get(h, (0, "", 0))))
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetGUIThreadInfo=lambda thread: (0, 7, 70, 0, 0, 0, 0, (0, 0, 0, 0)),
        GetClassName=lambda h: focus_class if h == 70 else ""))
    replies = [{"action": "hotkey", "parameter": keys},
               {"action": "press_key", "parameter": "enter"},
               {"action": "press_key", "parameter": "delete"},
               {"action": "press_key", "parameter": "escape"},
               {"action": "done", "parameter": "", "speech": "Saved."},
               {"action": "done", "parameter": "", "speech": "Saved."}]
    return _batched_steps_run(monkeypatch, tmp_path, replies, answers=answers,
                              foreground=foreground, enter_safe=enter_safe)


# ============================================================
# OWN DIALOG KEYS TESTS
# ============================================================

def test_keys_inside_the_dialog_the_agent_opened_are_not_confirmed(monkeypatch, tmp_path, capsys):
    rec = _own_dialog_run(monkeypatch, tmp_path, "ctrl+s", [_DOC, _SAVE_AS])
    assert rec.asked == []                                         # no question at all
    assert rec.calls == [("hotkey", "ctrl+s"), ("press_key", "enter"),
                         ("press_key", "delete"), ("press_key", "escape")]
    log = capsys.readouterr().out
    assert "[DialogKeys] the dialog this phase opened is in front" in log
    assert log.count("[Confirm] not asked: a key inside the dialog this phase opened") == 2
    assert "Save as" not in log and "milk" not in log              # content-free


def test_delete_in_the_dialogs_file_list_still_asks(monkeypatch, tmp_path):
    rec = _own_dialog_run(monkeypatch, tmp_path, "ctrl+s", [_DOC, _SAVE_AS],
                          answers=["yes"], focus_class="DirectUIHWND")
    assert len(rec.asked) == 1 and "press_key(delete)" in rec.asked[0]


@pytest.mark.parametrize("keys, foreground, why", [
    ("ctrl+a", [_DOC], "no dialog key"),                           # nothing opened
    ("ctrl+s", [_DOC], "no dialog appeared"),
    ("ctrl+s", [_DOC, (30, "Other")], "another process"),
    ("ctrl+s", [_DOC, (11, "Popup")], "not a dialog window"),
    ("ctrl+p", [_DOC, (7, "Print")], "not a file dialog key"),     # Enter would print
])
def test_enter_and_delete_still_ask_everywhere_else(monkeypatch, tmp_path, capsys, keys,
                                                    foreground, why):
    rec = _own_dialog_run(monkeypatch, tmp_path, keys, foreground, answers=["yes", "yes"])
    assert len(rec.asked) == 2, why                                # Enter and Delete, as before
    assert "press_key(enter)" in rec.asked[0] and "press_key(delete)" in rec.asked[1]
    assert "[Confirm] not asked" not in capsys.readouterr().out


def test_the_replace_prompt_inside_the_flow_still_asks(monkeypatch, tmp_path):
    # The dialog opened, then the replace prompt (another hwnd) came to the front.
    rec = _own_dialog_run(monkeypatch, tmp_path, "ctrl+s",
                          [_DOC, _SAVE_AS, _SAVE_AS, _SAVE_AS, (9, "Confirm Save As")],
                          answers=["yes", "yes"])
    assert len(rec.asked) == 2 and "press_key(enter)" in rec.asked[0]


def test_enter_safe_still_skips_the_question_as_before(monkeypatch, tmp_path):
    rec = _own_dialog_run(monkeypatch, tmp_path, "ctrl+a", [_DOC], answers=["yes"],
                          enter_safe=True)
    assert len(rec.asked) == 1 and "press_key(delete)" in rec.asked[0]   # Delete still asks


def test_the_registry_skips_only_press_key_with_the_dialog_flag(tmp_path):
    reg = ToolRegistry(db_path=str(tmp_path / "t.db"))
    try:
        for key in ("enter", "return", "delete"):
            assert reg.requires_confirmation("press_key", key)
            assert not reg.requires_confirmation("press_key", key, dialog_key_safe=True)
            assert reg.requires_confirmation("browser_press_key", key, dialog_key_safe=True)
        assert not reg.requires_confirmation("press_key", "escape")
        assert reg.requires_confirmation("hotkey", "alt+f4", dialog_key_safe=True) == \
            reg.requires_confirmation("hotkey", "alt+f4")
        assert reg.requires_confirmation("window_close", "x", dialog_key_safe=True)
    finally:
        reg._conn and reg._conn.close()


def test_ctrl_a_then_type_text_never_asks(monkeypatch, tmp_path):
    """Replacing an editor's text: hotkey ctrl+a, then type_text over the
    selection -- neither step asks, in the agent's own document or with no
    known target. press_key Delete still would."""
    from test_agent import _batched_steps_run

    for opened in (True, False):
        replies = ([{"action": "switch_app", "parameter": "notepad"}] if opened else []) + [
            {"action": "hotkey", "parameter": "ctrl+a"},
            {"action": "type_text", "parameter": "call mom at 6"},
            {"action": "done", "parameter": "", "speech": "Changed it."}]
        rec = _batched_steps_run(monkeypatch, tmp_path, replies, foreground=[_DOC])
        assert rec.asked == []
        assert rec.calls[-2:] == [("hotkey", "ctrl+a"), ("type_text", "call mom at 6")]
    reg = ToolRegistry(db_path=str(tmp_path / "t.db"))
    try:
        assert not reg.requires_confirmation("hotkey", "ctrl+a")
        assert not reg.requires_confirmation("type_text", "call mom at 6")
        assert reg.requires_confirmation("press_key", "delete")
    finally:
        reg._conn and reg._conn.close()


def _save_left_open_run(monkeypatch, tmp_path, foreground, answers=(), focus_class="Edit"):
    """save_file_as stops with its dialog still open (fails), then press_key
    Enter (which saves the file) and Delete, then done."""
    from core.agent import AgentLoop
    from test_agent import _batched_steps_run

    path = str(tmp_path / "reminder.txt")
    monkeypatch.setattr(AgentLoop, "_window_facts",
                        staticmethod(lambda h: _OWN_DIALOG_WINDOWS.get(h, (0, "", 0))))
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetGUIThreadInfo=lambda thread: (0, 7, 70, 0, 0, 0, 0, (0, 0, 0, 0)),
        GetClassName=lambda h: focus_class if h == 70 else ""))

    def on_run(api, a, p):
        if (a, p) == ("press_key", "enter"):
            open(path, "w").close()

    replies = [{"action": "save_file_as", "parameter": path},
               {"action": "press_key", "parameter": "enter"},
               {"action": "press_key", "parameter": "delete"},
               {"action": "done", "parameter": "", "speech": "Saved."},
               {"action": "done", "parameter": "", "speech": "Saved."}]
    return _batched_steps_run(monkeypatch, tmp_path, replies, answers=answers,
                              foreground=foreground, fail=(("save_file_as", path),),
                              on_run=on_run)


def test_a_dialog_save_file_as_left_open_counts_as_the_agents_own(monkeypatch, tmp_path, capsys):
    # Real rt-notepad-edit runs: save_file_as stopped, then Enter in its dialog asked.
    rec = _save_left_open_run(monkeypatch, tmp_path, [_DOC, _SAVE_AS])
    assert rec.asked == []
    assert [c[0] for c in rec.calls] == ["save_file_as", "press_key", "press_key"]
    log = capsys.readouterr().out
    assert "[DialogKeys] the dialog this phase opened is in front" in log
    assert log.count("[Confirm] not asked: a key inside the dialog this phase opened") == 2


def test_after_save_file_as_the_file_list_and_replace_prompt_still_ask(monkeypatch, tmp_path):
    rec = _save_left_open_run(monkeypatch, tmp_path, [_DOC, _SAVE_AS], answers=["yes"],
                              focus_class="DirectUIHWND")
    assert len(rec.asked) == 1 and "press_key(delete)" in rec.asked[0]
    (tmp_path / "reminder.txt").unlink()        # else save_file_as itself asks (replace)
    # The replace prompt (another window) is in front for Enter and Delete.
    rec = _save_left_open_run(monkeypatch, tmp_path,
                              [_DOC, _SAVE_AS, _SAVE_AS, (9, "Confirm Save As")],
                              answers=["yes", "yes"])
    assert len(rec.asked) == 2 and "press_key(enter)" in rec.asked[0]


def test_a_save_file_as_that_closed_its_dialog_leaves_nothing_open(monkeypatch, tmp_path, capsys):
    # Saved: the document is in front again, so no dialog counts; Enter asks.
    rec = _save_left_open_run(monkeypatch, tmp_path, [_DOC], answers=["yes", "yes"])
    assert len(rec.asked) == 2
    assert "[DialogKeys]" not in capsys.readouterr().out


# ============================================================
# DECLINE GUARD
# ============================================================

def test_a_declined_confirmation_is_not_asked_again_even_reworded(monkeypatch):
    close = {"action": "window_close", "parameter": "Notepad - notes.txt"}
    again = {"action": "window_close", "parameter": "notepad -  Notes.txt."}   # a light rewording
    rec = _confirmation_answers_run(monkeypatch, [close, again, CONFIRMATION_ANSWERS_DONE], ["no"],
                                    mode="text")
    assert len(rec.asked) == 1 and rec.calls == []                 # never asked twice, never ran
    assert any("REFUSED: the user already declined 'window_close'" in o for o in _observations(rec))
    assert rec.result["success"] is True                           # the model's own done


def test_a_declined_ask_user_question_is_not_asked_again(monkeypatch):
    from core.agent import ASK_DECLINED_NOTE, ASK_REPEAT_REFUSAL

    ask = {"action": "ask_user", "parameter": "Should I delete the old drafts folder?"}
    reworded = {"action": "ask_user", "parameter": "Should I delete the old drafts folder now?"}
    rec = _confirmation_answers_run(monkeypatch, [ask, reworded, CONFIRMATION_ANSWERS_DONE],
                                    ["no thanks"], mode="text")
    assert rec.asked == ["Should I delete the old drafts folder?"]  # the rewording never reached the user
    sent = [m["content"] for m in rec.requests[-1]["messages"] if m.get("role") == "user"]
    assert ASK_DECLINED_NOTE in sent and f"[OBSERVATION] {ASK_REPEAT_REFUSAL}" in sent
    assert rec.result["success"] is True


def test_two_declines_end_the_run_with_an_honest_report(monkeypatch, capsys):
    from core.agent import declined_twice_speech

    ask = {"action": "ask_user", "parameter": "Want me to clear the downloads folder?"}
    close = {"action": "window_close", "parameter": "Calculator"}
    rec = _confirmation_answers_run(monkeypatch, [ask, close, CONFIRMATION_ANSWERS_DONE],
                                    ["no", "no"], mode="text")
    assert rec.calls == [] and len(rec.asked) == 2
    assert len(rec.requests) == 2                                   # no model call after the 2nd no
    assert rec.result["success"] is False
    assert rec.result["error"] == "User declined the requested action."
    assert rec.result["speech"] == declined_twice_speech(False)
    assert "didn't do what you declined" in rec.result["speech"]
    assert "[Confirm] declined twice; the phase ends" in capsys.readouterr().out
    assert declined_twice_speech(True).endswith("What I did before that stays as it was.")


def test_an_answer_that_says_no_and_more_is_an_answer_not_a_decline():
    from core.agent import is_decline_answer

    for no in ("no", "Nope.", "no thanks", "No, don't do that", "never mind", "cancel"):
        assert is_decline_answer(no), no
    for answer in ("no, use Documents", "not sure, maybe the Downloads one", "yes",
                   "the blue one", "", None):
        assert not is_decline_answer(answer), answer
