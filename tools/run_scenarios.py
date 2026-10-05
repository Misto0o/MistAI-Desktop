"""
tools/run_scenarios.py — run MistAI prompts unattended and summarize them.

    python tools/run_scenarios.py --stub                # scripted LLM, vision off, no backend
    python tools/run_scenarios.py --compare --yes       # real backend: each desktop scenario
                                                        # in each --compare mode (below)
Modes (--compare runs COMPARE_MODES; --mode picks any one; default = the app's own defaults):
    baseline      MISTAI_VISION_GATING=0  MISTAI_LAZY_VISION=0
    gating        MISTAI_VISION_GATING=1  MISTAI_LAZY_VISION=0
    gating+lazy1  MISTAI_VISION_GATING=1  MISTAI_LAZY_VISION=1  (title + apps + 1.5s UIA;
                  not in --compare: the UIA walk hit its cap with VS Code in front in
                  nearly every real run, so it added 1.5s for lazy2's information)
    gating+lazy2  MISTAI_VISION_GATING=1  MISTAI_LAZY_VISION=2  (title + apps only)
Options:
    --scenarios PATH     scenario file (default: tools/scenarios.json)
    --only a,b           run only these scenario names
    --repeat N           run every scenario (in every mode) N times, interleaving
                         modes per round; the summary adds min/median/max seconds.
                         Notepad scenarios may SKIP on later rounds: Win11 Notepad
                         restores the tab typed in round 1. Fix it on the machine
                         (Notepad > Settings > "When Notepad starts" > "Open a new
                         window"), not in the harness
    --stub               scripted model replies from the scenario file; implies
                         --no-vision and blocks requests to the MistAI backend/Cohere
    --no-vision          force vision off (no screenshots leave the machine)
    --compare            desktop scenarios run once per mode
    --mode NAME          one mode for a normal run
    --allow-ui-input     also run scenarios tagged "ui_input" (they type/click)
    --yes                skip the 5-second "about to drive your desktop" pause

Scenario types (tools/scenarios.json):
    "desktop": calls Api.ask_mistai for each turn on the real desktop.
    "plan":    calls planner.make_plan only and asserts the node list. No desktop.

Isolation: memory/tasks/tool stats go to a fresh throwaway SQLite file per
scenario (MISTAI_DB_PATH), and the workspace and live state are reset before
each one; each scenario gets its own folder under %TEMP% ("{tmp}" in
the scenario) and only that folder is deleted afterwards; the only processes
closed are ones whose names are listed in cleanup AND that did not exist
before the scenario started. Confirmations are answered from the scenario's
"answers" (default "no"). Logs: ~/MistAI/logs/scenarios-<timestamp>/.
"""

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from datetime import datetime
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DEFAULT_SCENARIOS = os.path.join(REPO, "tools", "scenarios.json")
DEFAULT_TIMEOUT = 180
TMP_PREFIX = "mist_scn_"
BLOCKED_HOSTS_IN_STUB = ("mistai.org", "mist-ai.fly.dev", "cohere.com", "cohere.ai")
MODES = {
    "baseline": {"MISTAI_VISION_GATING": "0", "MISTAI_LAZY_VISION": "0"},
    "gating": {"MISTAI_VISION_GATING": "1", "MISTAI_LAZY_VISION": "0"},
    "gating+lazy1": {"MISTAI_VISION_GATING": "1", "MISTAI_LAZY_VISION": "1"},
    "gating+lazy2": {"MISTAI_VISION_GATING": "1", "MISTAI_LAZY_VISION": "2"},
}
COMPARE_MODES = ("baseline", "gating", "gating+lazy2")
MODE_ENV_KEYS = ("MISTAI_VISION_GATING", "MISTAI_LAZY_VISION")


# -- Scenario file -----------------------------------------------------------------


def load_scenarios(path, only=None):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    scenarios = data["scenarios"] if isinstance(data, dict) else data
    names = set()
    for sc in scenarios:
        if not sc.get("name"):
            raise ValueError("every scenario needs a name")
        if sc["name"] in names:
            raise ValueError(f"duplicate scenario name: {sc['name']}")
        names.add(sc["name"])
        sc.setdefault("type", "desktop")
        if sc["type"] not in ("desktop", "plan"):
            raise ValueError(f"{sc['name']}: type must be desktop or plan")
        if sc["type"] == "desktop" and not (sc.get("prompt") or sc.get("turns")):
            raise ValueError(f"{sc['name']}: desktop scenarios need prompt or turns")
        if sc["type"] == "plan" and not sc.get("prompt"):
            raise ValueError(f"{sc['name']}: plan scenarios need a prompt")
        if "uia_after_read" in sc and not isinstance(sc["uia_after_read"], str):
            raise ValueError(f"{sc['name']}: uia_after_read must be a process name")
    if only:
        wanted = set(only)
        missing = wanted - names
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(missing))}")
        scenarios = [s for s in scenarios if s["name"] in wanted]
    return scenarios


def expand(obj, tmp):
    """Replace {tmp} in every string of a scenario (paths stay Windows-style)."""
    if isinstance(obj, str):
        return obj.replace("{tmp}", tmp)
    if isinstance(obj, list):
        return [expand(x, tmp) for x in obj]
    if isinstance(obj, dict):
        return {k: expand(v, tmp) for k, v in obj.items()}
    return obj


def turns_of(sc):
    return sc.get("turns") or [sc["prompt"]]


# -- Console capture & log parsing -------------------------------------------------


class Tee:
    """Copies everything printed (from any thread) to a log file."""

    def __init__(self, path, console):
        self._f = open(path, "w", encoding="utf-8", errors="replace")
        self._console = console
        self._lock = threading.Lock()
        self.parts = []

    def write(self, s):
        with self._lock:
            self._f.write(s)
            self.parts.append(s)
            try:
                self._console.write(s)
            except Exception:
                pass
        return len(s)

    def flush(self):
        with self._lock:
            self._f.flush()
            try:
                self._console.flush()
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._f.close()

    @property
    def text(self):
        with self._lock:
            return "".join(self.parts)


_STEP_RX = re.compile(r"^\s*Step (\d+): ([\w.]+)\((.*)\)\s*$", re.M)
_TIMING_RX = re.compile(r"\[Timing\] step (\d+): (.*)$", re.M)
_KV_RX = re.compile(r"(\w+)=([\d.]+)s")
_REASON_RX = re.compile(r"\[Executor\] (\S+) -> FAIL.*?\| reason: (.*)$", re.M)
_REFUSED_RX = re.compile(r"\[Refused\] (.*)$", re.M)
_TASK_RX = re.compile(r"\[Timing\] Task: (.*)$", re.M)
_PHASE_SCREEN_RX = re.compile(r"\[Timing\] Phase: .*?\bscreen ([\d.]+)s/(\d+)", re.M)
_SCREEN_REASON_RX = re.compile(r"\[Screen\] re-read: (.*)$", re.M)
_ESCALATE_RX = re.compile(r"\[Executor\] Escalating (\S+) -> (\S+)", re.M)
_EXEC_RX = re.compile(r"^\[Exec\] (\w+)\((.*)\)\s*$", re.M)  # printed when a handler runs


def parse_log(text):
    steps = [{"step": int(n), "action": a, "parameter": p} for n, a, p in _STEP_RX.findall(text)]
    step_times = []
    for n, rest in _TIMING_RX.findall(text):
        times = {k: float(v) for k, v in _KV_RX.findall(rest)}
        step_times.append({"step": int(n), **times})
    return {
        "steps": steps,
        "actions": [s["action"] for s in steps],
        "executed": [{"action": a, "parameter": p} for a, p in _EXEC_RX.findall(text)],
        "step_times": step_times,
        # Phase totals count every read, including steps that ended without
        # a step-timing line (refusals, declined confirmations).
        "screen_reads": sum(int(n) for _, n in _PHASE_SCREEN_RX.findall(text)),
        "screen_seconds": round(sum(float(s) for s, _ in _PHASE_SCREEN_RX.findall(text)), 2),
        "screen_reasons": _SCREEN_REASON_RX.findall(text),
        "failures": [f"{a}: {r}" for a, r in _REASON_RX.findall(text)],
        "refusals": _REFUSED_RX.findall(text),
        "escalations": [f"{a} -> {b}" for a, b in _ESCALATE_RX.findall(text)],
        "task_timing": (_TASK_RX.findall(text) or [""])[-1],
    }


# -- Expectations ------------------------------------------------------------------


def _norm(s):
    return " ".join(str(s or "").lower().split())


def check_desktop(expect, outcome):
    """Returns a list of failed expectation messages (empty = pass)."""
    fails = []
    parsed = outcome["parsed"]
    if "success" in expect and outcome["success"] is not expect["success"]:
        fails.append(f"success was {outcome['success']}, expected {expect['success']}")
    speech = _norm(outcome["speech"])
    any_of = expect.get("speech_contains_any")
    if any_of and not any(_norm(w) in speech for w in any_of):
        fails.append(f"speech {outcome['speech']!r} has none of {any_of}")
    for a in expect.get("actions_include", []):
        if a not in parsed["actions"]:
            fails.append(f"action {a} never ran (ran: {parsed['actions']})")
    for a in expect.get("actions_exclude", []):
        if a in parsed["actions"]:
            fails.append(f"action {a} ran but must not")
    for want in expect.get("executed_with_param", []):
        if not any(e["action"] == want["action"] and _norm(want["contains"]) in _norm(e["parameter"])
                   for e in parsed.get("executed", [])):
            fails.append(f"{want['action']} with {want['contains']!r} never actually ran")
    if "max_steps" in expect and len(parsed["steps"]) > expect["max_steps"]:
        fails.append(f"{len(parsed['steps'])} steps > max {expect['max_steps']}")
    for s in expect.get("log_not_contains", []):
        if s in outcome["log"]:
            fails.append(f"log contains forbidden text {s!r}")
    if "confirmation_asked" in expect and bool(outcome["questions"]) is not expect["confirmation_asked"]:
        fails.append(
            f"confirmation asked={bool(outcome['questions'])}, expected {expect['confirmation_asked']}"
        )
    for p in expect.get("files_exist", []):
        if not os.path.exists(p):
            fails.append(f"missing file {p}")
    for p in expect.get("files_missing", []):
        if os.path.exists(p):
            fails.append(f"file should not exist: {p}")
    for s in expect.get("secret_never_logged", []):
        if s in outcome["log"] or s in str(outcome["speech"]):
            fails.append("SECRET LEAKED into the log or speech")
    end = outcome.get("end_state", {})
    for name in expect.get("process_started", []):
        if not end.get("process_started", {}).get(name):
            fails.append(f"no new {name} process was started")
    wt = expect.get("window_text_contains")
    if wt:
        texts = end.get("window_text")
        if texts is None:
            outcome.setdefault("notes", []).append(
                f"window text of {wt['process']} could not be read (unverified)"
            )
        elif not any(wt["text"].lower() in t.lower() for t in texts):
            fails.append(f"{wt['process']} window does not contain {wt['text']!r}")
    alternatives = expect.get("any_of")
    if alternatives:
        alt_fails = [check_desktop(alt, outcome) for alt in alternatives]
        if all(alt_fails):
            fails.append("no alternative held: " + " | ".join("; ".join(f) for f in alt_fails))
    return fails


def _expect_parts(expect):
    """The expectation and each any_of alternative (for end-state reads)."""
    return [expect] + list(expect.get("any_of", []))


def check_plan(expect, nodes):
    fails = []
    descs = [_norm(n["desc"]) for n in nodes]
    if "nodes_count" in expect and len(nodes) != expect["nodes_count"]:
        fails.append(f"{len(nodes)} nodes, expected {expect['nodes_count']}: {descs}")
    any_of = expect.get("node_desc_any")
    if any_of and not any(_norm(w) in d for d in descs for w in any_of):
        fails.append(f"no node mentions any of {any_of}: {descs}")
    for w in expect.get("nodes_exclude_words", []):
        hits = [d for d in descs if re.search(rf"\b{re.escape(_norm(w))}\b", d)]
        if hits:
            fails.append(f"node(s) mention {w!r}: {hits}")
    return fails


# -- Stub backend & safety fences --------------------------------------------------


class _Resp:
    ok = True
    status_code = 200

    def __init__(self, text):
        self._text = text

    def json(self):
        return {"response": self._text}


class StubBackend:
    """Scripted agent replies, one list per turn. Exhausted -> 'done'."""

    def __init__(self):
        self._replies = []
        self._lock = threading.Lock()

    def load(self, replies):
        with self._lock:
            self._replies = [r if isinstance(r, str) else json.dumps(r) for r in (replies or [])]

    def post(self, *_args, **_kwargs):
        with self._lock:
            reply = self._replies.pop(0) if self._replies else json.dumps(
                {"action": "done", "speech": "(stub script exhausted)"}
            )
        return _Resp(reply)


def install_stub_fences(stub, plan_reply):
    """--stub: the agent and planner talk to scripts, and anything else that
    tries to reach the MistAI backend or Cohere fails loudly."""
    import requests

    import core.agent
    import core.planner

    saved = (core.agent.requests, core.planner._llm, requests.sessions.Session.request)
    core.agent.requests = SimpleNamespace(post=stub.post)
    core.planner._llm = lambda *_a, **_k: plan_reply[0]
    original = saved[2]

    def guarded(self, method, url, *a, **k):
        if any(h in str(url) for h in BLOCKED_HOSTS_IN_STUB):
            raise RuntimeError(f"--stub blocked a network call to {url}")
        return original(self, method, url, *a, **k)

    requests.sessions.Session.request = guarded

    def undo():
        core.agent.requests, core.planner._llm, requests.sessions.Session.request = saved

    return undo


def disable_vision():
    import core.vision

    saved = core.vision.CohereVision.ready
    core.vision.CohereVision.ready = property(lambda self: False)

    def undo():
        core.vision.CohereVision.ready = saved

    return undo


# -- Processes & temp folders ------------------------------------------------------


def pids_named(names):
    import psutil

    from core.config import process_matches

    out = set()
    for p in psutil.process_iter(["name", "pid"]):
        if any(process_matches(n, p.info["name"] or "") for n in names):
            out.add(p.info["pid"])
    return out


def close_new_processes(names, before):
    import psutil

    closed = []
    for pid in sorted(pids_named(names) - before):
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
            closed.append(pid)
        except Exception:
            pass
    return closed


def app_window_handles(name):
    """[(hwnd, title)] of visible, titled top-level windows that belong to
    ``name`` -- by process, or for Store/UWP apps by their frame's title, so a
    frame left behind after its process exited (a leftover Calculator) counts."""
    try:
        import win32gui

        from core.live_state import window_belongs_to
    except ImportError:
        return []
    found = []

    def _cb(h, _):
        try:
            title = win32gui.GetWindowText(h)
            if win32gui.IsWindowVisible(h) and title.strip() and window_belongs_to(h, name):
                found.append((h, title))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return []
    return found


def close_windows(hwnds):
    """Ask windows to close (WM_CLOSE), the way their own X button does."""
    import win32con
    import win32gui

    for h in hwnds:
        try:
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass


def ensure_windows_gone(names, wait=3.0):
    """After cleanup: wait for ``names``' windows to disappear, close any
    that linger (a UWP frame can outlive its process), and return the
    titles still open afterwards. Only called for scenarios that started
    with no such window, so whatever is left came from the scenario."""
    lingering = []
    for name in names:
        if not wait_pumping(lambda: not app_window_handles(name), wait):
            left = app_window_handles(name)
            print(f"[Harness] {name} window(s) still open after cleanup: "
                  f"{[t for _, t in left]}; closing them")
            close_windows([h for h, _ in left])
            if not wait_pumping(lambda: not app_window_handles(name), wait):
                lingering += [t for _, t in app_window_handles(name)]
    return lingering


def running_elevated():
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def make_scenario_dir():
    return tempfile.mkdtemp(prefix=TMP_PREFIX)


def remove_scenario_dir(path):
    """Delete only a folder this harness created under %TEMP%."""
    root = os.path.realpath(tempfile.gettempdir())
    real = os.path.realpath(path)
    if (
        os.path.dirname(real) != root
        or not os.path.basename(real).startswith(TMP_PREFIX)
    ):
        raise RuntimeError(f"refusing to delete {path}: not a harness temp folder")
    shutil.rmtree(real, ignore_errors=True)


def run_setup(steps):
    """Returns a skip reason, or None."""
    for step in steps or []:
        if "require_process_absent" in step:
            name = step["require_process_absent"]
            if pids_named([name]):
                return f"{name} is running (scenario needs it closed; not killing it)"
        elif "write_file" in step:
            spec = step["write_file"]
            os.makedirs(os.path.dirname(spec["path"]), exist_ok=True)
            with open(spec["path"], "w", encoding="utf-8") as f:
                f.write(spec.get("content", ""))
        elif "mkdir" in step:
            os.makedirs(step["mkdir"], exist_ok=True)
        elif "require_empty_window" in step:
            spec = step["require_empty_window"]
            reason = probe_restored_session(spec["process"], spec["launch"])
            if reason:
                return reason
        else:
            raise ValueError(f"unknown setup step: {step}")
    return None


def app_windows(name):
    """[(title, [(control_type, accessible_name, [contents])])] for every
    window owned by processes matching ``name``, or None if UIA failed.
    Runs on the main thread, where pywinauto lives."""
    try:
        from pywinauto import Desktop

        pids = pids_named([name])
        out = []
        for w in Desktop(backend="uia").windows():
            try:
                if w.process_id() not in pids and not (
                    # UWP apps (Calculator) draw inside ApplicationFrameHost.
                    w.element_info.class_name == "ApplicationFrameWindow"
                    and any(c.process_id() in pids for c in w.children())
                ):
                    continue
                controls = []
                for d in w.descendants():
                    contents = []
                    for getter in (
                        lambda: d.iface_value.CurrentValue,
                        lambda: d.iface_text.DocumentRange.GetText(-1),
                    ):
                        try:
                            contents.append(str(getter()))
                        except Exception:
                            pass
                    controls.append((d.element_info.control_type, d.window_text(), contents))
                out.append((w.window_text(), controls))
            except Exception:
                continue
        return out
    except Exception:
        return None


def window_texts(name):
    """Visible text of ``name``'s windows (titles plus Edit/Document/Text
    names and contents), or None if nothing could be read."""
    texts = []
    for title, controls in app_windows(name) or []:
        texts.append(title)
        for ctype, cname, contents in controls:
            if ctype in ("Edit", "Document", "Text"):
                texts += [cname, *contents]
    texts = [t for t in texts if t and t.strip()]
    return texts or None


def wrap_reads_with_uia(api, process):
    """Scenario key "uia_after_read": after every read_screen_text, log what
    UIA says ``process``'s window contains, so a vision answer that disagrees
    with the window shows up in the log. Returns an undo function."""
    from core.config import secret_text_reason

    had_own = "read_screen_text" in vars(api)
    original = api.read_screen_text

    def traced(query=""):
        result = original(query)
        texts = window_texts(process)
        shown = "(could not read)" if texts is None else " | ".join(texts)[:300]
        reason = secret_text_reason(shown)
        if reason:
            shown = f"(withheld: {reason})"
        print(f"[Harness] UIA after read_screen_text ({process}): {shown}")
        return result

    api.read_screen_text = traced

    def undo():
        if had_own:
            api.read_screen_text = original
        else:
            del api.read_screen_text

    return undo


NOTEPAD_SESSION_HINT = (
    r"Win11 Notepad restores unsaved tabs from %LOCALAPPDATA%\Packages"
    r"\Microsoft.WindowsNotepad_8wekyb3d8bbwe\LocalState\TabState; close those tabs "
    "without saving, or set Notepad > Settings > 'When Notepad starts' to "
    "'Open a new window' (the harness never deletes that state)"
)


def probe_restored_session(name, launch, timeout=8.0):
    """Launch the app and read what its window comes up with: a skip reason
    if it restored text or extra tabs from an earlier session (Win11
    Notepad), else None. Closes only the process(es) it started."""
    import subprocess

    before = pids_named([name])
    try:
        subprocess.Popen(launch)
    except OSError as e:
        return f"could not start {launch} to check for a restored session: {e}"
    controls = None
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # A window can show before its (possibly empty) document exists.
            controls = [c for _, cs in app_windows(name) or [] for c in cs]
            if any(ctype == "Document" for ctype, _, _ in controls):
                break
            controls = None
            wait_pumping(lambda: False, 0.5)
    finally:
        close_new_processes([name], before)
    return restored_session_reason(name, controls)


def restored_session_reason(name, controls):
    """Skip reason if the controls of a freshly started window show restored
    content: text in a document (its value, not its accessible name such as
    "Text editor") or more than one tab. None = clean."""
    if controls is None:
        return f"could not read a fresh {name} window to check for a restored session"
    text = next((t.strip() for ctype, _, contents in controls if ctype == "Document"
                 for t in contents if t.strip()), "")
    tabs = sum(ctype == "TabItem" for ctype, _, _ in controls)
    if text or tabs > 1:
        return (f"{name} came up with leftover content ({tabs} tab(s), text {text[:40]!r}); "
                f"{NOTEPAD_SESSION_HINT}")
    return None


def reset_session(api, db_path):
    """Fresh memory/task/tool-stat databases, re-seeded like Api.__init__,
    plus a clean workspace and a fresh live-state read, so nothing from an
    earlier scenario (last command, conversation, Open apps) leaks in."""
    os.environ["MISTAI_DB_PATH"] = db_path
    try:
        from core.memory import Memory
        from core.seed_data import DEFAULT_HOTKEYS, DEFAULT_SITE_SHORTCUTS, SEED_FACTS
        from core.task_memory import TaskMemory
        from core.task_store import TaskStore
        from core.tools import ToolRegistry

        memory = Memory()
        memory.seed_defaults(DEFAULT_HOTKEYS)
        memory.seed_defaults(DEFAULT_SITE_SHORTCUTS)
        memory.seed_facts(SEED_FACTS)
        api.memory = memory
        api.task_memory = TaskMemory()
        api.tools = ToolRegistry()
        api.task_store = TaskStore()
    except Exception as e:
        print(f"[Harness] Could not reset databases: {e}")
    workspace = getattr(api, "workspace", None)
    if workspace is not None and hasattr(workspace, "reset_task_state"):
        workspace.reset_task_state()
    for attr, value in (("last_result_text", ""), ("last_command_result", None),
                        ("_workspace_required_for_task", False)):
        if hasattr(api, attr):
            setattr(api, attr, value)
    if hasattr(api, "_trusted_project_roots"):
        api._trusted_project_roots = set()
    live = getattr(api, "live", None)
    if live is not None and hasattr(live, "refresh"):
        live.refresh()


def wait_pumping(done, timeout):
    """Wait for done() while pumping this thread's COM/window messages.

    pywinauto creates its UIA COM object on the importing (main) thread. The
    agent thread's UIA calls are marshalled back here, so a plain join()
    deadlocks them; in the real app pywebview's GUI loop does this pumping."""
    try:
        import pythoncom
    except ImportError:
        pythoncom = None
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        if pythoncom is not None:
            pythoncom.PumpWaitingMessages()
        time.sleep(0.05)
    return done()


def _agent_busy(api):
    try:
        return bool(api.is_agent_busy().get("busy"))
    except Exception:
        return False


def dump_threads():
    """Where every thread is stuck, into the scenario log (timeouts only)."""
    import traceback

    names = {t.ident: t.name for t in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
        if names.get(ident) in ("MainThread", None):
            continue
        print(f"[Harness] --- thread {names.get(ident)} ---")
        print("".join(traceback.format_stack(frame)[-8:]).rstrip())


# -- Runner ------------------------------------------------------------------------


class Runner:
    def __init__(self, args, log_dir, api_factory=None):
        self.args = args
        self.log_dir = log_dir
        self.stub = StubBackend() if args.stub else None
        self.plan_reply = [""]
        self._api = None
        self._api_factory = api_factory
        self.questions = []
        self.answers = []
        self.spoken = []
        self.run_tmp = None  # set by main(): per-scenario databases live here
        self._session = 0

    # The real Api is built once, lazily, and only for desktop scenarios.
    def api(self):
        if self._api is None:
            if self._api_factory:
                self._api = self._api_factory()
            else:
                from core.api import Api
                from core.config import setup_tesseract

                setup_tesseract()
                self._api = Api()
            self._api.speak = lambda text, *a, **k: self.spoken.append(text)
            self._api.speak_now = self._api.speak
            self._api._get_text_answer = self._answer
        return self._api

    def _answer(self, question, timeout=60):
        self.questions.append(question)
        print(f"[Harness] Confirmation asked: {question}")
        answer = self.answers.pop(0) if self.answers else "no"
        print(f"[Harness] Answering: {answer}")
        return answer

    def _ask(self, prompt, timeout):
        api = self.api()
        api.AGENT_HARD_TIMEOUT = timeout + 30
        box = {}

        def call():
            try:
                box["result"] = api.ask_mistai(prompt, input_mode="text")
            except Exception as e:
                box["error"] = repr(e)

        t = threading.Thread(target=call, daemon=True, name="ScenarioTurn")
        t.start()
        wait_pumping(lambda: not t.is_alive(), timeout)
        if t.is_alive() or _agent_busy(api):
            print(f"[Harness] TIMEOUT after {timeout}s -- stopping the agent")
            dump_threads()
            try:
                api.stop_current_task()
            except Exception:
                pass
            wait_pumping(lambda: not t.is_alive() and not _agent_busy(api), 30)
            return None, "timeout", t.is_alive() or _agent_busy(api)
        if "error" in box:
            return None, box["error"], False
        return box["result"], None, False

    def run_desktop(self, sc, mode, rep=None):
        name = (sc["name"] + (f"--{mode.replace('+', '-')}" if mode else "")
                + (f"--r{rep}" if rep else ""))
        tmp = make_scenario_dir()
        sc = expand(sc, tmp)
        record = {"name": sc["name"], "type": "desktop", "mode": mode, "status": "PASS",
                  "failures": [], "log": os.path.join(self.log_dir, f"{name}.log")}
        if rep:
            record["repeat"] = rep
        if mode:
            os.environ.update(MODES[mode])
        self._session += 1
        reset_session(self.api(), os.path.join(self.run_tmp or tmp, f"memory-{self._session}.db"))
        cleanup = sc.get("cleanup", {})
        expect = sc.get("expect", {})
        parts = _expect_parts(expect)
        started_names = sorted({n for e in parts for n in e.get("process_started", [])})
        text_procs = sorted({e["window_text_contains"]["process"] for e in parts
                             if e.get("window_text_contains")})
        watched = sorted(set(cleanup.get("close_new_processes", [])) | set(started_names)
                         | set(text_procs))
        before = pids_named(watched)
        end_state = {}
        console = sys.stdout
        tee = Tee(record["log"], console)
        stuck = False
        undo_reads = None
        try:
            # Scenarios that open an app need it closed: a window left over
            # from an earlier run (by title too, for UWP frames) is not ours.
            skip = next((f"a {n} window is already open ({t[0][1]!r}); close it first "
                         "(the harness never closes windows it did not open)"
                         for n in watched for t in [app_window_handles(n)] if t), None)
            skip = skip or run_setup(sc.get("setup"))
            if skip:
                record.update(status="SKIP", failures=[skip])
                remove_scenario_dir(tmp)
                return record, False
            self.questions, self.spoken = [], []
            self.answers = list(sc.get("answers", []))
            if sc.get("uia_after_read"):
                undo_reads = wrap_reads_with_uia(self.api(), sc["uia_after_read"])
            sys.stdout = tee
            print(f"[Harness] Scenario {name} (mode={mode or 'app default'}, tmp={tmp})")
            started = time.monotonic()
            result, error = None, None
            for i, prompt in enumerate(turns_of(sc)):
                if self.stub:
                    scripts = sc.get("stub", [])
                    self.stub.load(scripts[i] if i < len(scripts) else [])
                    self.plan_reply[0] = json.dumps(sc["stub_plan"]) if sc.get("stub_plan") else ""
                print(f"[Harness] Turn {i + 1}: {prompt}")
                result, error, stuck = self._ask(prompt, sc.get("timeout", DEFAULT_TIMEOUT))
                if error:
                    break
            record["seconds"] = round(time.monotonic() - started, 2)
        except Exception as e:
            error, result = f"harness error: {e!r}", None
        finally:
            if undo_reads:
                undo_reads()
            sys.stdout = console
            tee.close()
            # End state is judged BEFORE cleanup closes anything.
            end_state = {
                "process_started": {n: bool(pids_named([n]) - before) for n in started_names},
            }
            if text_procs:
                # ponytail: one window_text list shared by all alternatives;
                # per-process lists if alternatives ever read different apps.
                end_state["window_text"] = window_texts(text_procs[0])
            try:
                closed = close_new_processes(cleanup.get("close_new_processes", []), before)
                if closed:
                    print(f"[Harness] Closed processes started by {name}: {closed}")
                if record["status"] != "SKIP":
                    left = ensure_windows_gone(cleanup.get("close_new_processes", []))
                    if left:
                        print(f"[Harness] WARNING: window(s) still open after cleanup: {left}")
                        record.setdefault("cleanup_left", left)
            except Exception as e:
                print(f"[Harness] Cleanup of processes failed: {e}")
        log = tee.text
        parsed = parse_log(log)
        outcome = {
            "success": bool(result and result.get("success")),
            "speech": (result or {}).get("command", {}).get("speech", ""),
            "parsed": parsed,
            "log": log,
            "questions": list(self.questions),
            "end_state": end_state,
        }
        record.update(
            speech=outcome["speech"],
            success=outcome["success"],
            steps=parsed["steps"],
            step_times=parsed["step_times"],
            screen_reads=parsed["screen_reads"],
            screen_seconds=parsed["screen_seconds"],
            screen_reasons=parsed["screen_reasons"],
            reasons=parsed["failures"] + parsed["refusals"],
            escalations=parsed["escalations"],
            task_timing=parsed["task_timing"],
            questions=outcome["questions"],
            secrets=expect.get("secret_never_logged", []),
        )
        if error:
            record.update(status="TIMEOUT" if error == "timeout" else "ERROR", failures=[error])
        else:
            fails = check_desktop(expect, outcome)
            if fails:
                record.update(status="FAIL", failures=fails)
        record["notes"] = outcome.get("notes", [])
        remove_scenario_dir(tmp)
        return record, stuck

    def run_plan(self, sc, rep=None):
        from core import planner
        from core.api_manager import APIKeyManager
        from core.config import AGENT_MODEL
        from core.events import EventBus
        from core.workspace import Workspace

        log_name = sc["name"] + (f"--r{rep}" if rep else "")
        record = {"name": sc["name"], "type": "plan", "mode": None, "status": "PASS",
                  "failures": [], "log": os.path.join(self.log_dir, f"{log_name}.log")}
        if rep:
            record["repeat"] = rep
        if self.stub:
            self.plan_reply[0] = json.dumps(sc["stub_plan"]) if sc.get("stub_plan") else ""
        plan_api = SimpleNamespace(api_manager=APIKeyManager(), workspace=Workspace(EventBus()))
        console = sys.stdout
        tee = Tee(record["log"], console)
        sys.stdout = tee
        started = time.monotonic()
        try:
            plan, task_name = planner.make_plan(plan_api, sc["prompt"], AGENT_MODEL)
            nodes = [{"id": nid, "desc": plan.nodes[nid].desc,
                      "depends_on": plan.nodes[nid].depends_on} for nid in plan.order]
        except Exception as e:
            nodes, task_name = [], ""
            record.update(status="ERROR", failures=[repr(e)])
        finally:
            sys.stdout = console
            tee.close()
        record.update(nodes=nodes, task_name=task_name,
                      seconds=round(time.monotonic() - started, 2))
        if record["status"] == "PASS":
            fails = check_plan(sc.get("expect", {}), nodes)
            if fails:
                record.update(status="FAIL", failures=fails)
        return record


# -- Reporting ---------------------------------------------------------------------


def _cell(r):
    if not r:
        return "-"
    return (f"{r['status']:<7} steps={len(r.get('steps', [])):<2} "
            f"t={r.get('seconds', 0):>6.1f}s reads={r.get('screen_reads', 0):<2} "
            f"esc={len(r.get('escalations', []))}")


def timing_stats(records):
    """{(name, mode): {n, passed, skipped, min, median, max}} over repeated
    runs. Skipped runs are counted but have no time; min/median/max are None
    when no run of that scenario+mode produced a time."""
    groups = {}
    for r in records:
        groups.setdefault((r["name"], r.get("mode")), []).append(r)
    stats = {}
    for key, runs in groups.items():
        secs = [r["seconds"] for r in runs if r["status"] != "SKIP" and "seconds" in r]
        stats[key] = {
            "n": len(runs),
            "passed": sum(r["status"] == "PASS" for r in runs),
            "skipped": sum(r["status"] == "SKIP" for r in runs),
            "min": min(secs) if secs else None,
            "median": round(statistics.median(secs), 2) if secs else None,
            "max": max(secs) if secs else None,
        }
    return stats


def format_timing(records):
    lines = ["Timing over repeated runs (seconds)",
             f"{'scenario':<26} | {'mode':<13} | {'pass':<6} | {'skip':<4} | min / median / max"]
    lines.append("-" * 80)
    for (name, mode), s in timing_stats(records).items():
        t = ("-" if s["min"] is None
             else f"{s['min']:.1f} / {s['median']:.1f} / {s['max']:.1f}")
        passed = f"{s['passed']}/{s['n']}"
        lines.append(f"{name:<26} | {mode or '-':<13} | {passed:<6} | {s['skipped']:<4} | {t}")
    return lines


def format_summary(records, compare):
    lines = []
    if compare:
        modes = list(COMPARE_MODES)
        lines.append(f"{'scenario':<26} | " + " | ".join(f"{m:<44}" for m in modes))
        lines.append("-" * (29 + 47 * len(modes)))
        by_name = {}
        for r in records:
            # With --repeat, the grid shows round 1; the timing table covers all rounds.
            by_name.setdefault(r["name"], {}).setdefault(r.get("mode"), r)
        for name, runs in by_name.items():
            if None in runs:  # run once: plan scenario, or a skipped desktop one
                r = runs[None]
                note = "(plan scenario)" if r["type"] == "plan" else "(not run)"
                lines.append(f"{name:<26} | {r['status']:<44} | {note}")
            else:
                lines.append(f"{name:<26} | " + " | ".join(f"{_cell(runs.get(m)):<44}" for m in modes))
    else:
        lines.append(f"{'scenario':<28} | result")
        lines.append("-" * 80)
        for r in records:
            if r["type"] == "plan":
                lines.append(f"{r['name']:<28} | {r['status']:<7} nodes={len(r.get('nodes', []))}")
            else:
                lines.append(f"{r['name']:<28} | {_cell(r)}")
    problems = [r for r in records if r["status"] not in ("PASS", "SKIP")]
    skipped = [r for r in records if r["status"] == "SKIP"]
    for r in problems + skipped:
        parts = [p for p in (r.get("mode"), f"run {r['repeat']}" if r.get("repeat") else "") if p]
        tag = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"  {r['status']} {r['name']}{tag}: " + "; ".join(r["failures"]))
        for reason in r.get("reasons", [])[:5]:
            lines.append(f"      reason: {reason[:200]}")
    for r in records:
        for note in r.get("notes", []):
            lines.append(f"  NOTE {r['name']} ({r.get('mode') or 'default'}): {note}")
    if any(r.get("repeat") for r in records):
        lines += [""] + format_timing(records)
    passed = sum(r["status"] == "PASS" for r in records)
    lines.append(f"{passed}/{len(records)} passed, {len(skipped)} skipped")
    return "\n".join(lines)


def check_secrets_across_logs(records, log_dir):
    """A scenario's secret must not appear in ANY log of the run, not just
    its own (a later scenario could echo an earlier one's workspace)."""
    logs = {}
    for fn in os.listdir(log_dir):
        if fn.endswith(".log"):
            with open(os.path.join(log_dir, fn), encoding="utf-8", errors="replace") as f:
                logs[fn] = f.read()
    for r in records:
        # pop: the secret strings themselves must not be written to summary.json
        for secret in r.pop("secrets", []):
            leaked = sorted(fn for fn, text in logs.items() if secret in text)
            if leaked:
                r["status"] = "FAIL"
                r["failures"] = r.get("failures", []) + [f"SECRET LEAKED into {', '.join(leaked)}"]


# -- CLI -----------------------------------------------------------------------------


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Run MistAI scenarios unattended.")
    ap.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    ap.add_argument("--only", default="")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--stub", action="store_true")
    ap.add_argument("--no-vision", action="store_true")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--mode", choices=tuple(MODES))
    ap.add_argument("--allow-ui-input", action="store_true")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--log-root", default=os.path.join(os.path.expanduser("~"), "MistAI", "logs"))
    args = ap.parse_args(argv)
    if args.repeat < 1:
        ap.error("--repeat must be at least 1")
    if args.stub:
        args.no_vision = True
    return args


def main(argv=None, api_factory=None):
    args = parse_args(argv)
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    scenarios = load_scenarios(args.scenarios, only)

    log_dir = os.path.join(args.log_root, f"scenarios-{datetime.now():%Y%m%d-%H%M%S}")
    os.makedirs(log_dir, exist_ok=True)
    run_tmp = make_scenario_dir()
    saved_env = {k: os.environ.get(k) for k in ("MISTAI_DB_PATH",) + MODE_ENV_KEYS}
    os.environ["MISTAI_DB_PATH"] = os.path.join(run_tmp, "memory.db")

    if not args.stub:
        try:
            from dotenv import load_dotenv

            load_dotenv(os.path.join(REPO, ".env"))
        except ImportError:
            pass
    runner = Runner(args, log_dir, api_factory=api_factory)
    runner.run_tmp = run_tmp
    undo = []
    if args.stub:
        undo.append(install_stub_fences(runner.stub, runner.plan_reply))
    if args.no_vision:
        undo.append(disable_vision())

    desktop = [s for s in scenarios if s["type"] == "desktop"]
    skipped_ui = [s for s in desktop if "ui_input" in s.get("tags", []) and not args.allow_ui_input]
    desktop = [s for s in desktop if s not in skipped_ui]
    print(f"[Harness] {len(scenarios)} scenario(s); logs -> {log_dir}")
    if running_elevated():
        print("[Harness] WARNING: running elevated (Administrator). Typed input can land in "
              "elevated windows such as an Administrator PowerShell; run the harness from a "
              "normal, non-elevated shell.")
    print(f"[Harness] mode: {'STUB (scripted LLM, no backend)' if args.stub else 'REAL backend'}, "
          f"vision {'off' if args.no_vision else 'on'}, compare={args.compare}")
    if desktop and not args.yes:
        print("[Harness] About to drive the REAL desktop (apps, keyboard, files in %TEMP%). "
              "Don't touch the mouse/keyboard. Starting in 5s -- Ctrl+C to abort.")
        time.sleep(5)

    if desktop:
        # Build the Api before any log capture: its startup prints (e.g. the
        # API-key prefix from APIKeyManager) must not land in scenario logs.
        runner.api()

    records = []
    for sc in skipped_ui:
        records.append({"name": sc["name"], "type": "desktop", "mode": None, "status": "SKIP",
                        "failures": ["tagged ui_input; pass --allow-ui-input to run"]})
    try:
        if args.compare:
            modes = COMPARE_MODES
        elif args.mode:
            modes = (args.mode,)
        else:
            modes = (None,)
        for sc in scenarios:
            # Modes interleave within each round so backend-latency drift
            # spreads across modes instead of landing on one of them.
            for rep in range(1, args.repeat + 1):
                tag = rep if args.repeat > 1 else None
                if sc["type"] == "plan":
                    records.append(runner.run_plan(sc, tag))
                elif sc in desktop:
                    for m in modes:
                        record, stuck = runner.run_desktop(sc, m, tag)
                        records.append(record)
                        if stuck:
                            print("[Harness] Agent did not stop after a timeout -- aborting the run.")
                            raise SystemExit(2)
    finally:
        check_secrets_across_logs(records, log_dir)
        with open(os.path.join(log_dir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2)
        summary = format_summary(records, args.compare)
        with open(os.path.join(log_dir, "summary.txt"), "w", encoding="utf-8") as f:
            f.write(summary + "\n")
        print("\n" + summary)
        print(f"[Harness] Logs: {log_dir}")
        for fn in reversed(undo):
            fn()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            remove_scenario_dir(run_tmp)
        except Exception as e:
            print(f"[Harness] Could not remove {run_tmp}: {e}")
    return 0 if all(r["status"] in ("PASS", "SKIP") for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
