"""Outbound scrub (config.scrub_outbound, applied in AgentLoop where the
request is assembled): secrets in the screen context and in observations are
replaced by "[REDACTED:<kind>]" before they leave the device; the user's task
and answers are untouched; MISTAI_SCRUB_OUTBOUND=0 turns it off; the log
prints kind counts only. Secret-shaped values are built at runtime, so the
repo holds no literal token."""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.config import luhn_ok, scrub_outbound
from core.tools import SPECS

A36 = "Ab1" * 12
PEM = ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA" + "x" * 40
       + "\n-----END " + "RSA PRIVATE KEY-----")
JWT = "ey" + "JhbGciOiJIUzI1NiJ9." + "ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N"
SECRETS = {
    "private_key": PEM,
    "aws_key": "AK" + "IA" + "IOSFODNN7EXAMPLE",
    "google_key": "AI" + "za" + "SyD" + "x" * 32,
    "github_token": "gh" + "p_" + A36,
    "slack_token": "xo" + "xb-" + "123456789012-abcdefABCDEF",
    "jwt": JWT,
    "card": "4111 1111 1111 1111",
}


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_each_pattern(kind):
    clean, counts = scrub_outbound(f"before {SECRETS[kind]} after")
    assert clean == f"before [REDACTED:{kind}] after"
    assert counts == {kind: 1}


def test_more_token_shapes():
    for value, kind in (("github_pat_" + "A" * 30, "github_token"), ("gh" + "s_" + A36, "github_token"),
                        ("4111-1111-1111-1111", "card"), ("378282246310005", "card"),
                        ("5555555555554444", "card")):
        assert scrub_outbound(value) == (f"[REDACTED:{kind}]", {kind: 1}), value


def test_bearer_keeps_the_word_and_redacts_the_token():
    clean, counts = scrub_outbound("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    assert clean == "Authorization: Bearer [REDACTED:bearer_token]" and counts == {"bearer_token": 1}
    assert scrub_outbound("Bearer short")[1] == {}


def test_a_truncated_key_block_is_still_redacted():
    head = PEM.split("\n-----END")[0]
    clean, counts = scrub_outbound(f"output:\n{head} [truncated, 900 more chars]")
    assert clean == "output:\n[REDACTED:private_key]" and counts == {"private_key": 1}


@pytest.mark.parametrize("text", [
    "4111111111111112",            # 16 digits, Luhn fails
    "4111 1111 1111 1112",
    "1731196800000",               # a 13-digit millisecond timestamp (passes no card prefix)
    "Order 12345, total 1,234.56, phone 555-123-4567, year 2026",
    "41111111111111111111111",     # 23 digits: too long to be a card
    "AKIA lowercase akiaiosfodnn7example",
    "Bearer of good news",
    "eyJ alone is not a token",
])
def test_ordinary_text_is_untouched(text):
    assert scrub_outbound(text) == (text, {})


def test_luhn():
    assert luhn_ok("4111111111111111") and luhn_ok("79927398713")
    assert not luhn_ok("4111111111111112") and not luhn_ok("79927398710")


def test_counts_add_up():
    text = f"{SECRETS['card']} and {SECRETS['aws_key']} and 5555555555554444"
    clean, counts = scrub_outbound(text)
    assert counts == {"card": 2, "aws_key": 1}
    assert "1111" not in clean and "4444" not in clean


# -- in the agent loop ---------------------------------------------------------------------

TOKEN = SECRETS["github_token"]
CARD = SECRETS["card"]


def _run(monkeypatch, *, task=f"why does {TOKEN} fail?", answer=f"use {CARD}"):
    replies = iter(json.dumps(r) for r in [
        {"action": "run_command", "parameter": "git config --list"},
        {"action": "ask_user", "parameter": "Which card?"},
        {"action": "done", "speech": "ok"},
    ])
    rec = SimpleNamespace(requests=[])
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *_a, **_k: False),
        executor=SimpleNamespace(run=lambda a, p: SimpleNamespace(
            action=a, parameter=p, success=True, detail=f"remote.token={TOKEN}\ncard={CARD}",
            verified=None, escalate_to_user=False, reliability_note=lambda: "", escalated_from="")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda t, **_k: None,
        tts=SimpleNamespace(speaking=False), _last_opened_app="",
        _get_text_answer=lambda q, timeout=60: answer,
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
    )

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context",
                        lambda *a, **k: f"Window: Settings. Text: key {SECRETS['aws_key']}")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "App"))
    loop._loop(task, "m", "none", "text", None)
    return rec


def test_observations_and_screen_context_are_scrubbed(monkeypatch):
    rec = _run(monkeypatch)
    last = rec.requests[-1]
    obs = [m["content"] for m in last["messages"] if m["content"].startswith("[OBSERVATION]")]
    assert obs and all(TOKEN not in o and "1111 1111" not in o for o in obs)
    assert any("[REDACTED:github_token]" in o and "[REDACTED:card]" in o for o in obs)
    assert SECRETS["aws_key"] not in last["system"] and "[REDACTED:aws_key]" in last["system"]


def test_the_task_and_the_users_answer_are_untouched(monkeypatch):
    rec = _run(monkeypatch)
    last = rec.requests[-1]
    assert last["message"] == f"why does {TOKEN} fail?"
    assert last["messages"][0]["content"] == f"why does {TOKEN} fail?"
    assert any(m["content"] == f"[USER RESPONSE] use {CARD}" for m in last["messages"])


def test_flag_off_sends_everything(monkeypatch):
    monkeypatch.setenv("MISTAI_SCRUB_OUTBOUND", "0")
    rec = _run(monkeypatch)
    last = rec.requests[-1]
    assert SECRETS["aws_key"] in last["system"]
    assert any(TOKEN in m["content"] for m in last["messages"][1:] if "[OBSERVATION]" in m["content"])


def test_the_log_has_counts_only_and_each_text_once(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _run(monkeypatch)
    out = capsys.readouterr().out
    lines = [l.strip() for l in out.splitlines() if "[Scrub]" in l]
    assert "[Scrub] redacted 1 item(s): aws_key=1" in lines
    assert "[Scrub] redacted 2 item(s): card=1, github_token=1" in lines
    assert len(lines) == 2                 # resent texts are not rescanned or relogged
    assert TOKEN not in out and SECRETS["aws_key"] not in out and "1111 1111" not in out
