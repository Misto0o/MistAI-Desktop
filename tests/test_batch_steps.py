"""Batched steps (MISTAI_BATCH_STEPS, default on): one model reply may carry a
short "steps" list of predictable input actions. Every step runs through the
same per-step path as a single action (guards, confirmation and Enter rules,
Executor), the batch stops at the first failure, refusal, No or a target that
left the front. A partly valid list runs its usable part (a batchable prefix
of 2+, else its first step alone) and the model is told which steps did not
run; only a list with no usable first step is a parse retry, with its reason."""

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


# --- Degraded lists: the valid part runs, the rest is reported, never run ------

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
def test_a_partly_valid_list_runs_its_usable_part_and_reports_the_rest(
        monkeypatch, tmp_path, capsys, reply, ran, label, dropped):
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == ran                        # dropped steps never execute
    assert len(rec.requests) == 2                  # no wasted model call
    note = _observations(rec)[-1]
    assert note.startswith("[OBSERVATION] These steps of your list were NOT run (")
    assert dropped in note and "one reply of its own" in note
    log = capsys.readouterr().out
    assert f"[Batch] malformed steps list ({label}) -- running {len(ran)} of" in log
    assert "parse retry" not in log


def test_a_list_starting_with_run_command_runs_it_alone(monkeypatch, tmp_path, capsys):
    reply = batch(("run_command", "Get-Date"), ("open_app", "notepad"), ("type_text", "hi"))
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == [("run_command", "Get-Date")]
    sent = [json.loads(m["content"]) for m in _messages(rec) if m["role"] == "assistant"]
    assert sent[0] == {"action": "run_command", "parameter": "Get-Date",
                       "thinking": "t", "speech": ""}       # no internals in history
    obs = _observations(rec)
    assert len(obs) == 2                           # the command's result, then the note
    assert "'run_command' cannot share a reply" in obs[1]
    assert "open_app(notepad), type_text(hi)" in obs[1]
    log = capsys.readouterr().out
    assert "(non-batchable-action run_command) -- running 1 of 3 steps" in log
    assert "Get-Date" not in log and "notepad" not in log.split("[Batch]", 1)[1].split("\n")[0]


def test_a_valid_prefix_then_a_non_batchable_step_runs_only_the_prefix(monkeypatch, tmp_path):
    reply = batch(("open_app", "notepad"), ("type_text", "hi"), ("run_command", "Get-Date"),
                  ("press_key", "tab"))
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == [("open_app", "notepad"), ("type_text", "hi")]
    assert len(rec.requests) == 2
    obs = _observations(rec)
    assert len(obs) == 3                           # both steps' results, then the note
    assert "run_command(Get-Date), press_key(tab)" in obs[2]


def test_a_stopped_prefix_reports_its_queued_steps_and_the_dropped_ones(monkeypatch, tmp_path):
    reply = batch(("open_app", "notepad"), ("type_text", "hi"), ("run_command", "Get-Date"))
    rec = _run(monkeypatch, tmp_path, [reply, DONE], fail={("open_app", "notepad")})
    assert rec.calls == [("open_app", "notepad")]
    obs = _observations(rec)
    assert "Batch stopped; these steps were NOT run: type_text(hi)" in obs[1]
    assert "NOT run" in obs[2] and "run_command(Get-Date)" in obs[2]


@pytest.mark.parametrize("answer, ran", [
    ("no", [("type_text", "x")]),
    ("yes", [("type_text", "x"), ("press_key", "enter")]),
])
def test_enter_in_a_degraded_batch_still_asks(monkeypatch, tmp_path, answer, ran):
    reply = batch(("type_text", "x"), ("press_key", "enter"), ("run_command", "Get-Date"))
    rec = _run(monkeypatch, tmp_path, [reply, DONE], [answer])
    assert len(rec.asked) == 1
    assert rec.calls == ran                        # run_command never runs either way


def test_a_mutating_command_run_alone_from_a_list_still_asks(monkeypatch, tmp_path):
    reply = batch(("run_command", "Remove-Item C:\\scratch\\old.txt"), ("type_text", "hi"))
    rec = _run(monkeypatch, tmp_path, [reply, DONE], ["no"])
    assert len(rec.asked) == 1
    assert rec.calls == []                         # declined; the dropped step never ran


def test_reason_labels_are_content_free(monkeypatch, tmp_path, capsys):
    reply = {"steps": [{"action": "type_text", "parameter": "SECRETWORD"},
                       {"action": "press_key", "parameter": "tab"},
                       {"action": "leak_SECRETNAME", "parameter": "SECRETPARAM"}]}
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == [("type_text", "SECRETWORD"), ("press_key", "tab")]
    log = capsys.readouterr().out
    assert "(non-batchable-action unknown) -- running 2 of 3 steps" in log
    assert "SECRET" not in log


def test_a_bare_list_with_a_non_batchable_first_step_runs_it_alone(monkeypatch, tmp_path):
    steps = [{"action": "run_command", "parameter": "Get-Date"},
             {"action": "type_text", "parameter": "x"}]
    rec = _run(monkeypatch, tmp_path, [json.dumps(steps), DONE])
    assert rec.calls == [("run_command", "Get-Date")]
    assert "type_text(x)" in _observations(rec)[-1]


def test_a_one_step_list_runs_alone_without_a_note(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [batch(("press_key", "tab")), DONE])
    assert rec.calls == [("press_key", "tab")]
    assert not any("NOT run" in o for o in _observations(rec))


# --- Unusable lists: parse_retry with the specific reason ----------------------

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
def test_an_unusable_list_is_a_parse_retry_with_its_reason(monkeypatch, tmp_path, capsys,
                                                           reply, label, why):
    rec = _run(monkeypatch, tmp_path, [reply, DONE])
    assert rec.calls == []
    obs = _observations(rec)
    assert obs[0] == AgentLoop.BATCH_RETRY_NOTE.format(
        why=AgentLoop.BATCH_REASONS[label].format(action=""))
    assert why in obs[0] and "NOTHING was run" in obs[0]
    assert f"[Batch] malformed steps list ({label}) -- parse retry" in capsys.readouterr().out


def test_three_malformed_batches_end_the_phase(monkeypatch, tmp_path):
    bad = {"thinking": "t", "steps": []}
    rec = _run(monkeypatch, tmp_path, [bad, bad, bad])
    assert rec.calls == []
    assert "3x in a row" in rec.result["error"]


def test_flag_off_runs_a_lists_first_object_with_no_note(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
    rec = _run(monkeypatch, tmp_path,
               [batch(("run_command", "Get-Date"), ("type_text", "y")), DONE])
    assert rec.calls == [("run_command", "Get-Date")]
    assert not any("NOT run" in o for o in _observations(rec))
    assert "[Batch]" not in capsys.readouterr().out


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


def test_a_bare_list_of_input_steps_is_one_batch(monkeypatch, tmp_path, capsys):
    steps = [{"action": a, "parameter": p} for a, p in STEPS3]
    rec = _run(monkeypatch, tmp_path, [json.dumps(steps), DONE])
    assert rec.calls == list(STEPS3)
    assert len(rec.requests) == 2
    log = capsys.readouterr().out
    assert "[Reply] shape=array of 3" in log and "hello" not in log


def test_a_list_with_a_non_input_step_keeps_its_first_object(monkeypatch, tmp_path):
    steps = [{"action": "type_text", "parameter": "x"},
             {"action": "read_screen_text", "parameter": "q"}]
    rec = _run(monkeypatch, tmp_path, [json.dumps(steps), DONE])
    assert rec.calls == [("type_text", "x")]
    assert "read_screen_text(q)" in _observations(rec)[-1]   # no longer dropped silently


def test_a_bare_list_is_not_a_batch_with_the_flag_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
    steps = [{"action": a, "parameter": p} for a, p in STEPS3]
    rec = _run(monkeypatch, tmp_path, [json.dumps(steps), DONE])
    assert rec.calls == [("open_app", "notepad")]


def test_every_reply_logs_its_shape_only(monkeypatch, tmp_path, capsys):
    _run(monkeypatch, tmp_path, [batch(*STEPS3), "not json at all", DONE])
    log = capsys.readouterr().out
    assert "[Reply] shape=steps" in log
    assert "[Reply] shape=text" in log
    assert "[Reply] shape=object" in log
    assert "not json" not in log


def test_the_format_block_shows_both_shapes_and_the_last_message_allows_steps(
        monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [DONE])
    system = rec.requests[0]["system"]
    fmt = system.split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
    assert '"action":"..."' in fmt and '"steps":[' in fmt
    assert "(both in one steps reply)" in system
    assert rec.requests[0]["messages"][-1]["content"] == (
        "What next? Reply with JSON only: one action, or a steps list.")


def test_flag_off_keeps_the_old_format_and_last_message(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_BATCH_STEPS", "0")
    rec = _run(monkeypatch, tmp_path, [DONE])
    assert "steps" not in rec.requests[0]["system"].split("RESPONSE FORMAT", 1)[1]
    assert "(both in one steps reply)" not in rec.requests[0]["system"]
    assert rec.requests[0]["messages"][-1]["content"] == (
        "What is your next action? Reply with JSON only.")


def test_the_prompt_carries_one_merged_batch_rule(monkeypatch, tmp_path):
    rec = _run(monkeypatch, tmp_path, [DONE])
    system = rec.requests[0]["system"]
    assert system.count('"steps":[') == 1
    assert "no reads, no done" in system
    fmt = system.split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
    assert "ONLY open_app/switch_app/type_text/press_key/hotkey" in fmt
    assert "run_command, file, browser and read actions are one per reply" in fmt
    assert "decide exactly ONE action" not in system
