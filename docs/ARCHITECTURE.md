# MistAI architecture

For someone reading the code for the first time. CLAUDE.md has the full list
of settings and the rules the tests pin; this file explains how the pieces
fit.

## Processes and threads

One Python process. `assistant.py` builds `core.api.Api` and hands it to
pywebview as the page's `js_api`, so every public `Api` method is callable
from `ui/html.py`'s JavaScript. Python talks back to the page with
`Api._notify_js("handleX(...)")`.

Long work never runs on the UI thread: the agent runs on its own thread
(`AgentLoop.run_project`), the wake-word and push-to-talk listeners have
theirs, the event bus (`core/events.py`) has one dispatch thread, and the
floating orb has a 4 Hz control thread plus a per-flight animation thread.
`Api._agent_lock` is the run gate: one task at a time; a second request is
answered "I'm still working on ..." instead of queued.

## A request, end to end

1. **`Api.ask_mistai(message)`** (core/api.py). Handles a typed "stop",
   "resume <name>" and fixed filler replies ("thanks") without any model
   call.
2. **Router** (`core/conversation.py` `try_chat`): one small model call
   returns `{"route": "chat"|"task", "reply"}`. Chat replies are answered
   at once. Anything doubtful, failed or slow (`MISTAI_CHAT_TIMEOUT`,
   default 5 s) goes to the agent; the log line says why
   (`route=task in 5.01s (timeout)`).
3. **Run gate, minimize, plan.** `PlanRunner` (`core/planner.py`) runs
   simple goals (short commands, questions, "go to X and read Y") as one
   phase with no planning call; other goals get one planning call that
   returns a small DAG of phases. A failed phase blocks only the phases
   that depend on it; progress is saved in `core/task_store.py`.
4. **`AgentLoop._loop`** (`core/agent.py`) runs one phase, at most 15
   steps. Each step:
   - screen context: by default only the window title and the Open apps
     list on the first read (`MISTAI_LAZY_VISION=2`), the window's own
     accessibility text when it is enough (`MISTAI_UIA_FIRST`), vision only
     when needed, and re-read only after something changed
     (`MISTAI_VISION_GATING`);
   - one model call with the system prompt (`_build_system`) and the
     history; the reply is one action, or 2-4 predictable input steps
     (`"steps"`, `MISTAI_BATCH_STEPS`) that then run without further model
     calls;
   - guards, in order: valid action name, project-workspace rules, repeated
     failures, repeated identical input, repeated success, a declined
     action, then the confirmation gate;
   - `Executor.run`, then an observation appended to the history (result
     text cut to the tool's `result_chars`, notes for unsubmitted typing,
     credentials, instruction-like text, contradictions).
   - A `done` is checked once: if its answer states numbers the last read
     doesn't show, follows unsubmitted typing, has no answer to a question,
     or input went astray, the model gets one reminder.
5. **`Executor.run`** (`core/executor.py`): reroutes UIA actions to OCR in
   browsers, calls `Api._exec_sync`, waits the tool's settle time, runs the
   ground-truth verifier (`core/verify.py`), scores confidence, and falls
   back to the tool's `fallback_tools` when confidence is low.
6. **`Api._exec_sync`** dispatches to an `_act_*` handler, which reports its
   detail in `api.last_result_text`.

## Where the code lives

| area | files |
|---|---|
| UI pages (main, floating orb, panels) | `ui/html.py` |
| glue, handlers, voice, settings | `core/api.py` + mixins `core/api_browser.py`, `core/api_system.py`, `core/api_windows.py` |
| router / planner / loop / execution | `core/conversation.py`, `core/planner.py`, `core/agent.py`, `core/executor.py` |
| tool metadata and the confirmation rule | `core/tools.py` (`SPECS`, `requires_confirmation`) |
| verification | `core/verify.py` |
| screen | `core/screen.py` (frame buffer), `core/vision.py` (Cohere), `core/uia_text.py`, `core/uia_nav.py`, `core/click_handler.py` (OCR/UIA/vision clicks) |
| browser | `core/browser_session.py` (Playwright Firefox, own profile) |
| live state and events | `core/live_state.py`, `core/watchers.py`, `core/events.py` |
| storage | `core/memory.py`, `core/task_store.py`, `core/task_memory.py`, `core/tools.py` stats (SQLite in `~/MistAI/memory.db`, or `MISTAI_DB_PATH`) |
| settings and pure helpers | `core/config.py` |
| floating orb and panels | `core/floating_orb.py`, `core/orb_panels.py`, `core/orb_layout.py` (geometry), `core/orb_motion.py` (spring motion) |

The `Api` mixins are a plain split by topic: methods moved verbatim, `Api`
inherits them. Methods that read a module-level name tests patch on
`core.api` (`wake_clips_enabled`, `recipient_match`, `_show_window_async`,
`HEARTBEAT_SECONDS`), or constants defined in api.py, stay in api.py;
`tests/test_api_structure.py` pins that.

## Adding an action

Three places: a `ToolSpec` in `core/tools.py` `SPECS` (settle time,
confidence, destructive/confirmation flags, fallbacks, verifier,
`result_chars`) plus its `_PLAYBOOK` entry (one-line purpose, exact parameter
format, limits, example), from which the prompt's action menu is generated;
a dispatch line in `Api._exec_sync`; optionally a verifier in
`core/verify.py`. The model sees one line per action and can call
`describe_action` for the full entry. `tests/test_action_playbook.py` keeps
the registry, the dispatch and the menu in sync.

## Safety gates

All in the loop, before `Executor.run`:
`ToolRegistry.requires_confirmation` decides (destructive specs, mutating
commands, overwriting files, dangerous click text, raw-coordinate clicks,
Enter unless `Api.enter_target_is_safe()`), and `Api.ask_confirmation` asks
with Yes/No buttons; unclear twice = the phase ends. `quick_message` has its
own setting (`ask_before_sending`: always / when unsure / never) and checks
the recipient on screen before typing and again right before Enter. Input
handlers refuse to type when the intended app is not in front. Batched
steps go through every one of these gates individually.

## Tests and harnesses

- `python -m pytest -q`: unit and integration tests; Windows calls, sleeps,
  threads and the backend are faked. UI pages are executed in Node against a
  fake DOM where Node is installed.
- `tools/run_scenarios.py`: scripted scenarios through the real
  `Api.ask_mistai`; `--stub` scripts the model and blocks the network;
  `--compare` runs each in three vision modes. Backend/network failures are
  reported as INFRA.
- `tools/live_checks.py`: safety and behaviour checks (`--stub` by default;
  `--live` on a real desktop).
