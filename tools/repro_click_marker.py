"""
tools/repro_click_marker.py -- LIVE check that uia_click reaches the app with
the click indicator ON.

    python tools/repro_click_marker.py --yes            # 20 clicks
    python tools/repro_click_marker.py --yes --n 50

REAL desktop run: opens Calculator and presses digit keys through the real
path (Executor.run -> Api._act_uia_click: UIA Invoke first, the debug marker
after the action). Before the fix the marker's dot sat on the click point and
swallowed the click: uia_click reported success while the display stayed 0
and MistAI's Tk window took the foreground.

Per click: Clear, then uia_click on a digit 1-9 (0 would leave the display
at 0), then read Calculator's display through UIA. PASS = every display shows
the pressed digit, no MistAI window was ever in front, and no click came back
as "UIA-unchanged". Refuses when Calculator is already running; closes only
the Calculator it opened; databases go to a temp folder that is removed.
Output: digits, labels and counts only.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def calculator_display():
    from pywinauto import Desktop

    try:
        w = Desktop(backend="uia").window(title="Calculator")
        el = w.child_window(auto_id="CalculatorResults").wrapper_object()
        return el.window_text().replace("Display is", "").strip()
    except Exception as e:
        return f"? ({type(e).__name__})"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--yes", action="store_true", help="confirm a REAL desktop run")
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args(argv)
    if not args.yes:
        print("[ClickMarker] REAL desktop run (opens Calculator and clicks in it). "
              "Pass --yes to run it.")
        return 2
    from repro_clipboard_crash import calculator_pids, close_calculators

    before = calculator_pids()
    if before:
        print("[ClickMarker] Calculator is already running; close it first.")
        return 2
    tmp = tempfile.mkdtemp(prefix="mist_marker_")
    os.environ["MISTAI_DB_PATH"] = os.path.join(tmp, "marker.db")
    os.environ.pop("MISTAI_VERBOSE_LOGS", None)
    passed, rows, api = 0, [], None
    try:
        import win32gui
        import win32process

        from core.api import Api
        from core.caption import get_indicator

        api = Api()
        api.speak = lambda *a, **k: None
        indicator = get_indicator()
        print(f"[ClickMarker] indicator on: {indicator.root is not None}, "
              f"marker ex-style {indicator.ex_style or 0:#010x}", flush=True)
        if not api._exec_sync("open_app", "calculator"):
            print("[ClickMarker] Calculator did not open")
            return 1
        time.sleep(1.5)
        own = os.getpid()
        import pyautogui

        for i in range(args.n):
            digit = str(i % 9 + 1)
            # Clear with Escape (Calculator stays in front: Invoke never moves
            # focus). Not uia_click("Clear"): its partial name match can pick
            # another "Clear ..." button (e.g. Clear all memory).
            pyautogui.press("escape")
            time.sleep(0.3)
            if calculator_display() != "0":
                print(f"[ClickMarker] {i + 1:>2}: the display did not clear; stopping")
                break
            res = api.executor.run("uia_click", digit)
            fg_own = []
            for _ in range(8):                     # 0.4 s: is a MistAI window ever in front?
                h = win32gui.GetForegroundWindow()
                fg_own.append(bool(h) and win32process.GetWindowThreadProcessId(h)[1] == own)
                time.sleep(0.05)
            shown = calculator_display()
            ok = shown == digit and not any(fg_own) and res.success and res.method != "UIA-unchanged"
            passed += ok
            rows.append(ok)
            print(f"[ClickMarker] {i + 1:>2}: pressed {digit} -> display {shown!r}, "
                  f"method {res.method}, own window in front: {'yes' if any(fg_own) else 'no'}"
                  f" -> {'PASS' if ok else 'FAIL'}", flush=True)
    finally:
        close_calculators(before)
        # The Api keeps its SQLite files open: close them, or the folder stays.
        for owner in ([getattr(api, n, None) for n in ("memory", "task_memory", "task_store",
                                                       "tools")] if api is not None else []):
            for attr in ("conn", "_conn"):
                conn = getattr(owner, attr, None)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.isdir(tmp):
            print(f"[ClickMarker] could not remove the temp folder {tmp}")
    print(f"[ClickMarker] {passed}/{args.n} clicks reached Calculator with the marker on")
    return 0 if passed == args.n else 1


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(main())
