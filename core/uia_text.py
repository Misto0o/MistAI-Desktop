"""
core/uia_text.py — exact on-screen text from the accessibility tree.

read_screen_text puts this next to the vision answer in native windows: UIA
reports what a control actually contains (a Calculator display, an Edit box),
where vision has been seen to report a value that was never displayed.

Password fields are skipped, and a snapshot that looks like it holds a secret
(private key block, credential-looking token) is withheld entirely. A process
whose walk ran past the budget once is skipped for the rest of the session.
"""

import threading
import time

from .config import secret_text_reason

TEXT_CONTROL_TYPES = ("Text", "Edit", "Document")
UIA_VALUE_PROPERTY_ID = 30045  # UIA_ValueValuePropertyId
UIA_IS_PASSWORD_PROPERTY_ID = 30019  # UIA_IsPasswordPropertyId
MAX_ELEMENTS = 2000

_slow_processes = set()  # process names whose walk timed out this session
_slow_lock = threading.Lock()


def _is_password(info):
    try:
        return bool(info.element.GetCurrentPropertyValue(UIA_IS_PASSWORD_PROPERTY_ID))
    except Exception:
        return False


def _walk_text(hwnd, deadline, cap):
    """(text, timed_out): names/values of the Text/Edit/Document elements
    under ``hwnd`` in tree order, deduplicated, joined with ' | ' and cut to
    ``cap`` chars. Stops early at ``cap``; timed_out = the deadline arrived
    with elements still unvisited (a big window like VS Code)."""
    from pywinauto.uia_element_info import UIAElementInfo

    parts, seen, total, visited = [], set(), 0, 0
    queue = [UIAElementInfo(hwnd)]
    timed_out = False
    while queue and total < cap and visited < MAX_ELEMENTS:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        info = queue.pop(0)
        visited += 1
        try:
            queue.extend(info.children())
        except Exception:
            pass
        try:
            if info.control_type not in TEXT_CONTROL_TYPES or _is_password(info):
                continue
            texts = [info.name or ""]
            try:
                texts.append(str(info.element.GetCurrentPropertyValue(UIA_VALUE_PROPERTY_ID) or ""))
            except Exception:
                pass
        except Exception:
            continue
        for t in texts:
            t = " ".join(t.split())
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
                total += len(t) + 3
    return " | ".join(parts)[:cap], timed_out


def _process_name(hwnd):
    try:
        import psutil
        import win32process

        return psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name()
    except Exception:
        return ""


def _mark_slow(proc):
    if not proc:
        return
    with _slow_lock:
        if proc in _slow_processes:
            return
        _slow_processes.add(proc)
    print(f"[UIA] skipping {proc}: too slow")


def foreground_uia_text(hwnd=None, budget=1.5, cap=300):
    """(text, title) for the foreground window (or ``hwnd``); text is "" when
    UIA found nothing, ran past ``budget`` seconds, the process is on the
    too-slow list, or the text looks like it holds a secret. Never raises."""
    title = ""
    try:
        import win32gui

        hwnd = hwnd or win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return "", title
    if not hwnd:
        return "", title
    proc = _process_name(hwnd)
    with _slow_lock:
        if proc and proc in _slow_processes:
            return "", title
    box = []

    def run():
        try:
            box.append(_walk_text(hwnd, time.monotonic() + budget, cap))
        except Exception:
            box.append(("", False))

    # ponytail: an over-budget walk is abandoned, not cancelled (UIA can't be
    # interrupted); it stops at its own deadline and its result is dropped.
    t = threading.Thread(target=run, daemon=True, name="UIAText")
    t.start()
    t.join(budget + 0.25)
    if not box or box[0][1]:
        _mark_slow(proc)
        return "", title
    text = box[0][0]
    reason = secret_text_reason(text)
    if reason:
        print(f"[UIA] withheld the text of '{title[:60]}': {reason}")
        return "", title
    return text, title
