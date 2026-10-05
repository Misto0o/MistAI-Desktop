"""find_file search, result-limit audit, repeat-guard wording, exact process
matching, timing output, and the TTS network cooldown."""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from core import file_search, planner
from core.agent import AgentLoop, Timings
from core.api import Api
from core.config import TRACKED_PROCESS_NAMES, normalize_process_name, process_matches
from core.tools import SPECS


# -- file_search ---------------------------------------------------------------


def _tree(base, files):
    for rel in files:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


def test_search_skips_dependency_and_cache_trees_and_ranks_exact_first(tmp_path):
    _tree(
        tmp_path,
        [
            "proj/node_modules/jest-jasmine2/build/jest.environment.js",
            "proj/node_modules/pkg/.env",
            "proj/.git/.env",
            "proj/.venv/.env",
            "proj/__pycache__/.env",
            "proj/dist/.env",
            "proj/build/.env",
            "proj/.pytest_cache/.env",
            "proj/webpack-cache/.env",
            "proj/src/environment.ts",
            "proj/.env.example",
            "proj/deep/er/.env",
            "proj/.env",
        ],
    )
    matches, info = file_search.search(".env", [str(tmp_path / "proj")])
    paths = [p for _, p in matches]
    assert paths[0] == str(tmp_path / "proj" / ".env")
    assert matches[0][0] == file_search.RANK_EXACT
    assert str(tmp_path / "proj" / "deep" / "er" / ".env") in paths
    assert str(tmp_path / "proj" / ".env.example") in paths  # prefix, ranked after exact
    assert [r for r, _ in matches] == sorted(r for r, _ in matches)
    for bad in ("node_modules", ".git", ".venv", "__pycache__", "dist", "build", "cache"):
        assert not any(f"{os.sep}{bad}" in p for p in paths), bad
    # A dotfile query is not a substring search: no environment.ts noise.
    assert not any("environment" in p for p in paths)
    assert info["roots"] == [str(tmp_path / "proj")]


def test_search_caps_results_and_supports_wildcards(tmp_path):
    _tree(tmp_path, [f"docs/report{i}.pdf" for i in range(40)] + ["docs/notes.txt"])
    matches, info = file_search.search("*.pdf", [str(tmp_path)], max_results=25)
    assert len(matches) == 25 and info["total"] == 40
    assert all(p.endswith(".pdf") for _, p in matches)
    text = file_search.describe("*.pdf", matches, info)
    assert "40 match(es), showing best 25" in text


def test_search_respects_depth_limit_for_home(tmp_path):
    _tree(tmp_path, ["a/b/c/d/target.txt", "a/target.txt"])
    matches, _ = file_search.search(
        "target.txt", [str(tmp_path)], depth_limits={str(tmp_path): 2}
    )
    assert [p for _, p in matches] == [str(tmp_path / "a" / "target.txt")]


def test_ties_follow_root_order_and_home_sweep_skips_dot_folders(tmp_path):
    _tree(
        tmp_path,
        [
            "home/.vscode/extensions/x/package.json",
            "home/tools/package.json",
            "work/app/.github/package.json",
            "work/app/sub/dir/package.json",
        ],
    )
    roots = [str(tmp_path / "work" / "app"), str(tmp_path / "home")]
    matches, _ = file_search.search(
        "package.json", roots, depth_limits={str(tmp_path / "home"): 3}
    )
    paths = [p for _, p in matches]
    # Workspace root results come first even when a home path is shorter.
    assert paths[0].startswith(roots[0]) and paths[1].startswith(roots[0])
    assert str(tmp_path / "work" / "app" / ".github" / "package.json") in paths
    assert str(tmp_path / "home" / "tools" / "package.json") in paths
    assert not any(".vscode" in p for p in paths)


def _find_api(tmp_path, project_root=None):
    api = Api.__new__(Api)
    folders = {
        "{desktop}": str(tmp_path / "Desktop"),
        "{documents}": str(tmp_path / "Documents"),
        "{downloads}": str(tmp_path / "Downloads"),
    }
    for path in folders.values():
        os.makedirs(path, exist_ok=True)

    def resolve(p):
        for ph, real in folders.items():
            p = p.replace(ph, real)
        return p

    api._resolve_path_placeholders = resolve
    api.workspace = SimpleNamespace(project_root=project_root)
    api.last_result_text = ""
    return api


def test_find_file_searches_workspace_root_first_and_reports_roots(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    _tree(tmp_path, ["work/app/.env", "Desktop/other/.env"])
    api = _find_api(tmp_path, project_root=str(tmp_path / "work" / "app"))

    roots, depths = Api.find_file_roots(api)
    assert roots[0] == str(tmp_path / "work" / "app")
    assert roots[1:4] == [str(tmp_path / n) for n in ("Desktop", "Documents", "Downloads")]
    assert depths == {str(tmp_path / "home"): file_search.HOME_DEPTH}

    assert Api._act_find_file(api, ".env")
    text = api.last_result_text
    assert "searched: " + str(tmp_path / "work" / "app") in text
    assert text.index(str(tmp_path / "work" / "app" / ".env")) < text.index(
        str(tmp_path / "Desktop" / "other" / ".env")
    )


def test_find_file_with_a_folder_searches_only_that_folder(tmp_path):
    _tree(tmp_path, ["Desktop/proj/.env", "Documents/.env"])
    api = _find_api(tmp_path)
    assert Api._act_find_file(api, "{desktop}\\proj\\*.env*")
    assert "searched: " + str(tmp_path / "Desktop" / "proj") + "." in api.last_result_text
    assert "Documents" not in api.last_result_text
    assert Api._act_find_file(api, str(tmp_path / "Desktop" / "proj") + os.sep + ".env")
    assert "exists (exact path)" in api.last_result_text
    assert not Api._act_find_file(api, str(tmp_path / "nope") + os.sep + ".env")
    assert "folder not found" in api.last_result_text
    assert not Api._act_find_file(api, "")
    assert not Api._act_find_file(api, "proj\.env")
    assert "not an absolute folder" in api.last_result_text


def test_bare_filename_is_searched_not_resolved_against_the_cwd(tmp_path, monkeypatch):
    (tmp_path / "cwd").mkdir()
    (tmp_path / "cwd" / ".env").write_text("SECRET=1", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    api = _find_api(tmp_path)
    assert not Api._act_find_file(api, ".env")
    assert "exists (exact path)" not in api.last_result_text
    assert "NO MATCHES" in api.last_result_text


# -- result_chars audit ----------------------------------------------------------


def test_data_returning_tools_have_result_limits():
    assert SPECS["find_file"].result_chars == 4000
    assert SPECS["analyze_screen"].result_chars == 2000
    assert SPECS["recall"].result_chars == 1500
    assert SPECS["get_reminders"].result_chars == 1500
    assert SPECS["check_email"].result_chars == 1200
    # find_file no longer drives the UI, so it must not force a screen refresh.
    assert "info" in SPECS["find_file"].categories
    assert SPECS["find_file"].settle == 0.0


# -- Repeat-guard wording --------------------------------------------------------


def _advice(action):
    return AgentLoop(SimpleNamespace(tools=SimpleNamespace(get=lambda n: SPECS[n])), None)._repeat_advice(
        action
    )


def test_repeat_advice_depends_on_the_tool_kind():
    for action in ("run_command", "find_file", "inspect_file"):
        text = _advice(action)
        assert "identical call already ran" in text
        assert "different parameters, a narrower query, or an absolute path" in text
        assert "browser_get_text" not in text
    assert "browser_get_text" in _advice("browser_search")
    assert "already ran without error" in _advice("open_app")
    assert "did take effect" not in _advice("open_app")


def test_repeated_run_command_is_refused_with_search_advice(monkeypatch):
    replies = iter(
        [json.dumps({"action": "run_command", "parameter": "Get-Process"})] * 2
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    sent, calls = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: calls.append(a)
            or SimpleNamespace(
                success=True, detail="out", verified=True, escalate_to_user=False,
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
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert calls == ["run_command"]
    refusal = sent[-1][-2]["content"]
    assert "REFUSED" in refusal and "a narrower query" in refusal
    assert "browser_get_text" not in refusal


def test_search_result_rule_is_merged_into_the_find_file_entry():
    api = SimpleNamespace(
        workspace=SimpleNamespace(context_block=lambda: ""),
        _resolve_path_placeholders=lambda p: p,
    )
    system = AgentLoop(api, None)._build_system("none", {"open_apps": []}, "s", "", [])
    line = next(l for l in system.splitlines() if "- find_file" in l)
    assert "several results" in line and "checking its path" in line


# -- Exact process matching ------------------------------------------------------


@pytest.mark.parametrize(
    "app, proc, expected",
    [
        ("code", "Code.exe", True),
        ("code", "codex.exe", False),
        ("code", "codex-windows-sandbox-service.exe", False),
        ("CODE", "code", True),
        ("vscode", "Code.exe", True),
        ("Visual Studio Code", "Code.exe", True),
        ("edge", "msedge.exe", True),
        ("edge", "msedgewebview2.exe", False),
        ("obs studio", "obs64.exe", True),
        ("word", "WINWORD.EXE", True),
        ("notepad", "notepad++.exe", False),
        ("notepad", "notepad.exe", True),
        ("teams", "ms-teams.exe", True),
        ("settings", "SystemSettings.exe", True),
        ("calculator", "CalculatorApp.exe", True),
        ("discord", "Discord.exe", True),
    ],
)
def test_process_matching_is_exact_with_aliases(app, proc, expected):
    assert process_matches(app, proc) is expected


def test_is_app_running_does_not_confuse_code_with_codex(monkeypatch):
    api = Api.__new__(Api)
    procs = [
        SimpleNamespace(info={"name": "codex.exe", "pid": 10}),
        SimpleNamespace(info={"name": "codex-windows-sandbox-service.exe", "pid": 11}),
    ]
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: list(procs))
    assert Api.is_app_running(api, "code") is False
    procs.append(SimpleNamespace(info={"name": "Code.exe", "pid": 12}))
    assert Api.is_app_running(api, "code") is True
    assert Api.is_app_running(api, "Visual Studio Code") is True


def test_open_apps_list_tracks_exact_process_names(monkeypatch):
    from core import live_state

    assert normalize_process_name("codex") not in TRACKED_PROCESS_NAMES
    assert normalize_process_name("Code.exe") in TRACKED_PROCESS_NAMES
    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    monkeypatch.setattr(
        live_state.psutil,
        "process_iter",
        lambda _attrs: [
            SimpleNamespace(info={"name": n, "pid": i})
            for i, n in enumerate(
                ["codex.exe", "codex-windows-sandbox-service.exe", "Code.exe", "msedge.exe",
                 "msedgewebview2.exe", "obs64.exe", "notepad++.exe"],
                start=100,
            )
        ],
    )
    tracker._update()
    assert tracker._state["open_apps"] == ["code", "msedge", "obs64", "notepad++"]


# -- Timing ----------------------------------------------------------------------


def test_step_phase_and_planning_times_are_printed(monkeypatch, capsys):
    replies = iter(
        [json.dumps({"action": "open_app", "parameter": "notepad"}),
         json.dumps({"action": "done", "speech": "ok"})]
    )
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
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
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    loop.task_timing = Timings()
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    out = capsys.readouterr().out
    assert "[Timing] step 1: screen=" in out and "llm=" in out and "exec=" in out
    assert "[Timing] step 2: " in out
    assert "[Timing] Phase: " in out and "total" in out
    assert loop.task_timing.calls["llm"] == 2 and loop.task_timing.calls["exec"] == 1

    api.agent = loop
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: "not json")
    api.workspace = SimpleNamespace(context_block=lambda: "")
    planner.make_plan(api, "research things then write them down", "m")
    assert loop.task_timing.calls["plan"] == 1
    assert "[Timing] Planning LLM: " in capsys.readouterr().out


# -- TTS cooldown ------------------------------------------------------------------


def test_tts_skips_edge_tts_for_a_while_after_a_failure(monkeypatch):
    from core import tts

    monkeypatch.setattr(tts, "EDGE_TTS_AVAILABLE", True)
    engine = tts.NeuralTTS.__new__(tts.NeuralTTS)
    engine._cv = threading.Condition()
    engine._generation = 0
    engine._neural_down_until = 0.0
    neural_calls, fallback_calls = [], []

    def failing_neural(text, gen):
        neural_calls.append(text)
        raise OSError("self-signed certificate in certificate chain")

    engine._neural = failing_neural
    engine._fallback = lambda text, gen: fallback_calls.append(text)
    now = [1000.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])

    engine._speak_one(0, "one")
    engine._speak_one(0, "two")
    now[0] += tts.NeuralTTS.NEURAL_COOLDOWN - 1
    engine._speak_one(0, "three")
    assert neural_calls == ["one"]
    assert fallback_calls == ["one", "two", "three"]

    now[0] += 2  # cooldown over: the network is tried again
    engine._speak_one(0, "four")
    assert neural_calls == ["one", "four"]


@pytest.mark.parametrize(
    "app, proc",
    [
        ("edge", "msedgewebview2.exe"),
        ("edge", "MicrosoftEdgeUpdate.exe"),
        ("edge", "msedge_proxy.exe"),
        ("word", "wordpad.exe"),
        ("settings", "SystemSettingsBroker.exe"),
        ("settings", "SettingSyncHost.exe"),
        ("settings", "SettingsHandlerHost.exe"),
        ("teams", "WinTeamsUpdate.exe"),
        # Vendor-prefix stripping never turns another vendor's binary into a
        # known app: "wincode" is not VS Code, "mschrome" is not Chrome.
        ("code", "WinCode.exe"),
        ("chrome", "MsChrome.exe"),
        ("discord", "WindowsDiscord.exe"),
        ("notepad", "WinNotepad.exe"),
    ],
)
def test_process_matching_negatives(app, proc):
    assert process_matches(app, proc) is False


def test_no_tracked_apps_process_matches_a_different_tracked_app():
    from core.config import TRACKED_APPS, app_process_names

    collisions = [
        (a, p, b)
        for a in TRACKED_APPS
        for p in app_process_names(a)
        for b in TRACKED_APPS
        if not (app_process_names(a) & app_process_names(b)) and process_matches(b, p)
    ]
    assert collisions == []


def test_built_in_apps_still_match_through_vendor_prefixes():
    assert process_matches("settings", "SystemSettings.exe")
    assert process_matches("store", "WinStore.App.exe")
    assert process_matches("photos", "Microsoft.Photos.exe")
    assert process_matches("camera", "WindowsCamera.exe")


# -- Vision gating -----------------------------------------------------------------


def _gated_run(monkeypatch, script, gating, on_exec=None):
    """Run the loop over scripted actions; returns the number of screen reads."""
    monkeypatch.setenv("MISTAI_VISION_GATING", "1" if gating else "0")
    replies = iter(
        [json.dumps({"action": a, "parameter": p}) for a, p in script]
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    snap = {"open_apps": [], "foreground_hwnd": 1, "foreground_window": "Editor"}

    def run(action, param):
        if on_exec:
            on_exec(action, snap)
        return SimpleNamespace(
            success=True, detail="", verified=True, escalate_to_user=False,
            reliability_note=lambda: "",
        )

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snap),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    reads = []
    monkeypatch.setattr(loop, "_get_screen_context", lambda: reads.append(1) or "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    assert loop._loop("t", "m", "none", "text", None)["success"]
    return len(reads), loop


NON_UI = [("run_command", "Get-Process"), ("find_file", "a.txt"), ("write_file", "x|||y")]


def test_gating_off_keeps_the_old_refresh_after_system_and_file_actions(monkeypatch, capsys):
    reads, _ = _gated_run(monkeypatch, NON_UI, gating=False)
    # run_command and write_file (system/file) each force a re-read.
    assert reads == 3
    assert "[Agent] Vision gating: off" in capsys.readouterr().out


def test_gating_on_skips_reads_after_non_ui_and_browser_dom_actions(monkeypatch, capsys):
    script = NON_UI + [("browser_click", "Subscribe"), ("browser_get_text", "")]
    reads, _ = _gated_run(monkeypatch, script, gating=True)
    assert reads == 1
    assert "[Agent] Vision gating: on" in capsys.readouterr().out


def test_gating_on_always_rereads_after_ui_changing_actions(monkeypatch):
    # Same foreground window key throughout: UI actions still invalidate.
    script = [("open_app", "notepad"), ("type_text", "hi"), ("press_key", "enter"),
              ("window_maximize", "Notepad"), ("click_on_text", "File"), ("open_url", "x.com")]
    reads, _ = _gated_run(monkeypatch, script, gating=True)
    assert reads == 1 + len(script)


def test_gating_on_rereads_when_the_foreground_window_changes(monkeypatch):
    def switch_window(action, snap):
        if action == "run_command":
            snap["foreground_hwnd"], snap["foreground_window"] = 2, "Notepad"

    reads, _ = _gated_run(monkeypatch, NON_UI, gating=True, on_exec=switch_window)
    assert reads == 2


def test_gating_on_rereads_after_the_cache_expires(monkeypatch):
    monkeypatch.setattr(AgentLoop, "GATED_CACHE_SECONDS", -1.0)
    reads, _ = _gated_run(monkeypatch, NON_UI, gating=True)
    assert reads == 1 + len(NON_UI)
