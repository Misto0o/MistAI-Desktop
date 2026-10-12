"""Planner/agent prompt rules that keep answers grounded in observed data."""

import json
import threading
from types import SimpleNamespace

from core import planner
from core.agent import AgentLoop
from core.api import Api
from core.tools import SPECS


def _system():
    api = SimpleNamespace(
        workspace=SimpleNamespace(context_block=lambda: ""),
        _resolve_path_placeholders=lambda p: p,
    )
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


# -- Planner -------------------------------------------------------------------


def test_plan_prompt_plans_only_what_remains_and_only_what_was_asked():
    prompt = planner._PLAN_PROMPT
    assert "Plan ONLY what remains" in prompt
    assert "QUESTION's deliverable is the spoken answer" in prompt
    assert "write" in prompt and "true/false" in prompt
    assert "that output is a deliverable and must have a phase" in prompt
    # Still formats cleanly (no stray braces).
    planner._PLAN_PROMPT.format(goal="g", max_nodes=4, workspace="w")


RESEARCH_GOAL = "research 3 VS Code extensions for React then write the names in Notepad"


def test_research_then_write_goal_is_planned_and_keeps_a_writing_phase(monkeypatch):
    seen_prompts = []
    raw = json.dumps(
        {
            "task_name": "ReactExtensions",
            "nodes": [
                {
                    "id": "research",
                    "desc": "Identify 3 popular VS Code extensions for React",
                    "depends_on": [],
                },
                {
                    "id": "write_names",
                    "desc": "Write the 3 extension names in Notepad",
                    "depends_on": ["research"],
                },
            ],
        }
    )

    def fake_llm(_api, prompt, _model):
        seen_prompts.append(prompt)
        return raw

    monkeypatch.setattr(planner, "_llm", fake_llm)
    plan, _ = planner.make_plan(SimpleNamespace(), RESEARCH_GOAL, "m")

    assert seen_prompts, "a two-deliverable goal must reach the planner"
    assert plan.order == ["research", "write_names"]
    assert "notepad" in plan.nodes["write_names"].desc.lower()


def test_research_then_write_fallback_still_contains_the_write(monkeypatch):
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: "not json")
    plan, _ = planner.make_plan(SimpleNamespace(), RESEARCH_GOAL, "m")
    assert [plan.nodes[n].desc for n in plan.order] == [RESEARCH_GOAL]


# -- Agent rules ---------------------------------------------------------------


def test_agent_prompt_has_grounding_rules():
    system = _system()
    assert 'use it and call "done"' in system
    assert "read again more specifically, or say you couldn't get it" in system
    assert "Never state data you haven't seen as fact" in system
    assert "never write placeholder content" in system
    assert "not merely because an action returned ok" in system
    assert "never from read_screen_text or" in system and "analyze_screen" in system
    assert "confirm absence with Get-Process" in system
    # A screen description of an app that Open apps does not list is not proof it is open.
    assert "The Open apps list outranks the screen description for which apps" in system
    assert "if an app isn't listed, don't assume it's open; use open_app." in system
    assert "add -ErrorAction SilentlyContinue" in system
    assert "got there by another route, say so in the final speech" in system
    assert "content VISIBLE on screen" in system
    # What a window shows is read from it, never computed and reported as read.
    assert "must come from an observation of that window" in system
    assert "fix the state or report what it actually shows" in system
    assert "never present your own computation" in system
    # Keys before clicks in native apps; OCR/vision clicks are the last resort.
    assert "type_text / press_key / hotkey before any clicking" in system
    assert "click_on_text / click_nth (OCR+Vision) -- the last resort" in system
    assert "not from web APIs called with run_command" in system
    assert "Never ask the user for API keys, tokens or passwords" in system


# -- read_screen_text vision prompt -------------------------------------------


def _vision_api(reply):
    api = Api.__new__(Api)
    prompts = []

    def call(prompt, _frame):
        prompts.append(prompt)
        return reply

    api.vision = SimpleNamespace(ready=True, _call=call)
    api.screen = SimpleNamespace(frame=object())
    api.last_result_text = ""
    api._exact_ui_text = lambda query="", include_browser=False: ("", "", False, "exact", None)
    return api, prompts


def test_whats_on_my_screen_still_gets_the_full_description():
    api, prompts = _vision_api("A browser showing the React docs and a terminal.")
    assert Api.read_screen_text(api, "") == "A browser showing the React docs and a terminal."
    assert prompts == ["Describe the visible text and content on this screen in detail."]

    api, prompts = _vision_api("A browser showing the React docs.")
    Api.read_screen_text(api, "what's on my screen")
    assert "what's on my screen" in prompts[0]
    assert api.last_result_text == "A browser showing the React docs."


def test_specific_question_is_limited_to_what_is_visible():
    api, prompts = _vision_api("NOT VISIBLE ON SCREEN")
    Api.read_screen_text(api, "is Edge running?")
    assert "ONLY from what is visible" in prompts[0]
    assert "NOT VISIBLE ON SCREEN" in prompts[0]
    # Transcribe, never compute: a pending 37*23 must not be read back as 851.
    assert "exactly as displayed" in prompts[0]
    assert "Never calculate, complete, or infer" in prompts[0]


def test_not_visible_on_screen_sends_the_agent_to_another_source(monkeypatch):
    replies = iter(
        [
            json.dumps({"action": "read_screen_text", "parameter": "is Edge running?"}),
            json.dumps({"action": "run_command", "parameter": "Get-Process msedge"}),
            # No numbers to contradict the read and no unsubmitted typing:
            # the first done finishes without the done-time check.
            json.dumps({"action": "done", "speech": "Edge is not running."}),
        ]
    )
    details = {
        "read_screen_text": "NOT VISIBLE ON SCREEN",
        "run_command": "COMMAND FAILED (exit code 1).",
    }
    calls, sent = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": ["code"], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "https://example.invalid/a",
            get_fallback_url=lambda: "https://example.invalid/b",
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: calls.append(a)
            or SimpleNamespace(
                success=True,
                detail=details[a],
                verified=None,
                escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    assert loop._loop("is Edge open?", "m", "none", "text", None)["success"]

    assert calls == ["read_screen_text", "run_command"]
    obs_after_screen = sent[1][-2]["content"]
    assert "NOT VISIBLE ON SCREEN" in obs_after_screen
    assert "Get it from another source" in obs_after_screen
    assert "do not call done" in obs_after_screen
    assert len(sent) == 3


def test_open_apps_are_printed_once_per_phase(monkeypatch, capsys):
    replies = iter([json.dumps({"action": "done", "speech": "ok"})])
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": ["code", "firefox"]}),
        tools=SimpleNamespace(specs=SPECS),
        api_manager=SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "u",
            get_fallback_url=lambda: "f",
        ),
        speak=lambda _s: None,
    )
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert capsys.readouterr().out.count("[Agent] Open apps: code, firefox") == 1
