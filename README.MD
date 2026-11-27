# MistAI Desktop

MistAI is a voice and text assistant for Windows 10/11 (64-bit) that does
things on your computer: it opens and switches apps, types and clicks in
them, runs PowerShell commands, reads and edits files, and drives its own
browser. You type a request or press **Talk**, and a language model plans
the work one step at a time while MistAI carries out each step and checks
the result.

It is a personal project in active development, not a finished product.
Read [Limits](#limits) before trusting it with anything important.

## What it can do

- **Answer and chat.** Small talk and general questions get a direct spoken
  reply; anything about your computer or the web goes to the agent.
- **Use your apps.** Open or switch to an app, type into it, press keys,
  click controls through the Windows accessibility tree (UI Automation),
  with OCR and screen vision as fallbacks.
- **Run commands and handle files.** PowerShell commands, find files,
  inspect, write and patch files (an existing file is never replaced
  without asking).
- **Browse.** A separate Firefox profile driven through Playwright: search
  (DuckDuckGo), open pages, read their text, click and fill forms by the
  page's real structure.
- **Send messages.** `quick_message` opens a chat app's quick switcher,
  checks the recipient's name on screen twice, and asks before sending
  (configurable).
- **Longer tasks.** A goal with several deliverables is split into phases;
  progress is saved, so "Mist, resume <name>" continues after a stop or a
  crash.
- **Floating orb and task panels (optional).** A small always-on-top orb
  shows whether Mist is listening, thinking or speaking, and can be held to
  talk. It flies next to the window Mist is working in and points at what it
  is about to click; "Task panels" shows the steps and the answer in a spot
  chosen for each task, out of the way. Both are off by default.

## Limits

- **Windows only.** It depends on Win32 APIs, UI Automation and pywin32.
- **Needs the network.** Every model call goes to the MistAI backend
  (`mist-ai.fly.dev` / `api.mistai.org`); screen vision sends screenshots to
  Cohere; speech recognition uses Google's web API. Offline, it only says it
  can't reach the server.
- **It takes over the desktop while it works.** MistAI minimizes itself and
  types and clicks in the window in front. Don't use the mouse or keyboard
  during a task.
- **It can be wrong.** Vision can misread the screen, clicks can land on the
  wrong control, and the model can misunderstand. The safety gates below
  catch the irreversible cases; they don't make every step correct.
- **Sites that detect automation** (Google, YouTube) may reject its browser
  session; logging in once by hand in MistAI's browser window usually
  persists.
- Push-to-talk and the wake word ("Mist", "Hey Mist") depend on microphone
  quality and Google STT; the wake word is marked beta.

## Safety model

- **Confirmations.** Deleting, overwriting an existing file, a mutating
  shell command, sending a message, raw-coordinate clicks, clicks on
  controls such as Send/Delete/Buy, and pressing Enter outside a known safe
  app all stop and ask. The question appears with Yes/No buttons (and is
  spoken in voice mode). An unclear answer is asked once more, then the
  step is skipped: it fails closed.
- **Input goes only where intended.** Typing and keys go to the app the
  current phase opened or switched to; if that app is not in front, nothing
  is sent. Nothing is ever typed into a terminal or into MistAI itself.
- **Secrets.** Command output that touches `.env`, keys or certificates is
  withheld from the model; with the outbound scrub on (default) tokens and
  card numbers are redacted before anything leaves the machine. The guard
  is pattern-based and not airtight.
- **Untrusted text.** Text read from web pages, files, the screen and
  command output is marked as data; instruction-like wording in it gets an
  explicit note so the model does not follow it.
- **Logs are content-free** unless `MISTAI_VERBOSE_LOGS=1` (ignored in
  built apps): no typed text, window titles, paths or answers.

## Setup

```bash
pip install -r requirements.txt
python -m playwright install firefox
```

Create a `.env` file next to `assistant.py` with `MISTAI_API_KEY=...` and
`COHERE_API_KEY=...` (or enter the MistAI key in Settings). Then:

```bash
python assistant.py
```

Settings (the gear) holds the API key, "Add facts about me", "Ask me before
sending a message for you" (on by default) and browser sign-ins (Mist's own
browser; sign-ins can be copied from Firefox, or made once inside Mist's
browser). The sidebar holds the wake word, captions, the floating orb, task
panels and how Mist addresses you. The model is
always Nova, with Flux as the automatic fallback.

Release build (CI does this on a `v*` tag; output `dist/MistAI.exe`):

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = "0"; python -m PyInstaller MistAI.spec --clean --noconfirm
```

## How it works

```
UI (pywebview, ui/html.py) ──► Api.ask_mistai (core/api.py)
        │
        ├─ stop / resume / filler replies
        ├─ conversation router (core/planner.py): chat reply, or ...
        ▼
  PlanRunner (core/planner.py)      one phase for simple goals, else a small DAG
        ▼
  AgentLoop (core/agent.py)         per step: screen context ► model ► one action
        │                           (or a short batch of input steps) ► guards ► ...
        ▼
  Executor (core/executor.py)       run ► settle ► verify ► confidence ► fallback
        ▼
  Api._exec_sync ► _act_* handlers  apps, input, files, commands, browser, messages
```

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Contributor notes
and every setting: [CLAUDE.md](CLAUDE.md).

## Tests

```bash
python -m pytest -q                                   # ~1650 tests, no desktop or network
python -m compileall -q core ui tools assistant.py
python tools/live_checks.py --stub                    # end-to-end checks, scripted model
python tools/run_scenarios.py --stub --compare --yes  # scripted scenarios in three vision modes
```

The tests replace sleeps, threads, the backend and Windows calls with
fakes. `run_scenarios.py` and `live_checks.py` without `--stub` use the real
backend and the real desktop; run them only on a machine where that is
fine, with private windows closed. A run that fails only because the
backend or network was down is reported as INFRA, not FAIL.

## License

See [LICENSE](LICENSE).
