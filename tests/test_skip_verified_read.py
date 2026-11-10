"""MISTAI_SKIP_VERIFIED_READ (default on): after open_app/switch_app succeeded,
verified True at confidence >= 0.93 with that app in front, the NEXT read is
title + Open apps (lazy=2) instead of vision, once per phase (gated mode)."""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.tools import SPECS


def _run(monkeypatch, script, results=None, in_front=True, gating=True):
    """Scripted phase; returns the list of reads: "vision" or the lazy level."""
    monkeypatch.setenv("MISTAI_VISION_GATING", "1" if gating else "0")
    monkeypatch.delenv("MISTAI_LAZY_VISION", raising=False)
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


@pytest.fixture(autouse=True)
def _default_flag(monkeypatch):
    monkeypatch.delenv("MISTAI_SKIP_VERIFIED_READ", raising=False)


@pytest.mark.parametrize("action", ["open_app", "switch_app"])
def test_a_verified_launch_skips_the_next_vision_read_once(monkeypatch, capsys, action):
    reads = _run(monkeypatch, [(action, "notepad"), ("type_text", "hi")])
    assert reads == ["vision", 2, "vision"]       # first read unchanged; then lazy; then vision
    assert "[Screen] after a verified launch: title + Open apps, no vision" in capsys.readouterr().out


def test_once_per_phase(monkeypatch):
    reads = _run(monkeypatch, [("open_app", "notepad"), ("switch_app", "calculator"),
                               ("type_text", "1")])
    assert reads == ["vision", 2, "vision", "vision"]


@pytest.mark.parametrize("results, in_front", [
    ({"open_app": {"success": False}}, True),            # a failure
    ({"open_app": {"verified": None}}, True),            # unverified
    ({"open_app": {"verified": False}}, True),
    ({"open_app": {"confidence": 0.92}}, True),          # below 0.93
    ({}, False),                                         # the app is not in front
])
def test_each_condition_is_required(monkeypatch, results, in_front):
    assert _run(monkeypatch, [("open_app", "notepad")], results, in_front) == ["vision", "vision"]


@pytest.mark.parametrize("action", ["type_text", "click_on_text", "press_key", "window_activate",
                                    "open_url"])
def test_other_actions_never_skip(monkeypatch, action):
    assert _run(monkeypatch, [(action, "x")]) == ["vision", "vision"]


def test_the_foreground_check_uses_the_launched_app(monkeypatch):
    asked = []
    reads = _run(monkeypatch, [("open_app", "notepad")],
                 in_front=lambda app: asked.append(app) or app == "notepad")
    assert asked == ["notepad"] and reads == ["vision", 2]


@pytest.mark.parametrize("value", ["0", "off", "false"])
def test_flag_off_restores_todays_reads(monkeypatch, value):
    monkeypatch.setenv("MISTAI_SKIP_VERIFIED_READ", value)
    assert _run(monkeypatch, [("open_app", "notepad"), ("type_text", "hi")]) == [
        "vision", "vision", "vision"]


def test_read_at_call_time(monkeypatch):
    from core.config import skip_verified_read_enabled
    assert skip_verified_read_enabled() is True
    monkeypatch.setenv("MISTAI_SKIP_VERIFIED_READ", "0")
    assert skip_verified_read_enabled() is False


def test_gating_off_is_unchanged(monkeypatch):
    # Without gating the read cadence is the old one; the skip never applies.
    reads = _run(monkeypatch, [("open_app", "notepad"), ("type_text", "hi")], gating=False)
    assert 2 not in reads
