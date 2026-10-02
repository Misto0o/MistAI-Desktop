"""Result-size limits, READ-result history shrinking, secret guards, and
known-folder placeholders. No desktop, network, or real shell is used."""

import json
import subprocess
import threading
from types import SimpleNamespace

import pytest

from core.agent import AgentLoop
from core.api import Api
from core.config import is_secret_file, secret_file_in, secret_output_reason
from core.tools import SPECS, ToolRegistry
from core.workspace import Workspace


class _Bus:
    def subscribe(self, *_args):
        return lambda: None

    def publish(self, *_args):
        pass


def _api_with_workspace():
    api = Api.__new__(Api)
    api.workspace = Workspace(_Bus())
    api.last_result_text = ""
    api.last_command_result = None
    api._workspace_required_for_task = False
    api._trusted_project_roots = set()
    api._resolve_path_placeholders = lambda path: str(path)
    return api


# -- ToolSpec.result_chars ---------------------------------------------------


def test_read_tools_have_large_result_limits_and_others_keep_default():
    for name in (
        "run_command",
        "browser_get_text",
        "inspect_file",
        "read_screen_text",
        "get_clipboard",
        "window_list",
        "list_apps",
    ):
        assert SPECS[name].result_chars == 4000, name
    assert SPECS["browser_navigate"].result_chars == 1200
    assert SPECS["browser_search"].result_chars == 1200
    assert SPECS["open_app"].result_chars == 300


# -- AgentLoop truncation and READ shrinking ---------------------------------


class _Response:
    ok = True
    status_code = 200

    def __init__(self, response):
        self._response = response

    def json(self):
        return {"response": self._response}


def _loop_with_outputs(monkeypatch, steps):
    """steps: list of (action, parameter, detail). Returns the messages of
    every model request, in order."""
    details = {(a, p): d for a, p, d in steps}
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(
            snapshot={"foreground_window": "x", "open_apps": [], "screen_resolution": (1, 1)}
        ),
        api_manager=SimpleNamespace(
            get_headers=lambda: {},
            get_api_url=lambda: "https://example.invalid/a",
            get_fallback_url=lambda: "https://example.invalid/b",
        ),
        tools=SimpleNamespace(
            specs=SPECS,
            get=lambda name: SPECS[name],
            requires_confirmation=lambda *a, **k: False,
        ),
        executor=SimpleNamespace(
            run=lambda action, param: SimpleNamespace(
                success=True,
                detail=details[(action, param)],
                verified=True,
                escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    sent = []
    replies = iter(
        [json.dumps({"action": a, "parameter": p}) for a, p, _ in steps]
        + [json.dumps({"action": "done", "speech": "ok"})]
    )

    def fake_post(*_args, json=None, **_kwargs):
        sent.append([dict(m) for m in json["messages"]])
        return _Response(next(replies))

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    result = loop._loop("task", "m", "none", "text", None)
    assert result["success"]
    return sent


def _observations(messages):
    return [m["content"] for m in messages if m["content"].startswith("[OBSERVATION]")]


def test_result_is_cut_to_the_tool_limit_with_a_truncation_marker(monkeypatch):
    sent = _loop_with_outputs(
        monkeypatch,
        [("run_command", "big", "R" * 5000), ("open_app", "notepad", "N" * 500)],
    )
    obs = _observations(sent[-1])
    assert "R" * 4000 + " [truncated, 1000 more chars]" in obs[0]
    assert "R" * 4001 not in obs[0]
    assert "N" * 300 + " [truncated, 200 more chars]" in obs[1]


def test_only_the_two_newest_read_results_stay_full(monkeypatch):
    sent = _loop_with_outputs(
        monkeypatch,
        [
            ("run_command", "a", "A" * 5000),
            ("inspect_file", "b", "B" * 5000),
            # 1200-char navigate excerpts are not READ-type and must not
            # evict a real read from the full window.
            ("browser_navigate", "c", "C" * 5000),
            ("run_command", "d", "D" * 5000),
        ],
    )
    obs = _observations(sent[-1])
    assert "A" * 300 + " [truncated, 4700 more chars]" in obs[0]
    assert "A" * 301 not in obs[0]
    assert "B" * 4000 + " [truncated, 1000 more chars]" in obs[1]
    assert "C" * 1200 + " [truncated, 3800 more chars]" in obs[2]
    assert "D" * 4000 + " [truncated, 1000 more chars]" in obs[3]
    # Before the third read arrived, the first was still full.
    assert "A" * 4000 in _observations(sent[3])[0]


# -- Secret-file / env guard helpers -----------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        r"C:\proj\.env.local",
        "server.pem",
        "tls.KEY",
        "cert.pfx",
        "cert.p12",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ecdsa",
        "release.keystore",
        "credentials.json",
    ],
)
def test_secret_file_patterns_match(name):
    assert is_secret_file(name)


@pytest.mark.parametrize("name", ["notes.txt", "env.py", "package.json", "keys.md"])
def test_ordinary_files_are_not_secret(name):
    assert not is_secret_file(name)


@pytest.mark.parametrize(
    "command, needle",
    [
        ("Get-Content .env", "secret-pattern file"),
        ("type 'C:\\Users\\me\\.ssh\\id_rsa'", "secret-pattern file"),
        ("Get-ChildItem Env:", "dumps environment"),
        ("dir env:", "dumps environment"),
        ("gci env:\\ | Format-List", "dumps environment"),
        ("printenv", "dumps environment"),
        ("cmd /c set", "dumps environment"),
        ("set", "dumps environment"),
        ("[Environment]::GetEnvironmentVariables()", "dumps environment"),
        ("echo $env:OPENAI_API_KEY", "OPENAI_API_KEY"),
        ("Write-Output ${env:GITHUB_TOKEN}", "GITHUB_TOKEN"),
        ("cmd /c echo %DB_PASSWORD%", "DB_PASSWORD"),
        ("[Environment]::GetEnvironmentVariable('MY_SECRET')", "MY_SECRET"),
    ],
)
def test_secret_commands_are_withheld(command, needle):
    reason = secret_output_reason(command, "")
    assert reason and needle in reason


@pytest.mark.parametrize(
    "command",
    [
        "Set-Content -Path notes.txt -Value hi",
        "Get-Content notes.txt",
        "Get-ChildItem $env:TEMP",
        "Test-Path $env:USERPROFILE\\Downloads",
        "Get-Process | Select-Object -First 5",
        "Get-ChildItem C:\\proj -Filter *.py",
    ],
)
def test_ordinary_commands_are_not_withheld(command):
    assert secret_output_reason(command, "plain output") is None
    assert secret_file_in(command) is None


def test_private_key_in_output_is_withheld():
    out = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    assert "private key" in secret_output_reason("Get-Content notes.txt", out)


# -- run_command withholding -------------------------------------------------


def _fake_run(monkeypatch, code, stdout, stderr=""):
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], code, stdout, stderr),
    )


def test_run_command_withholds_secret_output_but_keeps_exit_status(monkeypatch):
    api = _api_with_workspace()
    _fake_run(monkeypatch, 0, "API_KEY=sk-live-123", "")
    assert Api._act_run_command(api, "Get-Content .env")
    assert "sk-live-123" not in api.last_result_text
    assert "OUTPUT WITHHELD" in api.last_result_text
    assert "exit code 0" in api.last_result_text
    assert api.last_command_result["stdout"] == ""
    assert api.last_command_result["exit_code"] == 0

    _fake_run(monkeypatch, 1, "", "TOKEN=abc")
    assert not Api._act_run_command(api, "gci env:")
    assert "abc" not in api.last_result_text
    assert "COMMAND FAILED (exit code 1)" in api.last_result_text
    assert api.last_command_result["stderr"] == ""


def test_run_command_withholds_private_key_output(monkeypatch):
    api = _api_with_workspace()
    _fake_run(monkeypatch, 0, "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n")
    assert Api._act_run_command(api, "Get-Content notes.txt")
    assert "MIIE" not in api.last_result_text


def test_run_command_withholds_on_timeout_too(monkeypatch):
    api = _api_with_workspace()

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired("x", 15, output="SECRET=1", stderr="")

    monkeypatch.setattr("core.api.subprocess.run", boom)
    assert not Api._act_run_command(api, "Get-Content .env.production")
    assert "SECRET=1" not in api.last_result_text
    assert "COMMAND TIMED OUT" in api.last_result_text


@pytest.mark.parametrize(
    "command",
    ["Set-Content x.txt hi", "Get-Content notes.txt", "echo $env:TEMP", "echo $env:USERPROFILE"],
)
def test_run_command_does_not_withhold_ordinary_output(monkeypatch, command):
    api = _api_with_workspace()
    _fake_run(monkeypatch, 0, "visible output")
    assert Api._act_run_command(api, command)
    assert "STDOUT: visible output" in api.last_result_text
    assert "WITHHELD" not in api.last_result_text


def test_run_command_keeps_up_to_the_memory_bound(monkeypatch):
    api = _api_with_workspace()
    _fake_run(monkeypatch, 0, "x" * 30000)
    Api._act_run_command(api, "Get-Content big.txt")
    assert len(api.last_result_text) == 20000


# -- inspect_file ------------------------------------------------------------


def test_inspect_file_returns_content_up_to_20000_chars(tmp_path):
    target = tmp_path / "big.txt"
    target.write_text("y" * 25000, encoding="utf-8")
    api = _api_with_workspace()
    assert Api._act_inspect_file(api, str(target))
    assert "CONTENT:\n" + "y" * 20000 in api.last_result_text
    assert "y" * 20001 not in api.last_result_text
    assert "content cut at 20000" in api.last_result_text
    # The stored snapshot keeps the 50k cache rule.
    assert len(api.workspace.file_snapshots[str(target)]["content"]) == 25000


@pytest.mark.parametrize(
    "raw", [b"abc\x00def", b"\xff\xfe\xfa not utf8"], ids=["nul", "invalid-utf8"]
)
def test_inspect_file_shows_no_content_for_binary(tmp_path, raw):
    target = tmp_path / "blob.bin"
    target.write_bytes(raw)
    api = _api_with_workspace()
    assert Api._act_inspect_file(api, str(target))
    assert "Binary or non-UTF-8" in api.last_result_text
    assert "def" not in api.last_result_text and "utf8" not in api.last_result_text
    assert api.workspace.file_snapshots[str(target)]["content"] is None


def test_inspected_secret_file_never_exposes_content_and_keeps_overwrite_gate(tmp_path):
    target = tmp_path / ".env"
    target.write_text("MISTAI_API_KEY=sk-very-secret\n", encoding="utf-8")
    api = _api_with_workspace()
    seen = []

    # Not inspected yet: the normal overwrite protection refuses.
    assert not Api._act_write_file(api, f"{target}|||NEW=1")
    seen.append(api.last_result_text)
    assert "REFUSED" in api.last_result_text
    assert "sk-very-secret" in target.read_text(encoding="utf-8")

    assert Api._act_inspect_file(api, str(target))
    seen.append(api.last_result_text)
    assert "CONTENT WITHHELD" in api.last_result_text
    snapshot = api.workspace.file_snapshots[str(target)]
    assert snapshot["content"] is None and snapshot["sha256"]
    # Size and hash are registered with the Workspace, content is not.
    info = api.workspace.known_files[str(target)]
    assert info.size == target.stat().st_size and info.content is None
    assert "sk-very-secret" not in api.workspace.context_block()

    # Replacing it still needs confirmation like any existing file.
    registry = ToolRegistry(db_path=str(tmp_path / "stats.db"))
    try:
        assert registry.requires_confirmation("write_file", f"{target}|||NEW=1")
    finally:
        if registry._conn:
            registry._conn.close()
    loop = AgentLoop(api, None)
    assert not loop._existing_file_needs_inspection("write_file", f"{target}|||NEW=1")
    seen.append(loop._confirmation_question("write_file", f"{target}|||NEW=1"))

    assert Api._act_write_file(api, f"{target}|||NEW=1")
    seen.append(api.last_result_text)
    assert target.read_text(encoding="utf-8") == "NEW=1"
    assert not any("sk-very-secret" in text for text in seen)


# -- {downloads} placeholder -------------------------------------------------


def test_downloads_and_documents_use_shell_folders_with_home_fallback(monkeypatch, tmp_path):
    api = Api.__new__(Api)
    api._get_desktop_path = lambda: str(tmp_path / "Desktop")
    looked_up = []

    def fake_shell_folder(value_name, fallback):
        looked_up.append(value_name)
        return str(tmp_path / fallback)

    monkeypatch.setattr(Api, "_shell_folder", staticmethod(fake_shell_folder))
    assert Api._resolve_path_placeholders(api, "{downloads}\\a.zip") == str(
        tmp_path / "Downloads"
    ) + "\\a.zip"
    assert Api._resolve_path_placeholders(api, "{documents}") == str(tmp_path / "Documents")
    assert looked_up == ["{374DE290-123F-4565-9164-39C4925E467B}", "Personal"]


def test_shell_folder_falls_back_to_home_subfolder(monkeypatch, tmp_path):
    import winreg

    def missing(*_a, **_k):
        raise OSError("no key")

    monkeypatch.setattr(winreg, "OpenKey", missing)
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path))
    assert Api._shell_folder("Personal", "Downloads") == str(tmp_path / "Downloads")


def test_system_prompt_lists_known_folders_and_path_rule():
    api = SimpleNamespace(
        workspace=SimpleNamespace(context_block=lambda: ""),
        _resolve_path_placeholders=lambda p: {"{downloads}": "D:\\Dl"}.get(p, p),
    )
    system = AgentLoop(api, None)._build_system(
        "none", {"open_apps": []}, "screen", "", []
    )
    assert "Known folders:" in system and "Downloads=D:\\Dl" in system
    assert "{downloads}" in system
    assert "Never invent environment variables" in system
    assert system.count("brightness_get    :") == 1
