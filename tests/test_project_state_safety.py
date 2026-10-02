"""Regression coverage for existing-project state and safe mutation paths."""

import json
import subprocess
import threading
from types import SimpleNamespace

from core.agent import AgentLoop
from core.api import Api
from core.executor import ExecutionResult, Executor
from core.planner import make_plan
from core.tools import ToolRegistry
from core.workspace import Workspace


class _Bus:
    def __init__(self):
        self.events = []

    def subscribe(self, *_args):
        return lambda: None

    def publish(self, *args):
        self.events.append(args)


def _api_with_workspace(tmp_path):
    api = Api.__new__(Api)
    api.workspace = Workspace(_Bus())
    api.last_result_text = ""
    api.last_command_result = None
    api._workspace_required_for_task = False
    api._trusted_project_roots = set()
    api._resolve_path_placeholders = lambda path: str(path)
    return api


def test_explicit_existing_project_is_scanned_before_planning(tmp_path, monkeypatch):
    for name, content in {
        "index.html": "<main>portfolio</main>",
        "style.css": "main { color: red; }",
        "script.js": "console.log('portfolio')",
        "animate.js": "export const animate = true",
        "package.json": '{"scripts":{"dev":"vite"},"dependencies":{"animejs":"1"}}',
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'convert workspace at "{tmp_path}" to React')
    context = api.workspace.context_block()
    assert "Project files (already exist -- do NOT recreate)" in context
    assert "index.html" in context and "package.json" in context
    assert "dependencies: animejs" in context

    captured = {}

    def fake_llm(_api, prompt, _model):
        captured["prompt"] = prompt
        return '{"task_name":"PortfolioMigration","nodes":[{"id":"migrate","desc":"Migrate the existing portfolio","depends_on":[]}]}'

    monkeypatch.setattr("core.planner._llm", fake_llm)
    plan, _ = make_plan(api, "convert this existing website to React", "test")
    assert plan.nodes["migrate"].desc == "Migrate the existing portfolio"
    assert "index.html" in captured["prompt"]
    assert "package.json" in captured["prompt"]
    assert "dependencies: animejs" in captured["prompt"]


def test_existing_folder_is_not_inferred_without_explicit_or_vscode_evidence(tmp_path, monkeypatch):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(api, "_vscode_workspace_path", lambda: None)

    assert not Api._establish_workspace_for_task(api, "convert portfolio website to React")
    assert api.workspace.project_root is None


def test_explicit_project_path_persists_and_allows_mutation_after_inspection(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True

    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    assert api.workspace.project_root == str(project.resolve())
    assert not api._workspace_required_for_task
    assert Api._act_inspect_file(api, str(index))
    assert not AgentLoop(api, None)._workspace_mutation_is_blocked(
        "patch_file", f"{index}|||</main>|||<section>New</section></main>"
    )


def test_vscode_discovered_root_persists_for_later_actions(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    monkeypatch.setattr(api, "_vscode_workspace_path", lambda: str(project))

    assert Api._establish_workspace_for_task(api, "add a section to this website")
    assert Api._act_inspect_file(api, str(index))
    assert api.workspace.project_root == str(project.resolve())
    assert not AgentLoop(api, None)._workspace_mutation_is_blocked(
        "write_file", f"{index}|||<main>replacement</main>"
    )


def test_explicit_file_path_establishes_its_parent_but_arbitrary_inspection_does_not(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")

    explicit_api = _api_with_workspace(tmp_path)
    explicit_api._workspace_required_for_task = True
    assert Api._establish_workspace_for_task(explicit_api, f'Use "{index}"')
    assert explicit_api.workspace.project_root == str(project.resolve())
    assert Api._act_inspect_file(explicit_api, str(index))
    assert not explicit_api._workspace_required_for_task

    untrusted = tmp_path / "unrelated.txt"
    untrusted.write_text("not a project", encoding="utf-8")
    arbitrary_api = _api_with_workspace(tmp_path)
    assert Api._act_inspect_file(arbitrary_api, str(untrusted))
    assert arbitrary_api.workspace.project_root is None
    assert not Api._is_trusted_project_root(arbitrary_api, str(tmp_path))


def test_workspace_root_is_available_to_later_phase_framing(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    (project / "index.html").write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    assert Api._act_inspect_file(api, str(project / "index.html"))

    from core.planner import Plan, PlanNode, PlanRunner, DONE

    plan = Plan(
        [
            PlanNode("inspect", "Inspect the existing site"),
            PlanNode("edit", "Patch the inspected existing site", ["inspect"]),
        ]
    )
    plan.mark("inspect", DONE, "Verified phase actions: inspect_file(index.html)")
    frame = PlanRunner(api, None)._frame("Add a section", plan, plan.nodes["edit"])
    assert str(project) in frame
    assert "inspect_file(index.html)" in frame


def test_explicit_path_answer_clears_the_latched_workspace_gate(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    # This mirrors the first successful inspection in the reported failure:
    # it is useful evidence for editing, but is not enough by itself to trust
    # a model-provided project root.
    assert Api._act_inspect_file(api, str(index))
    assert api.workspace.project_root is None

    api._stop_requested = threading.Event()
    api.live = SimpleNamespace(
        snapshot={
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        }
    )
    api.vision = SimpleNamespace(ready=False)
    api.caption_window = None
    api.captions_enabled = False
    api.memory = SimpleNamespace(add_action=lambda *_args: None)
    api.task_memory = SimpleNamespace(get_hints_for_task=lambda _task: [])
    api.api_manager = SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://qa.invalid",
        get_fallback_url=lambda: "https://qa.invalid",
    )
    api._get_text_answer = lambda _question, timeout=60: str(project)
    api._normalize_utterance = lambda text: str(text or "").lower()
    api._VOICE_STOP_WORDS = set()
    api.speak = lambda *_args, **_kwargs: None
    api.tools = ToolRegistry(":memory:")

    replies = iter(
        [
            {"action": "ask_user", "parameter": "Which project folder should I use?", "speech": ""},
            {"action": "done", "parameter": "", "speech": ""},
        ]
    )

    class _Response:
        ok = True

        def __init__(self, reply):
            self.reply = reply

        def json(self):
            return {"response": json.dumps(self.reply)}

    monkeypatch.setattr(
        "core.agent.requests.post", lambda *_args, **_kwargs: _Response(next(replies))
    )
    try:
        loop = AgentLoop(api, None)
        loop._get_screen_context = lambda: "VS Code is open."
        result = loop._loop("Add a section to this website", "test", "none", "text", None)
    finally:
        api.tools._conn.close()

    assert result["success"]
    assert api.workspace.project_root == str(project.resolve())
    assert not api._workspace_required_for_task


def test_portfolio_path_handoff_inspects_patches_and_opens_same_project(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    original = "<main><h1>Portfolio</h1></main>"
    index.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    api._stop_requested = threading.Event()
    api.live = SimpleNamespace(
        snapshot={
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        }
    )
    api.vision = SimpleNamespace(ready=False)
    api.caption_window = None
    api.captions_enabled = False
    api.memory = SimpleNamespace(add_action=lambda *_args: None)
    api.task_memory = SimpleNamespace(
        get_hints_for_task=lambda _task: [], record=lambda *_args: None
    )
    api.api_manager = SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://qa.invalid",
        get_fallback_url=lambda: "https://qa.invalid",
    )
    api._get_text_answer = lambda question, timeout=60: (
        str(project) if question.startswith("Which project") else "yes"
    )
    api._normalize_utterance = lambda text: str(text or "").lower()
    api._VOICE_STOP_WORDS = set()
    api.speak = lambda *_args, **_kwargs: None
    api.tools = ToolRegistry(":memory:")
    api.executor = Executor(api)
    api._resolve_app_exe = lambda _name: "code"

    opened = []
    process_lists = iter([[], [SimpleNamespace(info={"name": "Code.exe"})]])
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: opened.append(args))
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: next(process_lists))
    monkeypatch.setattr("core.api.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    replies = iter(
        [
            {"action": "inspect_file", "parameter": str(index), "speech": ""},
            {"action": "ask_user", "parameter": "Which project folder should I use?", "speech": ""},
            {
                "action": "patch_file",
                "parameter": (
                    f"{index}|||</main>|||<section id=\"about\">About</section></main>"
                ),
                "speech": "",
            },
            {"action": "open_project_in_vscode", "parameter": str(project), "speech": ""},
            {"action": "done", "parameter": "", "speech": ""},
        ]
    )

    class _Response:
        ok = True

        def __init__(self, reply):
            self.reply = reply

        def json(self):
            return {"response": json.dumps(self.reply)}

    monkeypatch.setattr(
        "core.agent.requests.post", lambda *_args, **_kwargs: _Response(next(replies))
    )
    try:
        loop = AgentLoop(api, None)
        loop._get_screen_context = lambda: "VS Code is open."
        result = loop._loop("Add a section to this website", "test", "none", "text", None)
    finally:
        api.tools._conn.close()

    assert result["success"]
    assert api.workspace.project_root == str(project.resolve())
    assert "<h1>Portfolio</h1>" in index.read_text(encoding="utf-8")
    assert "<section id=\"about\">About</section>" in index.read_text(encoding="utf-8")
    assert opened == [["code", "-n", str(project)]]


def test_vscode_status_discovers_one_direct_desktop_workspace(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    documents = tmp_path / "Documents"
    project = desktop / "Portfolio"
    project.mkdir(parents=True)
    api = _api_with_workspace(tmp_path)
    api._get_desktop_path = lambda: str(desktop)
    api._resolve_path_placeholders = lambda _path: str(documents)

    captured = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args, 0, "|   Folder (Portfolio): 5 files\n", ""
        )

    monkeypatch.setattr("core.api.shutil.which", lambda _name: r"C:\\VS Code\\bin\\code.cmd")
    monkeypatch.setattr("core.api.subprocess.run", fake_run)

    assert Api._vscode_workspace_path(api) == str(project.resolve())
    assert captured["args"][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "--status" in captured["args"][-1]

    (documents / "Portfolio").mkdir(parents=True)
    assert Api._vscode_workspace_path(api) is None


def test_executor_status_text_is_safe_for_legacy_windows_console():
    status = ExecutionResult(
        "inspect_file", "package.json", True, confidence=1.0, verified=True
    ).short()
    assert status.encode("cp1252")
    assert "verify=OK" in status


def test_existing_file_requires_inspection_before_full_replacement(tmp_path):
    target = tmp_path / "package.json"
    original = '{"scripts":{"dev":"vite"},"dependencies":{"animejs":"1"}}'
    target.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)

    assert not Api._act_write_file(api, f"{target}|||{{\"license\":\"MIT\"}}")
    assert target.read_text(encoding="utf-8") == original
    assert "REFUSED" in api.last_result_text

    assert Api._act_inspect_file(api, str(target))
    assert Api._act_write_file(api, f"{target}|||{{\"license\":\"MIT\"}}")
    assert json.loads(target.read_text(encoding="utf-8")) == {"license": "MIT"}

    registry = ToolRegistry(db_path=str(tmp_path / "stats.db"))
    try:
        assert registry.requires_confirmation("write_file", f"{target}|||{{}}")
    finally:
        if registry._conn:
            registry._conn.close()
    loop = AgentLoop(api, None)
    assert "REPLACE all existing content" in loop._confirmation_question(
        "write_file", f"{target}|||{{}}"
    )


def test_write_file_documentation_does_not_describe_an_append(tmp_path):
    target = tmp_path / "index.html"
    target.write_text("<main>existing</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    loop = AgentLoop(api, None)

    system = loop._build_system(
        "none",
        {
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        },
        "test screen",
        "",
        [],
    )
    assert "REPLACES THE ENTIRE FILE" in system
    assert "NEVER appends" in system
    assert "patch_file" in system

    # This is the exact misunderstanding: even after inspection, a write with
    # only a new section is a complete replacement, never an append.
    assert Api._act_inspect_file(api, str(target))
    assert Api._act_write_file(api, f"{target}|||<section>new section</section>")
    assert target.read_text(encoding="utf-8") == "<section>new section</section>"


def test_patch_file_preserves_source_with_one_unique_inspected_anchor(tmp_path):
    target = tmp_path / "index.html"
    original = "<main><h1>Portfolio</h1></main>"
    target.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    patch = f"{target}|||</main>|||<section id=\"about\">About</section></main>"

    assert not Api._act_patch_file(api, patch)
    assert target.read_text(encoding="utf-8") == original
    assert Api._act_inspect_file(api, str(target))
    assert Api._act_patch_file(api, patch)
    updated = target.read_text(encoding="utf-8")
    assert "<h1>Portfolio</h1>" in updated
    assert "<section id=\"about\">About</section>" in updated

    duplicate = tmp_path / "duplicate.html"
    duplicate.write_text("<main></main><main></main>", encoding="utf-8")
    assert Api._act_inspect_file(api, str(duplicate))
    before = duplicate.read_text(encoding="utf-8")
    assert not Api._act_patch_file(api, f"{duplicate}|||</main>|||changed")
    assert duplicate.read_text(encoding="utf-8") == before


def test_open_existing_trusted_project_is_allowed_without_mutation_gate(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    api._workspace_required_for_task = True
    loop = AgentLoop(api, None)
    assert not loop._workspace_mutation_is_blocked("open_project_in_vscode", str(project))

    calls = []
    process_lists = iter(
        [
            [],
            [SimpleNamespace(info={"name": "Code.exe"})],
        ]
    )
    api._resolve_app_exe = lambda _name: "code"
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: calls.append(args))
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: next(process_lists))
    monkeypatch.setattr("core.api.time.sleep", lambda _seconds: None)

    assert Api._act_open_project_in_vscode(api, str(project))
    assert calls == [["code", "-n", str(project)]]

    untrusted = tmp_path / "Untrusted"
    untrusted.mkdir()
    assert not Api._act_open_project_in_vscode(api, str(untrusted))
    assert "REFUSED" in api.last_result_text


def test_agent_blocks_project_write_until_workspace_and_file_are_inspected(tmp_path):
    target = tmp_path / "package.json"
    target.write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    loop = AgentLoop(api, None)

    assert loop._workspace_mutation_is_blocked("write_file", f"{target}|||{{}}")
    api._workspace_required_for_task = False
    assert loop._existing_file_needs_inspection("write_file", f"{target}|||{{}}")
    assert Api._act_inspect_file(api, str(target))
    assert not loop._existing_file_needs_inspection("write_file", f"{target}|||{{}}")


def test_patch_json_preserves_package_scripts_and_dependencies(tmp_path):
    target = tmp_path / "package.json"
    target.write_text(
        json.dumps(
            {
                "scripts": {"dev": "vite"},
                "dependencies": {"animejs": "1.0.0"},
                "devDependencies": {"vite": "5.0.0"},
            }
        ),
        encoding="utf-8",
    )
    api = _api_with_workspace(tmp_path)

    assert Api._act_inspect_file(api, str(target))
    assert Api._act_patch_json(api, f'{target}|||{{"license":"MIT"}}')
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["license"] == "MIT"
    assert data["scripts"] == {"dev": "vite"}
    assert data["dependencies"] == {"animejs": "1.0.0"}
    assert data["devDependencies"] == {"vite": "5.0.0"}


def test_command_result_preserves_stdout_stderr_and_warning_status(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0], 0, "installed package\nwarning optional dependency", "stderr note"
        ),
    )

    assert Api._act_run_command(api, "yarn install animejs")
    result = api.last_command_result
    assert result["completed"] and result["success"] and result["has_warnings"]
    assert result["stdout"].startswith("installed package")
    assert result["stderr"] == "stderr note"
    assert "COMMAND COMPLETED WITH WARNINGS" in api.last_result_text
    assert "STDERR: stderr note" in api.last_result_text


def test_command_result_handles_nonzero_timeout_and_empty_output(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 7, "useful stdout", "failure"),
    )
    assert not Api._act_run_command(api, "failing-command")
    assert api.last_command_result["exit_code"] == 7
    assert api.last_command_result["stdout"] == "useful stdout"
    assert api.last_command_result["stderr"] == "failure"
    assert "COMMAND FAILED" in api.last_result_text

    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("slow-command", 15, output="partial", stderr="still running")
        ),
    )
    assert not Api._act_run_command(api, "slow-command")
    assert api.last_command_result["timed_out"]
    assert "COMMAND TIMED OUT" in api.last_result_text

    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 0, "", ""),
    )
    assert Api._act_run_command(api, "quiet-command")
    assert api.last_command_result["success"]
    assert "OUTPUT: (no output)" in api.last_result_text
