"""The action playbook lives in core/tools.py: every registered action has docs,
the prompt's action menu is generated from it, and the registry, the dispatch
in Api._exec_sync and the menu stay in sync."""

import inspect
import re
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.api import Api
from core.tools import SPECS, action_entry, action_menu


def _system():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


def _menu_names(system):
    block = system[system.index("AVAILABLE ACTIONS"):system.index("RESPONSE FORMAT")]
    return re.findall(r"^- ([a-z_]+)\b", block, re.M)


def _dispatched():
    src = inspect.getsource(Api._exec_sync)
    return set(re.findall(r'action == "([a-z_]+)"', src))


def test_every_registered_action_has_docs():
    missing = [n for n, s in SPECS.items() if not s.purpose.strip()]
    assert not missing, missing


def test_every_dispatched_action_is_registered_and_in_the_menu():
    names = _menu_names(_system())
    assert _dispatched() - set(SPECS) == set()
    assert _dispatched() - set(names) == set()


def test_menu_lists_every_registered_action_once_and_nothing_else():
    names = _menu_names(_system())
    assert sorted(names) == sorted(SPECS)
    assert len(names) == len(set(names))


def test_the_only_registered_actions_not_dispatched_are_the_meta_ones():
    assert set(SPECS) - _dispatched() == {"done", "ask_user"}


@pytest.mark.parametrize("name, fmt", [
    ("write_file", "path|||complete content"),
    ("patch_json", "path|||json object"),
    ("patch_file", "path|||exact existing text|||replacement text"),
    ("quick_message", "app|recipient|message"),
    ("remind_me", "text|due_time"),
    ("schedule_reminder", "seconds|text"),
    ("click_nth", "N|type"),
    ("click_coords", "x,y"),
])
def test_argument_formats_keep_their_separators_verbatim(name, fmt):
    assert SPECS[name].args == fmt
    assert fmt in action_entry(name)
    assert fmt in action_menu()


def test_take_notes_is_not_an_action():
    # It typed blindly into whatever was in front (no target check, no NOT SENT).
    assert "take_notes" not in SPECS
    assert "take_notes" not in _dispatched()
    loop = AgentLoop(SimpleNamespace(tools=SimpleNamespace(specs=SPECS)), None)
    _a, _p, note, valid = loop._resolve_action("take_notes", "hello")
    assert not valid and "not a valid action name" in note


def test_menu_fills_the_type_text_submit_hint():
    assert "{submit_hint}" not in action_menu("")
    assert "press_key('enter') (both in one steps reply)" in action_menu(" (both in one steps reply)")


# -- Stage 2: compact menu, details on demand ------------------------------------------


def test_menu_is_one_short_line_per_action_with_its_exact_format():
    lines = action_menu().splitlines()
    assert len(lines) == len(SPECS)
    for name, line in zip(SPECS, lines):
        args = SPECS[name].args
        assert line.startswith(f"- {name}" + (f" <{args}>: " if args else ": ")), line
        assert len(line) <= 200, line
    # Details stay in the registry: the menu never carries the limits text.
    assert SPECS["write_file"].limits not in action_menu()


def test_safety_meaning_stays_in_the_always_visible_lines():
    menu = action_menu()
    assert "REPLACES THE ENTIRE FILE" in menu and "NEVER appends" in menu
    assert "does NOT submit" in menu and "only appends" in menu
    assert "LAST RESORT ONLY" in menu
    assert "ONLY when the user explicitly asks" in menu


def test_describe_action_is_a_registered_read_only_action():
    spec = SPECS["describe_action"]
    assert spec.categories == ("info",) and not spec.destructive
    assert not spec.requires_confirmation and spec.settle == 0.0
    from core.tools import ToolRegistry
    reg = ToolRegistry(db_path=":memory:")
    assert not reg.requires_confirmation("describe_action", "write_file")
    loop = AgentLoop(SimpleNamespace(), None)
    assert not loop._changes_ui("describe_action", set(spec.categories))   # no screen re-read


def test_describe_action_returns_the_full_entry_or_the_closest_names():
    api = Api.__new__(Api)
    api.last_result_text = ""
    assert Api._exec_sync(api, "describe_action", "write_file") is True
    text = api.last_result_text
    assert text == action_entry("write_file")
    assert SPECS["write_file"].limits in text and "Example parameter:" in text
    assert Api._exec_sync(api, "describe_action", "writefile") is False
    assert "No action named" in api.last_result_text and "write_file" in api.last_result_text


def test_unknown_action_note_names_the_closest_actions_and_the_entry():
    loop = AgentLoop(SimpleNamespace(tools=SimpleNamespace(specs=SPECS)), None)
    _a, _p, note, valid = loop._resolve_action("browser_clik", "Subscribe")
    assert not valid
    assert "Did you mean 'browser_click'" in note
    assert action_entry("browser_click") in note
    _a, _p, note, valid = loop._resolve_action("launch_program", "x")
    assert not valid and "AVAILABLE ACTIONS" in note


@pytest.mark.parametrize("name, good, bad", [
    ("write_file", "{desktop}\\a.txt|||hi", "{desktop}\\a.txt"),
    ("patch_file", "a.txt|||old|||new", "a.txt|||old"),
    ("quick_message", "discord|Alex|hi", "discord|Alex"),
    ("click_coords", "10, 20", "the button"),
    ("brightness_set", "70", "bright"),
])
def test_argument_check_is_structural(name, good, bad):
    from core.tools import arg_problem
    assert not arg_problem(name, good)
    assert arg_problem(name, bad)
    assert not arg_problem("run_command", "anything at all")   # free-form: never flagged


def test_bad_parameter_observation_carries_the_action_entry(monkeypatch):
    import json
    import threading

    replies = iter([json.dumps({"action": "click_coords", "parameter": "the OK button"}),
                    json.dumps({"action": "done", "speech": ""})])
    sent, ran = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: ran.append(a) or SimpleNamespace(
            success=False, detail="bad coordinates", verified=None, escalate_to_user=False,
            reliability_note=lambda: "")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _s: None,
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda lazy=False: "screen")
    loop._loop("click OK", "m", "none", "text", None)
    assert ran == ["click_coords"]                      # still runs: a note, not a refusal
    obs = sent[1][-2]["content"]
    assert "FORMAT: the parameter does not match." in obs
    assert action_entry("click_coords") in obs


# -- Stage 4: shorter output (wording only) ---------------------------------------------


def test_the_format_asks_for_one_short_thinking_sentence_and_keeps_speech_whole():
    fmt = _system().split("RESPONSE FORMAT (JSON ONLY):", 1)[1]
    assert '"thinking":"one short sentence"' in fmt
    assert "about 25 words at most" in fmt
    assert 'Never shorten "speech" or a parameter' in fmt


def test_the_parser_still_accepts_a_verbose_reply():
    import json
    thinking = " ".join(["word"] * 120)
    param = "C:\\x\\notes.txt|||" + "line\n" * 200
    raw = json.dumps({"thinking": thinking, "action": "write_file", "parameter": param,
                      "speech": "A long spoken answer. " * 20})
    cmd = AgentLoop(SimpleNamespace(), None)._parse_cmd(raw)
    assert cmd["action"] == "write_file"
    assert cmd["parameter"] == param and cmd["thinking"] == thinking
    assert cmd["speech"] == "A long spoken answer. " * 20


# -- MISTAI_SINGLE_SHOT (default off) ----------------------------------------------------

GOAL = "find the largest file in Downloads and tell me its size"


def _router(monkeypatch, reply):
    from core import conversation, planner
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: reply)
    api = SimpleNamespace(agent=SimpleNamespace(_recent_conversation_block=lambda _t: ""),
                          workspace=SimpleNamespace(context_block=lambda: ""))
    assert conversation.try_chat(api, GOAL) is None          # a task, never a chat reply
    return api


def _plans(monkeypatch, api, goal=GOAL):
    from core import planner
    calls = []
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: calls.append(a) or "not json")
    planner.make_plan(api, goal, "m")
    return len(calls)


def test_single_shot_off_changes_nothing(monkeypatch):
    from core import conversation
    monkeypatch.delenv("MISTAI_SINGLE_SHOT", raising=False)
    assert '"single"' not in conversation.build_prompt("x", "now")
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert getattr(api, "_single_shot_goal", None) is None
    assert _plans(monkeypatch, api) == 1                     # the planning call still happens


def test_single_shot_yes_skips_the_planning_call_for_that_goal_only(monkeypatch):
    from core import conversation
    monkeypatch.setenv("MISTAI_SINGLE_SHOT", "1")
    p = conversation.build_prompt("x", "now", single=True)
    assert '"route": "chat" or "task"' in p and '"single": true or false' in p
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert _plans(monkeypatch, api) == 0
    assert api._single_shot_goal is None                     # consumed
    assert _plans(monkeypatch, api) == 1                     # not reused for the next goal
    api = _router(monkeypatch, '{"route": "task", "reply": "", "single": true}')
    assert _plans(monkeypatch, api, goal="open notepad then write a long poem about it") == 1


@pytest.mark.parametrize("reply", [
    '{"route": "task", "reply": "", "single": false}',
    '{"route": "task", "reply": ""}',
    '{"route": "task", "reply": "", "single": "true"}',
    "not json",
    "",
])
def test_single_shot_fails_closed_to_planning(monkeypatch, reply):
    monkeypatch.setenv("MISTAI_SINGLE_SHOT", "1")
    api = _router(monkeypatch, reply)
    assert _plans(monkeypatch, api) == 1
