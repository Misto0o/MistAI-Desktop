# ============================================================
# PLANNER AND ROUTER TESTS
# ============================================================
#
# The planner, the tool registry and the conversation router.
#
# SECTION INDEX
#   1. PLANNER AND TOOLS
#   2. PLANNER AND TOOLS HELPERS
#   3. PLANNER AND TOOLS TESTS
#   4. CONVERSATION ROUTER
#   5. CONVERSATION ROUTER HELPERS
#   6. CONVERSATION ROUTER TESTS
#   7. CONVERSATION ROUTER API
#   8. CONVERSATION ROUTER TESTS (2)
#   9. CONVERSATION ROUTER HELPERS (2)
#   10. CONVERSATION ROUTER AGENT
#   11. CONVERSATION ROUTER ROUTING API
#   12. CONVERSATION ROUTER HELPERS (3)
#   13. CONVERSATION ROUTER TESTS (3)
#   14. CONVERSATION ROUTER BUS
#   15. API AFTER FAILED COMMAND
#   16. CONVERSATION ROUTER TESTS (4)
#   17. CONVERSATION ROUTER RESP
#   18. CONVERSATION ROUTER TESTS (5)
#   19. CONVERSATION ROUTER HELPERS (4)
#   20. CONVERSATION ROUTER TESTS (6)
#   21. CONVERSATION ROUTER GENDER API
#   22. CONVERSATION ROUTER TESTS (7)
#   23. CONVERSATION ROUTER HOTKEY MODELS
#   24. CONVERSATION ROUTER TESTS (8)
#   25. ROUTER VIA AGENT MODE
#   26. ORB SIGNALS FROM THE PLAN RUNNER
#   27. ORB SIGNALS FROM THE PLAN RUNNER HELPERS
#   28. ORB SIGNALS FROM THE PLAN RUNNER TESTS
# ============================================================

"""The planner, the tool registry and the conversation router."""

import json
import re
import threading
from types import SimpleNamespace

import pytest

from core import planner, planner as conversation, config
from core.memory import TaskRecord
from core.tools import ToolRegistry, command_is_mutating, SPECS
from core.config import CHAT_TIMEOUT, DEFAULT_MODEL, describe_api_error
from core.api import Api
import core.api as api_mod
from core.agent import AgentLoop


# ============================================================
# PLANNER AND TOOLS
# ============================================================


# ============================================================
# PLANNER AND TOOLS HELPERS
# ============================================================

def _plan_from(payload):
    return json.dumps(payload)


# ============================================================
# PLANNER AND TOOLS TESTS
# ============================================================

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


def test_help_that_names_a_mutating_verb_now_asks():
    """Changed on purpose: a verb is mutating anywhere as its own token, so a
    command that only names one asks too (a confirmation beats a miss)."""
    assert command_is_mutating("Get-Help Remove-Item")


RUN4_MOVE = (r"Get-ChildItem -Path C:\t\mess -Filter *.txt | ForEach-Object "
             r"{ Move-Item -Path $_.FullName -Destination C:\t\texts -Force }")


@pytest.mark.parametrize("command", [
    RUN4_MOVE,                                                      # the real run 4
    r"Get-ChildItem C:\t | % { Remove-Item $_.FullName }",
    r"Get-ChildItem C:\t | ForEach-Object -Process { Rename-Item $_.FullName x.bak }",
    r"foreach ($f in Get-ChildItem C:\t) { Copy-Item $f.FullName C:\out }",
    r"if (Test-Path C:\t) { Remove-Item C:\t -Recurse }",
    r"Start-Job { Stop-Process -Name notepad }",
    r"$x = $(Remove-Item C:\t\a.txt)",
    r"(Remove-Item C:\t\a.txt)",
    r"[IO.File]::Move('C:\t\a.txt', 'C:\t\b.txt')",
    r"[IO.File]::Delete('C:\t\a.txt')",
    r"[System.IO.Directory]::Delete('C:\t', $true)",
    r"New-Object System.IO.StreamWriter 'C:\t\a.txt'",
    r"(Get-Item C:\t\a.txt).Delete()",
    r"Get-ChildItem C:\t | % Delete",
    r"Get-ChildItem C:\t | ForEach-Object { $_.MoveTo('C:\x\' + $_.Name) }",
    r"Get-Content cmds.txt | iex",
    r"Invoke-Expression $cmd",
    r"$c = 'Remove-Item'; & $c C:\t\a.txt",
    r"& ('Remo' + 've-Item') C:\t\a.txt",
    r"& 'C:\tools\cleanup.exe' /all",
    r"Invoke-Command -ScriptBlock ([scriptblock]::Create('Remove-Item C:\t\a.txt'))",
    r"C:\Windows\System32\robocopy.exe C:\t C:\u /MOVE",
    r"Microsoft.PowerShell.Management\Remove-Item C:\t\a.txt",
    r"$null = reg add HKCU\Software\X /v Y /d 1 /f",
    r"robocopy C:\t C:\u /MOVE",
    r"reg add HKCU\Software\X /v Y /d 1 /f",
    r"Get-ChildItem C:\t | ForEach-Object { cmd /c del $_.FullName }",
    r'cmd /c "del /q C:\t\*.txt"',
])
def test_a_verb_hidden_in_a_block_loop_or_call_is_mutating(command):
    assert command_is_mutating(command)


@pytest.mark.parametrize("command", [
    r"Get-ChildItem -Path C:\t -Filter *.txt",
    r"Get-ChildItem C:\t -Recurse | Measure-Object -Property Length -Sum",
    r"Get-Content C:\t\a.txt | Select-String -Pattern error",
    r"Test-Path C:\t\texts",
    r"Get-Content C:\t\a.txt -Tail 20",
    r"Get-ChildItem C:\t -Recurse -File | Measure-Object | Select-Object -ExpandProperty Count",
    r"Get-ChildItem C:\t | Sort-Object Length -Descending | Select-Object -First 5 Name, Length",
    r"Get-ChildItem C:\t | Where-Object { $_.Extension -eq '.txt' } | Format-Table Name, Length",
    r"Get-ChildItem C:\t | ForEach-Object { $_.Name }",
    r"(Get-ChildItem C:\t -File).Count",
    r"Get-Process | Sort-Object CPU -Descending | Select-Object -First 5",
    r"Get-Process notepad -ErrorAction SilentlyContinue",
    r"Get-Date -Format 'yyyy-MM-dd'",
    r"Get-CimInstance Win32_OperatingSystem | Format-List Caption, Version",
    r"Get-Service | Where-Object Status -eq Running | Select-Object -First 10",
    r"Resolve-Path C:\t; Split-Path C:\t -Leaf; Join-Path C:\t texts",
    r"[math]::Round((Get-PSDrive C).Free / 1GB, 1)",
])
def test_ordinary_read_only_pipelines_stay_read_only(command):
    assert not command_is_mutating(command)


@pytest.mark.parametrize("command, mutating", [
    # A verb only inside a longer word or file name never counts.
    (r"Get-Content C:\t\copyright.txt -TotalCount 5", False),
    (r"Get-ChildItem C:\t\removed -Recurse | Measure-Object", False),
    (r"Get-Item 'C:\t\report-copy.docx'", False),
    (r"Select-String -Path C:\t\notes.txt -Pattern 'files were moved'", False),
    # A verb that is a whole word in a quoted string or a path segment asks:
    # PowerShell can run strings (&, iex, Start-Process, scriptblock::Create).
    (r"Select-String -Path C:\t\notes.txt -Pattern 'please move the files'", True),
    (r"Get-Item 'C:\t\copy of report.docx'", True),
    (r"Write-Output 'Remove-Item C:\t\a.txt'", True),
    # Any .NET System.IO call asks, the read-only [IO.Path] ones too.
    (r"[IO.Path]::GetExtension('C:\t\a.txt')", True),
])
def test_quoted_text_and_file_names_prefer_a_confirmation(command, mutating):
    assert command_is_mutating(command) is mutating


def test_the_run4_move_now_asks_before_it_runs(tmp_path):
    registry = ToolRegistry(db_path=str(tmp_path / "memory.db"))
    try:
        assert registry.requires_confirmation("run_command", RUN4_MOVE)
        assert not registry.requires_confirmation("run_command", r"Get-ChildItem C:\t\texts")
    finally:
        if registry._conn:
            registry._conn.close()


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


class _FinalSpeechBus:
    """Synchronous bus: delivers every publish to its subscribers at once."""

    def __init__(self):
        self.subs = []
        self.names = []

    def publish(self, name, data=None, source=""):
        from core.live_state import Event

        self.names.append(name)
        for want, fn in list(self.subs):
            if want in (None, name):
                fn(Event(name, data or {}, source))

    def subscribe(self, name, fn):
        entry = (name, fn)
        self.subs.append(entry)
        return lambda: self.subs.remove(entry) if entry in self.subs else None


def _final_speech_api():
    api = SimpleNamespace(
        _stop_requested=threading.Event(),
        bus=_FinalSpeechBus(),
        task_store=SimpleNamespace(save=lambda _record: None,
                                   create=lambda name, goal, plan: TaskRecord(
                                       "t1", name, goal, plan)),
        workspace=SimpleNamespace(
            context_block=lambda: "",
            record_task_output=lambda *_args: None,
            phase_evidence=lambda _since: "",
        ),
        caption_window=None,
        captions_enabled=False,
        spoken=[],
    )
    api.speak = lambda text: api.spoken.append(text)
    return api


class _PhaseAgent:
    """Stands in for AgentLoop._loop: per phase, (success, done speech). A
    non-empty done speech is published and spoken exactly as _loop does."""

    def __init__(self, api, outcomes):
        self.api, self.outcomes, self.calls = api, list(outcomes), 0

    def _loop(self, *_args, **_kwargs):
        from core.live_state import AGENT_STEP

        ok, speech = self.outcomes[self.calls]
        self.calls += 1
        if not ok:
            return {"success": False, "error": "phase failed", "steps": 1}
        if speech:
            self.api.bus.publish(AGENT_STEP, {"action": "done", "speech": speech}, "AgentLoop")
            self.api.speak(speech)
        return {"success": True, "speech": speech, "steps": 2}


def _two_phase_run(outcomes, confirm=False):
    api = _final_speech_api()
    plan = planner.Plan([
        planner.PlanNode("setup", "create the files"),
        planner.PlanNode("count", "say how many files there are", ["setup"]),
    ])
    record = TaskRecord("t1", "ProjectSetup", "goal", plan.to_dict())
    runner = planner.PlanRunner(api, _PhaseAgent(api, outcomes))
    runner._confirm = lambda *_args: confirm
    return api, record, runner._drive(record, plan, "model", "none", "text", None)


def test_all_phases_done_the_final_speech_is_the_last_phase_answer():
    api, record, result = _two_phase_run([(True, "Created both files."),
                                          (True, "There are 3 files in the folder.")])
    assert result["success"] is True
    assert result["speech"] == "There are 3 files in the folder."
    # Spoken once by its own phase, never followed by the generic line.
    assert api.spoken == ["Created both files.", "There are 3 files in the folder."]
    assert not any("Completed all" in s for s in api.spoken)
    assert record.status == "completed"


def test_an_empty_last_phase_answer_falls_back_to_the_generic_line():
    api, _record, result = _two_phase_run([(True, "Created both files."), (True, "")])
    assert result["success"] is True
    assert result["speech"] == "Completed all 2 phases of ProjectSetup."
    assert api.spoken[-1] == "Completed all 2 phases of ProjectSetup."


def test_a_failed_phase_keeps_the_partial_failure_message():
    api, record, result = _two_phase_run([(True, "Created both files."), (False, "")])
    expected = ("Finished 1 of 2 phases -- 1 failed. "
                "Say 'resume ProjectSetup' to retry the rest.")
    assert result["success"] is False
    assert result["speech"] == expected
    assert api.spoken[-1] == expected
    assert record.status == "partial"


def test_a_failed_first_phase_keeps_the_failure_message_and_resume_hint():
    api, record, result = _two_phase_run([(False, "")])
    expected = ("Finished 0 of 2 phases -- 1 failed, 1 blocked. "
                "Say 'resume ProjectSetup' to retry the rest.")
    assert result["speech"] == expected and api.spoken == [expected]
    assert record.status == "failed"


def test_a_single_phase_plan_returns_the_loop_result_unchanged(monkeypatch):
    api = _final_speech_api()
    agent = _PhaseAgent(api, [(True, "Calculator is open.")])
    monkeypatch.setattr(planner, "make_plan",
                        lambda *_a, **_k: (planner.Plan([planner.PlanNode("main", "g")]), "N"))
    result = planner.PlanRunner(api, agent).run_goal("open calculator", "m", "none", "text", None)
    assert result == {"success": True, "speech": "Calculator is open.", "steps": 2}
    assert api.spoken == ["Calculator is open."]
    assert planner.events.TASK_COMPLETED not in api.bus.names


def test_the_answer_panel_shows_the_final_speech():
    from core.floating_orb import PanelModel

    api = _final_speech_api()
    panels = PanelModel()
    api.bus.subscribe(None, panels.on_event)
    plan = planner.Plan([
        planner.PlanNode("setup", "create the files"),
        planner.PlanNode("count", "say how many files there are", ["setup"]),
    ])
    record = TaskRecord("t1", "ProjectSetup", "goal", plan.to_dict())
    agent = _PhaseAgent(api, [(True, "Created both files."),
                              (True, "There are 3 files in the folder.")])
    result = planner.PlanRunner(api, agent)._drive(record, plan, "m", "none", "text", None)
    assert panels.state("answer")["text"] == result["speech"]
    assert panels.visible("answer", busy=False)


# ============================================================
# CONVERSATION ROUTER
# ============================================================

# Conversation layer: routing prompt, fail-closed parsing, router call.
#
# Merged by area (pure moves, no test changed) from: test_conversation_layer.py, test_model_choice.py.


# ============================================================
# CONVERSATION ROUTER HELPERS
# ============================================================

NOW = "Tuesday, October 06, 2026 at 03:15 PM"


# -- Prompt grounding ---------------------------------------------------------


# ============================================================
# CONVERSATION ROUTER TESTS
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER API
# ============================================================

def _api(history="", history_raises=False):
    def block(_task):
        if history_raises:
            raise AttributeError("no agent")
        return history

    return SimpleNamespace(agent=SimpleNamespace(_recent_conversation_block=block))


# ============================================================
# CONVERSATION ROUTER TESTS (2)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER HELPERS (2)
# ============================================================

CHAT = '{"route": "chat", "reply": "Ha."}'


# ============================================================
# CONVERSATION ROUTER AGENT
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER ROUTING API
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER HELPERS (3)
# ============================================================

def _finish(api):
    if api.agent.callbacks:
        api.agent.callbacks["on_done"]({"success": True, "steps": 1})


# ============================================================
# CONVERSATION ROUTER TESTS (3)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER BUS
# ============================================================

class _Bus:
    def subscribe(self, *_args):
        return lambda: None

    def publish(self, *_args):
        pass


# ============================================================
# API AFTER FAILED COMMAND
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER TESTS (4)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER RESP
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER TESTS (5)
# ============================================================

def test_choices_labels_and_default():
    assert config.MODEL_CHOICES == ("gemini", "cohere", "mistral")
    assert config.MODEL_LABELS == {"gemini": "Nova", "cohere": "Sage", "mistral": "Flux"}
    assert set(config.MODEL_LABELS) == set(config.MODEL_CHOICES)  # a label for every id
    assert config.DEFAULT_MODEL == "cohere" and config.AGENT_MODEL == "cohere"
    assert not hasattr(config, "CHAT_MODEL")
    assert Api.__new__(Api).current_model() == "cohere"   # bare test object


# ============================================================
# CONVERSATION ROUTER HELPERS (4)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER TESTS (6)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER GENDER API
# ============================================================

def _gender_api(memory):
    voices = []
    api = Api.__new__(Api)
    api.memory = memory
    api.tts = SimpleNamespace(set_gender=voices.append)
    return api, voices


# ============================================================
# CONVERSATION ROUTER TESTS (7)
# ============================================================

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


# ============================================================
# CONVERSATION ROUTER HOTKEY MODELS
# ============================================================

def _hotkey_models(monkeypatch, api):
    sent = []
    monkeypatch.setattr(api_mod.requests, "post", lambda url, json=None, **_k: (
        sent.append(json["model"]), _Resp(200, {"response": "ctrl+k"}))[1])
    api.api_manager = SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u")
    return sent


# ============================================================
# CONVERSATION ROUTER TESTS (8)
# ============================================================

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


# -- why a message went to the agent (a fixed label on the route line) -----------


@pytest.mark.parametrize("raw, label", [
    ("", "no reply"),
    ("I think this is a task", "unparsable"),
    ('{"route": "chat", "reply": ""}', "no chat reply"),
    ('{"route": "task", "reply": ""}', ""),
    ('{"route": "chat", "reply": "Hi."}', ""),
])
def test_route_reason_labels(raw, label):
    assert conversation.route_reason(raw) == label


def test_the_route_line_names_the_reason_and_never_the_message(monkeypatch, capsys):
    monkeypatch.setattr("core.planner._llm", lambda *a, **k: "secret words here")
    assert conversation.try_chat(_api(), "open my diary", "none") is None
    line = [l for l in capsys.readouterr().out.splitlines() if "route=" in l][0]
    assert line.startswith("[Conversation] route=task in ") and line.endswith("(unparsable)")
    assert "diary" not in line and "secret" not in line


def test_a_reply_that_never_came_within_the_timeout_is_labelled_timeout(monkeypatch, capsys):
    clock = iter([0.0, 6.0])
    monkeypatch.setattr(conversation.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr("core.planner._llm", lambda *a, **k: "")
    conversation.try_chat(_api(), "hi there", "none")
    assert "route=task in 6.00s (timeout)" in capsys.readouterr().out


def test_a_task_decision_has_no_label(monkeypatch, capsys):
    monkeypatch.setattr("core.planner._llm", lambda *a, **k: '{"route": "task", "reply": ""}')
    conversation.try_chat(_api(), "open notepad", "none")
    out = capsys.readouterr().out
    assert "route=task in" in out and "(" not in out.split("route=task in", 1)[1].splitlines()[0]


@pytest.mark.parametrize("env, want", [(None, 5), ("2.5", 2.5), ("0", 1.0), ("99", 30.0),
                                       ("abc", 5), ("nan", 5)])
def test_chat_timeout_reads_the_environment(monkeypatch, env, want):
    from core.config import chat_timeout

    if env is None:
        monkeypatch.delenv("MISTAI_CHAT_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CHAT_TIMEOUT", env)
    assert chat_timeout() == want
    calls = []
    monkeypatch.setattr("core.planner._llm",
                        lambda api, p, m, timeout, label, fallback: calls.append(timeout) or "")
    conversation.try_chat(_api(), "hello there", "none")
    assert calls == [want]


# ============================================================
# ROUTER VIA AGENT MODE
# ============================================================

def _posting_api(monkeypatch, reply='{"route": "task", "reply": ""}', ok=True):
    sent = []
    monkeypatch.setattr(planner.requests, "post", lambda url, json=None, **_k: (
        sent.append(json), _Resp(200 if ok else 500, {"response": reply} if ok else {"error": "x"}))[1])
    api = _api()
    api.api_manager = SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "https://p.invalid",
                                      get_fallback_url=lambda: "https://f.invalid")
    return api, sent


def test_flag_off_sends_the_plain_message_form(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_ROUTER_VIA_AGENT", raising=False)
    api, sent = _posting_api(monkeypatch)
    conversation.try_chat(api, "open notepad", "none")
    assert set(sent[0]) == {"message", "model"}
    assert re.search(r"\[Timing\] Router LLM: [\d.]+s mode=message", capsys.readouterr().out)


def test_flag_on_sends_router_in_agent_form_with_the_voice_line(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_ROUTER_VIA_AGENT", "1")
    api, sent = _posting_api(monkeypatch, '{"route": "chat", "reply": "Hello."}')
    assert conversation.try_chat(api, "hi there", "none") == "Hello."
    body = sent[0]
    assert body["mode"] == "agent" and body["system"] == planner.ROUTER_SYSTEM
    assert "spoken aloud" in body["system"]
    assert body["messages"] == [{"role": "user", "content": body["message"]}]
    assert '"route": "chat" or "task"' in body["message"]          # same schema prompt
    assert re.search(r"\[Timing\] Router LLM: [\d.]+s mode=agent", capsys.readouterr().out)


def test_flag_on_sends_planning_in_agent_form(monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_ROUTER_VIA_AGENT", "1")
    api, sent = _posting_api(monkeypatch, '{"task_name": "T", "nodes": [{"id": "a", "desc": "x"}]}')
    api.workspace = SimpleNamespace(context_block=lambda: "(none)")
    planner.make_plan(api, "build a site with three pages then deploy it", "m")
    assert sent[0]["mode"] == "agent" and sent[0]["system"] == planner.PLANNER_SYSTEM
    assert "PHASES" in sent[0]["message"]
    assert re.search(r"\[Timing\] Planning LLM: [\d.]+s mode=agent", capsys.readouterr().out)


@pytest.mark.parametrize("reply, ok", [("", True), ("not json", True), ("", False)])
def test_flag_on_fails_closed(monkeypatch, reply, ok):
    monkeypatch.setenv("MISTAI_ROUTER_VIA_AGENT", "1")
    api, _ = _posting_api(monkeypatch, reply, ok)
    assert conversation.try_chat(api, "hi there", "none") is None
    api.workspace = SimpleNamespace(context_block=lambda: "(none)")
    plan, _name = planner.make_plan(api, "build a site with three pages then deploy it", "m")
    assert len(plan.order) == 1                       # one phase, as without the flag


def test_router_flag_is_read_at_call_time(monkeypatch):
    from core.config import router_via_agent
    monkeypatch.delenv("MISTAI_ROUTER_VIA_AGENT", raising=False)
    assert router_via_agent() is False and planner.call_mode() == "message"
    monkeypatch.setenv("MISTAI_ROUTER_VIA_AGENT", "1")
    assert router_via_agent() is True and planner.call_mode() == "agent"

# ============================================================
# ORB SIGNALS FROM THE PLAN RUNNER
# ============================================================

# PlanRunner publishes the floating orb's started / finished signals
# (events.ORB_SIGNAL, core/floating_orb.py) for every task, one-node plans
# too (they still skip _drive's task events), and planning around the
# planning call. Only while the orb is shown.


# ============================================================
# ORB SIGNALS FROM THE PLAN RUNNER HELPERS
# ============================================================

def _orb_signal_api(shown=True):
    api = _final_speech_api()
    api._orb = SimpleNamespace(visible=shown)
    return api


def _orb_signals(api):
    sent = []
    api.bus.subscribe(planner.events.ORB_SIGNAL, lambda ev: sent.append(dict(ev.data)))
    return sent


def _one_node(monkeypatch):
    monkeypatch.setattr(planner, "make_plan",
                        lambda *_a, **_k: (planner.Plan([planner.PlanNode("main", "g")]), "N"))


# ============================================================
# ORB SIGNALS FROM THE PLAN RUNNER TESTS
# ============================================================

def test_a_one_node_task_signals_started_and_finished(monkeypatch):
    _one_node(monkeypatch)
    api = _orb_signal_api()
    sent = _orb_signals(api)
    agent = _PhaseAgent(api, [(True, "Calculator is open.")])
    result = planner.PlanRunner(api, agent).run_goal("open calculator", "m", "none", "text", None)
    assert result == {"success": True, "speech": "Calculator is open.", "steps": 2}
    assert sent == [{"signal": "started", "on": True},
                    {"signal": "finished", "on": True, "outcome": "completed", "reason": ""}]
    assert planner.events.TASK_COMPLETED not in api.bus.names       # still no task events
    assert planner.events.TASK_STARTED not in api.bus.names


def test_a_failed_one_node_task_signals_an_error(monkeypatch):
    _one_node(monkeypatch)
    api = _orb_signal_api()
    sent = _orb_signals(api)
    agent = _PhaseAgent(api, [(False, "")])
    planner.PlanRunner(api, agent).run_goal("open calculator", "m", "none", "text", None)
    assert sent[-1] == {"signal": "finished", "on": True, "outcome": "error", "reason": "failed"}


def test_a_crashing_task_still_signals_finished(monkeypatch):
    _one_node(monkeypatch)
    api = _orb_signal_api()
    sent = _orb_signals(api)

    class Boom:
        def _loop(self, *_a, **_k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        planner.PlanRunner(api, Boom()).run_goal("open calculator", "m", "none", "text", None)
    assert [s["signal"] for s in sent] == ["started", "finished"]
    assert sent[-1]["outcome"] == "error" and sent[-1]["reason"] == "crash"


def test_a_multi_phase_task_signals_once_around_its_task_events(monkeypatch):
    api = _orb_signal_api()
    sent = _orb_signals(api)
    plan = planner.Plan([
        planner.PlanNode("setup", "create the files"),
        planner.PlanNode("count", "say how many files there are", ["setup"]),
    ])
    monkeypatch.setattr(planner, "make_plan", lambda *_a, **_k: (plan, "ProjectSetup"))
    agent = _PhaseAgent(api, [(True, "Created."), (True, "Three files.")])
    planner.PlanRunner(api, agent).run_goal("set up and count", "m", "none", "text", None)
    assert [s["signal"] for s in sent] == ["started", "finished"]
    assert sent[-1]["outcome"] == "completed"
    names = api.bus.names
    assert names.index(planner.events.ORB_SIGNAL) < names.index(planner.events.TASK_STARTED)
    assert names.index(planner.events.TASK_COMPLETED) < len(names) - 1   # finished comes last
    assert names[-1] == planner.events.ORB_SIGNAL


def test_resume_signals_started_and_finished(monkeypatch):
    api = _orb_signal_api()
    sent = _orb_signals(api)
    plan = planner.Plan([planner.PlanNode("a", "first"), planner.PlanNode("b", "second", ["a"])])
    record = TaskRecord("t1", "Job", "goal", plan.to_dict())
    agent = _PhaseAgent(api, [(True, "A."), (False, "")])
    planner.PlanRunner(api, agent).resume(record, "m", "none", "text", None)
    assert [s["signal"] for s in sent] == ["started", "finished"]
    assert sent[-1]["outcome"] == "error" and sent[-1]["reason"] == "phases"


def test_planning_is_signalled_only_around_the_planning_call(monkeypatch):
    api = _orb_signal_api()
    sent = _orb_signals(api)
    during = []

    def fake_llm(*_a, **_k):
        during.append([s["signal"] for s in sent])
        return '{"task_name": "Site", "nodes": [{"id": "a", "desc": "make it"}]}'

    monkeypatch.setattr(planner, "_llm", fake_llm)
    planner.make_plan(api, "build a small portfolio site with three pages and a contact form", "m")
    assert during == [["planning"]]
    assert sent == [{"signal": "planning", "on": True}, {"signal": "planning", "on": False}]
    sent.clear()
    planner.make_plan(api, "what time is it?", "m")               # simple: no planning call
    assert sent == []


def test_no_orb_no_signals(monkeypatch):
    _one_node(monkeypatch)
    for api in (_final_speech_api(), _orb_signal_api(shown=False)):
        sent = _orb_signals(api)
        agent = _PhaseAgent(api, [(True, "Done.")])
        planner.PlanRunner(api, agent).run_goal("open calculator", "m", "none", "text", None)
        assert sent == [] and planner.events.ORB_SIGNAL not in api.bus.names
