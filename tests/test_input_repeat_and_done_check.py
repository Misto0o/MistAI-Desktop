"""AgentLoop: identical-input cap, repeat wording, and the done-time check."""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.tools import SPECS


def _result(action, detail="", success=True, escalated_from=""):
    return SimpleNamespace(
        action=action, parameter="", success=success, detail=detail, verified=None,
        escalate_to_user=False, reliability_note=lambda: "", escalated_from=escalated_from,
    )


def _run(monkeypatch, replies, results=None, foreground=None, task="task"):
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


DONE = {"action": "done", "speech": "It shows 23."}


# -- identical input ------------------------------------------------------------------


def test_identical_inputs_both_run_and_get_a_neutral_note(monkeypatch):
    out, _, calls, obs, _ = _run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        DONE,
    ])
    assert out["success"]
    assert calls == [("press_key", "3"), ("press_key", "3")]
    assert "repeat it only if you mean to" in obs[1]
    assert "REFUSED" not in " ".join(obs)
    assert "did take effect" not in " ".join(obs)


def test_identical_input_is_refused_after_three_in_a_row_on_an_unchanged_window(monkeypatch):
    out, _, calls, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        {"action": "click_on_text", "parameter": "3"},
        DONE,
    ])
    assert calls == [("click_on_text", "3")] * 3
    assert "already ran 3 times in a row" in obs[3]


def test_a_window_change_or_another_action_resets_the_input_count(monkeypatch):
    keys = iter([(1, "A"), (1, "A"), (2, "B"), (2, "B"), (2, "B"), (2, "B")])
    _, _, calls, _, _ = _run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},   # foreground changed: count restarts
        {"action": "press_key", "parameter": "3"},
        DONE,
    ], foreground=keys)
    assert calls == [("press_key", "3")] * 4

    _, _, calls, _, _ = _run(monkeypatch, [
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "3"},
        {"action": "press_key", "parameter": "+"},
        {"action": "press_key", "parameter": "3"},
        DONE,
    ])
    assert calls[-1] == ("press_key", "3") and len(calls) == 5


def test_non_input_side_effects_are_still_refused_on_repeat(monkeypatch):
    _, _, calls, obs, _ = _run(monkeypatch, [
        {"action": "open_app", "parameter": "calculator"},
        {"action": "open_app", "parameter": "calculator"},
        DONE,
    ])
    assert calls == [("open_app", "calculator")]
    assert "already ran without error" in obs[1]


def test_input_target_is_reset_at_phase_start(monkeypatch):
    _, api, _, _, _ = _run(monkeypatch, [DONE])
    assert api._last_opened_app is None


# -- done-time check --------------------------------------------------------------------


def test_plain_phase_finishes_on_the_first_done(monkeypatch):
    out, _, _, obs, spoken = _run(monkeypatch, [{"action": "press_key", "parameter": "3"}, DONE])
    assert out["success"] and spoken == ["It shows 23."]
    assert not any("Before finishing" in o for o in obs)


def test_screen_read_gets_one_check_when_the_speech_states_unseen_numbers(monkeypatch):
    read = ("Vision: 851\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): "
            "Display is 23")
    read += " | x" * 200
    out, _, _, obs, spoken = _run(monkeypatch, [
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
    out, _, _, obs, _ = _run(monkeypatch, [
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
    out, _, calls, obs, spoken = _run(monkeypatch, [
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


def test_typing_that_ends_in_a_newline_counts_as_submitted(monkeypatch):
    read = "EXACT UI TEXT of 'Terminal' (accessibility tree, exact): 42"
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "type_text", "parameter": "echo 42\n"},
        {"action": "read_screen_text", "parameter": "output?"},
        {"action": "done", "speech": "It printed 42."},
    ], results={"read_screen_text": _result("read_screen_text", read)})
    assert out["speech"] == "It printed 42."
    assert not any("Before finishing" in o for o in obs)


def test_the_second_done_always_finishes(monkeypatch):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "q"},
        {"action": "done", "speech": "It is 5."},
        {"action": "read_screen_text", "parameter": "q2"},
        {"action": "done", "speech": "It is 7."},
    ], results={"read_screen_text": _result("read_screen_text", "Display is 23")})
    assert out["speech"] == "It is 7."
    assert sum("Before finishing" in o for o in obs) == 1


@pytest.mark.parametrize("task", ["whats the weather?", "is Edge open"])
def test_question_finished_with_empty_speech_gets_one_reminder(monkeypatch, task):
    out, _, _, obs, spoken = _run(monkeypatch, [
        {"action": "browser_get_text", "parameter": ""},
        {"action": "done", "speech": ""},
        {"action": "done", "speech": "It is 18 degrees and sunny."},
    ], results={"browser_get_text": _result("browser_get_text", "18 degrees, sunny")},
        task=task)
    assert out["success"] and spoken == ["It is 18 degrees and sunny."]
    assert sum("Before finishing" in o for o in obs) == 1
    assert "Your speech is empty but the user asked a question" in obs[-1]


def test_question_second_empty_done_still_finishes(monkeypatch):
    out, _, _, obs, spoken = _run(monkeypatch, [
        {"action": "done", "speech": ""},
        {"action": "done", "speech": ""},
    ], task="whats the weather?")
    assert out["success"] and out["speech"] == "" and spoken == []
    assert sum("Before finishing" in o for o in obs) == 1


def test_non_question_empty_done_finishes_without_a_reminder(monkeypatch):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "browser_get_text", "parameter": ""},
        {"action": "done", "speech": ""},
    ], task="open notepad")
    assert out["success"] and out["speech"] == ""
    assert not any("Before finishing" in o for o in obs)


def test_off_target_input_is_named_in_the_check(monkeypatch):
    refused = _result("type_text", "NOT SENT: the text was not sent because calculator is "
                                   "not in front ('main.py - VS Code' is).", success=False)
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "type_text", "parameter": "37*23="},
        DONE, DONE,
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
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": escalated_from, "parameter": "x"}, DONE, DONE,
    ], results={escalated_from: _result(executed, "ok", escalated_from=escalated_from)})
    assert out["success"]
    fired = any("Before finishing" in o for o in obs)
    assert fired is fires
    if fires:
        assert f"{executed} ran as an automatic fallback for {escalated_from}" in obs[-1]
