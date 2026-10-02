# ☁️ MistAI Desktop Assistant — V2

MistAI is a JARVIS-style AI assistant that runs directly on your computer.
It listens for a wake word, holds a real back-and-forth conversation, sees
and reads your screen, controls your desktop and browser through real
structure (not screenshots), and executes system commands directly — all
through one continuous, self-verifying agentic loop.

🚀 **Current Version:** V2
💻 **Platform:** Windows (10/11, 64-bit) — see [Platform Support](#-platform-support) below
🎤 **Wake Word + Continuous Conversation**
👁️ **Vision-Based Screen Understanding**
🧠 **Event-Driven Core with Resumable, Multi-Phase Tasks**
🌐 **Real Browser Automation (DOM-based, not pixel-guessing)**
✅ **Every Action Verified Against Ground Truth, Not Assumed**
📺 **Live Caption Overlay + On-Screen Click Debugging**

---

## 🆕 What's New in V2

V2 is a full rebuild of the assistant's core — not just a bigger action
menu, but a fundamentally different architecture: instead of one big loop
guessing whether an action worked, there's now a real event system, a
dependency-graph planner, and a verification layer that gives the model
honest, specific feedback about what actually happened.

### 🧠 The agent reasons over real, verified results
- Every action goes through a single **Executor** that settles, verifies,
  and scores its own confidence before reporting back — a "success" the
  model sees genuinely happened; it isn't inferred from a screenshot.
- Verification checks ground truth directly where possible (did the file
  actually get written? is the process actually running? did the window
  actually get focus?) instead of trusting the handler's return value.
- The agent's `WORKSPACE` context (current project, known files, terminal
  output, browser state) is injected straight into its reasoning, so it
  stops re-querying things it already knows.

### 🗂️ A dependency-graph planner for real multi-step goals
Complex requests ("build this project, style it, and open it in VS Code")
get broken into a small DAG of phases instead of one long, error-prone
chain:
- A failed phase blocks only its *descendants* — independent branches keep
  going.
- Every phase's progress is persisted, so a task is fully **resumable**:
  say **"Mist, resume ProjectName"** and it picks up exactly where it left
  off, even after a crash or an app restart.
- Simple one-step requests skip planning entirely and run directly — no
  overhead for "open Notepad."

### 🌐 Browser automation that reads the real page, not a screenshot
This was the single biggest rebuild in V2. Browser actions no longer rely
on OCR/Vision pixel-guessing at all — they talk to the page's actual DOM
through a dedicated Playwright-controlled Firefox instance:
- `browser_click`, `browser_fill`, `browser_get_text`, `browser_press_key`
  find and act on real elements, and raise a real, specific error if
  something genuinely isn't there — no more guessing at coordinates, no
  more clicking the wrong thing because two elements looked similar.
- **Persistent profile** — the automation browser keeps its own profile on
  disk, so a login sticks across restarts instead of resetting every time.
- **`browser_import_cookies`** — carry over a login from your real, daily
  Firefox for a specific site, without opening or risking your actual
  browser profile.
- **`browser_use_daily_profile`** — one-time clone of the login-carrying
  files (cookies, saved logins, session storage) from your real Firefox
  profile into MistAI's, so you're logged into *everything* at once. Your
  real profile is only ever read from, never opened or modified.
- Dark mode by default (`prefers-color-scheme: dark`), and page navigation
  correctly waits for JS-rendered content to actually appear before the
  agent reads the page — not just the initial empty HTML shell.
- **Known limitation:** sites with aggressive automation detection
  (Google/YouTube specifically) can silently reject an imported session
  even with valid cookies, since the browser is detectably
  Playwright-controlled. The reliable path there is logging in once by
  hand in MistAI's browser window — it persists after that.
- OCR/UIA-based clicking is still available as an intentional last resort
  for the rare canvas-only page with no real DOM to speak of, but it's no
  longer the default for anything inside a browser.

### 🎯 Tool metadata + learned reliability
Every action has real metadata behind it now — expected runtime, whether
it's destructive/reversible, which fallback to escalate to on failure —
and the system tracks each tool's *observed* success rate over time,
blending it with a starting prior. The more MistAI runs, the better its
own sense of what actually works gets, with zero manual tuning.

### 🖱️ Confidence-based clicking, with fallback and visual debugging
- Native-app clicks try the accessibility tree (UIA) first, then OCR/Vision
  as a fallback chain, each tagged with a real confidence score reflecting
  how trustworthy that resolution method actually is.
- A small on-screen indicator flashes at the exact pixel MistAI is about
  to click, color-coded by which method resolved it — genuinely useful for
  seeing *why* a click missed instead of guessing from logs.

### ❓ The agent can ask you a clarifying question — and actually waits
`ask_user` pauses execution and asks out loud or in the app. If you don't
respond, the task pauses rather than the model shrugging and improvising a
real action on its own initiative.

### 🎙️ Actual continuous conversation (not walkie-talkie mode)
Say the wake word once, give a command, and keep talking — no need to
repeat "Hey Mist" for follow-ups. Falls back to wake-word-only mode after a
period of silence or a stop phrase ("that's all," "nevermind," etc.).

---

## 🧩 Core Features

### 🎤 Wake Word + Continuous Conversation
- Activate with "Mist," "Hey Mist," "MistAI," or "Mist AI" (fuzzy-matched,
  tolerant of mishearing)
- Natural follow-up commands without repeating the wake word

### 👁️ Vision Mode (Screen Understanding)
- Real screen comprehension via multimodal AI (Cohere Vision) for native
  desktop content
- `read_screen_text` answers specific questions about what's visible —
  reserved for native apps; browser content uses real DOM reads instead

### 🌐 Browser Automation (Playwright / DOM-based)
- `browser_navigate`, `browser_click`, `browser_fill`, `browser_get_text`,
  `browser_press_key`, `browser_import_cookies`, `browser_use_daily_profile`
- Dedicated, persistent Firefox profile — separate from your daily browser,
  but keeps its own logins across restarts

### 🗂️ Desktop, File & System Automation
- **App control:** open, focus, maximize, close applications, with real
  process/window verification
- **File operations:** direct filesystem access; `{desktop}`, `{documents}`,
  `{home}` placeholders resolve correctly regardless of username or OneDrive
  redirection
- **System commands:** `run_command` executes PowerShell directly and
  returns real output
- **Window management:** list, activate, close, maximize, minimize

### 🧠 Persistent Memory + Long-Running, Resumable Tasks
- SQLite-backed memory: facts, reminders, app usage, conversation history
- `remember` / `recall` for mid-conversation fact storage
- Multi-phase tasks persist and can be resumed by name after a stop, crash,
  or restart

### 📺 Live Captions + Click Debugging
- Real-time overlay showing what the assistant is thinking/doing/saying
- On-screen flash showing exactly where a click landed and how it was
  resolved

---

## 💻 Platform Support

### Windows (fully supported)
✔ Windows 10 / 11 (64-bit)
✔ Microphone
✔ Internet connection
✔ Vision OCR (bundled Tesseract engine)
✔ Full desktop automation (`pywin32`, `pywinauto`, Windows registry)
✔ Playwright (Firefox) for browser automation

### macOS / Linux (not currently supported)
The current core relies heavily on Windows-specific APIs (`win32gui`,
`win32clipboard`, `winreg`, `pywinauto`) for desktop automation. Cross-
platform support is a planned future project, not a current guarantee.
Contributions welcome if you want to help build that layer.

---

## 🚀 Installation (Manual)

### Requirements
```bash
pip install -r requirements.txt
python -m playwright install firefox
```
Set `MISTAI_API_KEY` and `COHERE_API_KEY` in a `.env` file next to
`assistant.py`, or configure the API key via the ⚙ button in the app.

### Run
```bash
python assistant.py
```

Say **"Hey Mist"** to start, or just type a command in the chat box.

---

## 📁 Project Structure

```
MistAI/
├── assistant.py              # entry point
├── requirements.txt
├── core/
│   ├── config.py              # constants, wake-word matching, Tesseract setup
│   ├── api_manager.py         # API key / backend URL handling
│   ├── api.py                 # glues every subsystem together, exposed to the UI
│   ├── agent.py                # AgentLoop — per-phase step loop, system prompt
│   ├── planner.py              # dependency-graph planner + PlanRunner (resumable tasks)
│   ├── executor.py             # single execution path: settle, verify, confidence, escalate
│   ├── verify.py               # ground-truth verifiers (file written, process running, ...)
│   ├── tools.py                # tool metadata + learned reliability
│   ├── events.py               # internal event bus
│   ├── watchers.py             # LiveState/filesystem → event bridge
│   ├── workspace.py            # live project/file/terminal/browser state, injected into prompts
│   ├── task_store.py           # persistent, resumable TaskRecords
│   ├── browser_session.py      # Playwright-driven Firefox: navigate/click/fill/get_text,
│   │                           #   persistent profile, cookie import, daily-profile clone
│   ├── click_handler.py        # OCR → UIA → Vision fallback chain (native apps + last-resort)
│   ├── click_indicator.py      # on-screen click-location debug overlay
│   ├── tts.py                  # text-to-speech (edge-tts)
│   ├── screen.py                # continuous screen capture buffer
│   ├── live_state.py            # foreground window, CPU/RAM, clipboard tracking
│   ├── memory.py                 # SQLite memory (conversations, facts, reminders)
│   ├── vision.py                  # Cohere Vision (screen understanding)
│   ├── caption.py                 # caption overlay
│   ├── uia_nav.py                  # raw UIA window/element navigation
│   └── enhanced_utils.py            # window mgmt, scheduling
└── ui/
    └── html.py                      # pywebview frontend
```

---

## 🛠️ Known Limitations

- Windows-only for now (see Platform Support above)
- Voice recognition depends on Google's speech API — requires an internet
  connection and is subject to its accuracy/rate limits
- Vision calls (Cohere) have a small delay; very fast-changing screens may
  be read slightly stale
- MistAI's browser is a separate, dedicated automation profile — not your
  daily Firefox. Logins carry over via cookie import or a one-time profile
  clone, but sites with strong automation detection (Google/YouTube) may
  still require logging in by hand once

  BuiltByKristian