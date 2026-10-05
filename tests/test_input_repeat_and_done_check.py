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


def _run(monkeypatch, replies, results=None, foreground=None):
    """Run one phase. ``results`` maps action -> ExecutionResult-like (or a
    callable(param)); ``foreground`` is an iterator of foreground keys."""
    results = results or {}
    replies = iter(json.dumps(r) for r in replies)
    calls, sent, spoken = [], [], []

    def run(action, param):
        calls.append((action, param))
        r = results.get(action, _result(action))
        return r(param) if callable(r) else r

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
    out = loop._loop("task", "m", "none", "text", None)
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


def test_screen_read_gets_one_check_quoting_the_read(monkeypatch):
    read = "Vision: 851\nEXACT UI TEXT of 'Calculator' (accessibility tree, exact): Display is 23"
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
    assert "never a value you worked out yourself" in check
    assert out["success"] and out["speech"] == "It shows 23."
    assert spoken == ["It shows 23."]  # the first done was never spoken


def test_the_second_done_always_finishes(monkeypatch):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "q"},
        {"action": "done", "speech": "A"},
        {"action": "read_screen_text", "parameter": "q2"},
        {"action": "done", "speech": "B"},
    ], results={"read_screen_text": _result("read_screen_text", "Display is 23")})
    assert out["speech"] == "B"
    assert sum("Before finishing" in o for o in obs) == 1


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
