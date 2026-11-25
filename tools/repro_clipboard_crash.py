"""
tools/repro_clipboard_crash.py -- LIVE repro for the 0xC0000374 heap
corruption seen in type_text -> set_clipboard -> live_state._set while
Calculator was the target.

    python tools/repro_clipboard_crash.py --yes                 # all variants, 200 each
    python tools/repro_clipboard_crash.py --yes --n 50 --variants events-tk,noevents-tk

REAL desktop run: it opens Calculator and types "37" into it through the real
type_text path (Api._act_type_text: clipboard save, stage, Ctrl+A, Delete,
Ctrl+V, restore). Input only ever goes to Calculator (the input-target guard).
Close anything private first and leave mouse and keyboard alone while it runs.

Each variant runs in its own child process (a native crash kills only that
child) with faulthandler writing to ~/MistAI/logs/crash.log; the parent reads
that child's section of crash.log to tell whether it crashed. Variants:

    events-tk          window events on, the click indicator's Tk thread on
    noevents-tk        MISTAI_WINDOW_EVENTS=0, Tk on
    events-notk        window events on, no Tk thread
    events-tk-clicks   as events-tk, plus a uia_click on a Calculator key
                       before each type (closest to the failing scenario)

Each child also counts calls that reach the SHARED ctypes.windll
GlobalAlloc / GlobalLock / GlobalUnlock / GlobalFree / SetClipboardData
(pyreadline3, imported via pyautogui -> pyrect -> doctest -> pdb ->
rlcompleter, retypes them; GlobalUnlock gets argtypes [c_int], which
truncates a 64-bit handle).

Safety and cleanup: refuses when Calculator is already running or the
clipboard holds non-text data; restores the clipboard text after every
variant (a copy is kept in a temp folder until the end); closes only the
Calculator it started; memory, task and tool databases go to that temp
folder (MISTAI_DB_PATH); removes the temp folder. Output is counts and
labels only (no clipboard or screen text).
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

VARIANTS = {
    "events-tk": {"events": True, "tk": True, "clicks": False},
    "noevents-tk": {"events": False, "tk": True, "clicks": False},
    "events-notk": {"events": True, "tk": False, "clicks": False},
    "events-tk-clicks": {"events": True, "tk": True, "clicks": True},
}
CALC_PROCESSES = {"calculatorapp", "calculator"}
SHARED_WATCH = {"kernel32": ("GlobalAlloc", "GlobalLock", "GlobalUnlock", "GlobalFree"),
                "user32": ("SetClipboardData",)}
CHILD_TIMEOUT_S = 900


# ============================================================
# CALCULATOR PROCESSES
# ============================================================

def calculator_pids():
    import psutil

    out = set()
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").lower().replace(".exe", "")
        if name in CALC_PROCESSES:
            out.add(p.pid)
    return out


def close_calculators(keep):
    import psutil

    closed = 0
    for pid in calculator_pids() - set(keep):
        try:
            psutil.Process(pid).kill()
            closed += 1
        except Exception:
            pass
    return closed


# ============================================================
# CLIPBOARD SAVE AND RESTORE
# ============================================================

def clipboard_snapshot():
    """('text', str) | ('empty', None) | ('other', n) -- never logged."""
    import win32clipboard as w

    for _ in range(50):
        try:
            w.OpenClipboard()
            break
        except Exception:
            time.sleep(0.02)
    else:
        raise SystemExit("[Repro] the clipboard is busy; not starting")
    try:
        if w.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
            return "text", str(w.GetClipboardData(w.CF_UNICODETEXT))
        n, fmt = 0, w.EnumClipboardFormats(0)
        while fmt:
            n, fmt = n + 1, w.EnumClipboardFormats(fmt)
        return ("other", n) if n else ("empty", None)
    finally:
        w.CloseClipboard()


def clipboard_restore(kind, text):
    from core import live_state

    if kind == "text":
        return live_state.set_text(text)
    import win32clipboard as w

    w.OpenClipboard()
    try:
        w.EmptyClipboard()
    finally:
        w.CloseClipboard()
    return True


# ============================================================
# CRASH LOG SECTIONS
# ============================================================

def crash_section(pid):
    """The crash.log lines written by process ``pid`` (its start line on)."""
    from core.config import crash_log_dir

    path = os.path.join(crash_log_dir(), "crash.log")
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    start = next((i for i, l in enumerate(lines)
                  if l.startswith(f"--- process {pid} started")), None)
    if start is None:
        return []
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("--- process ")),
               len(lines))
    return lines[start:end]


# ============================================================
# CHILD: ONE VARIANT
# ============================================================

def _watch_shared(counts):
    """Count calls that reach the shared ctypes.windll functions above."""
    import ctypes

    for dll, names in SHARED_WATCH.items():
        lib = getattr(ctypes.windll, dll)
        for name in names:
            real = getattr(lib, name)
            counts[name] = 0

            def wrapper(*a, _real=real, _name=name):
                counts[_name] += 1
                return _real(*a)
            setattr(lib, name, wrapper)


def run_child(name, n):
    from core.config import enable_crash_log

    enable_crash_log()
    v = VARIANTS[name]
    shared = {}
    from core.api import Api
    import pyautogui

    _watch_shared(shared)
    api = Api()
    api.speak = lambda *a, **k: None
    api.speak_now = api.speak
    indicator = None
    if v["tk"]:
        from core.caption import get_indicator

        indicator = get_indicator()
    from core.config import window_events_enabled

    print(f"[Repro] {name}: window events {'on' if window_events_enabled() else 'off'}, "
          f"tk {'on' if indicator is not None and indicator.root is not None else 'off'}",
          flush=True)
    if not api._exec_sync("open_app", "calculator"):
        print(f"[Repro] {name}: Calculator did not open", flush=True)
        os._exit(3)
    time.sleep(1.5)
    sent = refused = clicks = 0
    for i in range(n):
        if v["clicks"]:
            clicks += bool(api._exec_sync("uia_click", str(i % 10)))
        if api._act_type_text("37"):
            sent += 1
        else:
            refused += 1
        if indicator is not None and i % 10 == 0:
            indicator.show(10 + i % 100, 10, method="UIA", label="")
        if i % 25 == 24:
            pyautogui.press("escape")
        if (i + 1) % 50 == 0:
            print(f"[Repro] {name}: {i + 1} iterations", flush=True)
    print(f"[Repro] {name}: done {n} iterations, sent={sent} refused={refused}"
          + (f" clicks_ok={clicks}" if v["clicks"] else "")
          + " shared calls: " + ", ".join(f"{k}={c}" for k, c in shared.items()), flush=True)
    os._exit(0)


# ============================================================
# PARENT: ALL VARIANTS
# ============================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="confirm a REAL desktop run")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--child", default="")
    args = ap.parse_args(argv)
    if args.child:
        run_child(args.child, args.n)
        return 0
    names = [s.strip() for s in args.variants.split(",") if s.strip()]
    unknown = [s for s in names if s not in VARIANTS]
    if unknown:
        print(f"[Repro] unknown variant(s): {', '.join(unknown)}")
        return 2
    if not args.yes:
        print("[Repro] REAL desktop run (opens Calculator and types into it). "
              "Pass --yes to run it.")
        return 2
    before = calculator_pids()
    if before:
        print("[Repro] Calculator is already running; close it first.")
        return 2
    kind, value = clipboard_snapshot()
    if kind == "other":
        print("[Repro] the clipboard holds non-text data; not starting (it could not be restored).")
        return 2
    tmp = tempfile.mkdtemp(prefix="mist_repro_")
    with open(os.path.join(tmp, "clipboard.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(value or "")
    results = []
    try:
        for name in names:
            env = dict(os.environ)
            env.pop("MISTAI_VERBOSE_LOGS", None)
            env["MISTAI_DB_PATH"] = os.path.join(tmp, f"{name}.db")
            env["MISTAI_WINDOW_EVENTS"] = "1" if VARIANTS[name]["events"] else "0"
            out_path = os.path.join(tmp, f"{name}.log")
            t0 = time.monotonic()
            with open(out_path, "w", encoding="utf-8") as out:
                proc = subprocess.Popen(
                    [sys.executable, "-X", "faulthandler", os.path.abspath(__file__),
                     "--child", name, "--n", str(args.n)],
                    cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
                try:
                    code = proc.wait(timeout=CHILD_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    code = "timeout"
            section = crash_section(proc.pid)
            fatal = next((l for l in section if l.startswith("Windows fatal exception")), "")
            repro = [l for l in open(out_path, encoding="utf-8", errors="replace")
                     if l.startswith("[Repro]")]
            closed = close_calculators(before)
            restored = clipboard_restore(kind, value)
            results.append((name, code, fatal, repro[-1].strip() if repro else "", closed,
                            restored, time.monotonic() - t0, section))
            print(f"[Repro] {name}: exit {code}, "
                  f"{'CRASHED (' + fatal + ')' if fatal else 'no crash'}, "
                  f"{time.monotonic() - t0:.0f}s", flush=True)
    finally:
        close_calculators(before)
        clipboard_restore(kind, value)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nvariant            | result")
    print("-" * 78)
    for name, code, fatal, last, closed, restored, secs, section in results:
        print(f"{name:<18} | {'CRASH ' + fatal.split(':')[-1].strip() if fatal else 'ok':<20}"
              f" exit={code} {secs:.0f}s calc_closed={closed} clipboard_restored={restored}")
        if last:
            print(f"{'':<18} |   {last}")
        if fatal:
            frames = [l.strip() for l in section if l.strip().startswith("File ")][:6]
            for fr in frames:
                print(f"{'':<18} |   {fr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
