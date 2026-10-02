import json
import threading
from types import SimpleNamespace

import pytest

from core import planner
from core.task_store import TaskRecord
from core.tools import ToolRegistry, command_is_mutating


def _plan_from(payload):
    return json.dumps(payload)


def test_make_plan_normalizes_dependency_ids_and_deduplicates_them(monkeypatch):
    """Model IDs may vary in case, whitespace, and punctuation."""
    raw = _plan_from(
        {
            "task_name": "StaticSite",
            "nodes": [
                {
                    "id": "Create Folder!",
                    "desc": "Create the project directory",
                    "depends_on": [],
                },
                {
                    "id": "Write-Assets",
                    "desc": "Write the project asset files",
                    "depends_on": ["CREATE FOLDER!", "create-folder"],
                },
            ],
        }
    )
    monkeypatch.setattr(planner, "_llm", lambda *args, **kwargs: raw)

    plan, name = planner.make_plan(
        object(), "Build a project with separate folders and assets", "test-model"
    )

    assert name == "StaticSite"
    assert plan.order == ["createfolder", "writeassets"]
    assert plan.nodes["writeassets"].depends_on == ["createfolder"]


def test_make_plan_falls_back_when_a_dependency_is_unknown(monkeypatch):
    """An unknown dependency must not leave a phase permanently unrunnable."""
    goal = "Build a deliverable with setup and final output"
    raw = _plan_from(
        {
            "task_name": "BrokenPlan",
            "nodes": [
                {"id": "setup", "desc": "Prepare the workspace", "depends_on": []},
                {
                    "id": "deliver",
                    "desc": "Build the requested deliverable",
                    "depends_on": ["missing phase"],
                },
            ],
        }
    )
    monkeypatch.setattr(planner, "_llm", lambda *args, **kwargs: raw)

    plan, _ = planner.make_plan(object(), goal, "test-model")

    assert plan.order == ["main"]
    assert plan.nodes["main"].desc == goal
    assert [node.id for node in plan.ready_nodes()] == ["main"]


def test_write_file_confirmation_uses_resolved_placeholder_path(tmp_path):
    """Overwrite checks must inspect the actual Desktop/Documents path."""
    existing = tmp_path / "redirected_desktop" / "note.txt"
    existing.parent.mkdir()
    existing.write_text("old", encoding="utf-8")
    registry = ToolRegistry(db_path=str(tmp_path / "memory.db"))
    seen = []

    def resolve_path(path):
        seen.append(path)
        return str(existing)

    try:
        parameter = "{desktop}\\note.txt|||new contents"
        assert registry.is_destructive(
            "write_file", parameter, resolve_path=resolve_path
        )
        assert seen == ["{desktop}\\note.txt"]
        assert registry.requires_confirmation(
            "write_file", parameter, resolve_path=resolve_path
        )
    finally:
        if registry._conn:
            registry._conn.close()


def test_new_resolved_write_file_does_not_require_confirmation(tmp_path):
    registry = ToolRegistry(db_path=str(tmp_path / "memory.db"))
    try:
        assert not registry.requires_confirmation(
            "write_file",
            "{documents}\\new.txt|||contents",
            resolve_path=lambda _path: str(tmp_path / "new.txt"),
        )
    finally:
        if registry._conn:
            registry._conn.close()


def test_quick_message_is_explicitly_confirmation_gated(tmp_path):
    registry = ToolRegistry(db_path=str(tmp_path / "memory.db"))
    try:
        spec = registry.get("quick_message")
        assert spec.destructive
        assert not spec.reversible
        assert spec.requires_confirmation
        assert registry.requires_confirmation("quick_message", "discord|Ava|hello")
    finally:
        if registry._conn:
            registry._conn.close()


@pytest.mark.parametrize(
    "command",
    [
        "cmd /c del C:/temporary.txt",
        'powershell -Command "Remove-Item C:/temporary.txt"',
        "& { Remove-Item C:/temporary.txt }",
        "& 'Remove-Item' C:/temporary.txt",
        "Copy-Item source.txt destination.txt -Force",
    ],
)
def test_command_wrappers_and_copy_overwrites_are_confirmation_worthy(command):
    assert command_is_mutating(command)


def test_read_only_help_is_not_classified_as_mutating():
    assert not command_is_mutating("Get-Help Remove-Item")


def test_manual_send_and_close_shortcuts_are_confirmation_worthy(tmp_path):
    registry = ToolRegistry(db_path=str(tmp_path / "memory.db"))
    try:
        assert registry.requires_confirmation("press_key", "Enter")
        assert registry.requires_confirmation("browser_press_key", "return")
        assert registry.requires_confirmation("hotkey", "alt+f4")
        assert registry.requires_confirmation("browser_click", "Send|button")
        assert registry.requires_confirmation("click_on_text", "Delete")
        assert registry.requires_confirmation("click_coords", "740,320")
        assert not registry.requires_confirmation("hotkey", "ctrl+a")
    finally:
        if registry._conn:
            registry._conn.close()


def test_stop_during_continue_prompt_pauses_instead_of_finishing_as_partial():
    """A stop that wakes the prompt is not the same as declining a retry."""

    class _Bus:
        def publish(self, *_args, **_kwargs):
            pass

        def subscribe(self, *_args, **_kwargs):
            return lambda: None

    class _Store:
        def save(self, _record):
            pass

    class _Api:
        def __init__(self):
            self._stop_requested = threading.Event()
            self.bus = _Bus()
            self.task_store = _Store()
            self.workspace = SimpleNamespace(record_task_output=lambda *_args: None)
            self.caption_window = None
            self.captions_enabled = False
            self.spoken = []

        def speak(self, text):
            self.spoken.append(text)

    class _FailingAgent:
        def _loop(self, *_args, **_kwargs):
            return {"success": False, "error": "phase failed", "steps": 1}

    api = _Api()
    plan = planner.Plan(
        [
            planner.PlanNode("first", "first deliverable"),
            planner.PlanNode("second", "independent deliverable"),
        ]
    )
    record = TaskRecord("test", "Test", "goal", plan.to_dict())
    runner = planner.PlanRunner(api, _FailingAgent())

    def stop_at_prompt(*_args):
        api._stop_requested.set()
        return False

    runner._confirm = stop_at_prompt
    result = runner._drive(record, plan, "model", "none", "text", None)

    assert result["paused"] is True
    assert record.status == "paused"


def test_phase_hints_match_only_the_phase_goal_not_the_framed_prompt():
    """WORKSPACE file names and other phase IDs must not trigger hints."""

    class _Bus:
        def publish(self, *_args, **_kwargs):
            pass

        def subscribe(self, *_args, **_kwargs):
            return lambda: None

    calls = []

    class _RecordingAgent:
        def _loop(self, task, *_args, hint_text=None):
            calls.append((task, hint_text))
            return {"success": False, "error": "phase failed", "steps": 1}

    api = SimpleNamespace(
        _stop_requested=threading.Event(),
        bus=_Bus(),
        task_store=SimpleNamespace(save=lambda _record: None),
        workspace=SimpleNamespace(
            context_block=lambda: "discord_notes.txt",
            record_task_output=lambda *_args: None,
        ),
        caption_window=None,
        captions_enabled=False,
        speak=lambda _text: None,
    )

    def run(desc):
        api._stop_requested.clear()
        plan = planner.Plan(
            [
                planner.PlanNode("write_page", desc),
                planner.PlanNode("open_spotify", "independent deliverable"),
            ]
        )
        record = TaskRecord("test", "Test", "build a page", plan.to_dict())
        runner = planner.PlanRunner(api, _RecordingAgent())

        def stop_at_prompt(*_args):
            api._stop_requested.set()
            return False

        runner._confirm = stop_at_prompt
        runner._drive(record, plan, "model", "none", "text", None)
        return calls[-1]

    task, hint_text = run("Write index.html")
    assert "discord_notes.txt" in task and "open_spotify" in task
    assert hint_text == "Write index.html"

    # An empty desc must stay empty, not fall back to the framed prompt.
    _task, hint_text = run("")
    assert hint_text == ""
