"""Conversation layer: routing prompt, fail-closed parsing, router call."""

from types import SimpleNamespace

import pytest

from core import conversation
from core.config import CHAT_MODEL, CHAT_TIMEOUT

NOW = "Tuesday, October 06, 2026 at 03:15 PM"


# -- Prompt grounding ---------------------------------------------------------


def test_prompt_supplies_the_clock_and_the_message():
    p = conversation.build_prompt('tell me a "joke"', NOW)
    assert NOW in p and "system clock" in p
    assert '"tell me a \\"joke\\""' in p


def test_prompt_escalates_everything_but_pure_conversation():
    p = conversation.build_prompt("x", NOW).lower()
    for rule in ("computer", "screen", "files", "apps", "web", "weather",
                 "news", "prices", "needs an action", "mixing chat with a request",
                 "too loud", "whenever you are unsure"):
        assert rule in p, rule
    assert '"route": "chat" or "task"' in p


def test_prompt_forbids_claims_about_the_computer_and_long_replies():
    p = conversation.build_prompt("x", NOW)
    assert "never claim you did, opened, checked, saw or changed anything" in p
    assert "1-3 sentences" in p
    assert "spoken aloud: plain sentences only, no markdown, lists, emoji or stage directions" in p


def test_prompt_includes_history_and_honorific_only_when_given():
    block = "\n    RECENT CONVERSATION (context only):\n    User: hi\n"
    with_all = conversation.build_prompt("x", NOW, block, "ma'am")
    assert block in with_all
    assert "Occasionally address the user as ma'am, not in every reply." in with_all
    assert "address the user" not in conversation.build_prompt("x", NOW)


# -- Parser (fails closed) ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"route": "chat", "reply": "Ha."}', "Ha."),
        ('```json\n{"route": "chat", "reply": " Ha. "}\n```', "Ha."),
        ('{"route": "task", "reply": "Sure!"}', None),
        ('{"route": "chat", "reply": "   "}', None),
        ('{"route": "chat"}', None),
        ('{"route": "chat", "reply": 5}', None),
        ('{"reply": "Ha."}', None),
        ('[{"route": "chat", "reply": "Ha."}]', None),
        ("Ha. Here you go.", None),
        ("", None),
        (None, None),
        ('{"task_name": "X", "nodes": []}', None),  # --stub plan reply
    ],
)
def test_parse_route_returns_a_reply_only_for_a_clean_chat_route(raw, expected):
    assert conversation.parse_route(raw) == expected


# -- Router call ---------------------------------------------------------------


def _api(history="", history_raises=False):
    def block(_task):
        if history_raises:
            raise AttributeError("no agent")
        return history

    return SimpleNamespace(agent=SimpleNamespace(_recent_conversation_block=block))


def test_try_chat_sends_one_small_call_and_returns_the_reply(monkeypatch):
    calls = []

    def fake_llm(api, prompt, model, timeout, label, fallback):
        calls.append((prompt, model, timeout, label, fallback))
        return '{"route": "chat", "reply": "Ha."}'

    monkeypatch.setattr("core.planner._llm", fake_llm)
    assert conversation.try_chat(_api("\n    User: earlier\n"), "joke", "male") == "Ha."
    prompt, model, timeout, label, fallback = calls[0]
    assert len(calls) == 1
    assert (model, timeout, label) == (CHAT_MODEL, CHAT_TIMEOUT, "Conversation")
    assert fallback is False
    assert "User: earlier" in prompt and "address the user as sir," in prompt


@pytest.mark.parametrize(("fallback", "urls"), [
    (False, ["https://primary.invalid"]),
    (True, ["https://primary.invalid", "https://fallback.invalid"]),
])
def test_llm_without_fallback_posts_once_to_the_primary_url(monkeypatch, fallback, urls):
    from core import planner

    posted = []

    def fake_post(url, **_k):
        posted.append(url)
        raise TimeoutError("read timeout")

    monkeypatch.setattr("core.planner.requests.post", fake_post)
    api = SimpleNamespace(api_manager=SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://primary.invalid",
        get_fallback_url=lambda: "https://fallback.invalid",
    ))
    assert planner._llm(api, "p", "m", timeout=5, fallback=fallback) == ""
    assert posted == urls


def test_try_chat_tolerates_missing_history(monkeypatch):
    monkeypatch.setattr("core.planner._llm",
                        lambda *_a, **_k: '{"route": "chat", "reply": "Hi."}')
    assert conversation.try_chat(_api(history_raises=True), "hello there") == "Hi."


@pytest.mark.parametrize("failure", ["raise", "empty", "task"])
def test_try_chat_fails_closed(monkeypatch, failure):
    def fake_llm(*_a, **_k):
        if failure == "raise":
            raise AttributeError("api_manager")
        return "" if failure == "empty" else '{"route": "task", "reply": ""}'

    monkeypatch.setattr("core.planner._llm", fake_llm)
    assert conversation.try_chat(_api(), "open notepad") is None


def test_try_chat_fails_closed_without_api_manager():
    # The real _llm on a bare Api (as in the smoke tests): no network, no raise.
    assert conversation.try_chat(SimpleNamespace(), "open calculator") is None


# -- Routing inside Api.ask_mistai ---------------------------------------------

import threading  # noqa: E402

from core.api import Api  # noqa: E402

CHAT = '{"route": "chat", "reply": "Ha."}'


class _Agent:
    def __init__(self):
        self.runs = []
        self.callbacks = None

    def run_project(self, *args, **kwargs):
        self.runs.append(("run", args))
        self.callbacks = kwargs

    def resume_project(self, *args, **kwargs):
        self.runs.append(("resume", args))
        self.callbacks = kwargs


def _routing_api(monkeypatch, llm_reply, resumable=None):
    """Bare Api like the smoke tests, with recorders and a scripted router."""
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_state_lock = threading.Lock()
    api._agent_task = None
    api._agent_started_at = 0.0
    api._agent_generation = 0
    api._stop_requested = threading.Event()
    api.AGENT_HARD_TIMEOUT = 0.01
    api.rec = {"router": [], "convo": [], "spoken": [], "minimized": 0}
    api.memory = SimpleNamespace(
        get_profile=lambda _key, default: default,
        add_conversation=lambda *a: api.rec["convo"].append(a),
    )
    api.task_store = SimpleNamespace(find_resumable=lambda _name: resumable)
    api.agent = _Agent()
    api._notify_js = lambda _js: None
    api.speak = lambda text, **_k: api.rec["spoken"].append(text)

    def minimize():
        api.rec["minimized"] += 1

    api.minimize_window = minimize

    def fake_llm(*a, **_k):
        api.rec["router"].append(a)
        if isinstance(llm_reply, Exception):
            raise llm_reply
        return llm_reply

    monkeypatch.setattr("core.planner._llm", fake_llm)
    return api


def _finish(api):
    if api.agent.callbacks:
        api.agent.callbacks["on_done"]({"success": True, "steps": 1})


@pytest.mark.parametrize("mode", ["text", "voice"])
def test_chat_reply_skips_gate_agent_and_minimize(monkeypatch, mode):
    api = _routing_api(monkeypatch, CHAT)
    result = Api.ask_mistai(api, "tell me a joke", input_mode=mode)

    assert result == {
        "success": True,
        "command": {"action": "done", "parameter": "", "speech": "Ha."},
        "steps": 0,
        "error": "",
    }
    assert api.agent.runs == []
    assert api.rec["minimized"] == 0
    assert api._agent_generation == 0 and not api._agent_lock.locked()
    assert api.rec["convo"] == [("user", "tell me a joke"), ("assistant", "Ha.")]
    assert api.rec["spoken"] == (["Ha."] if mode == "voice" else [])


@pytest.mark.parametrize(
    "llm_reply",
    [
        AttributeError("api_manager"),
        "",
        "not json",
        '{"reply": "Ha."}',
        '{"route": "chat", "reply": "  "}',
        '[{"route": "chat", "reply": "Ha."}]',
        '{"route": "task", "reply": "Sure!"}',  # uncertain/mixed: agent, never spoken
        '```json\n{"route": "task", "reply": ""}\n```',
    ],
)
def test_anything_but_a_clean_chat_reply_runs_the_agent_as_today(monkeypatch, llm_reply):
    api = _routing_api(monkeypatch, llm_reply)
    result = Api.ask_mistai(api, "tell me a joke and open notepad", input_mode="voice")

    assert len(api.rec["router"]) == 1
    assert [kind for kind, _ in api.agent.runs] == ["run"]
    assert result["error"] == "still_running"  # agent path, as in the smoke tests
    assert api.rec["minimized"] == 1
    assert api.rec["convo"] == [("user", "tell me a joke and open notepad")]
    assert api.rec["spoken"] == []
    _finish(api)


def test_busy_agent_skips_the_router_and_keeps_the_busy_reply(monkeypatch):
    api = _routing_api(monkeypatch, CHAT)
    api._agent_lock.acquire()
    api._agent_task = "open calculator"

    result = Api.ask_mistai(api, "tell me a joke")

    assert result["error"] == "busy"
    assert api.rec["router"] == [] and api.agent.runs == []


def test_filler_shortcut_never_calls_the_router(monkeypatch):
    api = _routing_api(monkeypatch, CHAT)
    result = Api.ask_mistai(api, "thanks", input_mode="voice")

    assert result["command"]["speech"] == "You're welcome."
    assert api.rec["router"] == [] and api.agent.runs == []


def test_resume_never_calls_the_router(monkeypatch):
    api = _routing_api(monkeypatch, CHAT, resumable=SimpleNamespace(name="Site"))
    Api.ask_mistai(api, "resume Site")

    assert api.rec["router"] == []
    assert [kind for kind, _ in api.agent.runs] == ["resume"]
    _finish(api)
