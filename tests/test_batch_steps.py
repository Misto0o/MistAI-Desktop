"""Batched steps (MISTAI_BATCH_STEPS, default on): one model reply may carry a
short "steps" list of predictable input actions. Every step runs through the
same per-step path as a single action (guards, confirmation and Enter rules,
Executor), the batch stops at the first failure, refusal, No or a target that
left the front, and a malformed list is a parse retry."""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.tools import ToolRegistry
from confirm_helpers import bind_confirmation

DONE = {"action": "done", "speech": "Done."}


def batch(*steps, **extra):
    return dict({"thinking": "t", "steps": [{"action": a, "parameter": p} for a, p in steps],
                 "speech": ""}, **extra)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.delenv("MISTAI_BATCH_STEPS", raising=False)
    monkeypatch.setenv("MISTAI_VISION_GATING", "1")


def _run(monkeypatch, tmp_path, replies, answers=(), fail=(), enter_safe=False,
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


def _messages(rec):
    return rec.requests[-1]["messages"] if rec.requests else []


def _observations(rec):
    return [m["content"] for m in _messages(rec)
            if m["role"] == "user" and "[OBSERVATION]" in m["content"]]


STEPS3 = (("open_app", "notepad"), ("type_text", "hello"), ("press_key", "tab"))


def test_batch_of_three_runs_in_order_with_one_model_call(monkeypatch, tmp_path, capsys):
    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE])
    assert rec.calls == list(STEPS3)
    assert len(rec.requests) == 2                  # the batch, then the done
    assert rec.result["success"] is True
    # Each step is recorded as its own action + observation, with no internals.
    sent = [json.loads(m["content"]) for m in _messages(rec) if m["role"] == "assistant"]
    assert [(s["action"], s["parameter"]) for s in sent] == list(STEPS3)
    assert not any("_batch" in s or "steps" in s for s in sent)
    assert len(_observations(rec)) == 3
    assert [a for _n, a in rec.steps] == ["open_app", "type_text", "press_key", "done"]
    log = capsys.readouterr().out
    assert "[Batch] 3 steps in one reply" in log
    assert log.count("[Batch] next step, no model call") == 2
    assert "hello" not in log                      # content-free


def test_queued_steps_skip_the_screen_read(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE])
    assert len(rec.reads) == 2                     # before the batch, before the done


def test_batch_stops_at_the_first_failure(monkeypatch, tmp_path, capsys):
    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE], fail={("type_text", "hello")})
    assert rec.calls == [("open_app", "notepad"), ("type_text", "hello")]
    obs = _observations(rec)
    assert "FAILED" in obs[1]
    assert obs[2] == ("[OBSERVATION] Batch stopped; these steps were NOT run: press_key(tab). "
                      "Decide the next step from the current state.")
    assert "[Batch] stopped: 1 step(s) not run" in capsys.readouterr().out


def test_batch_stops_when_the_target_left_the_front(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE], in_front=lambda app: False)
    assert rec.calls == [("open_app", "notepad")]
    assert "NOT run: type_text(hello), press_key(tab)" in _observations(rec)[1]


def test_target_check_errors_stop_the_batch(monkeypatch, tmp_path):
    def boom(_app):
        raise OSError("gone")

    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE], in_front=boom)
    assert rec.calls == [("open_app", "notepad")]


@pytest.mark.parametrize("enter_safe", [False, True])
def test_enter_in_a_batch_asks_exactly_when_it_would_ask_alone(monkeypatch, tmp_path,
                                                                enter_safe):
    alone = _run(monkeypatch, tmp_path,
                 [{"action": "press_key", "parameter": "enter"}, DONE], ["yes"],
                 enter_safe=enter_safe)
    batched = _run(monkeypatch, tmp_path,
                   [batch(("type_text", "37*23"), ("press_key", "enter")), DONE], ["yes"],
                   enter_safe=enter_safe)
    assert len(alone.asked) == len(batched.asked) == (0 if enter_safe else 1)
    assert batched.calls == [("type_text", "37*23"), ("press_key", "enter")]


def test_confirmation_no_stops_the_batch(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path,
               [batch(("type_text", "hi"), ("press_key", "enter"), ("type_text", "more")), DONE],
               ["no"])
    assert rec.calls == [("type_text", "hi")]       # Enter declined, the rest never ran
    assert len(rec.asked) == 1
    obs = _observations(rec)
    assert any("User explicitly declined" in o for o in obs)
    assert "NOT run: type_text(more)" in obs[-1]


def test_a_batch_never_merges_confirmations(monkeypatch, tmp_path):
    # Two Enters: each asks on its own; the second question is a second question.
    rec = _run(monkeypatch, tmp_path,
               [batch(("press_key", "enter"), ("type_text", "x"), ("press_key", "enter")), DONE],
               ["yes", "yes"])
    assert len(rec.asked) == 2
    assert rec.calls == [("press_key", "enter"), ("type_text", "x"), ("press_key", "enter")]


def test_stop_during_a_batch_halts_it(monkeypatch, tmp_path):
    def stop_after_first(api, a, p):
        api._stop_requested.set()

    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE], on_run=stop_after_first)
    assert rec.calls == [("open_app", "notepad")]
    assert rec.result == {"success": False, "error": "Stopped by user"}


@pytest.mark.parametrize("reply", [
    batch(("open_app", "notepad"), ("read_screen_text", "what is shown?")),   # a read
    batch(("type_text", "x"), ("done", "")),                                  # done
    batch(("click_on_text", "OK"), ("press_key", "tab")),                     # not batchable
    batch(("press_key", "tab")),                                              # one step
    batch(*([("press_key", "tab")] * 5)),                                     # too many
    batch(*STEPS3, action="open_app"),                                        # both forms
    {"thinking": "t", "steps": "open_app notepad"},                           # not a list
    {"steps": [{"parameter": "x"}, {"action": "press_key", "parameter": "tab"}]},
    {"steps": [{"action": "type_text", "parameter": {"a": 1}},
               {"action": "press_key", "parameter": "tab"}]},
])
def test_a_malformed_batch_is_a_parse_retry(monkeypatch, tmp_path, reply):
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == []
    obs = _observations(rec)
    assert obs[0] == AgentLoop.BATCH_RETRY_NOTE
    assert "NOTHING was run" in obs[0]


def test_three_malformed_batches_end_the_phase(monkeypatch, tmp_path):
    bad = batch(("open_app", "notepad"), ("analyze_screen", ""))
    rec = _run(monkeypatch, tmp_path, [bad, bad, bad])
    assert rec.calls == []
    assert "3x in a row" in rec.result["error"]


def test_a_batch_wrapped_in_text_is_still_one_batch(monkeypatch, tmp_path):
    text = "Here is the plan:\n" + json.dumps(batch(*STEPS3)) + "\nThat's it."
    rec = _run(monkeypatch, tmp_path, [text, DONE])
    assert rec.calls == list(STEPS3)


def test_flag_off_keeps_one_action_per_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
    rec = _run(monkeypatch, tmp_path, [batch(*STEPS3), DONE])
    # Today's parsing: a reply without a top-level action yields its first object.
    assert rec.calls == [("open_app", "notepad")]
    assert '"steps"' not in rec.requests[0]["system"]
    assert "decide exactly ONE action" in rec.requests[0]["system"]


def test_the_prompt_carries_one_merged_batch_rule(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [DONE])
    system = rec.requests[0]["system"]
    assert system.count('"steps":[') == 1
    assert "no reads, no done" in system
    assert "decide exactly ONE action" not in system
