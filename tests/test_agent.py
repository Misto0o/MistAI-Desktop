# ============================================================
# AGENT LOOP TESTS
# ============================================================
#
# The agent loop: safety gates, repeats and the done check, loop cycles, batched steps, network failures, prompt rules, size and action playbook.
#
# SECTION INDEX
#   1. INPUT REPEAT AND DONE CHECK
#   2. INPUT REPEAT AND DONE CHECK HELPERS
#   3. INPUT REPEAT AND DONE CHECK RUN
#   4. INPUT REPEAT AND DONE CHECK HELPERS (2)
#   5. INPUT REPEAT AND DONE CHECK TESTS
#   6. AGENT LOOP SAFETY
#   7. AGENT LOOP SAFETY RESPONSE
#   8. AGENT LOOP SAFETY EXECUTOR
#   9. AGENT LOOP SAFETY TOOLS
#   10. AGENT LOOP SAFETY FAKE API
#   11. AGENT LOOP SAFETY RUN LOOP
#   12. AGENT LOOP SAFETY TESTS
#   13. LOOP CYCLES
#   14. LOOP CYCLES HELPERS
#   15. LOOP CYCLES RUN
#   16. LOOP CYCLES HELPERS (2)
#   17. LOOP CYCLES TESTS
#   18. BATCHED STEPS
#   19. BATCHED STEPS HELPERS
#   20. BATCHED STEPS RUN
#   21. BATCHED STEPS HELPERS (2)
#   22. BATCHED STEPS TESTS
#   23. NETWORK FAILURE
#   24. NETWORK FAILURE HELPERS
#   25. NETWORK FAILURE LOOP
#   26. NETWORK FAILURE HELPERS (2)
#   27. NETWORK FAILURE TESTS
#   28. GROUNDING RULES
#   29. GROUNDING RULES SYSTEM
#   30. GROUNDING RULES TESTS
#   31. GROUNDING RULES HELPERS
#   32. GROUNDING RULES TESTS (2)
#   33. GROUNDING RULES VISION API
#   34. GROUNDING RULES TESTS (3)
#   35. SYSTEM PROMPT SIZE
#   36. SYSTEM PROMPT SIZE HELPERS
#   37. SYSTEM PROMPT SIZE BUILD
#   38. SYSTEM PROMPT SIZE HELPERS (2)
#   39. SYSTEM PROMPT SIZE TESTS
#   40. SYSTEM PROMPT SIZE HELPERS (3)
#   41. SYSTEM PROMPT SIZE TESTS (2)
#   42. ACTION PLAYBOOK
#   43. ACTION PLAYBOOK HELPERS
#   44. ACTION PLAYBOOK TESTS
#   45. ACTION PLAYBOOK HELPERS (2)
#   46. ACTION PLAYBOOK ROUTER
#   47. ACTION PLAYBOOK PLANS
#   48. ACTION PLAYBOOK TESTS (2)
#   49. SKIP VERIFIED READ
#   50. SKIP VERIFIED READ RUN
#   51. SKIP VERIFIED READ TESTS
# ============================================================

"""The agent loop: safety gates, repeats and the done check, loop cycles, batched steps, network failures, prompt rules, size and action playbook."""

import json
import threading
from types import SimpleNamespace
import hashlib
import inspect
import re

import pytest
import requests

from core.agent import (
    EMPTY_ANSWER_READS,
    NO_ANSWER_SPEECH,
    AgentLoop,
    LOOP_NOTE,
    LOOP_SPEECH,
    CycleGuard,
    NETWORK_FAILURE_SPEECH,
    BACKEND_SYSTEM_LIMIT,
)
from core.tools import SPECS, ToolRegistry, action_entry, action_menu
from confirm_helpers import bind_confirmation
from core import agent as agent_mod, planner
from core.api import Api


# ============================================================
# INPUT REPEAT AND DONE CHECK
# ============================================================

# AgentLoop: identical-input cap, repeat wording, and the done-time check.


# ============================================================
# INPUT REPEAT AND DONE CHECK HELPERS
# ============================================================

def _result(action, detail="", success=True, escalated_from=""):
    return SimpleNamespace(
        action=action, parameter="", success=success, detail=detail, verified=None,
        escalate_to_user=False, reliability_note=lambda: "", escalated_from=escalated_from,
    )


# ============================================================
# INPUT REPEAT AND DONE CHECK RUN
# ============================================================

def _input_repeat_and_done_check_run(monkeypatch, replies, results=None, foreground=None, task="task"):
    """Run one phase. ``results`` maps action -> ExecutionResult-like (or a
    callable(param)); ``foreground`` is an iterator of foreground keys."""
    results = results or {}
    replies = iter(json.dumps(r) for r in replies)
    calls, sent, spoken = [], [], []

    def run(action, param):
        calls.append((action, param))
        r = results.get(action, _result(action))
        r = r(param) if callable(r) else r
        r.parameter = r.parameter or param  # the real Executor reports what it ran
        return r

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=spoken.append,
        _last_opened_app="leftover from an earlier phase",
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    fg = foreground or iter(lambda: (1, "Calculator"), None)
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: next(fg))
    out = loop._loop(task, "m", "none", "text", None)
    observations = [m["content"] for msgs in sent[1:] for m in msgs[-2:-1]]
    return out, api, calls, observations, spoken


# ============================================================
# INPUT REPEAT AND DONE CHECK HELPERS (2)
# ============================================================

INPUT_REPEAT_AND_DONE_CHECK_DONE = {"action": "done", "speech": "It shows 23."}


# -- identical input ------------------------------------------------------------------


# ============================================================
# INPUT REPEAT AND DONE CHECK TESTS
# ============================================================

def test_identical_inputs_both_run_and_get_a_neutral_note(monkeypatch):
    out, _, calls, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ])
    assert out["success"]
    assert calls == [("press_key", "3"), ("press_key", "3")]
    assert "repeat it only if you mean to" in obs[1]
    assert "REFUSED" not in " ".join(obs)
    assert "did take effect" not in " ".join(obs)


def test_identical_input_is_refused_after_three_in_a_row_on_an_unchanged_window(monkeypatch):
    out, _, calls, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ])
    assert calls == [("click_on_text", "3")] * 3
    assert "already ran 3 times in a row" in obs[3]


def test_a_window_change_or_another_action_resets_the_input_count(monkeypatch):
    keys = iter([(1, "A"), (1, "A"), (2, "B"), (2, "B"), (2, "B"), (2, "B")])
    _, _, calls, _, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},   # foreground changed: count restarts
        {"action": "press_key", "parameter": "3"},
        INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ], foreground=keys)
    assert calls == [("press_key", "3")] * 4

    _, _, calls, _, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "+"},
        {"action": "press_key", "parameter": "3"},
        INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ])
    assert calls[-1] == ("press_key", "3") and len(calls) == 5


def test_non_input_side_effects_are_still_refused_on_repeat(monkeypatch):
    _, _, calls, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "open_app", "parameter": "calculator"},
        {"action": "open_app", "parameter": "calculator"},
        INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ])
    assert calls == [("open_app", "calculator")]
    assert "already ran without error" in obs[1]


def test_input_target_is_reset_at_phase_start(monkeypatch):
    _, api, _, _, _ = _input_repeat_and_done_check_run(monkeypatch, [INPUT_REPEAT_AND_DONE_CHECK_DONE])
    assert api._last_opened_app is None


# -- done-time check --------------------------------------------------------------------


def test_plain_phase_finishes_on_the_first_done(monkeypatch):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [{"action": "press_key", "parameter": "3"}, INPUT_REPEAT_AND_DONE_CHECK_DONE])
    assert out["success"] and spoken == ["It shows 23."]
    assert not any("Before finishing" in o for o in obs)


def test_screen_read_gets_one_check_when_the_speech_states_unseen_numbers(monkeypatch):
    read = ("Vision: 851\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): "
            "Display is 23")
    read += " | x" * 200
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "what number?"},
        {"action": "done", "speech": "It shows 851."},
        {"action": "done", "speech": "It shows 23."},
    ], results={"read_screen_text": _result("read_screen_text", read)})
    check = obs[-1]
    assert check.startswith("[OBSERVATION] Before finishing")
    assert "The last screen read returned: \"Vision: 851" in check
    assert "Display is 23" in check and "[truncated," in check
    # Vision's 851 does not count: only the EXACT UI TEXT does.
    assert "Your draft speech states 851, which that read does not show." in check
    assert "continue and finish it" in check and "failure report only if you cannot" in check
    assert "never a value you worked out yourself" in check
    # The closing line always leaves an honest-failure way out.
    assert check.endswith("Then call done again with the corrected (or unchanged) speech, "
                          "or with an honest report of what you could not do.")
    assert out["success"] and out["speech"] == "It shows 23."
    assert spoken == ["It shows 23."]  # the first done was never spoken


def test_speech_that_matches_the_read_costs_no_extra_call(monkeypatch):
    read = ("Vision: 851\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): "
            "Display is 851 | Expression is 37 × 23=")
    out, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "type_text", "parameter": "37*23"},
        {"action": "press_key", "parameter": "enter"},
        {"action": "read_screen_text", "parameter": "what number?"},
        {"action": "done", "speech": "37 times 23 is 851."},
    ], results={"read_screen_text": _result("read_screen_text", read)})
    assert out["success"] and out["speech"] == "37 times 23 is 851."
    assert not any("Before finishing" in o for o in obs)


def test_unsubmitted_typing_gets_23_then_submits_and_gets_851(monkeypatch):
    """The real failure: '37*23' typed without submitting reads back 23. The
    speech matches the read, but the read followed unsubmitted typing, so
    the check tells the model to finish the task; it presses Enter, reads
    851 and finishes with that."""
    reads = iter([
        "Vision: 23\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): "
        "Display is 23 | Expression is 37 ×",
        "Vision: 851\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): "
        "Display is 851 | Expression is 37 × 23=",
    ])
    out, _, calls, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "open_app", "parameter": "calculator"},
        {"action": "type_text", "parameter": "37*23"},
        {"action": "read_screen_text", "parameter": "what number is shown?"},
        {"action": "done", "speech": "Calculator shows 23."},
        {"action": "press_key", "parameter": "enter"},
        {"action": "read_screen_text", "parameter": "what number is shown?"},
        {"action": "done", "speech": "Calculator shows 851."},
    ], results={"read_screen_text": lambda _p: _result("read_screen_text", next(reads))})
    check = next(o for o in obs if "Before finishing" in o)
    assert "never submitted (no Enter or submit key after it)" in check
    assert "pending expression or unsubmitted input), continue and finish it" in check
    assert [c[0] for c in calls] == ["open_app", "type_text", "read_screen_text",
                                     "press_key", "read_screen_text"]
    assert out["speech"] == "Calculator shows 851." and spoken == ["Calculator shows 851."]


@pytest.mark.parametrize("read_action", ["read_screen_text", "analyze_screen"])
def test_a_read_after_unsubmitted_typing_says_so_in_its_own_observation(monkeypatch,
                                                                         read_action):
    from core.agent import UNSUBMITTED_READ_NOTE

    _, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "type_text", "parameter": "37*23"},
        {"action": read_action, "parameter": "what is shown?"},
        {"action": "press_key", "parameter": "enter"},
        {"action": read_action, "parameter": "what is shown?"},
        {"action": "done", "speech": "Done."},
    ], results={read_action: _result(read_action, "Display is 23")})
    assert UNSUBMITTED_READ_NOTE in obs[1]
    assert UNSUBMITTED_READ_NOTE not in obs[3]       # Enter submitted it
    assert UNSUBMITTED_READ_NOTE not in obs[0]       # only reads carry it


def test_typing_that_ends_in_a_newline_counts_as_submitted(monkeypatch):
    read = "EXACT UI TEXT of 'Terminal' (accessibility tree, exact): 42"
    out, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "type_text", "parameter": "echo 42\n"},
        {"action": "read_screen_text", "parameter": "output?"},
        {"action": "done", "speech": "It printed 42."},
    ], results={"read_screen_text": _result("read_screen_text", read)})
    assert out["speech"] == "It printed 42."
    assert not any("Before finishing" in o for o in obs)


def test_the_second_done_always_finishes(monkeypatch):
    out, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "q"},
        {"action": "done", "speech": "It is 5."},
        {"action": "read_screen_text", "parameter": "q2"},
        {"action": "done", "speech": "It is 7."},
    ], results={"read_screen_text": _result("read_screen_text", "Display is 23")})
    assert out["speech"] == "It is 7."
    assert sum("Before finishing" in o for o in obs) == 1


@pytest.mark.parametrize("task", ["whats the weather?", "is Edge open"])
def test_question_finished_with_empty_speech_gets_one_reminder(monkeypatch, task):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "browser_get_text", "parameter": ""},
        {"action": "done", "speech": ""},
        {"action": "done", "speech": "It is 18 degrees and sunny."},
    ], results={"browser_get_text": _result("browser_get_text", "18 degrees, sunny")},
        task=task)
    assert out["success"] and spoken == ["It is 18 degrees and sunny."]
    assert sum("Before finishing" in o for o in obs) == 1
    assert "Your speech is empty but the user asked a question" in obs[-1]


def test_question_second_empty_done_ends_with_the_honest_line(monkeypatch):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "done", "speech": ""},
        {"action": "done", "speech": ""},
    ], task="whats the weather?")
    assert out == {"success": False, "error": "no answer",
                   "speech": NO_ANSWER_SPEECH, "steps": out["steps"]}
    assert spoken == ["I couldn't read the answer."]          # never silence
    assert sum("Before finishing" in o for o in obs) == 1     # no extra model call


@pytest.mark.parametrize("last", ["open_app", "type_text", "run_command"])
def test_an_action_task_not_ending_on_a_read_finishes_silently(monkeypatch, last):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "browser_get_text", "parameter": ""},
        {"action": last, "parameter": "notepad"},
        {"action": "done", "speech": ""},
    ], task="open notepad")
    assert out["success"] and out["speech"] == "" and spoken == []
    assert not any("Before finishing" in o for o in obs)


@pytest.mark.parametrize("read", sorted(EMPTY_ANSWER_READS))
def test_an_empty_done_right_after_a_read_gets_a_reminder(monkeypatch, read):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": read, "parameter": ""},
        {"action": "done", "speech": ""},
        {"action": "done", "speech": "It shows 18 degrees."},
    ], results={read: _result(read, "18 degrees")}, task="open notepad")
    assert out["success"] and spoken == ["It shows 18 degrees."]
    assert sum("Before finishing" in o for o in obs) == 1
    assert "Your speech is empty right after a read" in obs[-1]


def test_contiune_a_read_then_unseen_numbers_then_an_empty_done(monkeypatch, capsys):
    """The real run: a follow-up "contiune", a screen read, a draft that states
    numbers the read doesn't show (the one reminder), then an empty done."""
    out, _, calls, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "weather"},
        {"action": "done", "speech": "It is 49 degrees with a high of 72 and 5 mph wind."},
        {"action": "done", "speech": ""},
    ], results={"read_screen_text": _result("read_screen_text", "Weather: partly cloudy")},
        task="contiune")
    assert calls == [("read_screen_text", "weather")]
    assert sum("Before finishing" in o for o in obs) == 1
    assert "Your draft speech states 49, 72, 5" in obs[-1]
    assert out["success"] is False and out["error"] == "no answer"
    assert spoken == ["I couldn't read the answer."]
    assert "[DoneCheck] Still no answer; finishing with an honest line" in capsys.readouterr().out


def test_a_create_file_command_ending_with_an_empty_done_stays_silent(monkeypatch):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "run_command", "parameter": "New-Item -ItemType File notes.txt"},
        {"action": "done", "speech": ""},
    ], results={"run_command": _result(
        "run_command", "COMMAND COMPLETED SUCCESSFULLY (exit code 0).")},
        task="create a file called notes.txt")
    assert out["success"] and out["speech"] == "" and spoken == []
    assert not any("Before finishing" in o for o in obs)


def test_run_command_counts_when_the_goal_is_a_question(monkeypatch):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "run_command", "parameter": "Get-Process notepad"},
        {"action": "done", "speech": ""},
        {"action": "done", "speech": ""},
    ], task="is notepad running?")
    assert out["error"] == "no answer" and spoken == [NO_ANSWER_SPEECH]


def test_a_speech_that_matches_its_read_costs_no_extra_call(monkeypatch):
    out, _, _, obs, spoken = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "read_screen_text", "parameter": ""},
        {"action": "done", "speech": "It is 18 degrees."},
    ], results={"read_screen_text": _result("read_screen_text", "Temperature 18")},
        task="contiune")
    assert out["success"] and spoken == ["It is 18 degrees."]
    assert len(obs) == 1                  # one model call after the read, no reminder


def test_the_step_log_shows_the_done_speech_as_a_word_count_only(monkeypatch, capsys):
    _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "read_screen_text", "parameter": ""},
        {"action": "done", "speech": "It is 18 degrees outside."},
    ], results={"read_screen_text": _result("read_screen_text", "18")}, task="contiune")
    out = capsys.readouterr().out
    assert "Step 2: done('') speech: 5 words" in out
    assert "degrees" not in out and "outside" not in out


def test_off_target_input_is_named_in_the_check(monkeypatch):
    refused = _result("type_text", "NOT SENT: the text was not sent because calculator is "
                                   "not in front ('main.py - VS Code' is).", success=False)
    out, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": "type_text", "parameter": "37*23="},
        INPUT_REPEAT_AND_DONE_CHECK_DONE, INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ], results={"type_text": refused})
    check = obs[-1]
    assert "These steps did not go as asked: type_text was not sent" in check
    assert "say so in the speech" in check
    assert "screen read" not in check


@pytest.mark.parametrize("escalated_from, executed, fires", [
    ("click_on_text", "run_command", True),   # UI -> command: a different route
    ("switch_app", "open_app", False),        # same category: not a route change
    ("uia_click", "click_on_text", False),
])
def test_fallbacks_fire_the_check_only_across_categories(monkeypatch, escalated_from, executed,
                                                         fires):
    out, _, _, obs, _ = _input_repeat_and_done_check_run(monkeypatch, [
        {"action": escalated_from, "parameter": "x"}, INPUT_REPEAT_AND_DONE_CHECK_DONE, INPUT_REPEAT_AND_DONE_CHECK_DONE,
    ], results={escalated_from: _result(executed, "ok", escalated_from=escalated_from)})
    assert out["success"]
    fired = any("Before finishing" in o for o in obs)
    assert fired is fires
    if fires:
        assert f"{executed} ran as an automatic fallback for {escalated_from}" in obs[-1]


# ============================================================
# AGENT LOOP SAFETY
# ============================================================

# Small, hardware-free checks for AgentLoop parsing and confirmation gates.


# ============================================================
# AGENT LOOP SAFETY RESPONSE
# ============================================================

class _Response:
    def __init__(self, response="", status_code=200):
        self.ok = status_code < 400
        self.status_code = status_code
        self._response = response

    def json(self):
        return {"response": self._response}


# ============================================================
# AGENT LOOP SAFETY EXECUTOR
# ============================================================

class _Executor:
    def __init__(self):
        self.calls = []

    def run(self, action, parameter):
        self.calls.append((action, parameter))
        return SimpleNamespace(
            success=True,
            detail="",
            verified=True,
            escalate_to_user=False,
            reliability_note=lambda: "",
        )


# ============================================================
# AGENT LOOP SAFETY TOOLS
# ============================================================

class _Tools:
    """Enough registry surface to exercise AgentLoop without desktop tools."""

    specs = {
        "quick_message": object(),
        "window_close": object(),
        "browser_search": object(),
    }

    def requires_confirmation(self, action, parameter="", resolve_path=None, enter_safe=False):
        return action in {"quick_message", "window_close"}

    def get(self, action):
        category = "messaging" if action == "quick_message" else "window"
        return SimpleNamespace(categories=(category,))


# ============================================================
# AGENT LOOP SAFETY FAKE API
# ============================================================

class _FakeApi:
    def __init__(self, answers=()):
        self.task_memory = SimpleNamespace(get_hints_for_task=lambda _task: [])
        self.caption_window = None
        self.captions_enabled = False
        self._stop_requested = threading.Event()
        self.live = SimpleNamespace(
            snapshot={
                "foreground_window": "Test window",
                "open_apps": [],
                "screen_resolution": (1920, 1080),
            }
        )
        self.api_manager = SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "https://example.invalid/agent",
            get_fallback_url=lambda: "https://example.invalid/fallback",
        )
        self.tools = _Tools()
        self.executor = _Executor()
        self.memory_events = []
        self.memory = SimpleNamespace(
            add_action=lambda *args: self.memory_events.append(args)
        )
        self._answers = iter(answers)
        self.confirmations = []

    def _resolve_path_placeholders(self, path):
        return path

    def _get_text_answer(self, question, timeout=60):
        self.confirmations.append(question)
        return next(self._answers)

    def speak(self, _speech):
        pass


# ============================================================
# AGENT LOOP SAFETY RUN LOOP
# ============================================================

def _run_loop(monkeypatch, responses, answers=()):
    """Run a deterministic AgentLoop with no network, UI, or sleeps."""
    api = _FakeApi(answers)
    response_iter = iter(responses)

    def fake_post(*_args, **_kwargs):
        return _Response(next(response_iter))

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _seconds: None)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")
    return loop._loop("perform a test task", "test-model", "none", "text", None), api


# ============================================================
# AGENT LOOP SAFETY TESTS
# ============================================================

@pytest.mark.parametrize(
    "hint_text, expected", [(None, "framed prompt"), ("", ""), ("phase", "phase")]
)
def test_hints_match_hint_text_and_empty_does_not_fall_back(hint_text, expected):
    api = _FakeApi()
    seen = []
    api.task_memory = SimpleNamespace(
        get_hints_for_task=lambda text: seen.append(text) or []
    )
    api._stop_requested.set()  # exit right after the hint lookup
    bind_confirmation(api)
    loop = AgentLoop(api, None)

    loop._loop("framed prompt", "m", "none", "text", None, hint_text=hint_text)

    assert seen == [expected]


def test_parse_missing_action_becomes_an_internal_retry_not_an_action():
    loop = AgentLoop(SimpleNamespace(tools=_Tools()), None)

    parsed = loop._parse_cmd('{"thinking": "I forgot the action"}')

    assert parsed["action"] == "parse_retry"


def test_parse_none_becomes_an_internal_retry_not_an_agent_crash():
    loop = AgentLoop(SimpleNamespace(tools=_Tools()), None)

    assert loop._parse_cmd(None)["action"] == "parse_retry"


def test_parse_retry_is_not_dispatched_to_executor(monkeypatch):
    result, api = _run_loop(
        monkeypatch,
        [
            json.dumps({"thinking": "missing action"}),
            json.dumps({"action": "done", "speech": "Finished"}),
        ],
    )

    assert result["success"] is True
    assert api.executor.calls == []


def test_no_key_backend_failure_is_not_masked_by_an_unauthenticated_fallback(
    monkeypatch,
):
    api = _FakeApi()
    api.api_manager = SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://example.invalid/public",
        get_fallback_url=lambda: None,
    )
    calls = []

    def failed_post(*_args, **_kwargs):
        calls.append(1)
        return _Response(status_code=503)

    monkeypatch.setattr("core.agent.requests.post", failed_post)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")

    result = loop._loop("perform a test task", "test-model", "none", "text", None)

    assert calls == [1]  # no unauthenticated fallback call
    # The primary failure itself is reported, plainly (describe_api_error).
    assert result["error"] == "Test-model request failed (HTTP 503)."


def test_stop_while_model_request_is_in_flight_prevents_returned_action_dispatch(
    monkeypatch,
):
    api = _FakeApi()

    def response_after_stop(*_args, **_kwargs):
        api._stop_requested.set()
        return _Response(json.dumps({"action": "window_close", "parameter": "Test"}))

    monkeypatch.setattr("core.agent.requests.post", response_after_stop)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")

    result = loop._loop("close a test window", "test-model", "none", "text", None)

    assert result["error"] == "Stopped by user"
    assert api.executor.calls == []


def test_successful_search_is_not_executed_twice_in_one_phase(monkeypatch):
    result, api = _run_loop(
        monkeypatch,
        [
            json.dumps(
                {"action": "browser_search", "parameter": "Boston weather"}
            ),
            json.dumps(
                {"action": "browser_search", "parameter": "Boston weather"}
            ),
            json.dumps({"action": "done", "speech": "Finished"}),
        ],
    )

    assert result["success"] is True
    assert api.executor.calls == [("browser_search", "Boston weather")]


def test_voice_stop_at_confirmation_is_a_stop_not_a_decline(monkeypatch):
    api = _FakeApi()
    api.tts = SimpleNamespace(speaking=False)
    api._listen_once = lambda timeout: "stop"
    api._normalize_utterance = lambda text: str(text).lower().strip()
    api._VOICE_STOP_WORDS = {"stop"}
    api.stop_current_task = api._stop_requested.set

    def fake_post(*_args, **_kwargs):
        return _Response(
            json.dumps(
                {
                    "action": "quick_message",
                    "parameter": "discord|Ada|Hello",
                }
            )
        )

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")

    result = loop._loop("send a message", "test-model", "none", "voice", None)

    assert result["error"] == "Stopped by user"
    assert api.executor.calls == []


def test_unknown_action_is_observed_but_never_dispatched(monkeypatch):
    result, api = _run_loop(
        monkeypatch,
        [
            json.dumps({"action": "browser_serach", "parameter": "weather"}),
            json.dumps({"action": "done", "speech": "Finished"}),
        ],
    )

    assert result["success"] is True
    assert api.executor.calls == []


def test_identical_failed_action_is_refused_after_three_attempts(monkeypatch):
    api = _FakeApi()
    calls = []

    def fail(action, parameter):
        calls.append((action, parameter))
        return SimpleNamespace(
            action=action,
            parameter=parameter,
            success=False,
            detail="not found",
            verified=None,
            escalate_to_user=False,
            reliability_note=lambda: "",
        )

    api.executor.run = fail
    responses = [
        json.dumps({"action": "browser_search", "parameter": "weather"})
        for _ in range(4)
    ] + [json.dumps({"action": "done", "speech": "Finished"})]
    response_iter = iter(responses)
    monkeypatch.setattr(
        "core.agent.requests.post", lambda *_args, **_kwargs: _Response(next(response_iter))
    )
    monkeypatch.setattr("core.agent.time.sleep", lambda _seconds: None)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")

    result = loop._loop("find weather", "test-model", "none", "text", None)

    assert result["success"] is True
    assert calls == [("browser_search", "weather")] * 3


def test_recent_conversation_reaches_system_prompt_without_repeating_task(monkeypatch):
    api = _FakeApi()
    seen = {}
    rows = [
        ("summary", "old session summary"),
        ("user", "turn 0 should be trimmed"),
        ("assistant", "turn 1"),
        ("user", "turn 2"),
        ("assistant", "x" * 500),
        ("user", "turn 4"),
        ("assistant", "turn 5"),
        ("user", "open the latest report"),
        ("user", "perform a test task"),  # in-flight message, stored before the run
    ]

    def recent(limit, since=None):
        seen["limit"], seen["since"] = limit, since
        return rows

    api.memory.get_recent_conversations = recent
    requests_sent = []

    def fake_post(*_args, json=None, **_kwargs):
        requests_sent.append(json)
        return _Response('{"action": "done", "speech": "Finished"}')

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")
    loop._loop("perform a test task", "test-model", "none", "text", None)

    system = requests_sent[0]["system"]
    assert seen["limit"] == 10 and seen["since"]
    assert "RECENT CONVERSATION" in system
    assert "User: open the latest report" in system
    assert "Mist: turn 5" in system
    assert "turn 0" not in system and "old session summary" not in system
    assert "Mist: " + "x" * 200 + "..." in system and "x" * 201 not in system
    assert "User: perform a test task" not in system


def test_newest_user_row_is_kept_when_it_is_not_the_current_task():
    api = SimpleNamespace(
        memory=SimpleNamespace(
            get_recent_conversations=lambda limit, since=None: [
                ("user", "what's the weather")
            ]
        )
    )
    block = AgentLoop(api, None)._recent_conversation_block("open notepad")
    assert "User: what's the weather" in block


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("yes", True),
        ("go ahead", True),
        ("not sure", False),
        ("No, sure", False),
        ("cancel", False),
        ("", False),            # unclear answers are never a yes
        ("maybe", False),
    ],
)
def test_confirmation_requires_an_unambiguous_affirmative(answer, expected):
    assert AgentLoop._is_confirmation(answer) is expected


def test_confirming_one_action_does_not_turn_a_later_decline_into_failure(monkeypatch):
    """A previous successful confirmation must not consume the decline allowance."""
    result, api = _run_loop(
        monkeypatch,
        [
            json.dumps(
                {
                    "action": "quick_message",
                    "parameter": "discord|Ada|Hello",
                    "speech": "",
                }
            ),
            json.dumps(
                {
                    "action": "window_close",
                    "parameter": "Notepad",
                    "speech": "",
                }
            ),
            json.dumps({"action": "done", "speech": "Done"}),
        ],
        answers=("yes", "no"),
    )

    assert result["success"] is True
    assert api.executor.calls == [("quick_message", "discord|Ada|Hello")]
    assert len(api.confirmations) == 2


# ============================================================
# LOOP CYCLES
# ============================================================

# Loop cycle guard (AgentLoop + CycleGuard): the same 2 or 3 executed steps
# going round twice (A B A B, A B C A B C, identical parameters) add a one-time
# note to the observation; the same cycle once more ends the phase with
# "I got stuck going in circles, so I stopped." (success False, error "loop").
# A changed parameter never counts.


# ============================================================
# LOOP CYCLES HELPERS
# ============================================================

READ = {"action": "read_screen_text", "parameter": "what is the status?"}
CLICK = {"action": "click_on_text", "parameter": "Next"}
KEY = {"action": "press_key", "parameter": "tab"}
LOOP_CYCLES_DONE = {"action": "done", "speech": "ok"}


# ============================================================
# LOOP CYCLES RUN
# ============================================================

def _loop_cycles_run(monkeypatch, replies):
    replies = iter(json.dumps(r) for r in replies)
    rec = SimpleNamespace(calls=[], spoken=[], requests=[])

    def run(a, p):
        rec.calls.append((a, p))
        return SimpleNamespace(action=a, parameter=p, success=True, detail="status: pending",
                               verified=None, escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="")

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *_a, **_k: False),
        executor=SimpleNamespace(run=run), memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda t, **_k: rec.spoken.append(t),
        tts=SimpleNamespace(speaking=False), _last_opened_app="",
    )

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "App"))
    rec.result = loop._loop("check the status", "m", "none", "text", None)
    return rec


# ============================================================
# LOOP CYCLES HELPERS (2)
# ============================================================

def _loop_cycles_observations(rec):
    msgs = rec.requests[-1]["messages"]
    return [m["content"] for m in msgs if m["role"] == "user" and "[OBSERVATION]" in m["content"]]


# ============================================================
# LOOP CYCLES TESTS
# ============================================================

def test_a_b_a_b_warns_then_stops(monkeypatch):
    rec = _loop_cycles_run(monkeypatch, [READ, CLICK, READ, CLICK, READ, CLICK, LOOP_CYCLES_DONE])
    assert rec.result == {"success": False, "error": "loop", "speech": LOOP_SPEECH, "steps": 6}
    assert rec.spoken == [LOOP_SPEECH]
    assert len(rec.calls) == 6
    obs = _loop_cycles_observations(rec)
    assert [LOOP_NOTE in o for o in obs] == [False, False, False, True, False]


def test_a_b_c_a_b_c_warns_then_stops(monkeypatch):
    rec = _loop_cycles_run(monkeypatch, [READ, CLICK, KEY] * 3 + [LOOP_CYCLES_DONE])
    assert rec.result["error"] == "loop" and len(rec.calls) == 9
    obs = _loop_cycles_observations(rec)
    assert [i for i, o in enumerate(obs) if LOOP_NOTE in o] == [5]


def test_a_warning_followed_by_something_else_finishes_normally(monkeypatch):
    other = {"action": "read_screen_text", "parameter": "is it done now?"}
    rec = _loop_cycles_run(monkeypatch, [READ, CLICK, READ, CLICK, other, LOOP_CYCLES_DONE])
    assert rec.result["success"] is True and rec.spoken == ["ok"]


def test_changing_parameters_never_trigger(monkeypatch):
    replies = []
    for i in range(6):
        replies += [{"action": "read_screen_text", "parameter": f"status after {i} s?"}, CLICK]
    rec = _loop_cycles_run(monkeypatch, replies + [LOOP_CYCLES_DONE])
    assert rec.result.get("error") != "loop" and len(rec.calls) == 12
    assert not any(LOOP_NOTE in o for o in _loop_cycles_observations(rec))


def test_unit_rules():
    g = CycleGuard()
    assert [g.record(a, p) for a, p in [("r", 1), ("c", 2), ("r", 1), ("c", 2)]] == \
        [None, None, None, "warn"]
    assert g.record("r", 1) is None                   # half a cycle more: not yet
    assert g.record("c", 2) == "stop"
    g = CycleGuard()                                   # one pair repeated is not a cycle
    assert [g.record("r", 1) for _ in range(8)] == [None] * 8
    g = CycleGuard()                                   # a different cycle after the warning
    for a, p in [("r", 1), ("c", 2), ("r", 1), ("c", 2), ("x", 3), ("y", 4), ("x", 3)]:
        last = g.record(a, p)
    assert last is None and g.record("y", 4) is None   # no second note, no stop
    g = CycleGuard()
    seq = [("r", "a"), ("c", "n"), ("r", "b"), ("c", "n"), ("r", "c"), ("c", "n")]
    assert [g.record(a, p) for a, p in seq] == [None] * 6


def test_the_window_is_eight_pairs():
    g = CycleGuard()
    for i in range(20):
        g.record("a", i)
    assert len(g.recent) == 8


def test_loop_logs_carry_no_parameter(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _loop_cycles_run(monkeypatch, [READ, CLICK, READ, CLICK, READ, CLICK, LOOP_CYCLES_DONE])
    out = capsys.readouterr().out
    assert "[Loop] repeating steps; note added" in out
    assert "[Loop] the same steps repeated again after a warning; the phase ends" in out
    loop_lines = [l for l in out.splitlines() if "[Loop]" in l]
    assert not any("Next" in l or "status" in l for l in loop_lines)


# ============================================================
# BATCHED STEPS
# ============================================================

# Batched steps (MISTAI_BATCH_STEPS, default on): one model reply may carry a
# short "steps" list of predictable input actions. Every step runs through the
# same per-step path as a single action (guards, confirmation and Enter rules,
# Executor), the batch stops at the first failure, refusal, No or a target that
# left the front. A partly valid list runs its usable part (a batchable prefix
# of 2+, else its first step alone) and the model is told which steps did not
# run; only a list with no usable first step is a parse retry, with its reason.


# ============================================================
# BATCHED STEPS HELPERS
# ============================================================

BATCHED_STEPS_DONE = {"action": "done", "speech": "Done."}


def batch(*steps, **extra):
    return dict({"thinking": "t", "steps": [{"action": a, "parameter": p} for a, p in steps],
                 "speech": ""}, **extra)


# ============================================================
# BATCHED STEPS RUN
# ============================================================

def _batched_steps_run(monkeypatch, tmp_path, replies, answers=(), fail=(), enter_safe=False,
         in_front=lambda app: True, on_run=None):
    replies = iter(json.dumps(r) if isinstance(r, dict) else r for r in replies)
    answers = list(answers)
    rec = SimpleNamespace(calls=[], asked=[], requests=[], reads=[], steps=[])

    def answer(question):
        rec.asked.append(question)
        return answers.pop(0) if answers else None

    def run(a, p):
        rec.calls.append((a, p))
        ok = (a, p) not in fail
        if ok and a in ("open_app", "switch_app"):
            api._last_opened_app = p
        if on_run:
            on_run(api, a, p)
        return SimpleNamespace(action=a, parameter=p, success=ok,
                               detail="" if ok else "it failed", verified=None,
                               escalate_to_user=False, reliability_note=lambda: "",
                               escalated_from="")

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1),
                                       "foreground_window": "Notepad"}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=ToolRegistry(db_path=str(tmp_path / "tools.db")),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _listen_once=lambda timeout=10, **_k: None,
        _get_text_answer=lambda q, timeout=60: answer(q),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        stop_current_task=lambda: api._stop_requested.set(),
        enter_target_is_safe=lambda: enter_safe,
        _foreground_process_matches=in_front,
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
    monkeypatch.setattr(loop, "_get_screen_context",
                        lambda *a, **k: rec.reads.append(1) or "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Notepad"))
    rec.result = loop._loop("do it", "m", "none", "text",
                            lambda n, a, p, s: rec.steps.append((n, a)))
    api.tools._conn and api.tools._conn.close()
    return rec


# ============================================================
# BATCHED STEPS HELPERS (2)
# ============================================================

def _messages(rec):
    return rec.requests[-1]["messages"] if rec.requests else []


def _batched_steps_observations(rec):
    return [m["content"] for m in _messages(rec)
            if m["role"] == "user" and "[OBSERVATION]" in m["content"]]


STEPS3 = (("open_app", "notepad"), ("type_text", "hello"), ("press_key", "tab"))


# --- Degraded lists: the valid part runs, the rest is reported, never run ------


# --- Unusable lists: parse_retry with the specific reason ----------------------


# ============================================================
# BATCHED STEPS TESTS
# ============================================================

class TestBatchedSteps:
    @pytest.fixture(autouse=True)
    def _flags(self, monkeypatch):
        monkeypatch.delenv("MISTAI_BATCH_STEPS", raising=False)
        monkeypatch.setenv("MISTAI_VISION_GATING", "1")

    def test_batch_of_three_runs_in_order_with_one_model_call(self, monkeypatch, tmp_path, capsys):
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE])
        assert rec.calls == list(STEPS3)
        assert len(rec.requests) == 2                  # the batch, then the done
        assert rec.result["success"] is True
        # Each step is recorded as its own action + observation, with no internals.
        sent = [json.loads(m["content"]) for m in _messages(rec) if m["role"] == "assistant"]
        assert [(s["action"], s["parameter"]) for s in sent] == list(STEPS3)
        assert not any("_batch" in s or "steps" in s for s in sent)
        assert len(_batched_steps_observations(rec)) == 3
        assert [a for _n, a in rec.steps] == ["open_app", "type_text", "press_key", "done"]
        log = capsys.readouterr().out
        assert "[Batch] 3 steps in one reply" in log
        assert log.count("[Batch] next step, no model call") == 2
        assert "hello" not in log                      # content-free

    def test_queued_steps_skip_the_screen_read(self, monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE])
        assert len(rec.reads) == 2                     # before the batch, before the done

    def test_batch_stops_at_the_first_failure(self, monkeypatch, tmp_path, capsys):
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE], fail={("type_text", "hello")})
        assert rec.calls == [("open_app", "notepad"), ("type_text", "hello")]
        obs = _batched_steps_observations(rec)
        assert "FAILED" in obs[1]
        assert obs[2] == ("[OBSERVATION] Batch stopped; these steps were NOT run: press_key(tab). "
                          "Decide the next step from the current state.")
        assert "[Batch] stopped: 1 step(s) not run" in capsys.readouterr().out

    def test_batch_stops_when_the_target_left_the_front(self, monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE], in_front=lambda app: False)
        assert rec.calls == [("open_app", "notepad")]
        assert "NOT run: type_text(hello), press_key(tab)" in _batched_steps_observations(rec)[1]

    def test_target_check_errors_stop_the_batch(self, monkeypatch, tmp_path):
        def boom(_app):
            raise OSError("gone")

        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE], in_front=boom)
        assert rec.calls == [("open_app", "notepad")]

    @pytest.mark.parametrize("enter_safe", [False, True])
    def test_enter_in_a_batch_asks_exactly_when_it_would_ask_alone(self, monkeypatch, tmp_path,
                                                                    enter_safe):
        alone = _batched_steps_run(monkeypatch, tmp_path,
                     [{"action": "press_key", "parameter": "enter"}, BATCHED_STEPS_DONE], ["yes"],
                     enter_safe=enter_safe)
        batched = _batched_steps_run(monkeypatch, tmp_path,
                       [batch(("type_text", "37*23"), ("press_key", "enter")), BATCHED_STEPS_DONE], ["yes"],
                       enter_safe=enter_safe)
        assert len(alone.asked) == len(batched.asked) == (0 if enter_safe else 1)
        assert batched.calls == [("type_text", "37*23"), ("press_key", "enter")]

    def test_confirmation_no_stops_the_batch(self, monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path,
                   [batch(("type_text", "hi"), ("press_key", "enter"), ("type_text", "more")), BATCHED_STEPS_DONE],
                   ["no"])
        assert rec.calls == [("type_text", "hi")]       # Enter declined, the rest never ran
        assert len(rec.asked) == 1
        obs = _batched_steps_observations(rec)
        assert any("User explicitly declined" in o for o in obs)
        assert "NOT run: type_text(more)" in obs[-1]

    def test_a_batch_never_merges_confirmations(self, monkeypatch, tmp_path):
        # Two Enters: each asks on its own; the second question is a second question.
        rec = _batched_steps_run(monkeypatch, tmp_path,
                   [batch(("press_key", "enter"), ("type_text", "x"), ("press_key", "enter")), BATCHED_STEPS_DONE],
                   ["yes", "yes"])
        assert len(rec.asked) == 2
        assert rec.calls == [("press_key", "enter"), ("type_text", "x"), ("press_key", "enter")]

    def test_stop_during_a_batch_halts_it(self, monkeypatch, tmp_path):
        def stop_after_first(api, a, p):
            api._stop_requested.set()

        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE], on_run=stop_after_first)
        assert rec.calls == [("open_app", "notepad")]
        assert rec.result == {"success": False, "error": "Stopped by user"}

    @pytest.mark.parametrize("reply, ran, label, dropped", [
        (batch(("open_app", "notepad"), ("read_screen_text", "what is shown?")),   # a read
         [("open_app", "notepad")], "non-batchable-action read_screen_text",
         "read_screen_text(what is shown?)"),
        (batch(("type_text", "x"), ("done", "")),                                  # done
         [("type_text", "x")], "non-batchable-action done", "done()"),
        (batch(("click_on_text", "OK"), ("press_key", "tab")),                     # not batchable
         [("click_on_text", "OK")], "non-batchable-action click_on_text", "press_key(tab)"),
        (batch(*([("type_text", "a"), ("press_key", "tab")] * 3)),                 # too many
         [("type_text", "a"), ("press_key", "tab")] * 2, "too-many",
         "type_text(a), press_key(tab)"),
        ({"steps": [{"action": "type_text", "parameter": "a"},
                    {"action": "press_key", "parameter": {"k": 1}}]},              # bad parameter
         [("type_text", "a")], "bad-parameter", "press_key({'k': 1})"),
        ({"steps": [{"action": "type_text", "parameter": "a"}, "press tab"]},      # not an object
         [("type_text", "a")], "not-objects", "(not a step)"),
    ])
    def test_a_partly_valid_list_runs_its_usable_part_and_reports_the_rest(self, 
            monkeypatch, tmp_path, capsys, reply, ran, label, dropped):
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE])
        assert rec.calls == ran                        # dropped steps never execute
        assert len(rec.requests) == 2                  # no wasted model call
        note = _batched_steps_observations(rec)[-1]
        assert note.startswith("[OBSERVATION] These steps of your list were NOT run (")
        assert dropped in note and "one reply of its own" in note
        log = capsys.readouterr().out
        assert f"[Batch] malformed steps list ({label}) -- running {len(ran)} of" in log
        assert "parse retry" not in log

    def test_a_list_starting_with_run_command_runs_it_alone(self, monkeypatch, tmp_path, capsys):
        reply = batch(("run_command", "Get-Date"), ("open_app", "notepad"), ("type_text", "hi"))
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE])
        assert rec.calls == [("run_command", "Get-Date")]
        sent = [json.loads(m["content"]) for m in _messages(rec) if m["role"] == "assistant"]
        assert sent[0] == {"action": "run_command", "parameter": "Get-Date",
                           "thinking": "t", "speech": ""}       # no internals in history
        obs = _batched_steps_observations(rec)
        assert len(obs) == 2                           # the command's result, then the note
        assert "'run_command' cannot share a reply" in obs[1]
        assert "open_app(notepad), type_text(hi)" in obs[1]
        log = capsys.readouterr().out
        assert "(non-batchable-action run_command) -- running 1 of 3 steps" in log
        assert "Get-Date" not in log and "notepad" not in log.split("[Batch]", 1)[1].split("\n")[0]

    def test_a_valid_prefix_then_a_non_batchable_step_runs_only_the_prefix(self, monkeypatch, tmp_path):
        reply = batch(("open_app", "notepad"), ("type_text", "hi"), ("run_command", "Get-Date"),
                      ("press_key", "tab"))
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE])
        assert rec.calls == [("open_app", "notepad"), ("type_text", "hi")]
        assert len(rec.requests) == 2
        obs = _batched_steps_observations(rec)
        assert len(obs) == 3                           # both steps' results, then the note
        assert "run_command(Get-Date), press_key(tab)" in obs[2]

    def test_a_stopped_prefix_reports_its_queued_steps_and_the_dropped_ones(self, monkeypatch, tmp_path):
        reply = batch(("open_app", "notepad"), ("type_text", "hi"), ("run_command", "Get-Date"))
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE], fail={("open_app", "notepad")})
        assert rec.calls == [("open_app", "notepad")]
        obs = _batched_steps_observations(rec)
        assert "Batch stopped; these steps were NOT run: type_text(hi)" in obs[1]
        assert "NOT run" in obs[2] and "run_command(Get-Date)" in obs[2]

    @pytest.mark.parametrize("answer, ran", [
        ("no", [("type_text", "x")]),
        ("yes", [("type_text", "x"), ("press_key", "enter")]),
    ])
    def test_enter_in_a_degraded_batch_still_asks(self, monkeypatch, tmp_path, answer, ran):
        reply = batch(("type_text", "x"), ("press_key", "enter"), ("run_command", "Get-Date"))
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE], [answer])
        assert len(rec.asked) == 1
        assert rec.calls == ran                        # run_command never runs either way

    def test_a_mutating_command_run_alone_from_a_list_still_asks(self, monkeypatch, tmp_path):
        reply = batch(("run_command", "Remove-Item C:\\scratch\\old.txt"), ("type_text", "hi"))
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE], ["no"])
        assert len(rec.asked) == 1
        assert rec.calls == []                         # declined; the dropped step never ran

    def test_reason_labels_are_content_free(self, monkeypatch, tmp_path, capsys):
        reply = {"steps": [{"action": "type_text", "parameter": "SECRETWORD"},
                           {"action": "press_key", "parameter": "tab"},
                           {"action": "leak_SECRETNAME", "parameter": "SECRETPARAM"}]}
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE])
        assert rec.calls == [("type_text", "SECRETWORD"), ("press_key", "tab")]
        log = capsys.readouterr().out
        assert "(non-batchable-action unknown) -- running 2 of 3 steps" in log
        assert "SECRET" not in log

    def test_a_bare_list_with_a_non_batchable_first_step_runs_it_alone(self, monkeypatch, tmp_path):
        steps = [{"action": "run_command", "parameter": "Get-Date"},
                 {"action": "type_text", "parameter": "x"}]
        rec = _batched_steps_run(monkeypatch, tmp_path, [json.dumps(steps), BATCHED_STEPS_DONE])
        assert rec.calls == [("run_command", "Get-Date")]
        assert "type_text(x)" in _batched_steps_observations(rec)[-1]

    def test_a_one_step_list_runs_alone_without_a_note(self, monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(("press_key", "tab")), BATCHED_STEPS_DONE])
        assert rec.calls == [("press_key", "tab")]
        assert not any("NOT run" in o for o in _batched_steps_observations(rec))

    @pytest.mark.parametrize("reply, label, why", [
        (batch(*STEPS3, action="open_app"), "mixed-form", "both a top-level action"),
        ({"thinking": "t", "steps": "open_app notepad"}, "not-objects", "not an {"),
        ({"thinking": "t", "steps": []}, "too-few", "no steps"),
        ({"steps": [{"parameter": "x"}, {"action": "press_key", "parameter": "tab"}]},
         "missing-field", "had no action"),
        ({"steps": [{"action": "type_text", "parameter": {"a": 1}},
                    {"action": "press_key", "parameter": "tab"}]}, "bad-parameter", "not plain text"),
        ({"steps": ["open notepad", {"action": "press_key", "parameter": "tab"}]},
         "not-objects", "not an {"),
        ('Plan: {"steps": [ {"action": "open_app", "parameter": "notepad"', "not-json",
         "not valid JSON"),
    ])
    def test_an_unusable_list_is_a_parse_retry_with_its_reason(self, monkeypatch, tmp_path, capsys,
                                                               reply, label, why):
        rec = _batched_steps_run(monkeypatch, tmp_path, [reply, BATCHED_STEPS_DONE])
        assert rec.calls == []
        obs = _batched_steps_observations(rec)
        assert obs[0] == AgentLoop.BATCH_RETRY_NOTE.format(
            why=AgentLoop.BATCH_REASONS[label].format(action=""))
        assert why in obs[0] and "NOTHING was run" in obs[0]
        assert f"[Batch] malformed steps list ({label}) -- parse retry" in capsys.readouterr().out

    def test_three_malformed_batches_end_the_phase(self, monkeypatch, tmp_path):
        bad = {"thinking": "t", "steps": []}
        rec = _batched_steps_run(monkeypatch, tmp_path, [bad, bad, bad])
        assert rec.calls == []
        assert "3x in a row" in rec.result["error"]

    def test_flag_off_runs_a_lists_first_object_with_no_note(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
        rec = _batched_steps_run(monkeypatch, tmp_path,
                   [batch(("run_command", "Get-Date"), ("type_text", "y")), BATCHED_STEPS_DONE])
        assert rec.calls == [("run_command", "Get-Date")]
        assert not any("NOT run" in o for o in _batched_steps_observations(rec))
        assert "[Batch]" not in capsys.readouterr().out

    def test_a_batch_wrapped_in_text_is_still_one_batch(self, monkeypatch, tmp_path):
        text = "Here is the plan:\n" + json.dumps(batch(*STEPS3)) + "\nThat's it."
        rec = _batched_steps_run(monkeypatch, tmp_path, [text, BATCHED_STEPS_DONE])
        assert rec.calls == list(STEPS3)

    def test_flag_off_keeps_one_action_per_reply(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
        rec = _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), BATCHED_STEPS_DONE])
        # Today's parsing: a reply without a top-level action yields its first object.
        assert rec.calls == [("open_app", "notepad")]
        assert '"steps"' not in rec.requests[0]["system"]
        assert "decide exactly ONE action" in rec.requests[0]["system"]

    def test_a_bare_list_of_input_steps_is_one_batch(self, monkeypatch, tmp_path, capsys):
        steps = [{"action": a, "parameter": p} for a, p in STEPS3]
        rec = _batched_steps_run(monkeypatch, tmp_path, [json.dumps(steps), BATCHED_STEPS_DONE])
        assert rec.calls == list(STEPS3)
        assert len(rec.requests) == 2
        log = capsys.readouterr().out
        assert "[Reply] shape=array of 3" in log and "hello" not in log

    def test_a_list_with_a_non_input_step_keeps_its_first_object(self, monkeypatch, tmp_path):
        steps = [{"action": "type_text", "parameter": "x"},
                 {"action": "read_screen_text", "parameter": "q"}]
        rec = _batched_steps_run(monkeypatch, tmp_path, [json.dumps(steps), BATCHED_STEPS_DONE])
        assert rec.calls == [("type_text", "x")]
        assert "read_screen_text(q)" in _batched_steps_observations(rec)[-1]   # no longer dropped silently

    def test_a_bare_list_is_not_a_batch_with_the_flag_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
        steps = [{"action": a, "parameter": p} for a, p in STEPS3]
        rec = _batched_steps_run(monkeypatch, tmp_path, [json.dumps(steps), BATCHED_STEPS_DONE])
        assert rec.calls == [("open_app", "notepad")]

    def test_every_reply_logs_its_shape_only(self, monkeypatch, tmp_path, capsys):
        _batched_steps_run(monkeypatch, tmp_path, [batch(*STEPS3), "not json at all", BATCHED_STEPS_DONE])
        log = capsys.readouterr().out
        assert "[Reply] shape=steps" in log
        assert "[Reply] shape=text" in log
        assert "[Reply] shape=object" in log
        assert "not json" not in log

    def test_the_format_block_shows_both_shapes_and_the_last_message_allows_steps(self, 
            monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path, [BATCHED_STEPS_DONE])
        system = rec.requests[0]["system"]
        fmt = system.split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
        assert '"action":"..."' in fmt and '"steps":[' in fmt
        assert "(both in one steps reply)" in system
        assert rec.requests[0]["messages"][-1]["content"] == (
            "What next? Reply with JSON only: one action, or a steps list.")

    def test_flag_off_keeps_the_old_format_and_last_message(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
        rec = _batched_steps_run(monkeypatch, tmp_path, [BATCHED_STEPS_DONE])
        assert "steps" not in rec.requests[0]["system"].split("RESPONSE FORMAT", 1)[1]
        assert "(both in one steps reply)" not in rec.requests[0]["system"]
        assert rec.requests[0]["messages"][-1]["content"] == (
            "What is your next action? Reply with JSON only.")

    def test_the_prompt_carries_one_merged_batch_rule(self, monkeypatch, tmp_path):
        rec = _batched_steps_run(monkeypatch, tmp_path, [BATCHED_STEPS_DONE])
        system = rec.requests[0]["system"]
        assert system.count('"steps":[') == 1
        assert "no reads, no done" in system
        fmt = system.split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
        assert "ONLY open_app/switch_app/type_text/press_key/hotkey" in fmt
        assert "run_command, file, browser and read actions are one per reply" in fmt
        assert "decide exactly ONE action" not in system


# ============================================================
# NETWORK FAILURE
# ============================================================

# The agent call's network failures speak one fixed sentence; one immediate
# retry for a connection reset/refused only; the system prompt's size is logged
# as a number and warned about near the backend's limits.


# ============================================================
# NETWORK FAILURE HELPERS
# ============================================================

URL = "https://secret-backend.invalid/api/v1/chat?token=abc"
TASK = "message joanne quillfeather about the lighthouse"


# ============================================================
# NETWORK FAILURE LOOP
# ============================================================

def _loop(monkeypatch, outcomes, mode="voice"):
    """One phase; each requests.post call takes the next outcome (an exception
    instance to raise, or a reply dict)."""
    outcomes = list(outcomes)
    posts, spoken = [], []

    def fake_post(url, json=None, **_k):
        posts.append(url)
        o = outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": json_dumps(o)})

    json_dumps = json.dumps
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: URL,
                                    get_fallback_url=lambda: URL),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: None),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=spoken.append,
        _last_opened_app="",
    )
    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    result = loop._loop(TASK, "m", "none", mode, None)
    return result, posts, spoken


# ============================================================
# NETWORK FAILURE HELPERS (2)
# ============================================================

def _reset():
    # What requests raises for a reset connection: the socket error inside.
    inner = ConnectionResetError(10054, "An existing connection was forcibly closed")
    return requests.exceptions.ConnectionError(inner, request=None)


def _refused():
    e = requests.exceptions.ConnectionError("Max retries exceeded with url: " + URL)
    e.__context__ = ConnectionRefusedError(10061, "refused")
    return e


NETWORK_FAILURE_DONE = {"action": "done", "speech": "Done."}
FAILED = {"success": False, "error": NETWORK_FAILURE_SPEECH, "speech": NETWORK_FAILURE_SPEECH}


# ============================================================
# NETWORK FAILURE TESTS
# ============================================================

@pytest.mark.parametrize("mode", ["voice", "text"])
def test_a_read_timeout_speaks_the_sentence_once_with_no_retry(monkeypatch, capsys, mode):
    result, posts, spoken = _loop(monkeypatch, [requests.exceptions.ReadTimeout(
        f"HTTPSConnectionPool: Read timed out. (read timeout=30) {URL}")], mode=mode)
    assert result == FAILED
    assert len(posts) == 1                                      # never retried
    assert spoken == ([NETWORK_FAILURE_SPEECH] if mode == "voice" else [])
    log = capsys.readouterr().out
    assert "[AgentLoop] Network error: ReadTimeout after " in log
    assert "secret-backend" not in log and "token" not in log
    assert "Read timed out" not in log                          # never the message


def test_a_connection_reset_retries_once_then_succeeds(monkeypatch, capsys):
    result, posts, spoken = _loop(monkeypatch, [_reset(), NETWORK_FAILURE_DONE])
    assert result["success"] and result["speech"] == "Done."
    assert len(posts) == 2
    log = capsys.readouterr().out
    assert "[AgentLoop] ConnectionError (connection reset/refused); retrying once" in log
    assert "forcibly closed" not in log and "secret-backend" not in log


def test_a_refused_connection_also_retries_once(monkeypatch):
    result, posts, _ = _loop(monkeypatch, [_refused(), NETWORK_FAILURE_DONE])
    assert result["success"] and len(posts) == 2


def test_a_reset_twice_fails_with_the_sentence_after_exactly_one_retry(monkeypatch):
    result, posts, spoken = _loop(monkeypatch, [_reset(), _reset()])
    assert result == FAILED and len(posts) == 2 and spoken == [NETWORK_FAILURE_SPEECH]


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectTimeout("connect timed out"),
    TimeoutError("timed out"),
    requests.exceptions.ConnectionError("Name or service not known"),
    requests.exceptions.SSLError("certificate verify failed"),
])
def test_other_network_errors_fail_at_once_without_a_retry(monkeypatch, exc):
    result, posts, _ = _loop(monkeypatch, [exc])
    assert result == FAILED and len(posts) == 1


def test_success_is_unchanged(monkeypatch):
    result, posts, spoken = _loop(monkeypatch, [NETWORK_FAILURE_DONE])
    assert result == {"success": True, "speech": "Done.", "steps": 1}
    assert len(posts) == 1 and spoken == ["Done."]


def test_a_non_network_error_is_not_the_network_sentence(monkeypatch, capsys):
    result, _, spoken = _loop(monkeypatch, [ValueError(f"bad body from {URL}")])
    assert result == {"success": False, "error": "Agent call failed (ValueError)"}
    assert spoken == []
    assert "secret-backend" not in capsys.readouterr().out


def test_the_chain_walk():
    assert agent_mod._is_reset_or_refused(_reset())
    assert agent_mod._is_reset_or_refused(_refused())
    assert not agent_mod._is_reset_or_refused(requests.exceptions.ReadTimeout())
    assert not agent_mod._is_reset_or_refused(requests.exceptions.ConnectionError("dns"))


# -- request size -------------------------------------------------------------------------

def test_the_system_prompt_size_is_a_number_printed_once_per_phase(monkeypatch, capsys):
    _loop(monkeypatch, [{"action": "wait", "parameter": "0"}, NETWORK_FAILURE_DONE])
    log = capsys.readouterr().out
    lines = [l for l in log.splitlines() if l.startswith("[AgentLoop] System prompt:")]
    assert len(lines) == 1
    assert lines[0].split(":", 1)[1].strip().replace(" chars", "").isdigit()


def test_warns_once_over_80_percent_of_the_backend_limits(monkeypatch, capsys):
    monkeypatch.setattr(AgentLoop, "_size_warned", set())
    loop = AgentLoop(SimpleNamespace(), None)
    big_history = [{"role": "user", "content": "x" * 52_000}]
    loop._check_request_size("s" * 25_601, big_history)
    loop._check_request_size("s" * 30_000, big_history)
    log = capsys.readouterr().out
    assert log.count("WARNING: system prompt is 25601 chars, over 80% of the backend's 32000") == 1
    assert log.count("WARNING: message history is 52000 chars, over 80% of the backend's 64000") == 1
    assert "WARNING" not in "".join(l for l in log.splitlines() if "30000" in l)


def test_no_warning_under_the_limits(monkeypatch, capsys):
    monkeypatch.setattr(AgentLoop, "_size_warned", set())
    AgentLoop(SimpleNamespace(), None)._check_request_size("s" * 25_600, [{"content": "x"}])
    assert "WARNING" not in capsys.readouterr().out


def test_a_read_timeout_is_never_retried_even_with_a_reset_in_its_chain(monkeypatch):
    timeout = requests.exceptions.ReadTimeout("read timed out")
    timeout.__context__ = ConnectionResetError(10054, "reset")
    result, posts, _ = _loop(monkeypatch, [timeout, NETWORK_FAILURE_DONE])
    assert result == FAILED and len(posts) == 1


# ============================================================
# GROUNDING RULES
# ============================================================

# Planner/agent prompt rules that keep answers grounded in observed data.


# ============================================================
# GROUNDING RULES SYSTEM
# ============================================================

def _grounding_rules_system():
    api = SimpleNamespace(
        workspace=SimpleNamespace(context_block=lambda: ""),
        _resolve_path_placeholders=lambda p: p,
    )
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


# -- Planner -------------------------------------------------------------------


# ============================================================
# GROUNDING RULES TESTS
# ============================================================

def test_plan_prompt_plans_only_what_remains_and_only_what_was_asked():
    prompt = planner._PLAN_PROMPT
    assert "Plan ONLY what remains" in prompt
    assert "QUESTION's deliverable is the spoken answer" in prompt
    assert "write" in prompt and "true/false" in prompt
    assert "that output is a deliverable and must have a phase" in prompt
    # Still formats cleanly (no stray braces).
    planner._PLAN_PROMPT.format(goal="g", max_nodes=4, workspace="w")


# ============================================================
# GROUNDING RULES HELPERS
# ============================================================

RESEARCH_GOAL = "research 3 VS Code extensions for React then write the names in Notepad"


# ============================================================
# GROUNDING RULES TESTS (2)
# ============================================================

def test_research_then_write_goal_is_planned_and_keeps_a_writing_phase(monkeypatch):
    seen_prompts = []
    raw = json.dumps(
        {
            "task_name": "ReactExtensions",
            "nodes": [
                {
                    "id": "research",
                    "desc": "Identify 3 popular VS Code extensions for React",
                    "depends_on": [],
                },
                {
                    "id": "write_names",
                    "desc": "Write the 3 extension names in Notepad",
                    "depends_on": ["research"],
                },
            ],
        }
    )

    def fake_llm(_api, prompt, _model):
        seen_prompts.append(prompt)
        return raw

    monkeypatch.setattr(planner, "_llm", fake_llm)
    plan, _ = planner.make_plan(SimpleNamespace(), RESEARCH_GOAL, "m")

    assert seen_prompts, "a two-deliverable goal must reach the planner"
    assert plan.order == ["research", "write_names"]
    assert "notepad" in plan.nodes["write_names"].desc.lower()


def test_research_then_write_fallback_still_contains_the_write(monkeypatch):
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: "not json")
    plan, _ = planner.make_plan(SimpleNamespace(), RESEARCH_GOAL, "m")
    assert [plan.nodes[n].desc for n in plan.order] == [RESEARCH_GOAL]


# -- Agent rules ---------------------------------------------------------------


def test_agent_prompt_has_grounding_rules():
    system = _grounding_rules_system()
    assert 'use it and call "done"' in system
    assert "read again more specifically, or say you couldn't get it" in system
    assert "Never state data you haven't seen as fact" in system
    assert "never write placeholder content" in system
    assert "not merely because an action returned ok" in system
    assert "never from read_screen_text or" in system and "analyze_screen" in system
    assert "confirm absence with Get-Process" in system
    # A screen description of an app that Open apps does not list is not proof it is open.
    assert "The Open apps list outranks the screen description for which apps" in system
    assert "if an app isn't listed, don't assume it's open; use open_app." in system
    assert "add -ErrorAction SilentlyContinue" in system
    assert "got there by another route, say so in the final speech" in system
    assert "content VISIBLE on screen" in system
    # What a window shows is read from it, never computed and reported as read.
    assert "must come from an observation of that window" in system
    assert "fix the state or report what it actually shows" in system
    assert "never present your own computation" in system
    # Keys before clicks in native apps; OCR/vision clicks are the last resort.
    assert "type_text / press_key / hotkey before any clicking" in system
    assert "click_on_text / click_nth (OCR+Vision) -- the last resort" in system
    assert "not from web APIs called with run_command" in system
    assert "Its speech is the only thing the user hears or sees" in system
    assert "it must contain the answer; leave it empty only for a pure action" in system
    assert "Never ask the user for API keys, tokens or passwords" in system


def test_bare_app_name_and_message_source_clauses_are_merged_into_existing_rules():
    system = _grounding_rules_system()
    # Merged into the "Prefer making a reasonable decision" rule, not a new one.
    rule = next(l for l in system.splitlines() if "Prefer making a reasonable decision" in l)
    assert ("A goal that is only an app's name means open or switch to it; "
            "once it is in front, call done.") in rule
    assert system.count("only an app's name") == 1
    # Merged into the messaging flow's quick_message step (a rule, so it stays in
    # the prompt; the menu line is one short line generated from core/tools.py).
    menu = next(l for l in system.splitlines() if "ALWAYS try `quick_message" in l)
    assert ("The recipient and the message must both come from the user's words; "
            "if either is missing,") in menu
    assert "use ask_user FIRST" in menu
    assert system.count("must both come from the user's words") == 1


# -- read_screen_text vision prompt -------------------------------------------


# ============================================================
# GROUNDING RULES VISION API
# ============================================================

def _vision_api(reply):
    api = Api.__new__(Api)
    prompts = []

    def call(prompt, _frame):
        prompts.append(prompt)
        return reply

    api.vision = SimpleNamespace(ready=True, _call=call)
    api.screen = SimpleNamespace(frame=object())
    api.last_result_text = ""
    api._exact_ui_text = lambda query="", include_browser=False: ("", "", False, "exact", None)
    return api, prompts


# ============================================================
# GROUNDING RULES TESTS (3)
# ============================================================

def test_whats_on_my_screen_still_gets_the_full_description():
    api, prompts = _vision_api("A browser showing the React docs and a terminal.")
    assert Api.read_screen_text(api, "") == "A browser showing the React docs and a terminal."
    assert prompts == ["Describe the visible text and content on this screen in detail."]

    api, prompts = _vision_api("A browser showing the React docs.")
    Api.read_screen_text(api, "what's on my screen")
    assert "what's on my screen" in prompts[0]
    assert api.last_result_text == "A browser showing the React docs."


def test_specific_question_is_limited_to_what_is_visible():
    api, prompts = _vision_api("NOT VISIBLE ON SCREEN")
    Api.read_screen_text(api, "is Edge running?")
    assert "ONLY from what is visible" in prompts[0]
    assert "NOT VISIBLE ON SCREEN" in prompts[0]
    # Transcribe, never compute: a pending 37*23 must not be read back as 851.
    assert "exactly as displayed" in prompts[0]
    assert "Never calculate, complete, or infer" in prompts[0]


def test_not_visible_on_screen_sends_the_agent_to_another_source(monkeypatch):
    replies = iter(
        [
            json.dumps({"action": "read_screen_text", "parameter": "is Edge running?"}),
            json.dumps({"action": "run_command", "parameter": "Get-Process msedge"}),
            # No numbers to contradict the read and no unsubmitted typing:
            # the first done finishes without the done-time check.
            json.dumps({"action": "done", "speech": "Edge is not running."}),
        ]
    )
    details = {
        "read_screen_text": "NOT VISIBLE ON SCREEN",
        "run_command": "COMMAND FAILED (exit code 1).",
    }
    calls, sent = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": ["code"], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "https://example.invalid/a",
            get_fallback_url=lambda: "https://example.invalid/b",
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: calls.append(a)
            or SimpleNamespace(
                success=True,
                detail=details[a],
                verified=None,
                escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    assert loop._loop("is Edge open?", "m", "none", "text", None)["success"]

    assert calls == ["read_screen_text", "run_command"]
    obs_after_screen = sent[1][-2]["content"]
    assert "NOT VISIBLE ON SCREEN" in obs_after_screen
    assert "Get it from another source" in obs_after_screen
    assert "do not call done" in obs_after_screen
    assert len(sent) == 3


def test_open_apps_are_printed_once_per_phase(monkeypatch, capsys):
    replies = iter([json.dumps({"action": "done", "speech": "ok"})])
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": ["code", "firefox"]}),
        tools=SimpleNamespace(specs=SPECS),
        api_manager=SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "u",
            get_fallback_url=lambda: "f",
        ),
        speak=lambda _s: None,
    )
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert capsys.readouterr().out.count("[Agent] Open apps: code, firefox") == 1


# ============================================================
# SYSTEM PROMPT SIZE
# ============================================================

# The agent system prompt's template carries no indentation or trailing
# spaces (the backend rejects a system prompt over 32,000 chars), and the
# whitespace strip changed no word: for several fixed states the prompt,
# with every whitespace run collapsed to one space, hashes to the value
# recorded from the indented template before the strip.
#
# When the prompt is changed ON PURPOSE, update PROMPT_HASHES and
# PLAIN_LENGTH (print them with: python -m pytest tests/test_system_prompt_size.py -q -s).


# ============================================================
# SYSTEM PROMPT SIZE HELPERS
# ============================================================

LONG_SCREEN = "\n".join(
    f"    [{i}] Button '{name} {i}' at ({10 * i}, {20 + i})  enabled"
    for i, name in enumerate(["Save", "Open", "Close", "Print", "Share", "Undo"] * 7)
)
PROJECT_BLOCK = ("WORKSPACE:\n  Current project: C:\\work\\demo-site (trusted)\n"
                 "  Files: index.html, style.css, app.js\n  Last command: npm run build -> exit 0")

STATES = {
    "plain": dict(gender="none", workspace="", screen="Window: Desktop", last="", hints=[], convo=""),
    "project": dict(gender="female", workspace=PROJECT_BLOCK, screen="Window: Visual Studio Code",
                    last="run_command", hints=[], convo=""),
    "long-screen": dict(gender="none", workspace="", screen="Window: Settings\n" + LONG_SCREEN,
                        last="click_on_text", hints=[], convo=""),
    "hints-and-conversation": dict(
        gender="male", workspace="", screen="Window: Discord", last="",
        hints=["- discord: the quick switcher is ctrl+k", "- notepad: Windows 11 restores tabs"],
        convo="\nRECENT CONVERSATION:\n  user: what's the weather\n  you: Sunny, 21 C.\n"),
}

# Recorded from the indented template (before the strip) for each state;
# updated on purpose for the batched-steps LOOP rule (default on), then for the
# merged BROWSER search rule (DuckDuckGo; two nearby rules tightened to make room),
# then for the batch form moved from the LOOP rule into RESPONSE FORMAT (the model
# never batched while the last format it read showed one action only), then for
# the steps form naming its five input actions and that run_command, file, browser
# and read actions are one per reply (the LOOP rule tightened to make room), then
# for the action menu generated from core/tools.py (per-action text moved there),
# then for the compact menu (one line per action; details via describe_action),
# then for the rules merged without dropping one (toggles, UIA inputs, browser),
# then for one short thinking sentence in RESPONSE FORMAT.
PROMPT_HASHES = {
    "plain": "653ddd2fe110da29b1abd10ad4c2c780efe4e1821f71f2b0078063a55a4bdf10",
    "project": "40e68057a35ccff2329fbf6acf91bad9cdf5d9b18ed523af8026bf80342ab87c",
    "long-screen": "b38042578c376094c431f08c0ecd1b78541e76defe9dee1a6fb314e2fdd54a2c",
    "hints-and-conversation": "9fe72f4cb61f99c157997d1ee0cd160c2fb57430262dfdd3f4a4db1d2a619f5d",
}
PLAIN_LENGTH = 13532            # 13,417 before the thinking cap; 21,056 originally


# ============================================================
# SYSTEM PROMPT SIZE BUILD
# ============================================================

def build(state):
    s = STATES[state]
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: s["workspace"]),
                          _resolve_path_placeholders=lambda p: p)
    snap = {"open_apps": [], "foreground_window": "Desktop", "screen_resolution": (1920, 1080),
            "cpu_percent": 12, "ram_percent": 48}
    text = AgentLoop(api, None)._build_system(s["gender"], snap, s["screen"], s["last"],
                                              s["hints"], s["convo"])
    # The clock is the only moving part.
    return re.sub(r"CURRENT DATE/TIME: [^\n]*", "CURRENT DATE/TIME: <now>", text)


# ============================================================
# SYSTEM PROMPT SIZE HELPERS (2)
# ============================================================

def collapsed(text):
    return " ".join(text.split())


def digest(text):
    return hashlib.sha256(collapsed(text).encode("utf-8")).hexdigest()


# ============================================================
# SYSTEM PROMPT SIZE TESTS
# ============================================================

@pytest.mark.parametrize("state", sorted(STATES))
def test_the_strip_changed_no_word(state):
    print(f"\n{state}: {digest(build(state))} ({len(build(state))} chars)")
    assert digest(build(state)) == PROMPT_HASHES[state]


# ============================================================
# SYSTEM PROMPT SIZE HELPERS (3)
# ============================================================

def _template_lines():
    src = inspect.getsource(AgentLoop._build_system).split("\n")
    start = next(i for i, l in enumerate(src) if 'return f"""You are MistAI' in l)
    end = next(i for i in range(start + 1, len(src)) if src[i].endswith('"""'))
    return src[start + 1:end + 1]


# ============================================================
# SYSTEM PROMPT SIZE TESTS (2)
# ============================================================

def test_the_template_has_no_indentation_or_trailing_spaces():
    bad = [l for l in _template_lines() if l != l.strip()]
    assert not bad, bad[:3]
    text = build("plain")
    # Line breaks and list markers are kept.
    assert "\n- run_command <the command>: Run PowerShell" in text
    assert "\n1. run_command (PowerShell)" in text
    assert "\n- done: End the task" in text


def test_interpolated_blocks_keep_their_own_layout():
    text = build("long-screen")
    assert LONG_SCREEN in text                       # the screen description is not re-indented
    assert PROJECT_BLOCK in build("project")


def test_the_plain_prompt_length_is_pinned_with_headroom():
    n = len(build("plain"))
    assert n == PLAIN_LENGTH
    assert n < BACKEND_SYSTEM_LIMIT * 0.8


# ============================================================
# ACTION PLAYBOOK
# ============================================================

# The action playbook lives in core/tools.py: every registered action has docs,
# the prompt's action menu is generated from it, and the registry, the dispatch
# in Api._exec_sync and the menu stay in sync.


# ============================================================
# ACTION PLAYBOOK HELPERS
# ============================================================

def _action_playbook_system():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


def _menu_names(system):
    block = system[system.index("AVAILABLE ACTIONS"):system.index("RESPONSE FORMAT")]
    return re.findall(r"^- ([a-z_]+)\b", block, re.M)


def _dispatched():
    src = inspect.getsource(Api._exec_sync)
    return set(re.findall(r'action == "([a-z_]+)"', src))


# ============================================================
# ACTION PLAYBOOK TESTS
# ============================================================

def test_every_registered_action_has_docs():
    missing = [n for n, s in SPECS.items() if not s.purpose.strip()]
    assert not missing, missing


def test_every_dispatched_action_is_registered_and_in_the_menu():
    names = _menu_names(_action_playbook_system())
    assert _dispatched() - set(SPECS) == set()
    assert _dispatched() - set(names) == set()


def test_menu_lists_every_registered_action_once_and_nothing_else():
    names = _menu_names(_action_playbook_system())
    assert sorted(names) == sorted(SPECS)
    assert len(names) == len(set(names))


def test_the_only_registered_actions_not_dispatched_are_the_meta_ones():
    assert set(SPECS) - _dispatched() == {"done", "ask_user"}


@pytest.mark.parametrize("name, fmt", [
    ("write_file", "path|||complete content"),
    ("patch_json", "path|||json object"),
    ("patch_file", "path|||exact existing text|||replacement text"),
    ("quick_message", "app|recipient|message"),
    ("remind_me", "text|due_time"),
    ("schedule_reminder", "seconds|text"),
    ("click_nth", "N|type"),
    ("click_coords", "x,y"),
])
def test_argument_formats_keep_their_separators_verbatim(name, fmt):
    assert SPECS[name].args == fmt
    assert fmt in action_entry(name)
    assert fmt in action_menu()


def test_take_notes_is_not_an_action():
    # It typed blindly into whatever was in front (no target check, no NOT SENT).
    assert "take_notes" not in SPECS
    assert "take_notes" not in _dispatched()
    loop = AgentLoop(SimpleNamespace(tools=SimpleNamespace(specs=SPECS)), None)
    _a, _p, note, valid = loop._resolve_action("take_notes", "hello")
    assert not valid and "not a valid action name" in note


def test_menu_fills_the_type_text_submit_hint():
    assert "{submit_hint}" not in action_menu("")
    assert "press_key('enter') (both in one steps reply)" in action_menu(" (both in one steps reply)")


# -- Stage 2: compact menu, details on demand ------------------------------------------


def test_menu_is_one_short_line_per_action_with_its_exact_format():
    lines = action_menu().splitlines()
    assert len(lines) == len(SPECS)
    for name, line in zip(SPECS, lines):
        args = SPECS[name].args
        assert line.startswith(f"- {name}" + (f" <{args}>: " if args else ": ")), line
        assert len(line) <= 200, line
    # Details stay in the registry: the menu never carries the limits text.
    assert SPECS["write_file"].limits not in action_menu()


def test_safety_meaning_stays_in_the_always_visible_lines():
    menu = action_menu()
    assert "REPLACES THE ENTIRE FILE" in menu and "NEVER appends" in menu
    assert "does NOT submit" in menu and "only appends" in menu
    assert "LAST RESORT ONLY" in menu
    assert "ONLY when the user explicitly asks" in menu


def test_describe_action_is_a_registered_read_only_action():
    spec = SPECS["describe_action"]
    assert spec.categories == ("info",) and not spec.destructive
    assert not spec.requires_confirmation and spec.settle == 0.0
    from core.tools import ToolRegistry
    reg = ToolRegistry(db_path=":memory:")
    assert not reg.requires_confirmation("describe_action", "write_file")
    loop = AgentLoop(SimpleNamespace(), None)
    assert not loop._changes_ui("describe_action", set(spec.categories))   # no screen re-read


def test_describe_action_returns_the_full_entry_or_the_closest_names():
    api = Api.__new__(Api)
    api.last_result_text = ""
    assert Api._exec_sync(api, "describe_action", "write_file") is True
    text = api.last_result_text
    assert text == action_entry("write_file")
    assert SPECS["write_file"].limits in text and "Example parameter:" in text
    assert Api._exec_sync(api, "describe_action", "writefile") is False
    assert "No action named" in api.last_result_text and "write_file" in api.last_result_text


def test_unknown_action_note_names_the_closest_actions_and_the_entry():
    loop = AgentLoop(SimpleNamespace(tools=SimpleNamespace(specs=SPECS)), None)
    _a, _p, note, valid = loop._resolve_action("browser_clik", "Subscribe")
    assert not valid
    assert "Did you mean 'browser_click'" in note
    assert action_entry("browser_click") in note
    _a, _p, note, valid = loop._resolve_action("launch_program", "x")
    assert not valid and "AVAILABLE ACTIONS" in note


@pytest.mark.parametrize("name, good, bad", [
    ("write_file", "{desktop}\\a.txt|||hi", "{desktop}\\a.txt"),
    ("patch_file", "a.txt|||old|||new", "a.txt|||old"),
    ("quick_message", "discord|Alex|hi", "discord|Alex"),
    ("click_coords", "10, 20", "the button"),
    ("brightness_set", "70", "bright"),
])
def test_argument_check_is_structural(name, good, bad):
    from core.tools import arg_problem
    assert not arg_problem(name, good)
    assert arg_problem(name, bad)
    assert not arg_problem("run_command", "anything at all")   # free-form: never flagged


def test_bad_parameter_observation_carries_the_action_entry(monkeypatch):
    import json
    import threading

    replies = iter([json.dumps({"action": "click_coords", "parameter": "the OK button"}),
                    json.dumps({"action": "done", "speech": ""})])
    sent, ran = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: ran.append(a) or SimpleNamespace(
            success=False, detail="bad coordinates", verified=None, escalate_to_user=False,
            reliability_note=lambda: "")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _s: None,
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda lazy=False: "screen")
    loop._loop("click OK", "m", "none", "text", None)
    assert ran == ["click_coords"]                      # still runs: a note, not a refusal
    obs = sent[1][-2]["content"]
    assert "FORMAT: the parameter does not match." in obs
    assert action_entry("click_coords") in obs


# -- Stage 4: shorter output (wording only) ---------------------------------------------


def test_the_format_asks_for_one_short_thinking_sentence_and_keeps_speech_whole():
    fmt = _action_playbook_system().split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
    assert '"thinking":"one short sentence"' in fmt
    assert "about 25 words at most" in fmt
    assert 'Never shorten "speech" or a parameter' in fmt


def test_the_parser_still_accepts_a_verbose_reply():
    import json
    thinking = " ".join(["word"] * 120)
    param = "C:\\x\\notes.txt|||" + "line\n" * 200
    raw = json.dumps({"thinking": thinking, "action": "write_file", "parameter": param,
                      "speech": "A long spoken answer. " * 20})
    cmd = AgentLoop(SimpleNamespace(), None)._parse_cmd(raw)
    assert cmd["action"] == "write_file"
    assert cmd["parameter"] == param and cmd["thinking"] == thinking
    assert cmd["speech"] == "A long spoken answer. " * 20


# -- MISTAI_SINGLE_SHOT (default off) ----------------------------------------------------


# ============================================================
# ACTION PLAYBOOK HELPERS (2)
# ============================================================

GOAL = "find the largest file in Downloads and tell me its size"


# ============================================================
# ACTION PLAYBOOK ROUTER
# ============================================================

def _router(monkeypatch, reply):
    from core import planner
    from core import planner as conversation
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: reply)
    api = SimpleNamespace(agent=SimpleNamespace(_recent_conversation_block=lambda _t: ""),
                          workspace=SimpleNamespace(context_block=lambda: ""))
    assert conversation.try_chat(api, GOAL) is None          # a task, never a chat reply
    return api


# ============================================================
# ACTION PLAYBOOK PLANS
# ============================================================

def _plans(monkeypatch, api, goal=GOAL):
    from core import planner
    calls = []
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: calls.append(a) or "not json")
    planner.make_plan(api, goal, "m")
    return len(calls)


# ============================================================
# ACTION PLAYBOOK TESTS (2)
# ============================================================

def test_single_shot_off_changes_nothing(monkeypatch):
    from core import planner as conversation
    monkeypatch.delenv("MISTAI_SINGLE_SHOT", raising=False)
    assert '"single"' not in conversation.build_prompt("x", "now")
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert getattr(api, "_single_shot_goal", None) is None
    assert _plans(monkeypatch, api) == 1                     # the planning call still happens


def test_single_shot_yes_skips_the_planning_call_for_that_goal_only(monkeypatch):
    from core import planner as conversation
    monkeypatch.setenv("MISTAI_SINGLE_SHOT", "1")
    p = conversation.build_prompt("x", "now", single=True)
    assert '"route": "chat" or "task"' in p and '"single": true or false' in p
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert _plans(monkeypatch, api) == 0
    assert api._single_shot_goal is None                     # consumed
    assert _plans(monkeypatch, api) == 1                     # not reused for the next goal
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert _plans(monkeypatch, api, goal="open notepad then write a long poem about it") == 1


@pytest.mark.parametrize("reply", [
    '{"route": "task", "reply": "", "single": false}',
    '{"route": "task", "reply": ""}',
    '{"route": "task", "reply": "", "single": "true"}',
    "not json",
    "",
])
def test_single_shot_fails_closed_to_planning(monkeypatch, reply):
    monkeypatch.setenv("MISTAI_SINGLE_SHOT", "1")
    api = _router(monkeypatch, reply)
    assert _plans(monkeypatch, api) == 1


# ============================================================
# SKIP VERIFIED READ
# ============================================================

# MISTAI_SKIP_VERIFIED_READ (default on): after open_app/switch_app succeeded,
# verified True at confidence >= 0.93 with that app in front, the NEXT read is
# title + Open apps (lazy=2) instead of vision, once per phase (gated mode).


# ============================================================
# SKIP VERIFIED READ RUN
# ============================================================

def _skip_verified_read_run(monkeypatch, script, results=None, in_front=True, gating=True):
    """Scripted phase; returns the list of reads: "vision" or the lazy level."""
    monkeypatch.setenv("MISTAI_VISION_GATING", "1" if gating else "0")
    monkeypatch.setenv("MISTAI_LAZY_VISION", "0")   # these tests pin a vision first read
    results = results or {}
    replies = iter([json.dumps({"action": a, "parameter": p}) for a, p in script]
                   + [json.dumps({"action": "done", "speech": "ok"})])
    snap = {"open_apps": [], "foreground_hwnd": 1, "foreground_window": "Editor"}

    def run(action, param):
        r = dict(success=True, verified=True, confidence=0.93)
        r.update(results.get(action, {}))
        return SimpleNamespace(action=action, parameter=param, detail="", escalate_to_user=False,
                               reliability_note=lambda: "", escalated_from="", **r)

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snap),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u",
                                    get_fallback_url=lambda: "f"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _s: None,
        _foreground_process_matches=lambda app: in_front if not callable(in_front) else in_front(app),
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    reads = []
    monkeypatch.setattr(loop, "_get_screen_context",
                        lambda lazy=False: reads.append(lazy or "vision") or "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    assert loop._loop("t", "m", "none", "text", None)["success"]
    return reads


# ============================================================
# SKIP VERIFIED READ TESTS
# ============================================================

class TestSkipVerifiedRead:
    @pytest.fixture(autouse=True)
    def _default_flag(self, monkeypatch):
        monkeypatch.delenv("MISTAI_SKIP_VERIFIED_READ", raising=False)

    @pytest.mark.parametrize("action", ["open_app", "switch_app"])
    def test_a_verified_launch_skips_the_next_vision_read_once(self, monkeypatch, capsys, action):
        reads = _skip_verified_read_run(monkeypatch, [(action, "notepad"), ("type_text", "hi")])
        assert reads == ["vision", 2, "vision"]       # first read unchanged; then lazy; then vision
        assert "[Screen] after a verified launch: title + Open apps, no vision" in capsys.readouterr().out

    def test_once_per_phase(self, monkeypatch):
        reads = _skip_verified_read_run(monkeypatch, [("open_app", "notepad"), ("switch_app", "calculator"),
                                   ("type_text", "1")])
        assert reads == ["vision", 2, "vision", "vision"]

    @pytest.mark.parametrize("results, in_front", [
        ({"open_app": {"success": False}}, True),            # a failure
        ({"open_app": {"verified": None}}, True),            # unverified
        ({"open_app": {"verified": False}}, True),
        ({"open_app": {"confidence": 0.92}}, True),          # below 0.93
        ({}, False),                                         # the app is not in front
    ])
    def test_each_condition_is_required(self, monkeypatch, results, in_front):
        assert _skip_verified_read_run(monkeypatch, [("open_app", "notepad")], results, in_front) == ["vision", "vision"]

    @pytest.mark.parametrize("action", ["type_text", "click_on_text", "press_key", "window_activate",
                                        "open_url"])
    def test_other_actions_never_skip(self, monkeypatch, action):
        assert _skip_verified_read_run(monkeypatch, [(action, "x")]) == ["vision", "vision"]

    def test_the_foreground_check_uses_the_launched_app(self, monkeypatch):
        asked = []
        reads = _skip_verified_read_run(monkeypatch, [("open_app", "notepad")],
                     in_front=lambda app: asked.append(app) or app == "notepad")
        assert asked == ["notepad"] and reads == ["vision", 2]

    @pytest.mark.parametrize("value", ["0", "off", "false"])
    def test_flag_off_restores_todays_reads(self, monkeypatch, value):
        monkeypatch.setenv("MISTAI_SKIP_VERIFIED_READ", value)
        assert _skip_verified_read_run(monkeypatch, [("open_app", "notepad"), ("type_text", "hi")]) == [
            "vision", "vision", "vision"]

    def test_read_at_call_time(self, monkeypatch):
        from core.config import skip_verified_read_enabled
        assert skip_verified_read_enabled() is True
        monkeypatch.setenv("MISTAI_SKIP_VERIFIED_READ", "0")
        assert skip_verified_read_enabled() is False

    def test_gating_off_is_unchanged(self, monkeypatch):
        # Without gating the read cadence is the old one; the skip never applies.
        reads = _skip_verified_read_run(monkeypatch, [("open_app", "notepad"), ("type_text", "hi")], gating=False)
        assert 2 not in reads
