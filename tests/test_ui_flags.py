"""Developer-only Settings controls (Api.get_ui_flags, ui/html.py).

The "Use MistAI backend" switch shows only in a dev run: a source run, or a
build with MISTAI_DEV=1. In a build, Save sets the key and never touches the
route, so MISTAI_USE_BACKEND stays in charge. Node tests skip without Node.
"""

import re
import sys

import pytest

from core.api import Api
from core.api_manager import APIKeyManager
from test_ui_model_card import HTML, _run_page, _script, needs_node


def _flags(monkeypatch, frozen, dev_env):
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    if dev_env is None:
        monkeypatch.delenv("MISTAI_DEV", raising=False)
    else:
        monkeypatch.setenv("MISTAI_DEV", dev_env)
    return Api.__new__(Api).get_ui_flags()


@pytest.mark.parametrize("frozen, dev_env, is_dev", [
    (False, None, True),      # source run
    (False, "0", True),
    (True, None, False),      # release build
    (True, "1", True),        # build opted into dev controls
    (True, " 1 ", True),
    (True, "0", False),
    (True, "true", False),    # only "1" opts in
])
def test_get_ui_flags(monkeypatch, frozen, dev_env, is_dev):
    assert _flags(monkeypatch, frozen, dev_env) == {"is_dev": is_dev}


@pytest.mark.parametrize("use_backend, expect_backend", [("true", True), ("false", False)])
def test_a_build_save_keeps_the_route_from_the_environment(monkeypatch, use_backend,
                                                           expect_backend):
    """What a non-dev Save does (set_api_key only) leaves MISTAI_USE_BACKEND's
    route in place."""
    monkeypatch.setenv("MISTAI_API_KEY", "mistai_test")
    monkeypatch.setenv("MISTAI_USE_BACKEND", use_backend)
    api = Api.__new__(Api)
    api.api_manager = APIKeyManager()
    before = api.api_manager.get_api_url()
    api.set_api_key("mistai_other")
    assert api.api_manager.get_api_url() == before
    assert before.endswith("/api/chat") is expect_backend


@pytest.mark.parametrize("frozen, dev_env, allowed", [
    (False, None, True),      # source run
    (True, "1", True),        # build opted into dev controls
    (True, None, False),      # release build
])
@pytest.mark.parametrize("env_backend", ["true", "false"])
def test_set_api_mode_is_refused_outside_dev(monkeypatch, frozen, dev_env, allowed,
                                             env_backend):
    _flags(monkeypatch, frozen, dev_env)
    monkeypatch.setenv("MISTAI_API_KEY", "mistai_test")
    monkeypatch.setenv("MISTAI_USE_BACKEND", env_backend)
    api = Api.__new__(Api)
    api.api_manager = APIKeyManager()
    env_route = api.api_manager.get_api_url()
    flip = env_backend != "true"                      # ask for the other route
    result = api.set_api_mode(flip)
    if allowed:
        assert result["success"] is True
        assert api.api_manager.get_api_url().endswith("/api/chat") is flip
    else:
        assert result == {"success": False,
                          "error": "Switching the API route is a developer setting. "
                                   "Set MISTAI_USE_BACKEND in .env instead."}
        assert api.api_manager.get_api_url() == env_route   # changed nothing


# -- string checks (always run) ---------------------------------------------------

def test_backend_switch_is_hidden_until_python_says_dev():
    row = re.search(r'<label id="backend-row" style="([^"]*)">', HTML)
    assert row and row.group(1).endswith("display:none")
    js = _script()
    assert "let devMode=false;" in js
    assert "devMode=!!(f&&f.is_dev===true);" in js
    assert "document.getElementById('backend-row').style.display=devMode?'block':'none';" in js
    assert "runStep('flags',loadFlags);" in js


def test_save_switches_the_route_only_in_dev():
    js = _script()
    save = js.split("async function saveAPIKey(){", 1)[1].split("\n}\n", 1)[0]
    assert save.count("set_api_mode") == 1
    assert "if(devMode && (key || useBackend)){" in save
    assert save.index("if(devMode && (key || useBackend)){") < save.index("set_api_mode")
    # A refusal from Python is shown, not reported as a switch.
    assert "if(r&&r.success===false)addMsg(r.error||'Could not change the API route','error');" \
        in save


# -- the whole page in Node ----------------------------------------------------------

SAVES = [{"key": "", "useBackend": True}, {"key": "mistai_test", "useBackend": False}]


@needs_node
@pytest.mark.parametrize("frozen, dev_env", [(False, None), (True, None), (True, "1")])
def test_the_page_follows_the_real_flags(monkeypatch, tmp_path, frozen, dev_env):
    flags = _flags(monkeypatch, frozen, dev_env)
    r = _run_page(tmp_path, flags=flags, save=SAVES)
    assert r["consoleErrors"] == []
    assert r["backendRow"] == ("block" if flags["is_dev"] else "none")
    if flags["is_dev"]:
        # The old behaviour, kept for developers: Save with the box ticked
        # switches to the backend; a key with it unticked goes direct.
        assert r["after"]["calls"] == ["set_api_mode", "set_api_key", "set_api_mode"]
    else:
        assert r["after"]["calls"] == ["set_api_key"]          # never the route
        assert ["msg system", "API key configured"] in r["after"]["msgs"]


@needs_node
@pytest.mark.parametrize("fail", ["reject", "hang"])
def test_flags_that_never_load_leave_the_page_as_a_build(tmp_path, fail):
    r = _run_page(tmp_path, fail={"get_ui_flags": fail}, save=SAVES)
    assert r["backendRow"] == "unset"                          # the hidden HTML default
    assert "set_api_mode" not in r["after"]["calls"]
    assert r["options"] and r["online"] == "Online"            # nothing else blocked
