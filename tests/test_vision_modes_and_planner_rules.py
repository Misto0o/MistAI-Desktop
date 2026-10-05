"""Vision gating default, lazy first read, live-state freshness, withheld
wording, question routing in the planner, and no key characters in logs."""

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from core import planner
from core.agent import AgentLoop
from core.api import Api
from core.config import lazy_vision_mode, vision_gating_enabled
from core.live_state import describe_open_apps
from core.tools import SPECS


# -- Flags -------------------------------------------------------------------------


@pytest.mark.parametrize("value, gating", [(None, True), ("", True), ("1", True),
                                           ("0", False), ("off", False), ("junk", True)])
def test_vision_gating_is_on_by_default(monkeypatch, value, gating):
    if value is None:
        monkeypatch.delenv("MISTAI_VISION_GATING", raising=False)
    else:
        monkeypatch.setenv("MISTAI_VISION_GATING", value)
    assert vision_gating_enabled() is gating


@pytest.mark.parametrize("value, mode", [(None, 0), ("0", 0), ("junk", 0), ("1", 1),
                                         ("on", 1), ("2", 2), (" 2 ", 2)])
def test_lazy_vision_is_off_by_default(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("MISTAI_LAZY_VISION", raising=False)
    else:
        monkeypatch.setenv("MISTAI_LAZY_VISION", value)
    assert lazy_vision_mode() == mode


# -- Agent loop: refresh + lazy first read -------------------------------------------


def _loop_api(script, snap=None):
    replies = iter(
        [json.dumps({"action": a, "parameter": p}) for a, p in script]
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    refreshes = []
    snap = snap or {"open_apps": ["code"], "foreground_hwnd": 1, "foreground_window": "Editor"}
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snap, refresh=lambda: refreshes.append(1)),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: SimpleNamespace(
                success=True, detail="", verified=True, escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    post = lambda *a, **k: SimpleNamespace(  # noqa: E731
        ok=True, status_code=200, json=lambda: {"response": next(replies)}
    )
    return api, post, refreshes


def test_live_state_is_refreshed_once_at_the_start_of_each_phase(monkeypatch):
    api, post, refreshes = _loop_api([("run_command", "x")])
    monkeypatch.setattr("core.agent.requests.post", post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert refreshes == [1]


def test_lazy_mode_makes_only_the_first_read_lazy(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_LAZY_VISION", "1")
    monkeypatch.setenv("MISTAI_VISION_GATING", "1")
    api, post, _ = _loop_api([("open_app", "notepad"), ("type_text", "hi")])
    monkeypatch.setattr("core.agent.requests.post", post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    calls = []
    monkeypatch.setattr(loop, "_get_screen_context", lambda lazy=False: calls.append(lazy) or "s")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert calls == [True, False, False]  # first read lazy; UI actions re-read with vision
    assert "lazy first read: on" in capsys.readouterr().out


def test_lazy_context_uses_title_apps_and_uia_and_never_calls_vision(monkeypatch):
    vision_calls = []
    api = SimpleNamespace(
        live=SimpleNamespace(snapshot={
            "foreground_window": "Untitled - Notepad",
            "open_apps": ["notepad", "discord"], "background_apps": ["discord"],
        }),
        vision=SimpleNamespace(ready=True, understand_screen=lambda *a, **k: vision_calls.append(1)),
        screen=SimpleNamespace(frame=object()),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: "Controls: File | Edit")
    ctx = loop._get_screen_context(lazy=True)
    assert "Window: Untitled - Notepad" in ctx
    assert "Open apps: notepad, discord (background)" in ctx
    assert "Controls: File | Edit" in ctx
    assert "call analyze_screen" in ctx
    assert vision_calls == []


def test_lazy_mode_2_skips_the_uia_walk(monkeypatch):
    api = SimpleNamespace(live=SimpleNamespace(snapshot={
        "foreground_window": "Untitled - Notepad", "open_apps": ["notepad"]}))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: pytest.fail("UIA walked in mode 2"))
    ctx = loop._get_screen_context(lazy=2)
    assert "Window: Untitled - Notepad" in ctx and "Open apps: notepad" in ctx
    assert "Controls: (not read)" in ctx and "call analyze_screen" in ctx


def test_lazy_mode_1_falls_back_to_the_title_when_uia_is_slow(monkeypatch):
    release = threading.Event()
    api = SimpleNamespace(live=SimpleNamespace(snapshot={
        "foreground_window": "main.py - Visual Studio Code", "open_apps": ["code"]}))
    loop = AgentLoop(api, None)
    loop.LAZY_UIA_BUDGET = 0.05
    monkeypatch.setattr(loop, "_uia_summary", lambda: release.wait(5) and "Controls: late")
    try:
        ctx = loop._get_screen_context(lazy=1)
    finally:
        release.set()
    assert "Window: main.py - Visual Studio Code" in ctx
    assert "window too slow to scan" in ctx and "Controls: late" not in ctx


def test_lazy_off_keeps_the_original_screen_context(monkeypatch):
    api = SimpleNamespace(
        live=SimpleNamespace(snapshot={"foreground_window": "W", "open_apps": ["a"]}),
        vision=SimpleNamespace(ready=False),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_uia_summary", lambda: "Controls: OK")
    assert loop._get_screen_context() == "Controls: OK"
    monkeypatch.setattr(loop, "_uia_summary", lambda: "")
    assert loop._get_screen_context() == "Window: W | Apps: a"


# -- Background apps -----------------------------------------------------------------


def test_apps_without_a_visible_window_are_marked_background(monkeypatch):
    from core import live_state

    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    monkeypatch.setattr(live_state, "_visible_window_pids", lambda: {101, 103})
    monkeypatch.setattr(live_state, "_uwp_frame_apps", lambda: [])
    monkeypatch.setattr(
        live_state.psutil, "process_iter",
        lambda _attrs: [
            SimpleNamespace(info={"name": n, "pid": pid})
            for n, pid in [("Code.exe", 101), ("Notepad.exe", 102), ("Notepad.exe", 103),
                           ("Discord.exe", 104)]
        ],
    )
    tracker._update()
    snap = tracker._state
    # Win11 Notepad: one windowless broker + one windowed process = foreground app.
    assert snap["background_apps"] == ["discord"]
    assert describe_open_apps(snap) == "code, notepad, discord (background)"


def test_unknown_window_state_marks_nothing_background(monkeypatch):
    from core import live_state

    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()

    def boom():
        raise OSError("no desktop")

    monkeypatch.setattr(live_state, "_visible_window_pids", boom)
    monkeypatch.setattr(live_state, "_uwp_frame_apps", lambda: [])
    monkeypatch.setattr(live_state.psutil, "process_iter",
                        lambda _a: [SimpleNamespace(info={"name": "Discord.exe", "pid": 1})])
    tracker._update()
    assert tracker._state["background_apps"] == []


def test_prompt_explains_background_apps():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    system = AgentLoop(api, None)._build_system(
        "none", {"open_apps": ["discord"], "background_apps": ["discord"]}, "s", "", []
    )
    assert "Open apps: discord (background)" in system
    assert '"(background)" means running with no window, so use open_app' in system


# -- Withheld wording ----------------------------------------------------------------


def _bare_api():
    from core.events import EventBus
    from core.workspace import Workspace

    api = Api.__new__(Api)
    api.workspace = Workspace(EventBus())
    api.last_result_text = ""
    api.last_command_result = None
    api._resolve_path_placeholders = lambda p: str(p)
    return api


def test_withheld_output_tells_the_model_not_to_show_or_offer_it(monkeypatch, tmp_path):
    api = _bare_api()
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "KEY=1", ""),
    )
    Api._act_run_command(api, "Get-Content .env")
    text = api.last_result_text
    assert "OUTPUT WITHHELD" in text
    assert "You cannot see or show this content" in text
    assert "do not offer to reveal it" in text

    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    Api._act_inspect_file(api, str(secret))
    assert "CONTENT WITHHELD" in api.last_result_text
    assert "do not offer to reveal it" in api.last_result_text


# -- Planner -------------------------------------------------------------------------


@pytest.mark.parametrize("goal", [
    "is Edge open?", "Are there any PDFs in my Downloads?", "what time is it in Tokyo",
    "Which app is using the most memory?", "how many files are on my desktop",
    "does my Documents folder have a resume", "do I have Python installed",
    "can you see Spotify running", "where is my latest download",
])
def test_question_goals_skip_the_planning_llm(monkeypatch, goal):
    def no_llm(*_a, **_k):
        raise AssertionError("the planner LLM must not be called for a question")

    monkeypatch.setattr(planner, "_llm", no_llm)
    plan, _ = planner.make_plan(SimpleNamespace(), goal, "m")
    assert [plan.nodes[n].desc for n in plan.order] == [goal]


@pytest.mark.parametrize("goal", [
    "is Edge open and if so close all its windows please now",
    "what is the weather, then write it into a new note on my desktop",
    "isolate the failing test in the project and fix the import errors",
])
def test_compound_or_non_question_goals_still_reach_the_planner(monkeypatch, goal):
    prompts = []
    monkeypatch.setattr(planner, "_llm", lambda _api, prompt, _m: prompts.append(prompt) or "")
    planner.make_plan(SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: "")), goal, "m")
    assert len(prompts) == 1


def test_plan_prompt_forbids_output_formats_and_same_tool_splits(monkeypatch):
    prompts = []
    monkeypatch.setattr(planner, "_llm", lambda _api, prompt, _m: prompts.append(prompt) or "")
    goal = "create a folder called notes on my desktop and then a todo.txt file inside it"
    plan, _ = planner.make_plan(
        SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: "")), goal, "m"
    )
    (prompt,) = prompts
    assert "NEVER specify an output format" in prompt
    assert '"return true/false"' in prompt
    assert "QUESTION's deliverable is the spoken answer" in prompt
    assert "creating a folder and a file inside it is ONE phase" in prompt
    assert plan.order == ["main"]  # unparseable reply -> one phase


# -- No key characters in logs --------------------------------------------------------


def test_api_key_is_never_printed(monkeypatch, capsys):
    from core.api_manager import APIKeyManager

    monkeypatch.setenv("MISTAI_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz")
    mgr = APIKeyManager()
    mgr.set_api_key("sk-zyxwvutsrqponmlkjihgfedcba")
    out = capsys.readouterr().out
    assert "API key loaded" in out
    assert "sk-" not in out and "abcdef" not in out and "zyxw" not in out
    api = Api.__new__(Api)
    api.api_manager = mgr
    assert "sk-" not in Api.set_api_key(api, "sk-secretsecret")["message"]
