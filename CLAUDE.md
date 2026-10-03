# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

MistAI is a Windows-only (10/11, 64-bit) voice/text desktop assistant: a pywebview UI over a Python agent that controls the desktop (pywin32/pywinauto/UIA), a Playwright Firefox browser, and the shell. It depends on Win32 APIs, so there is no macOS/Linux path.

## Rules

- **Design philosophy:** MistAI is a general reasoning agent, not a collection of hardcoded per-task functions. Don't add task-specific actions or shortcuts to fix bad behavior. Improve planning, prompting, context, and model choice instead.
- **Never disable or bypass SSL/TLS verification.** If a TLS error appears, report it.
- **Never print, log, or send secret values** (`.env` keys, cookies, tokens), not even in tool output. Report names or lengths only.
- **Don't `git commit` or push unless asked.** Don't delete files; move candidates to a quarantine folder outside the repo.
- **Don't hardcode drive letters or user paths.** The repo may live on `E:\` or `D:\`.
- **Stub mode by default.** Real-backend or live-desktop runs only when explicitly asked (see Harness).
- **Keep the system prompt lean.** Merge new rules into existing ones and tighten wording instead of only appending.

## Commands

```bash
pip install -r requirements.txt
python -m playwright install firefox
python assistant.py                      # run the app (entry point is assistant.py)

python -m pytest -q                      # all tests (what CI runs; ~259 tests)
python -m pytest tests/test_planner_and_tools.py::test_name -q   # single test

python -m compileall -q core ui tools assistant.py               # CI sanity check
python -c "import core.agent, core.api, core.executor, core.planner"

# Release build (CI does this on a v* tag; output is dist/MistAI.exe)
$env:PLAYWRIGHT_BROWSERS_PATH = "0"; python -m PyInstaller MistAI.spec --clean --noconfirm
```

- `MistAI.spec` requires `MistAI_OCREngine/Tesseract-OCR/` (bundled Tesseract) and bundles Playwright's Firefox, so set `PLAYWRIGHT_BROWSERS_PATH=0` before `playwright install` too.
- Secrets live in `.env` next to `assistant.py`: `MISTAI_API_KEY`, `COHERE_API_KEY`. `MISTAI_USE_BACKEND` toggles backend mode. `.env` is gitignored.
- `tests/conftest.py` puts the repo root on `sys.path`. Tests monkeypatch `time.sleep`, `threading.Thread`, and similar, so no real desktop or network is used.
- `MISTAI_DB_PATH` overrides the SQLite path for memory, tool stats, task store, and task memory (used by the harness so it never touches the real database).

## Architecture

**Entry and UI.** `assistant.py` creates `core.api.Api` and passes it to pywebview as `js_api`. The UI is one HTML string in `ui/html.py` that calls `Api` methods from JS. `Api` (`core/api.py`, very large) wires every subsystem together: wake-word loop (Google STT), TTS, and all `_act_*` action handlers.

**LLM backend.** All LLM calls go over HTTP to MistAI's own backend (`mist-ai.fly.dev` / `api.mistai.org`), not a provider SDK. `core/api_manager.py` picks the URL and fallback. `core/config.py` holds `AGENT_MODEL`, the single place the model is chosen. Vision uses Cohere directly (`core/vision.py`).

**Request flow:** `Api.ask_mistai` → `PlanRunner` (`core/planner.py`) → `AgentLoop` (`core/agent.py`) → `Executor` (`core/executor.py`) → `Api._exec_sync` → `_act_*` handler.
- `make_plan` sends simple goals (and questions) straight to the agent with no planning call. Other goals become a small DAG of phases (`PlanNode`). A failed phase blocks only its descendants. Progress persists via `core/task_store.py`, so tasks resume by name ("Mist, resume X").
- `AgentLoop._loop` runs one phase step by step. The model returns one JSON object per step (`{"action", "parameter", "speech", "thinking"}`) parsed by `_parse_cmd` (malformed JSON → `parse_retry`). `_build_system` builds the prompt: action menu, `WORKSPACE` context, screen context, known folders, recent conversation.
- `Executor.run` is the single execution path: it reroutes UIA actions to OCR when a browser has focus, calls `_exec_sync`, waits the tool's settle time, runs the ground-truth verifier, scores confidence, and escalates to `fallback_tools`. Handlers report detail text via `api.last_result_text`.

**Adding or changing an action touches four places:**
1. `core/tools.py` `SPECS`: a `_T(...)` `ToolSpec` (settle, confidence, destructive/confirmation flags, fallbacks, `verifier`, and `result_chars`).
2. `core/api.py` `_exec_sync`: the `elif action == ...` dispatch to an `_act_*` method.
3. `core/verify.py` `VERIFIERS`: optional ground-truth check.
4. `core/agent.py` `_build_system`: the action's description in the prompt menu, the only way the model learns about it.

**Safety gates.** `ToolRegistry.is_destructive` / `requires_confirmation` and `command_is_mutating` decide when `AgentLoop` must ask the user (`ask_user`). Regression tests pin this: run them after touching the agent, tools, or wake-word matching.

**Browser.** `core/browser_session.py` drives a dedicated persistent Playwright Firefox profile (`~/MistAI/browser_profile_from_daily`, seeded from the user's Firefox on first launch) through the DOM (`browser_*`). OCR/UIA clicking (`core/click_handler.py`, `core/uia_nav.py`) is for native apps and the last resort inside browsers.

**Background state.** `core/events.py` is an event bus with one dispatch thread (subscribers must be fast). `core/watchers.py` bridges `LiveState` (foreground window, clipboard, processes) and file changes into events. Persistent data lives in `~/MistAI/memory.db`.

## Agent behavior (what the tests pin)

- **Result sizes.** The agent cuts each observation to its tool's `result_chars` (default 300; 4000 for read tools such as `run_command`, `browser_get_text`, `inspect_file`, `find_file`) and appends `[truncated, N more chars]`. Only the two most recent READ results (`result_chars >= 4000`) stay in full; older ones shrink to 300 before the history is resent.
- **Screen context.** `MISTAI_VISION_GATING` (default **on**; `0` disables): the screen is re-read only after UI-changing actions, when the foreground window changes, or after 30s. `MISTAI_LAZY_VISION` (default **off**): `1` = first read of a phase is title + Open apps + a UIA summary capped at 1.5s; `2` = title + Open apps only. The model can call `analyze_screen` / `read_screen_text` when it needs pixels; those answer only from what is visible or reply `NOT VISIBLE ON SCREEN`.
- **Grounding rules** live in `_build_system` and `planner._PLAN_PROMPT`, pinned by `tests/test_grounding_rules.py`: facts about the machine come from the Open apps list or commands (`Get-Process`, `Test-Path`), never from screen reads; "done" means the question was answered; never state unseen data or write placeholder content; if the task was done by a different route than asked, say so; the planner plans only what remains and delivers only what was asked (a question's deliverable is the spoken answer).
- **Secret guard** (`core/config.py`: `SECRET_FILE_PATTERNS`, `secret_file_in`, `secret_output_reason`). `run_command` still runs, but its output is withheld (exit status kept) if the command references a secret-pattern file (`.env*`, `*.pem`, `*.key`, `id_rsa*`, ...), dumps or references secret-named env vars, or the output contains a private key block. `inspect_file` shows only name/size/hash for secret files and no content for binary files. The guard is not airtight: wildcards, piped listings, and runtime-built names get past it. `run_command` exit code 1 with no output is reported as "no results" (success), not a failure. Other nonzero exits stay failures.
- **Process matching.** Use `process_matches` / `is_self_process` (`core/config.py`), never substring checks. MistAI's own `msedgewebview2` and its own PID are excluded from app lists and window lookups.
- **Windows and input.** `_window_belongs_to` (`core/api.py`) handles Store/UWP apps, whose windows belong to `ApplicationFrameHost`. `type_text` / `press_key` / `hotkey` report which window received input and return a `WARNING` if the intended app isn't in front.
- **Paths.** Use `{desktop}`, `{documents}`, `{downloads}`, `{home}` placeholders or the Known folders line in the prompt; Desktop and Documents may be OneDrive-redirected.
- **`find_file`** is a real filesystem search (`core/file_search.py`): workspace root, Desktop, Documents, Downloads, then home; skips `node_modules`, `.git`, venvs, build/cache folders; exact-name matches rank first.

## Scenario harness

`tools/run_scenarios.py` + `tools/scenarios.json` run scripted scenarios through the real `Api.ask_mistai`.

```bash
python tools/run_scenarios.py --stub --compare --yes     # safe: scripted LLM, no network, vision off
python tools/run_scenarios.py --compare --allow-ui-input --yes   # REAL run (see below)
```

- Modes: `baseline`, `gating`, `gating+lazy2` (`--compare` runs these three; `--mode gating+lazy1` still works; `--repeat N` repeats runs and adds a min/median/max timing table). Plan-only scenarios call `make_plan` and assert the node list.
- A **real** run uses the real backend and sends screenshots to Cohere, and `--allow-ui-input` types into windows. Close Notepad, Calculator, and anything private first, and don't touch mouse or keyboard while it runs. Only run it when the user explicitly asks.
- Notepad scenarios skip themselves if Windows 11 Notepad restores leftover tabs. The harness never deletes Notepad state and removes only its own `%TEMP%\mist_scn_*` folders and processes it started.
- Output goes to `~/MistAI/logs/scenarios-<timestamp>/` (`summary.txt`, `summary.json`, one log per run). Logs contain screen descriptions: skim before sharing.

## Gotchas

- `handtracking/` was moved out of the repo (separate MediaPipe experiment). The app doesn't use it.
- Tesseract (`MistAI_OCREngine/`, ~83 MB) is tracked on purpose because `MistAI.spec` bundles it; moving it to a CI download step is a separate task.
- Windows 11 Notepad restores unsaved tabs from its `TabState` folder; a "fresh" Notepad may not be empty.
- The old docs website (`docs.html` etc.) was removed; docs live on the main MistAI web site.