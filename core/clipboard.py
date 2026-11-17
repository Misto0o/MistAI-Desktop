"""core/clipboard.py -- the one place MistAI opens the Windows clipboard.

Every open goes through `lock`, so the live-state watcher, get/set_clipboard
and type_text's stage/paste/restore never overlap inside MistAI. The watcher
uses `peek_text`, which opens the clipboard only when
GetClipboardSequenceNumber changed. Logs here are content-free (a fixed label
only: never process names, titles or clipboard text).
"""
import ctypes
import os
import random
import threading
import time

lock = threading.RLock()
_ours_open = 0               # >0 while one of our own helpers has it open
_cache = ""                  # last text seen or set (first 300 chars)
_cache_seq = None            # sequence number that `_cache` belongs to

RETRY_BUDGET_S = 1.5         # total waiting time while another app holds it


def _seq():
    try:
        return ctypes.windll.user32.GetClipboardSequenceNumber()
    except Exception:
        return None


def holder():
    """Who holds the clipboard right now: self | other | unknown."""
    if _ours_open > 0:
        return "self"
    try:
        hwnd = ctypes.windll.user32.GetOpenClipboardWindow()
        if not hwnd:
            return "unknown"      # not open, or opened with no window handle
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return "self" if pid.value == os.getpid() else "other"
    except Exception:
        return "unknown"


def retry_delays(budget=RETRY_BUDGET_S):
    """Short jittered waits (~50 ms growing to ~300 ms) summing to <= budget."""
    out, total, base = [], 0.0, 0.05
    while True:
        d = base * random.uniform(0.7, 1.3)
        if total + d > budget:
            return out
        out.append(d)
        total += d
        base = min(base + 0.05, 0.3)


def _open_and(fn):
    """Open, run fn(win32clipboard), always close. Raises if it can't open."""
    global _ours_open
    import win32clipboard
    with lock:
        win32clipboard.OpenClipboard()
        _ours_open += 1
        try:
            return fn(win32clipboard)
        finally:
            _ours_open -= 1
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


def _text_of(w):
    if w.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
        return str(w.GetClipboardData(w.CF_UNICODETEXT))
    return ""


def read_text():
    """The whole clipboard text ("" when none), or None if it can't be read."""
    try:
        return _open_and(_text_of)
    except Exception:
        return None


def set_text(text):
    """Put text on the clipboard. False if it could not be opened or set;
    logs `[TypeText] clipboard busy holder=...` on failure."""
    global _cache, _cache_seq

    def _set(w):
        w.EmptyClipboard()
        w.SetClipboardData(w.CF_UNICODETEXT, text)

    try:
        _open_and(_set)
    except Exception:
        print(f"[TypeText] clipboard busy holder={holder()}")
        return False
    # Our own write must not make the watcher reopen the clipboard mid-paste.
    _cache, _cache_seq = (text or "")[:300], _seq()
    return True


def peek_text(limit=300):
    """Watcher read: reopen the clipboard only if its sequence number moved."""
    global _cache, _cache_seq
    seq = _seq()
    if seq is not None and seq == _cache_seq:
        return _cache[:limit]
    text = read_text()
    if text is None:
        return _cache[:limit]         # busy: keep the old value, retry next poll
    _cache, _cache_seq = text[:300], seq
    return _cache[:limit]


# -- keyboard fallback ------------------------------------------------------
SEND_MAX_CHARS = 200


def can_send_as_keys(text):
    return bool(text) and len(text) <= SEND_MAX_CHARS and "\n" not in text and "\r" not in text


class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MI(ctypes.Structure):      # only here so the union has the right size
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def send_unicode(text):
    """Type text with SendInput KEYEVENTF_UNICODE events. Never sends Enter
    (callers pass no newlines). True if every event was accepted."""
    units = text.encode("utf-16-le")
    codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    events = []
    for c in codes:
        for flags in (0x0004, 0x0004 | 0x0002):      # UNICODE, UNICODE|KEYUP
            events.append(_IN(type=1, ki=_KI(0, c, flags, 0, 0)))
    arr = (_IN * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_IN))
    time.sleep(0.05)
    return sent == len(events)
