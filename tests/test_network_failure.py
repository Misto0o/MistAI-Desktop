"""The agent call's network failures speak one fixed sentence; one immediate
retry for a connection reset/refused only; the system prompt's size is logged
as a number and warned about near the backend's limits."""

import json
import threading
from types import SimpleNamespace

import pytest
import requests

from core import agent as agent_mod
from core.agent import NETWORK_FAILURE_SPEECH, AgentLoop
from core.tools import SPECS

URL = "https://secret-backend.invalid/api/v1/chat?token=abc"
TASK = "message joanne quillfeather about the lighthouse"


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


def _reset():
    # What requests raises for a reset connection: the socket error inside.
    inner = ConnectionResetError(10054, "An existing connection was forcibly closed")
    return requests.exceptions.ConnectionError(inner, request=None)


def _refused():
    e = requests.exceptions.ConnectionError("Max retries exceeded with url: " + URL)
    e.__context__ = ConnectionRefusedError(10061, "refused")
    return e


DONE = {"action": "done", "speech": "Done."}
FAILED = {"success": False, "error": NETWORK_FAILURE_SPEECH, "speech": NETWORK_FAILURE_SPEECH}


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
    result, posts, spoken = _loop(monkeypatch, [_reset(), DONE])
    assert result["success"] and result["speech"] == "Done."
    assert len(posts) == 2
    log = capsys.readouterr().out
    assert "[AgentLoop] ConnectionError (connection reset/refused); retrying once" in log
    assert "forcibly closed" not in log and "secret-backend" not in log


def test_a_refused_connection_also_retries_once(monkeypatch):
    result, posts, _ = _loop(monkeypatch, [_refused(), DONE])
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
    result, posts, spoken = _loop(monkeypatch, [DONE])
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
    _loop(monkeypatch, [{"action": "wait", "parameter": "0"}, DONE])
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
    result, posts, _ = _loop(monkeypatch, [timeout, DONE])
    assert result == FAILED and len(posts) == 1
