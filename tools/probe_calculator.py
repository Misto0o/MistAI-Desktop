"""
tools/probe_calculator.py -- what does Calculator's display REALLY show after
each way the agent can enter 37*23? Local only: no LLM, no Cohere, no network.

    python tools/probe_calculator.py          # 3s "hands off" countdown
    python tools/probe_calculator.py --yes    # no countdown

- Exits without touching anything if Calculator is already running.
- Launches Calculator with the real open_app path and enters each variant with
  the real type_text / press_key handlers (Api._exec_sync):
    (a) type_text('37*23')            (b) type_text('37*23=')
    (c) type_text('37*23') + Enter    (d) press_key 3,7,*,2,3,=
- Before every variant, checks that Calculator is the foreground window and
  aborts WITHOUT sending input if it is not.
- Reads the display ~0.3s, ~2s and ~6s after each variant (real-run vision
  reads land 4-6s after typing) with the harness's own UIA reader
  (run_scenarios.window_texts) plus every Text element's automation id, name
  and value. Prints a table; no pass/fail.
- Clears with Escape between variants, restores the clipboard text, and closes
  only the Calculator process(es) it started.
Every non-loopback socket connection and DNS lookup is refused for the whole
run, so nothing can leave the machine even by accident.
"""

import argparse
import ipaddress
import os
import socket
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.run_scenarios import (  # noqa: E402
    close_new_processes,
    disable_vision,
    pids_named,
    wait_pumping,
    window_texts,
)

APP = "calculator"
READ_TIMES = (0.3, 2.0, 6.0)
VARIANTS = (
    ("a", "type_text('37*23')", [("type_text", "37*23")]),
    ("b", "type_text('37*23=')", [("type_text", "37*23=")]),
    ("c", "type_text('37*23') + Enter", [("type_text", "37*23"), ("press_key", "enter")]),
    ("d", "press_key 3,7,*,2,3,=", [("press_key", k) for k in "37*23="]),
)


# -- Network fence -------------------------------------------------------------------


def _is_loopback(host):
    host = str(host or "").strip("[]").lower()
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def block_network():
    """Refuse every non-loopback connect and DNS lookup (requests, httpx,
    aiohttp, edge-tts all end up here). Returns an undo function."""
    saved = (socket.socket.connect, socket.socket.connect_ex,
             socket.create_connection, socket.getaddrinfo)

    def _check(address):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            raise OSError(f"probe_calculator blocked a network connection to {host}")

    def connect(self, address):
        _check(address)
        return saved[0](self, address)

    def connect_ex(self, address):
        _check(address)
        return saved[1](self, address)

    def create_connection(address, *a, **k):
        _check(address)
        return saved[2](address, *a, **k)

    def getaddrinfo(host, *a, **k):
        _check((host,))
        return saved[3](host, *a, **k)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.create_connection = create_connection
    socket.getaddrinfo = getaddrinfo

    def undo():
        (socket.socket.connect, socket.socket.connect_ex,
         socket.create_connection, socket.getaddrinfo) = saved

    return undo


# -- Clipboard (full text; Api.get_clipboard truncates) -------------------------------


def read_clipboard():
    """Full clipboard text, or None if there is none / it can't be read."""
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
            return None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


def restore_clipboard(api, text):
    if text is not None:
        api.set_clipboard(text)


# -- Reading Calculator ---------------------------------------------------------------


def calculator_elements():
    """[(automation_id, control_type, name, value)] for every Text element of
    Calculator's window(s), or None if UIA failed."""
    try:
        from pywinauto import Desktop

        pids = pids_named([APP])
        out = []
        for w in Desktop(backend="uia").windows():
            try:
                if w.process_id() not in pids and not (
                    w.element_info.class_name == "ApplicationFrameWindow"
                    and any(c.process_id() in pids for c in w.children())
                ):
                    continue
                for d in w.descendants():
                    info = d.element_info
                    if info.control_type != "Text":
                        continue
                    try:
                        value = str(d.iface_value.CurrentValue)
                    except Exception:
                        value = ""
                    out.append((info.automation_id or "", info.control_type,
                                d.window_text(), value))
            except Exception:
                continue
        return out
    except Exception:
        return None


def read_once():
    elements = calculator_elements()
    by_id = {aid: name for aid, _, name, _ in elements or [] if aid}
    harness = window_texts(APP)
    return {
        "display": by_id.get("CalculatorResults", "?"),
        "expression": by_id.get("CalculatorExpression", "?"),
        "harness_has_851": None if harness is None else any("851" in t for t in harness),
        "harness": harness,
        "elements": elements,
    }


def foreground_title():
    try:
        import win32gui

        return win32gui.GetWindowText(win32gui.GetForegroundWindow())
    except Exception:
        return "?"


# -- Probe ----------------------------------------------------------------------------


def run_variant(api, steps):
    """Run the variant's handler calls. Returns (reports, aborted_reason)."""
    reports = []
    for action, param in steps:
        if not api._foreground_process_matches(APP):
            return reports, f"Calculator not in front before {action}({param!r}) " \
                            f"(foreground: {foreground_title()!r}); no input sent"
        clip = read_clipboard() if action == "type_text" else None
        try:
            ok = api._exec_sync(action, param)
        finally:
            if action == "type_text":
                restore_clipboard(api, clip)
        reports.append(f"{action}({param!r}) -> {ok}: {api.last_result_text}")
        api.last_result_text = ""
    return reports, None


def read_series():
    reads, started = [], time.monotonic()
    for t in READ_TIMES:
        wait_pumping(lambda: False, max(0.0, started + t - time.monotonic()))
        reads.append((t, read_once()))
    return reads


def clear(api):
    if not api._foreground_process_matches(APP):
        return f"not cleared: Calculator not in front ({foreground_title()!r})"
    api._exec_sync("press_key", "escape")
    api.last_result_text = ""
    wait_pumping(lambda: False, 0.5)
    return read_once()["display"]


def print_results(results):
    print("\n=== Calculator probe results " + "=" * 50)
    for key, label, reports, aborted, cleared, reads in results:
        print(f"\n({key}) {label}")
        print(f"    cleared before: {cleared}")
        for r in reports:
            print(f"    handler: {r}")
        if aborted:
            print(f"    ABORTED: {aborted}")
            continue
        print(f"    {'t':>5} | {'display':<26} | {'expression':<30} | harness has 851")
        for t, r in reads:
            print(f"    {t:>4.1f}s | {r['display'][:26]:<26} | {r['expression'][:30]:<30} | "
                  f"{r['harness_has_851']}")
        displays = [r["display"] for _, r in reads]
        if len(set(displays)) > 1:
            print(f"    CHANGED between reads: {' -> '.join(displays)}")
    print("\n=== Raw Text elements at the last read of each variant " + "=" * 23)
    for key, label, _, aborted, _, reads in results:
        if aborted or not reads:
            continue
        last = reads[-1][1]
        print(f"\n({key}) {label}")
        print(f"    harness window_texts: {last['harness']}")
        for aid, ctype, name, value in last["elements"] or []:
            print(f"    {ctype:<5} id={aid!r:<24} name={name!r} value={value!r}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--yes", action="store_true", help="skip the 3s countdown")
    args = ap.parse_args(argv)

    if pids_named([APP]):
        print("Calculator is already running; not touching it. Close it and re-run.")
        return 0

    undo = [block_network(), disable_vision()]
    os.environ["MISTAI_DB_PATH"] = os.path.join(
        tempfile.mkdtemp(prefix="mist_probe_"), "memory.db")
    before = pids_named([APP])
    results = []
    try:
        if not args.yes:
            print("About to open Calculator and type into it. Hands off mouse and "
                  "keyboard. Starting in 3s -- Ctrl+C to abort.")
            time.sleep(3)
        from core.api import Api

        api = Api()
        api.speak = lambda *a, **k: None
        api.speak_now = api.speak
        if not api._exec_sync("open_app", APP):
            print(f"open_app failed: {api.last_result_text}")
            return 1
        print(f"open_app: {api.last_result_text}")
        api.last_result_text = ""
        wait_pumping(lambda: False, 1.0)
        for key, label, steps in VARIANTS:
            cleared = clear(api)
            reports, aborted = run_variant(api, steps)
            reads = [] if aborted else read_series()
            results.append((key, label, reports, aborted, cleared, reads))
            if aborted:
                print(f"({key}) aborted: {aborted}")
                break
        print_results(results)
        return 0
    finally:
        closed = close_new_processes([APP], before)
        if closed:
            print(f"\nClosed the Calculator process(es) this probe started: {closed}")
        for fn in reversed(undo):
            fn()


if __name__ == "__main__":
    raise SystemExit(main())
