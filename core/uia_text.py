"""
core/uia_text.py — exact on-screen text from the accessibility tree.

read_screen_text puts this next to the vision answer in native windows: UIA
reports what a control actually contains (a Calculator display, an Edit box),
where vision has been seen to report a value that was never displayed.

Password fields are skipped, and a snapshot that looks like it holds a secret
(private key block, credential-looking token) is withheld entirely. A process
whose walk ran past the budget once is skipped for the rest of the session.
"""

import re
import threading
import time

from .config import secret_text_reason, verbose_logs

TEXT_CONTROL_TYPES = ("Text", "Edit", "Document")
TEXT_CAP = 300  # chars foreground_uia_text returns by default
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


# Questions about how things look, not what text they hold: a screenshot is
# needed. General words only, never app names.
_VISUAL_RX = re.compile(
    r"\b(colou?rs?|red|green|blue|yellow|orange|purple|pink|gr[ae]y|images?|pictures?|"
    r"photos?|icons?|logos?|thumbnails?|layout|positions?|arranged|aligned|highlighted|"
    r"bold|italic|underlined|fonts?|shapes?|charts?|graphs?|diagrams?|maps?|"
    r"looks? like|appearance|screenshot|where on)\b", re.I)


def is_visual_question(query):
    """True when the question is empty (a general description) or about
    something visual (colours, images, layout...)."""
    q = str(query or "").strip()
    return not q or bool(_VISUAL_RX.search(q))


def text_is_sufficient(text, title="", cap=TEXT_CAP):
    """UIA text that can answer a read on its own: it holds at least one
    element besides the window's own title, and the walk was not cut at
    ``cap`` (a cut walk may have dropped what the question is about)."""
    text = str(text or "")
    if not text.strip() or len(text) >= cap - 3:
        return False
    own = " ".join(str(title or "").split()).lower()
    return any(p.strip() and p.strip().lower() != own for p in text.split(" | "))


def foreground_uia_text(hwnd=None, budget=1.5, cap=TEXT_CAP):
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
        whose = f"'{title[:60]}'" if verbose_logs() else "the window"  # a title is content
        print(f"[UIA] withheld the text of {whose}: {reason}")
        return "", title
    return text, title
