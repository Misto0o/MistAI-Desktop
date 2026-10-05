"""Small, hardware-free checks for AgentLoop parsing and confirmation gates."""

import json
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop


class _Response:
    def __init__(self, response="", status_code=200):
        self.ok = status_code < 400
        self.status_code = status_code
        self._response = response

    def json(self):
        return {"response": self._response}


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


def _run_loop(monkeypatch, responses, answers=()):
    """Run a deterministic AgentLoop with no network, UI, or sleeps."""
    api = _FakeApi(answers)
    response_iter = iter(responses)

    def fake_post(*_args, **_kwargs):
        return _Response(next(response_iter))

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _seconds: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")
    return loop._loop("perform a test task", "test-model", "none", "text", None), api


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
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "test screen")

    result = loop._loop("perform a test task", "test-model", "none", "text", None)

    assert calls == [1]
    assert result["error"] == "API primary 503 (no compatible fallback)"


def test_stop_while_model_request_is_in_flight_prevents_returned_action_dispatch(
    monkeypatch,
):
    api = _FakeApi()

    def response_after_stop(*_args, **_kwargs):
        api._stop_requested.set()
        return _Response(json.dumps({"action": "window_close", "parameter": "Test"}))

    monkeypatch.setattr("core.agent.requests.post", response_after_stop)
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
