"""The user's model choice: one validated, saved setting that every LLM call
uses (agent, planner, router, hotkey lookup). A running request keeps its
model; failures are reported plainly and never switch to another model."""

import json
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core import config, planner
from core.agent import AgentLoop
from core.api import Api
from core.api_manager import describe_api_error
from core.tools import SPECS
from test_conversation_layer import _routing_api


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
