"""Conversation layer: routing prompt, fail-closed parsing, router call.

Merged by area (pure moves, no test changed) from: test_conversation_layer.py, test_model_choice.py."""

from types import SimpleNamespace
import pytest
from core import conversation
from core.config import CHAT_TIMEOUT, DEFAULT_MODEL
import threading  # noqa: E402
from core.api import Api  # noqa: E402
import json
import threading
import core.api as api_mod
from core import config, planner
from core.agent import AgentLoop
from core.api import Api
from core.api_manager import describe_api_error
from core.tools import SPECS



# ==========================================================================
# moved from tests/test_conversation_layer.py
# ==========================================================================

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
    # A bare api without current_model(): the default model.
    assert (model, timeout, label) == (DEFAULT_MODEL, CHAT_TIMEOUT, "Conversation")
    assert fallback is False
    assert "User: earlier" in prompt and "address the user as sir," in prompt


def test_try_chat_uses_the_requests_model_then_the_users_choice(monkeypatch):
    models = []
    monkeypatch.setattr("core.planner._llm",
                        lambda api, prompt, model, **_k: models.append(model) or "")
    api = _api()
    api.current_model = lambda: "mistral"
    conversation.try_chat(api, "joke")
    conversation.try_chat(api, "joke", model="cohere")
    assert models == ["mistral", "cohere"]


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
    assert api.rec["spoken"] == ["On it."]   # the fixed voice ack; the router reply never
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


# -- Workspace state across requests -------------------------------------------


class _Bus:
    def subscribe(self, *_args):
        return lambda: None

    def publish(self, *_args):
        pass


def _api_after_failed_command(monkeypatch, tmp_path, llm_reply):
    from core.workspace import Workspace

    api = _routing_api(monkeypatch, llm_reply)
    api.workspace = Workspace(_Bus())
    api.workspace.set_project("site", str(tmp_path))
    api.workspace.record_execution(
        "run_command", "Invoke-RestMethod https://api.example.test/weather",
        False, 0.0, "401 Unauthorized")
    assert "Last command (FAILED)" in api.workspace.context_block()
    return api


def test_new_agent_request_forgets_the_previous_run_but_keeps_the_project(
        monkeypatch, tmp_path):
    api = _api_after_failed_command(monkeypatch, tmp_path, '{"route": "task", "reply": ""}')
    Api.ask_mistai(api, "hey its too loud")

    context = api.workspace.context_block()
    assert [kind for kind, _ in api.agent.runs] == ["run"]
    assert "Last command" not in context and "Recent actions" not in context
    assert "Project: site" in context and api.workspace.project_root == str(tmp_path)
    _finish(api)


def test_chat_routed_turn_leaves_the_workspace_alone(monkeypatch, tmp_path):
    api = _api_after_failed_command(monkeypatch, tmp_path, CHAT)
    Api.ask_mistai(api, "tell me a joke")

    context = api.workspace.context_block()
    assert api.agent.runs == []
    assert "Last command (FAILED)" in context and "Recent actions" in context



# ==========================================================================
# moved from tests/test_model_choice.py
# The user's model choice: one validated, saved setting that every LLM call
# uses (agent, planner, router, hotkey lookup). A running request keeps its
# model; failures are reported plainly and never switch to another model.
# ==========================================================================

class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body, self._text = body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


# -- constants and the accessor ------------------------------------------------

def test_choices_labels_and_default():
    assert config.MODEL_CHOICES == ("gemini", "cohere", "mistral")
    assert config.MODEL_LABELS == {"gemini": "Nova", "cohere": "Sage", "mistral": "Flux"}
    assert set(config.MODEL_LABELS) == set(config.MODEL_CHOICES)  # a label for every id
    assert config.DEFAULT_MODEL == "cohere" and config.AGENT_MODEL == "cohere"
    assert not hasattr(config, "CHAT_MODEL")
    assert Api.__new__(Api).current_model() == "cohere"   # bare test object


@pytest.fixture
def memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "memory.db"))
    from core.memory import Memory

    return Memory()


def _api_with(memory):
    api = Api.__new__(Api)
    api.memory = memory
    api._model = api._load_model_choice()
    return api


def test_set_model_validates_saves_and_survives_a_restart(memory):
    api = _api_with(memory)
    assert api.current_model() == "cohere"
    assert api.set_model(" Mistral ") == {"success": True, "model": "mistral", "label": "Flux"}
    assert memory.get_profile("model") == "mistral"
    assert _api_with(memory).current_model() == "mistral"     # next start
    assert api.get_model_options() == {
        "choices": [{"id": "gemini", "label": "Nova"}, {"id": "cohere", "label": "Sage"},
                    {"id": "mistral", "label": "Flux"}],
        "current": "mistral",
    }


def test_real_model_options_are_complete():
    """What the UI renders: never empty, every entry has an id and a label."""
    opts = Api.__new__(Api).get_model_options()
    assert opts["choices"] and opts["current"] == "cohere"
    assert [c["id"] for c in opts["choices"]] == list(config.MODEL_CHOICES)
    assert all(isinstance(c["label"], str) and c["label"].strip() for c in opts["choices"])
    assert opts["current"] in [c["id"] for c in opts["choices"]]


@pytest.mark.parametrize("name", ["gpt-4", "Flux", "", None, "cohere; drop table"])
def test_set_model_rejects_anything_outside_the_choices(memory, name):
    """Ids only: a label such as "Flux" is not an id."""
    api = _api_with(memory)
    api.set_model("mistral")
    result = api.set_model(name)
    assert result["success"] is False and "gemini, cohere, mistral" in result["error"]
    assert api.current_model() == "mistral" and memory.get_profile("model") == "mistral"


def test_a_bad_saved_value_falls_back_to_the_default(memory, capsys):
    memory.set_profile("model", "gpt-9")
    assert _api_with(memory).current_model() == "cohere"
    assert "using cohere" in capsys.readouterr().out


# -- gender, read back for the UI -------------------------------------------------

@pytest.mark.parametrize("saved, expected", [
    ("female", "female"), (" MALE ", "male"), ("none", "none"),
    ("robot", "none"), ("", "none"), (None, "none"),
])
def test_get_user_gender_validates_the_saved_value(memory, saved, expected):
    if saved is not None:
        memory.set_profile("user_gender", saved)
    api = Api.__new__(Api)
    api.memory = memory
    assert api.get_user_gender() == expected


def _gender_api(memory):
    voices = []
    api = Api.__new__(Api)
    api.memory = memory
    api.tts = SimpleNamespace(set_gender=voices.append)
    return api, voices


def test_set_user_gender_saves_a_valid_choice(memory):
    api, voices = _gender_api(memory)
    assert api.set_user_gender(" Female ") == {"success": True, "gender": "female"}
    assert memory.get_profile("user_gender") == "female" and voices == ["female"]
    assert api.get_user_gender() == "female"


@pytest.mark.parametrize("value", ["robot", "", None, "Flux", "female; drop table", 1])
def test_set_user_gender_rejects_anything_outside_the_choices(memory, value):
    api, voices = _gender_api(memory)
    api.set_user_gender("male")
    result = api.set_user_gender(value)
    assert result == {"success": False,
                      "error": "Unknown gender address. Choose one of: male, female, none."}
    assert memory.get_profile("user_gender") == "male" and voices == ["male"]  # unchanged


def test_get_user_gender_never_raises(capsys):
    def broken(*_a):
        raise RuntimeError("database is locked")

    api = Api.__new__(Api)
    api.memory = SimpleNamespace(get_profile=broken)
    assert api.get_user_gender() == "none"
    assert "Could not read the saved gender (RuntimeError)" in capsys.readouterr().out


# -- one model per request: router, agent, hotkey -------------------------------

def _hotkey_models(monkeypatch, api):
    sent = []
    monkeypatch.setattr(api_mod.requests, "post", lambda url, json=None, **_k: (
        sent.append(json["model"]), _Resp(200, {"response": "ctrl+k"}))[1])
    api.api_manager = SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u")
    return sent


def test_request_uses_the_choice_everywhere_and_keeps_it_while_running(monkeypatch):
    api = _routing_api(monkeypatch, '{"route": "task", "reply": ""}')
    api.memory.set_profile = lambda *_a: None
    api._model = "mistral"

    Api.ask_mistai(api, "open notepad", model="cohere", input_mode="text")  # arg ignored
    assert api.rec["router"][0][2] == "mistral"                 # router
    assert api.agent.runs[0][1][1] == "mistral"                  # agent + planner
    sent = _hotkey_models(monkeypatch, api)

    api.set_model("cohere")                                      # changed mid-task
    assert Api._ask_llm_for_hotkey(api, "discord") == "ctrl+k"
    assert sent == ["mistral"]                                   # running request's model

    api.agent.callbacks["on_done"]({"success": True, "steps": 1})
    Api._ask_llm_for_hotkey(api, "discord")
    assert sent == ["mistral", "cohere"]                         # next one: the new choice


# -- the plain error message ------------------------------------------------------

@pytest.mark.parametrize("resp, model, expected", [
    (_Resp(429, {"error": "Rate limit exceeded"}), "mistral",
     "Flux is rate-limited or unavailable right now (HTTP 429: Rate limit exceeded). "
     "Pick another model in Settings or try again later."),
    (_Resp(400, {"error": {"message": "Unknown model 'mistral'"}}), "mistral",
     "Flux request failed (HTTP 400: Unknown model 'mistral')."),
    (_Resp(503, {"message": "Upstream down"}), "cohere",
     "Sage request failed (HTTP 503: Upstream down)."),
    (_Resp(502, text="<html>bad gateway</html>"), "cohere", "Sage request failed (HTTP 502)."),
])
def test_describe_api_error(resp, model, expected):
    assert describe_api_error(resp, model) == expected


def test_error_text_never_carries_urls_or_keys_and_is_capped():
    body = {"error": "Key mistai_abc123def456 rejected at https://api.mistai.org/v1?k=1 "
                     "bearer sk-ZYXWVUTSRQ " + "x" * 300}
    msg = describe_api_error(_Resp(401, body), "cohere")
    for leak in ("mistai_abc", "https://", "api.mistai.org", "sk-ZYX"):
        assert leak not in msg
    detail = msg.split("(HTTP 401: ", 1)[1].rsplit(").", 1)[0]
    assert len(detail) <= 120 and detail.endswith("...")


def test_planner_logs_the_failure_and_sends_only_the_given_model(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(planner.requests, "post", lambda url, json=None, **_k: (
        sent.append(json["model"]), _Resp(429, {"error": "Rate limit exceeded"}))[1])
    api = SimpleNamespace(api_manager=SimpleNamespace(
        get_headers=lambda: {}, get_api_url=lambda: "https://primary.invalid",
        get_fallback_url=lambda: "https://fallback.invalid"))
    assert planner._llm(api, "prompt", "mistral") == ""
    assert sent == ["mistral", "mistral"]            # fallback URL, same model
    out = capsys.readouterr().out
    assert "[Planner] Flux is rate-limited" in out
    assert "invalid" not in out                      # no URLs in the log line


def test_hotkey_failure_is_logged_plainly(monkeypatch, capsys):
    api = Api.__new__(Api)
    api.api_manager = SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u")
    monkeypatch.setattr(api_mod.requests, "post",
                        lambda *_a, **_k: _Resp(429, {"error": "slow down"}))
    assert Api._ask_llm_for_hotkey(api, "discord") is None
    assert "[Hotkey] Sage is rate-limited" in capsys.readouterr().out


@pytest.mark.parametrize("mode, spoken_expected", [("voice", True), ("text", False)])
def test_agent_reports_a_429_plainly_and_never_tries_another_model(
        monkeypatch, mode, spoken_expected):
    sent, spoken = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {"Authorization": "Bearer mistai_secret1"},
                                    get_api_url=lambda: "https://primary.invalid",
                                    get_fallback_url=lambda: "https://fallback.invalid"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=spoken.append,
        _last_opened_app=None,
    )
    monkeypatch.setattr("core.agent.requests.post", lambda url, json=None, **_k: (
        sent.append(json["model"]), _Resp(429, {"error": "Rate limit exceeded"}))[1])
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Notepad"))

    out = loop._loop("open notepad", "mistral", "none", mode, None)
    msg = ("Flux is rate-limited or unavailable right now (HTTP 429: Rate limit "
           "exceeded). Pick another model in Settings or try again later.")
    assert out == {"success": False, "error": msg, "speech": msg}
    assert spoken == ([msg] if spoken_expected else [])
    assert sent == ["mistral", "mistral"]            # same model on the fallback URL
    assert "secret" not in json.dumps(out) and "invalid" not in json.dumps(out)
