"""Hardware-free regression coverage for runtime execution safeguards.

These tests deliberately exercise the public orchestration code with small
in-memory fakes.  They must not open an app, touch the microphone, or require
an actual desktop window.
"""

import os
import threading
from types import SimpleNamespace

from core.api import Api
from core.executor import Executor
from core.tools import ToolSpec


class _Registry:
    def __init__(self, specs):
        self.specs = specs

    def get(self, action):
        return self.specs[action]

    def settle(self, action):
        return self.specs[action].settle

    def is_destructive(self, _action, _parameter):
        return False


class _Recorder:
    def __init__(self):
        self.calls = []

    def add_action(self, *args):
        self.calls.append(args)

    def record(self, *args):
        self.calls.append(args)

    def record_execution(self, *args):
        self.calls.append(args)

    def publish(self, *args):
        self.calls.append(args)


def _execution_api(specs, outcomes):
    """Build only the interface ``Executor`` needs; no real desktop state."""
    memory = _Recorder()
    task_memory = _Recorder()
    workspace = _Recorder()
    bus = _Recorder()
    api = SimpleNamespace(
        tools=_Registry(specs),
        memory=memory,
        task_memory=task_memory,
        workspace=workspace,
        bus=bus,
        uia_nav=SimpleNamespace(invalidate_cache=lambda: None),
        click_handler=SimpleNamespace(last_method=None),
        live=SimpleNamespace(snapshot={"foreground_window": ""}),
        last_result_text="",
    )

    def exec_sync(action, _parameter):
        value = outcomes[action]
        if isinstance(value, Exception):
            raise value
        return value

    api._exec_sync = exec_sync
    return api, memory, task_memory, workspace, bus


def test_tray_only_app_is_verification_failure_not_an_open_app_success(monkeypatch):
    """A running process without a visible window must not pass ``open_app``."""
    spec = ToolSpec(name="open_app", settle=0, verifier="app_visible")
    api, memory, task_memory, workspace, bus = _execution_api(
        {"open_app": spec}, {"open_app": True}
    )
    api.app_window_state = lambda _name: ("hidden", "")
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    result = Executor(api).run("open_app", "Discord")

    assert result.success is False
    assert result.verified is False
    assert "tray/background only" in result.detail
    assert memory.calls == [("open_app", "Discord", False)]
    assert task_memory.calls[0][:3] == ("open_app", "Discord", False)
    assert workspace.calls[0][:3] == ("open_app", "Discord", False)
    assert bus.calls[0][0] == "ActionFailed"


def test_executor_records_every_attempt_and_returns_primary_final_failure(monkeypatch):
    """A failed fallback must not hide the original failed attempt or result."""
    specs = {
        "primary": ToolSpec(name="primary", settle=0, fallback_tools=("fallback",)),
        "fallback": ToolSpec(name="fallback", settle=0),
    }
    api, memory, task_memory, workspace, bus = _execution_api(
        specs, {"primary": False, "fallback": False}
    )
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    result = Executor(api).run("primary", "target")

    assert result.action == "primary"
    assert result.success is False
    assert result.escalate_to_user is True
    assert memory.calls == [
        ("primary", "target", False),
        ("fallback", "target", False),
    ]
    assert [call[:3] for call in task_memory.calls] == memory.calls
    assert [call[:3] for call in workspace.calls] == memory.calls
    assert [call[0] for call in bus.calls] == ["ActionFailed", "ActionFailed"]


def test_information_action_text_reaches_executor_result(monkeypatch):
    """The model must receive the actual list, not merely a generic success."""
    api = Api.__new__(Api)
    api.last_result_text = ""
    spoken = []
    api.list_open_apps = lambda: "MistAI, Notepad"
    api.speak = spoken.append
    api.tools = _Registry({"list_apps": ToolSpec(name="list_apps", settle=0)})
    api.memory = _Recorder()
    api.task_memory = _Recorder()
    api.workspace = _Recorder()
    api.bus = _Recorder()
    api.uia_nav = SimpleNamespace(invalidate_cache=lambda: None)
    api.click_handler = SimpleNamespace(last_method=None)
    api.live = SimpleNamespace(snapshot={"foreground_window": ""})
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    result = Executor(api).run("list_apps", "")

    assert result.success is True
    assert result.detail == "Open apps: MistAI, Notepad"
    assert spoken == ["Open apps: MistAI, Notepad"]


def test_screen_analysis_description_reaches_executor_result(monkeypatch):
    """Perception responses are results, not speech-only side effects."""
    api = Api.__new__(Api)
    api.last_result_text = ""
    api.analyze_screen = lambda: {
        "app": "Settings",
        "open_apps": "Settings, MistAI",
        "description": "The display brightness slider is at 70 percent.",
    }
    api.tools = _Registry(
        {"analyze_screen": ToolSpec(name="analyze_screen", settle=0)}
    )
    api.memory = _Recorder()
    api.task_memory = _Recorder()
    api.workspace = _Recorder()
    api.bus = _Recorder()
    api.uia_nav = SimpleNamespace(invalidate_cache=lambda: None)
    api.click_handler = SimpleNamespace(last_method=None)
    api.live = SimpleNamespace(snapshot={"foreground_window": ""})
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    result = Executor(api).run("analyze_screen", "")

    assert result.success is True
    assert "Window: Settings" in result.detail
    assert "brightness slider is at 70 percent" in result.detail


def test_filler_does_not_start_an_agent_but_real_commands_stay_actionable():
    api = Api.__new__(Api)
    calls = []
    api.memory = SimpleNamespace(
        get_profile=lambda _key, default: default,
        add_conversation=lambda *args: calls.append(args),
    )
    api.agent = SimpleNamespace(run_project=lambda *args, **kwargs: calls.append(args))
    api.speak = lambda _text: None

    result = Api.ask_mistai(api, "Thanks")

    assert result["success"] is True
    assert result["steps"] == 0
    assert calls == []
    assert Api._is_non_actionable(api, "stop the music") is False
    assert Api._is_non_actionable(api, "please open calculator") is False
    assert Api._is_non_actionable(api, "thank you, please open calculator") is False


class _HoldingAgent:
    def __init__(self):
        self.runs = []
        self.callbacks = None

    def run_project(self, *args, **kwargs):
        self.runs.append(args)
        self.callbacks = kwargs


def _locked_api():
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_state_lock = threading.Lock()
    api._agent_task = None
    api._agent_started_at = 0.0
    api._agent_generation = 0
    api._stop_requested = threading.Event()
    api.AGENT_HARD_TIMEOUT = 0.01
    api.memory = SimpleNamespace(
        get_profile=lambda _key, default: default,
        add_conversation=lambda *_args: None,
    )
    api.task_store = SimpleNamespace(find_resumable=lambda _name: None)
    api.agent = _HoldingAgent()
    api.minimize_window = lambda: None
    api._notify_js = lambda _js: None
    api.speak = lambda _text: None
    return api


def test_agent_run_gate_rejects_concurrent_task_until_owner_callback_releases_it():
    api = _locked_api()

    first = Api.ask_mistai(api, "open calculator")
    second = Api.ask_mistai(api, "open notepad")

    assert first["error"] == "still_running"
    assert first["busy"] is True
    assert second["error"] == "busy"
    assert len(api.agent.runs) == 1
    assert Api.is_agent_busy(api)["busy"] is True

    api.agent.callbacks["on_done"]({"success": True, "steps": 1})

    assert Api.is_agent_busy(api)["busy"] is False
    third = Api.ask_mistai(api, "open notepad")
    assert third["error"] == "still_running"
    assert len(api.agent.runs) == 2
    api.agent.callbacks["on_done"]({"success": True, "steps": 1})


def test_typed_stop_interrupts_an_active_agent_instead_of_becoming_filler():
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_lock.acquire()
    api._agent_state_lock = threading.Lock()
    api._stop_requested = threading.Event()
    api._answer_lock = threading.Lock()
    api._awaiting_text_answer = threading.Event()
    api._answer_event = threading.Event()
    api._pending_answer = None
    interrupted = []
    api.tts = SimpleNamespace(interrupt=lambda: interrupted.append(True))
    spoken = []
    api.speak = lambda text, **_kwargs: spoken.append(text)
    api.memory = SimpleNamespace(get_profile=lambda _key, default: default)

    result = Api.ask_mistai(api, "stop")

    assert result["success"] is True
    assert api._stop_requested.is_set()
    assert interrupted == [True]
    assert spoken == ["Stopping."]


def test_stop_wakes_a_pending_text_confirmation_with_a_stop_sentinel():
    api = Api.__new__(Api)
    api._agent_lock = threading.Lock()
    api._agent_lock.acquire()
    api._agent_state_lock = threading.Lock()
    api._stop_requested = threading.Event()
    api._answer_lock = threading.Lock()
    api._awaiting_text_answer = threading.Event()
    api._awaiting_text_answer.set()
    api._answer_event = threading.Event()
    api._pending_answer = None
    api.tts = SimpleNamespace(interrupt=lambda: None)

    result = Api.stop_current_task(api)

    assert result == {"success": True, "was_running": True}
    assert api._stop_requested.is_set()
    assert api._answer_event.is_set()
    assert api._pending_answer == "__mistai_stop_requested__"


def test_type_text_does_not_clear_a_field_when_clipboard_staging_fails(monkeypatch):
    api = Api.__new__(Api)
    api.live = SimpleNamespace(snapshot={"foreground_window": "Notepad"})
    api.get_clipboard = lambda: "existing clipboard"
    api.set_clipboard = lambda _text: False
    api.last_result_text = ""
    input_events = []
    monkeypatch.setattr(
        "core.api.pyautogui.hotkey", lambda *keys: input_events.append(("hotkey", keys))
    )
    monkeypatch.setattr(
        "core.api.pyautogui.press", lambda key: input_events.append(("press", key))
    )

    assert Api._act_type_text(api, "new message") is False
    assert input_events == []
    assert "left unchanged" in api.last_result_text


def test_app_lookup_uses_literal_matching_and_rejects_empty_targets(monkeypatch):
    api = Api.__new__(Api)
    commands = []
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda args, **_kwargs: (
            commands.append(args[-1])
            or SimpleNamespace(stdout="Example.App\n")
        ),
    )

    assert Api._resolve_app_id(api, "Editor*[test]'") == "Example.App"
    assert "-like" not in commands[0]
    assert "IndexOf" in commands[0]
    assert "Editor*[test]''" in commands[0]
    assert Api._resolve_app_id(api, "") is None
    assert Api.is_app_running(api, "") is False


def test_is_app_running_ignores_mistai_own_processes(monkeypatch):
    api = Api.__new__(Api)
    procs = [
        SimpleNamespace(info={"name": "msedgewebview2.exe", "pid": 1}),
        SimpleNamespace(info={"name": "edgehelper.exe", "pid": os.getpid()}),
    ]
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: list(procs))
    assert Api.is_app_running(api, "edge") is False

    procs.append(SimpleNamespace(info={"name": "msedge.exe", "pid": 2}))
    assert Api.is_app_running(api, "edge") is True


def test_wake_toggle_reuses_one_listener_thread_instead_of_spawning_duplicates(
    monkeypatch,
):
    api = Api.__new__(Api)
    api.wake_word_active = False
    api._wake_thread = None
    api._wake_enabled = threading.Event()
    api._wake_shutdown = threading.Event()
    api._wake_lock = threading.Lock()
    created = []

    class _Thread:
        def __init__(self, **_kwargs):
            self.alive = False
            created.append(self)

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

    monkeypatch.setattr("core.api.threading.Thread", _Thread)

    assert Api.start_wake_word_detection(api)["success"] is True
    Api.stop_wake_word_detection(api)
    assert Api.start_wake_word_detection(api)["message"] == "Already listening"

    assert len(created) == 1
    assert api._wake_enabled.is_set()
