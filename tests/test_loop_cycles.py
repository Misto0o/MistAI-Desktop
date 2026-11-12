"""Loop cycle guard (AgentLoop + CycleGuard): the same 2 or 3 executed steps
going round twice (A B A B, A B C A B C, identical parameters) add a one-time
note to the observation; the same cycle once more ends the phase with
"I got stuck going in circles, so I stopped." (success False, error "loop").
A changed parameter never counts."""

import json
import threading
from types import SimpleNamespace

from core.agent import LOOP_NOTE, LOOP_SPEECH, AgentLoop, CycleGuard
from core.tools import SPECS

READ = {"action": "read_screen_text", "parameter": "what is the status?"}
CLICK = {"action": "click_on_text", "parameter": "Next"}
KEY = {"action": "press_key", "parameter": "tab"}
DONE = {"action": "done", "speech": "ok"}


def _run(monkeypatch, replies):
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


def _observations(rec):
    msgs = rec.requests[-1]["messages"]
    return [m["content"] for m in msgs if m["role"] == "user" and "[OBSERVATION]" in m["content"]]


def test_a_b_a_b_warns_then_stops(monkeypatch):
    rec = _run(monkeypatch, [READ, CLICK, READ, CLICK, READ, CLICK, DONE])
    assert rec.result == {"success": False, "error": "loop", "speech": LOOP_SPEECH, "steps": 6}
    assert rec.spoken == [LOOP_SPEECH]
    assert len(rec.calls) == 6
    obs = _observations(rec)
    assert [LOOP_NOTE in o for o in obs] == [False, False, False, True, False]


def test_a_b_c_a_b_c_warns_then_stops(monkeypatch):
    rec = _run(monkeypatch, [READ, CLICK, KEY] * 3 + [DONE])
    assert rec.result["error"] == "loop" and len(rec.calls) == 9
    obs = _observations(rec)
    assert [i for i, o in enumerate(obs) if LOOP_NOTE in o] == [5]


def test_a_warning_followed_by_something_else_finishes_normally(monkeypatch):
    other = {"action": "read_screen_text", "parameter": "is it done now?"}
    rec = _run(monkeypatch, [READ, CLICK, READ, CLICK, other, DONE])
    assert rec.result["success"] is True and rec.spoken == ["ok"]


def test_changing_parameters_never_trigger(monkeypatch):
    replies = []
    for i in range(6):
        replies += [{"action": "read_screen_text", "parameter": f"status after {i} s?"}, CLICK]
    rec = _run(monkeypatch, replies + [DONE])
    assert rec.result.get("error") != "loop" and len(rec.calls) == 12
    assert not any(LOOP_NOTE in o for o in _observations(rec))


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
    _run(monkeypatch, [READ, CLICK, READ, CLICK, READ, CLICK, DONE])
    out = capsys.readouterr().out
    assert "[Loop] repeating steps; note added" in out
    assert "[Loop] the same steps repeated again after a warning; the phase ends" in out
    loop_lines = [l for l in out.splitlines() if "[Loop]" in l]
    assert not any("Next" in l or "status" in l for l in loop_lines)
