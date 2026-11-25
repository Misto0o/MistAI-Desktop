# ============================================================
# LIVE STATE MODULE
# ============================================================
#
# Background desktop state: the event bus, the one owner of the Windows
# clipboard, the live state tracker (foreground window, apps, CPU/RAM) and
# the watchers that turn state and file changes into events.
#
# SECTION INDEX
#   1. EVENT BUS
#   2. EVENT NAMES
#   3. EVENT RECORD
#   4. EVENT DISPATCHER
#   5. CLIPBOARD OWNER
#   6. CLIPBOARD LOCK AND CACHE
#   7. CLIPBOARD HOLDER LABEL
#   8. CLIPBOARD RETRY DELAYS
#   9. OPENING THE CLIPBOARD
#   10. CLIPBOARD TEXT DECODING
#   11. CLIPBOARD READ
#   12. CLIPBOARD WRITE
#   13. CLIPBOARD CIRCUIT BREAKER
#   14. CLIPBOARD PEEK
#   15. UNICODE KEYSTROKE LIMITS
#   16. SENDINPUT KEYBOARD STRUCT
#   17. SENDINPUT MOUSE STRUCT
#   18. SENDINPUT INPUT STRUCT
#   19. UNICODE KEYSTROKE TYPING
#   20. OPEN APPS DESCRIPTION
#   21. WINDOW OWNERSHIP
#   22. UWP FRAME APPS
#   23. VISIBLE WINDOW PROCESSES
#   24. WINDOW EVENTS
#   25. WINDOW EVENT SETTINGS
#   26. WINDOW EVENT STATE
#   27. WINEVENT HOOK
#   28. PLAYWRIGHT BROWSER WINDOW
#   29. WINDOW SETTLE
#   30. LIVE STATE TRACKER
#   31. WATCHERS
#   32. OPTIONAL FILE WATCHING LIBRARY
#   33. STATE WATCHER
#   34. FILE EVENT HANDLER
#   35. FILE WATCHER
#   36. WINDOW IN FRONT FOR READING
# ============================================================

"""
core/live_state.py — tracks foreground window, open apps, clipboard, CPU/RAM.
"""

import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from queue import Empty, Queue
import ctypes
import os
import random

import psutil
import pyautogui

from . import live_state as clipboard, live_state as events
from .config import (
    window_events_enabled,
    TRACKED_APPS,
    TRACKED_PROCESS_NAMES,
    is_self_process,
    normalize_process_name,
    process_matches,
    log_text,
    verbose_logs,
)


# ============================================================
# EVENT BUS
# ============================================================

# core/events.py — internal event bus (design doc #4).
#
# Components publish and subscribe to named events instead of polling each
# other's state. Dispatch happens on ONE dedicated thread so:
# - publish() never blocks the caller (it's just a queue put)
# - a crashing subscriber can never take down the publisher
# - event ordering is deterministic
#
# Rules for subscribers: callbacks must be FAST (<~10ms). Anything heavy
# (vision calls, disk walks) should hand off to its own thread.
#
# Event names are plain strings; the canonical set lives below so every
# name is grep-able. Unknown names are allowed (forward compatible) --
# USBInserted / NetworkConnected are reserved here even though no watcher
# emits them yet.

# ============================================================
# EVENT NAMES
# ============================================================

WINDOW_FOCUSED = "WindowFocused"
WINDOW_OPENED = "WindowOpened"
WINDOW_CLOSED = "WindowClosed"

FILE_CREATED = "FileCreated"
FILE_MODIFIED = "FileModified"
FILE_DELETED = "FileDeleted"
DOWNLOAD_COMPLETED = "DownloadCompleted"

CLIPBOARD_CHANGED = "ClipboardChanged"
PROCESS_STARTED = "ProcessStarted"
PROCESS_EXITED = "ProcessExited"

ACTION_EXECUTED = "ActionExecuted"
ACTION_FAILED = "ActionFailed"
# AgentLoop: {"action"} just before a step runs; {"action": "done", "speech"}
# for the final answer; {"action": "failed"} when the agent call fails (the network
# sentence). For the floating orb and its panels only; never logged.
AGENT_STEP = "AgentStep"
# core/floating_orb.py orb_signal: {"signal": started|finished|planning|reading|
# waiting, "on", ...} from PlanRunner, the loop and Api; every task, one-node
# ones too. Only while the floating orb is shown; for it and its panels only;
# never logged.
ORB_SIGNAL = "OrbSignal"

TASK_STARTED = "TaskStarted"
TASK_PHASE_DONE = "TaskPhaseDone"
TASK_COMPLETED = "TaskCompleted"
TASK_FAILED = "TaskFailed"
TASK_PAUSED = "TaskPaused"

PROJECT_SET = "ProjectSet"

USB_INSERTED = "USBInserted"            # reserved -- no watcher yet
NETWORK_CONNECTED = "NetworkConnected"  # reserved -- no watcher yet


# ============================================================
# EVENT RECORD
# ============================================================

@dataclass
class Event:
    name: str
    data: dict = field(default_factory=dict)
    source: str = ""
    ts: float = field(default_factory=time.time)

    def __repr__(self):
        keys = ",".join(list(self.data.keys())[:4])
        return f"Event({self.name}, {{{keys}}}, src={self.source})"


# ============================================================
# EVENT DISPATCHER
# ============================================================

class EventBus:
    """Thread-safe pub/sub with a single dispatch thread and a small
    ring-buffer history for debugging ('what just happened?')."""

    def __init__(self, history=200):
        self._subs = {}     # event name -> list[callback]
        self._all = []      # wildcard subscribers
        self._lock = threading.RLock()
        self._q = Queue()
        self._history = deque(maxlen=history)
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True, name="EventBus")
        self._thread.start()

    # -- Subscribe -----------------------------------------------------------

    def subscribe(self, name, callback):
        """Subscribe to one event name. Returns an unsubscribe function."""
        with self._lock:
            self._subs.setdefault(name, []).append(callback)

        def _unsub():
            with self._lock:
                try:
                    self._subs.get(name, []).remove(callback)
                except ValueError:
                    pass
        return _unsub

    def subscribe_all(self, callback):
        """Subscribe to every event (loggers, debuggers)."""
        with self._lock:
            self._all.append(callback)

        def _unsub():
            with self._lock:
                try:
                    self._all.remove(callback)
                except ValueError:
                    pass
        return _unsub

    # -- Publish -------------------------------------------------------------

    def publish(self, name_or_event, data=None, source=""):
        """Non-blocking. Accepts either an Event or (name, data, source)."""
        if isinstance(name_or_event, Event):
            ev = name_or_event
        else:
            ev = Event(name_or_event, data or {}, source)
        self._q.put(ev)

    # -- Waiting (used by verification: 'did the process actually start?') ---

    def wait_for(self, name, timeout=5.0, predicate=None):
        """Block until an event with this name (matching predicate, if any)
        arrives, or timeout. Returns the Event or None."""
        got = threading.Event()
        holder = {}

        def _cb(ev):
            if predicate is None or predicate(ev):
                holder["ev"] = ev
                got.set()

        unsub = self.subscribe(name, _cb)
        try:
            got.wait(timeout=timeout)
            return holder.get("ev")
        finally:
            unsub()

    # -- Internals -----------------------------------------------------------

    def _pump(self):
        while self._running:
            try:
                ev = self._q.get(timeout=0.5)
            except Empty:
                continue
            self._history.append(ev)
            with self._lock:
                targets = list(self._subs.get(ev.name, [])) + list(self._all)
            for cb in targets:
                try:
                    cb(ev)
                except Exception as e:
                    print(f"[EventBus] Subscriber error on {ev.name}: {e}")

    def recent(self, n=20, name=None):
        evs = list(self._history)
        if name:
            evs = [e for e in evs if e.name == name]
        return evs[-n:]

    def stop(self):
        self._running = False


# ============================================================
# CLIPBOARD OWNER
# ============================================================

# core/clipboard.py -- the one place MistAI opens the Windows clipboard.
#
# Every open goes through `lock`, so the live-state watcher, get/set_clipboard
# and type_text's stage/paste/restore never overlap inside MistAI. The watcher
# uses `peek_text`, which opens the clipboard only when
# GetClipboardSequenceNumber changed. Logs here are content-free (a fixed label
# only: never process names, titles or clipboard text).

# ============================================================
# CLIPBOARD LOCK AND CACHE
# ============================================================

lock = threading.RLock()
_ours_open = 0               # >0 while one of our own helpers has it open
_cache = ""                  # last text seen or set (first 300 chars)
_cache_seq = None            # sequence number that `_cache` belongs to

RETRY_BUDGET_S = 1.5         # total waiting time while another app holds it
# OpenClipboard inside one helper: another process can hold the clipboard for
# a few ms (clipboard history, a viewer reacting to our last write), so the
# open is tried again after these short waits before the call reports busy.
OPEN_RETRY_DELAYS = (0.01, 0.02, 0.04)

_user32_typed = None


def _user32():
    """A private user32 with explicit 64-bit types for the clipboard helpers.
    Private (not ctypes.windll.user32, which every library shares and may
    retype): HWND/HANDLE results are pointer-sized, never the default c_int."""
    global _user32_typed
    if _user32_typed is None:
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetClipboardSequenceNumber.argtypes = []
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        u.GetOpenClipboardWindow.argtypes = []
        u.GetOpenClipboardWindow.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        _user32_typed = u
    return _user32_typed


_kernel32_typed = None


def _kernel32():
    """A private kernel32 with explicit 64-bit types for the clipboard write.
    Private for the same reason as _user32: pyreadline3 (imported through
    pyautogui -> pyrect -> doctest -> pdb -> rlcompleter -> readline) retypes
    the SHARED ctypes.windll.kernel32.GlobalUnlock to take a 32-bit int."""
    global _kernel32_typed
    if _kernel32_typed is None:
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.restype = wintypes.HGLOBAL
        _kernel32_typed = k
    return _kernel32_typed


def _seq():
    try:
        return _user32().GetClipboardSequenceNumber()
    except Exception:
        return None


# ============================================================
# CLIPBOARD HOLDER LABEL
# ============================================================

def holder():
    """Who holds the clipboard right now: self | other | unknown."""
    if _ours_open > 0:
        return "self"
    try:
        from ctypes import wintypes

        u = _user32()
        hwnd = u.GetOpenClipboardWindow()
        if not hwnd:
            return "unknown"      # not open, or opened with no window handle
        pid = wintypes.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return "self" if pid.value == os.getpid() else "other"
    except Exception:
        return "unknown"


# ============================================================
# CLIPBOARD RETRY DELAYS
# ============================================================

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


# ============================================================
# OPENING THE CLIPBOARD
# ============================================================

def _open(win32clipboard):
    """OpenClipboard, tried again after OPEN_RETRY_DELAYS while another
    process holds it; raises the last error if it never opens."""
    for delay in OPEN_RETRY_DELAYS + (None,):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            if delay is None:
                raise
            time.sleep(delay)


def _open_and(fn):
    """Open (with a short retry), run fn(win32clipboard), always close.
    Raises if it can't open. The memory behind SetClipboardData is pywin32's:
    it allocates the moveable global block and the clipboard owns it once
    the set succeeds; nothing here allocates or frees Windows memory."""
    global _ours_open
    import win32clipboard
    with lock:
        _open(win32clipboard)
        _ours_open += 1
        try:
            return fn(win32clipboard)
        finally:
            _ours_open -= 1
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass


# ============================================================
# CLIPBOARD TEXT DECODING
# ============================================================

def _text_of(w):
    if w.IsClipboardFormatAvailable(w.CF_UNICODETEXT):
        return str(w.GetClipboardData(w.CF_UNICODETEXT))
    return ""


# ============================================================
# CLIPBOARD READ
# ============================================================

def read_text():
    """The whole clipboard text ("" when none), or None if it can't be read."""
    try:
        return _open_and(_text_of)
    except Exception:
        return None


# ============================================================
# CLIPBOARD WRITE
# ============================================================

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _write_unicode(text):
    """SetClipboardData(CF_UNICODETEXT) with our own memory block; the
    clipboard must be open (and emptied) by this thread.

    Never pywin32's SetClipboardData with a str: pywin32 311 takes a str that
    int() accepts ("37", "3", " 007") as a HANDLE number, so typing digits
    (Calculator) handed Windows a bogus handle that the next write freed --
    heap corruption 0xC0000374 in _set ("12", "-5": 'The handle is invalid').

    Ownership: a moveable block of (UTF-16 units + 1) * 2 bytes, NUL
    included; unlocked before SetClipboardData; the system owns it once that
    succeeds (never freed then), freed here on any failure."""
    data = str(text or "").encode("utf-16-le") + b"\x00\x00"
    k, u = _kernel32(), _user32()
    handle = k.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    owned_by_clipboard = False
    try:
        ptr = k.GlobalLock(handle)
        if not ptr:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            k.GlobalUnlock(handle)
        if not u.SetClipboardData(CF_UNICODETEXT, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        owned_by_clipboard = True
    finally:
        if not owned_by_clipboard:
            k.GlobalFree(handle)


def set_text(text):
    """Put text on the clipboard. False if it could not be opened or set;
    logs `[TypeText] clipboard busy holder=...` on failure."""
    global _cache, _cache_seq

    opened = []

    def _set(w):
        opened.append(True)
        w.EmptyClipboard()
        _write_unicode(text)

    try:
        _open_and(_set)
    except Exception as e:
        # busy = it could not be opened (another holder); denied = opened but
        # the write was refused (e.g. a policy). Labels and the Win32 code only.
        code = getattr(e, "winerror", None)
        print(f"[TypeText] clipboard {'denied' if opened else 'busy'} holder={holder()}"
              + (f" code={code}" if isinstance(code, int) else ""))
        return False
    # Our own write must not make the watcher reopen the clipboard mid-paste.
    _cache, _cache_seq = (text or "")[:300], _seq()
    return True


# ============================================================
# CLIPBOARD CIRCUIT BREAKER
# ============================================================

class Breaker:
    """After ``fails`` consecutive type_text staging failures within
    ``window`` s, short text goes straight to key events for ``hold`` s.
    A successful staging closes it. Logs `[TypeText] clipboard breaker
    open|closed` once per change."""

    def __init__(self, fails=2, window=60.0, hold=60.0):
        self.fails, self.window, self.hold = fails, window, hold
        self._failed = []
        self._until = 0.0

    def is_open(self, now=None):
        now = time.monotonic() if now is None else now
        if self._until and now >= self._until:
            self._until = 0.0
            print("[TypeText] clipboard breaker closed")
        return bool(self._until)

    def record(self, ok, now=None):
        now = time.monotonic() if now is None else now
        if ok:
            if self._until:
                print("[TypeText] clipboard breaker closed")
            self._failed, self._until = [], 0.0
            return
        self._failed = [t for t in self._failed if now - t < self.window] + [now]
        if len(self._failed) >= self.fails and not self.is_open(now):
            self._failed, self._until = [], now + self.hold
            print("[TypeText] clipboard breaker open")


# ============================================================
# CLIPBOARD PEEK
# ============================================================

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


# ============================================================
# UNICODE KEYSTROKE LIMITS
# ============================================================

# -- keyboard fallback ------------------------------------------------------
SEND_MAX_CHARS = 200


def can_send_as_keys(text):
    return bool(text) and len(text) <= SEND_MAX_CHARS and "\n" not in text and "\r" not in text


# ============================================================
# SENDINPUT KEYBOARD STRUCT
# ============================================================

class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


# ============================================================
# SENDINPUT MOUSE STRUCT
# ============================================================

class _MI(ctypes.Structure):      # only here so the union has the right size
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


# ============================================================
# SENDINPUT INPUT STRUCT
# ============================================================

class _IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("mi", _MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


# ============================================================
# UNICODE KEYSTROKE TYPING
# ============================================================

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


# ============================================================
# OPEN APPS DESCRIPTION
# ============================================================

def describe_open_apps(snap, limit=20):
    """'code, discord (background), notepad' -- "background" = the app has a
    process but no visible window (tray apps, Win11 Notepad's broker), so
    switch_app has nothing to focus."""
    background = set(snap.get("background_apps", []))
    apps = snap.get("open_apps", [])[:limit]
    return ", ".join(f"{a} (background)" if a in background else a for a in apps)


# ============================================================
# WINDOW OWNERSHIP
# ============================================================

def window_belongs_to(hwnd, app_lower):
    """Does this top-level window belong to the app? By owning process;
    UWP/Store apps (Calculator, Settings) are drawn inside an
    ApplicationFrameHost window, so there the app's process owns a CHILD
    window -- and while the app is suspended (or gone, leaving the frame
    behind) even that child is detached, leaving only the frame's title
    ("Calculator") to go by."""
    import win32gui
    import win32process

    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    name = psutil.Process(pid).name()
    if is_self_process(name, pid):
        return False
    if process_matches(app_lower, name):
        return True
    if win32gui.GetClassName(hwnd) != "ApplicationFrameWindow":
        return False
    child_pids = set()

    def _child(c, _):
        child_pids.add(win32process.GetWindowThreadProcessId(c)[1])
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _child, None)
    except Exception:
        pass
    for cpid in child_pids - {pid}:
        try:
            if process_matches(app_lower, psutil.Process(cpid).name()):
                return True
        except Exception:
            pass
    title = win32gui.GetWindowText(hwnd).lower()
    return bool(app_lower) and re.search(rf"\b{re.escape(app_lower)}\b", title) is not None


# ============================================================
# UWP FRAME APPS
# ============================================================

def _uwp_frame_apps():
    """Tracked apps that have a visible Store/UWP frame window. The process
    scan misses a frame whose app process is suspended or already gone, yet
    the window is on screen (a leftover Calculator)."""
    import win32gui

    frames = []

    def _cb(h, _):
        if (win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).strip()
                and win32gui.GetClassName(h) == "ApplicationFrameWindow"):
            frames.append(h)
        return True

    win32gui.EnumWindows(_cb, None)
    found = []
    for h in frames:
        for app in TRACKED_APPS:
            try:
                if app not in found and window_belongs_to(h, app):
                    found.append(app)
            except Exception:
                pass
    return found


# ============================================================
# VISIBLE WINDOW PROCESSES
# ============================================================

def _visible_window_pids():
    """PIDs owning a visible, titled top-level window. UWP apps (Calculator)
    draw inside ApplicationFrameHost, so its child windows' PIDs count too."""
    import win32gui
    import win32process

    pids, frame_hosts = set(), []

    def _cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).strip():
            _, pid = win32process.GetWindowThreadProcessId(h)
            pids.add(pid)
            if win32gui.GetClassName(h) == "ApplicationFrameWindow":
                frame_hosts.append(h)
        return True

    win32gui.EnumWindows(_cb, None)
    for h in frame_hosts:
        def _child(c, _):
            pids.add(win32process.GetWindowThreadProcessId(c)[1])
            return True
        try:
            win32gui.EnumChildWindows(h, _child, None)
        except Exception:
            pass
    return pids


# ============================================================
# WINDOW EVENTS
# ============================================================

# Real window events instead of guessing (MISTAI_WINDOW_EVENTS, default on;
# for tests and rollback only). LiveStateTracker owns one WinEventHook: a
# daemon thread that installs SetWinEventHook out of context and runs its own
# message loop, so callbacks arrive on that thread. They are filtered to
# top-level windows and copied into WindowEvents (foreground hwnd + pid, a
# ring of recent events with timestamps, a sequence number). No titles, text
# or pixels are kept; logs carry numbers and labels only.
#
# Everything that uses it (core/screen.py WindowWatch: settles, the screen
# cache reuse, the accessibility-text cache) asks healthy() first and falls
# back to today's fixed waits and reads when the hook is not installed, its
# thread died, no event ever arrived, or the 1 s poll sees a foreground the
# hook never reported on two polls with no foreground event between them
# (events stopped arriving; never declared by time passing alone, cleared
# only by a new foreground event the poll agrees with).

# ============================================================
# WINDOW EVENT SETTINGS
# ============================================================

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0
CHILDID_SELF = 0
GA_ROOT = 2
WM_QUIT = 0x0012
WINDOW_EVENT_KINDS = {
    EVENT_SYSTEM_FOREGROUND: "foreground", EVENT_OBJECT_CREATE: "create",
    EVENT_OBJECT_DESTROY: "destroy", EVENT_OBJECT_SHOW: "show", EVENT_OBJECT_HIDE: "hide",
    EVENT_OBJECT_NAMECHANGE: "name",
}
WINDOW_EVENT_RANGES = ((EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND),
                       (EVENT_OBJECT_CREATE, EVENT_OBJECT_HIDE),
                       (EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE))
EVENT_RING = 64
QUIET_S = 0.15            # no relevant event for this long = quiet
WAIT_POLL_S = 0.05        # wait_for re-checks its condition at least this often
STALE_POLLS = 2           # polls in a row, with no foreground event between them, on
                          # a foreground the hook never reported
WATCH_STATE_LOGS = 5      # stopped/again transitions logged per session (then silent)
TOP_LEVEL_MEMORY = 4096   # top-level hwnds remembered so their destroy counts


# ============================================================
# WINDOW EVENT STATE
# ============================================================

class WindowEvents:
    """Thread-safe window state fed by the hook (or by tests). Events from
    MistAI's own process are kept but are not "relevant": the orb, its panels
    and the caption window never count as the desktop changing."""

    def __init__(self, clock=time.monotonic, own_pid=None):
        self._cv = threading.Condition()
        self._clock = clock
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self.ring = deque(maxlen=EVENT_RING)   # (time, kind, hwnd, pid)
        self.seq = 0                 # every event
        self.relevant_seq = 0        # events from other processes
        self.last_relevant_at = None
        self.foreground = (0, 0)     # (hwnd, pid) from the last foreground event
        self.seen = 0
        self.installed = False
        self.alive = lambda: False   # the hook thread is running (set by the hook)
        self.stale = False           # events stopped arriving (the poll disagrees)
        self._mismatch = (0, 0, -1)  # (polled hwnd, polls in a row, foreground_seq then)
        self.foreground_seq = 0      # foreground events so far
        self.last_event_at = None    # any event (own process too)
        self._stale_seq = -1         # foreground_seq when stale was declared
        self._state_logs = 0
        self._names = {}             # pid -> process name, looked up lazily
        self._logged = set()
        self.listener = None         # called (fast) on a foreground event

    # -- feeding ------------------------------------------------------------------

    def record(self, kind, hwnd, pid):
        now = self._clock()
        with self._cv:
            self.seq += 1
            self.seen += 1
            self.ring.append((now, kind, int(hwnd or 0), int(pid or 0)))
            self.last_event_at = now
            if kind == "foreground":
                self.foreground = (int(hwnd or 0), int(pid or 0))
                self.foreground_seq += 1
            if pid != self._own_pid:
                self.relevant_seq += 1
                self.last_relevant_at = now
            self._cv.notify_all()
        if kind == "foreground" and self.listener is not None:
            try:
                self.listener()
            except Exception:
                pass

    def check_poll(self, polled_hwnd, polled_pid=None):
        """The 1 s poll's foreground, cross-checked against the hook. Never
        time-based: a quiet desktop (no foreground change) always agrees.
        Stale ("events stopped arriving") only when the poll sees the
        foreground on a window the hook never reported, on STALE_POLLS polls
        in a row with NO foreground event between them (the hook demonstrably
        missed a change; an event that arrives, even naming another hwnd,
        restarts the count). Cleared only when the poll agrees AND a
        foreground event arrived since it was declared (the hook is live
        again), so agreeing by chance (the user going back to the last
        reported window) does not flap. Transitions log numbers and labels."""
        polled_hwnd = int(polled_hwnd or 0)
        if not polled_hwnd or not self.installed:
            return
        line = ""
        with self._cv:
            now = self._clock()
            if polled_hwnd == self.foreground[0]:
                self._mismatch = (0, 0, -1)
                if self.stale and self.foreground_seq != self._stale_seq:
                    self.stale = False
                    line = "[WindowWatch] window events arriving again"
            else:
                prev, n, seq = self._mismatch
                same = prev == polled_hwnd and seq == self.foreground_seq
                n = n + 1 if same else 1
                self._mismatch = (polled_hwnd, n, self.foreground_seq)
                if n >= STALE_POLLS and not self.stale:
                    self.stale = True
                    self._stale_seq = self.foreground_seq
                    since = (f"{now - self.last_event_at:.1f}s" if self.last_event_at is not None
                             else "never")
                    what = ("another window of the same process"
                            if polled_pid and int(polled_pid) == self.foreground[1]
                            else "another process" if polled_pid else "another window")
                    line = ("[WindowWatch] window events stopped arriving -- fixed waits "
                            f"(last event {since} ago; poll saw the foreground move to {what} "
                            f"with no event, {n} polls)")
            if line:
                self._state_logs += 1
                if self._state_logs > WATCH_STATE_LOGS:
                    line = ""
        if line:
            print(line)

    def _log_once(self, key, line):
        if key not in self._logged:
            self._logged.add(key)
            print(line)

    # -- reading ------------------------------------------------------------------

    def healthy(self):
        """Usable for early returns and reuse: installed, thread alive, at
        least one event seen and not stale. Logs once when the thread died."""
        if not self.installed:
            return False
        try:
            alive = bool(self.alive())
        except Exception:
            alive = False
        if not alive:
            self._log_once("dead", "[WindowWatch] window event thread stopped -- fixed waits")
            return False
        return self.seen > 0 and not self.stale

    def quiet_for(self, period=QUIET_S):
        """No relevant event for ``period`` seconds."""
        with self._cv:
            last = self.last_relevant_at
        return last is None or self._clock() - last >= period

    def mark(self):
        """The relevant-event sequence now (compare with changed_since)."""
        with self._cv:
            return self.relevant_seq

    def changed_since(self, mark):
        with self._cv:
            return self.relevant_seq != mark

    def foreground_changed(self, from_hwnd):
        """A wait_for condition: the hook reported another foreground window."""
        return lambda: self.foreground[0] not in (0, int(from_hwnd or 0))

    def quiet(self, period=QUIET_S):
        """A wait_for condition: no relevant event for ``period`` seconds."""
        return lambda: self.quiet_for(period)

    def process_name(self, pid):
        """A process's name (cached per pid; looked up on demand only)."""
        pid = int(pid or 0)
        if pid not in self._names:
            try:
                self._names[pid] = psutil.Process(pid).name().lower().replace(".exe", "")
            except Exception:
                return ""
            if len(self._names) > 512:
                self._names.clear()
        return self._names.get(pid, "")

    def window_exists(self, title=None, process=None):
        """A wait_for condition: a visible top-level window whose title
        contains ``title`` (case-insensitive) or whose process matches
        ``process`` exists. Titles are compared in memory, never logged."""
        want = str(title or "").lower()

        def check():
            try:
                import win32gui
                import win32process
            except Exception:
                return False
            found = []

            def visit(h, _):
                if found or not win32gui.IsWindowVisible(h):
                    return True
                if want and want in win32gui.GetWindowText(h).lower():
                    found.append(h)
                elif process:
                    pid = win32process.GetWindowThreadProcessId(h)[1]
                    if process_matches(process, self.process_name(pid)):
                        found.append(h)
                return True

            try:
                win32gui.EnumWindows(visit, None)
            except Exception:
                pass
            return bool(found)

        return check

    def wait_for(self, condition, timeout, poll=WAIT_POLL_S):
        """Wait until ``condition()`` is true (re-checked on every event and at
        least every ``poll`` s) or ``timeout`` s passed. True when it held."""
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            with self._cv:
                seen = self.seq
            try:
                if condition():
                    return True
            except Exception:
                pass
            left = deadline - self._clock()
            if left <= 0:
                return False
            with self._cv:
                if self.seq == seen:
                    self._cv.wait(min(poll, left))


# ============================================================
# WINEVENT HOOK
# ============================================================

class WinEventHook:
    """SetWinEventHook out of context on its own daemon thread with a
    GetMessage loop (the callbacks run there). start() waits briefly for the
    install; a failure is logged once and leaves WindowEvents uninstalled, so
    every user falls back to the fixed waits. stop() posts WM_QUIT; the
    daemon thread never blocks app exit."""

    def __init__(self, state, start_timeout=1.0):
        self.state = state
        self._start_timeout = start_timeout
        self._ready = threading.Event()
        self._thread = None
        self._tid = 0
        self._proc = None            # the ctypes callback; must outlive the hooks
        self._top = set()            # top-level hwnds seen (so their destroy counts)
        self._user32 = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="WindowEvents")
        self.state.alive = lambda: bool(self._thread and self._thread.is_alive())
        try:
            self._thread.start()
        except Exception as e:
            print(f"[WindowWatch] window events unavailable ({type(e).__name__}) -- fixed waits")
            return False
        self._ready.wait(self._start_timeout)
        return self.state.installed

    def _api(self):
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        proc_t = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                    wintypes.LONG, wintypes.LONG, wintypes.DWORD,
                                    wintypes.DWORD)
        u.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                      proc_t, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        u.SetWinEventHook.restype = wintypes.HANDLE
        u.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                  wintypes.UINT, wintypes.UINT]
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM,
                                         wintypes.LPARAM]
        u.GetForegroundWindow.restype = wintypes.HWND
        return u, proc_t

    def _run(self):
        from ctypes import wintypes

        hooks = []
        try:
            u, proc_t = self._api()
            self._user32 = u
            self._proc = proc_t(self._callback)
            for lo, hi in WINDOW_EVENT_RANGES:
                h = u.SetWinEventHook(lo, hi, None, self._proc, 0, 0, WINEVENT_OUTOFCONTEXT)
                if not h:
                    code = ctypes.get_last_error()
                    for x in hooks:
                        u.UnhookWinEvent(x)
                    print(f"[WindowWatch] window events unavailable (hook install failed, "
                          f"error {code}) -- fixed waits")
                    return
                hooks.append(h)
            self._tid = ctypes.WinDLL("kernel32").GetCurrentThreadId()
            fg = u.GetForegroundWindow() or 0
            pid = wintypes.DWORD(0)
            if fg:
                u.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            with self.state._cv:
                self.state.foreground = (int(fg), int(pid.value))
            self.state.installed = True
            print(f"[WindowWatch] window events on ({len(hooks)} hooks)")
            self._ready.set()
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            print(f"[WindowWatch] window events unavailable ({type(e).__name__}) -- fixed waits")
        finally:
            self.state.installed = False         # the loop ended: fixed waits from now
            try:
                for h in hooks:
                    self._user32.UnhookWinEvent(h)
            except Exception:
                pass
            self._ready.set()

    def _callback(self, _hook, event, hwnd, id_object, id_child, _thread, _ms):
        """On the hook thread, per event: integer filters first, then copy
        (kind, hwnd, pid) into WindowEvents. Never raises, never logs."""
        try:
            if id_object != OBJID_WINDOW or id_child != CHILDID_SELF or not hwnd:
                return
            kind = WINDOW_EVENT_KINDS.get(event)
            if kind is None:
                return
            u = self._user32
            if kind == "foreground":                 # the top-level window, as polled
                hwnd = u.GetAncestor(hwnd, GA_ROOT) or hwnd
            if kind == "destroy":                    # a destroyed window has no ancestor
                if hwnd not in self._top:
                    return
                self._top.discard(hwnd)
            elif kind != "foreground" and u.GetAncestor(hwnd, GA_ROOT) != hwnd:
                return
            else:
                if len(self._top) > TOP_LEVEL_MEMORY:
                    self._top.clear()
                self._top.add(hwnd)
            from ctypes import wintypes

            pid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            self.state.record(kind, hwnd, pid.value)
        except Exception:
            pass

    def stop(self):
        try:
            if self._tid and self._user32 is not None:
                self._user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        except Exception:
            pass


# ============================================================
# PLAYWRIGHT BROWSER WINDOW
# ============================================================

# MistAI's dedicated browser is Playwright's Firefox, run from its own folder.
PLAYWRIGHT_PATH_HINTS = ("ms-playwright", ".local-browsers")


def playwright_window():
    """The visible top-level window of MistAI's Playwright Firefox, or None."""
    try:
        import win32gui
        import win32process

        found = []

        def visit(hwnd, _):
            if found or not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
                return True
            try:
                exe = psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).exe()
            except Exception:
                return True
            if any(h in exe.lower() for h in PLAYWRIGHT_PATH_HINTS):
                found.append(hwnd)
            return True

        win32gui.EnumWindows(visit, None)
        return found[0] if found else None
    except Exception:
        return None


# ============================================================
# WINDOW SETTLE
# ============================================================

# A settle that ends early only when it is safe: never before ``floor``,
# early only when the expected window is there (``expect``) AND no relevant
# window event arrived for QUIET_S; otherwise the full ``cap`` (today's fixed
# value). No floor, no healthy hook or MISTAI_WINDOW_EVENTS=0: exactly
# time.sleep(cap), as before. One content-free log line per settle.

def settle(watched, cap, floor=None, expect=None, label="quiet", tag="settle", sleep=None):
    """Wait after an action; returns (seconds waited, reason label)."""
    sleep = sleep or time.sleep
    cap = max(0.0, float(cap or 0.0))
    usable = False
    if floor is not None and isinstance(watched, WindowEvents) and floor < cap:
        try:
            usable = window_events_enabled() and watched.healthy()
        except Exception:
            usable = False
    if not usable:
        sleep(cap)
        return cap, "fixed"
    t0 = time.monotonic()
    sleep(floor)

    def ready():
        if expect is not None and not expect():
            return False
        return watched.quiet_for(QUIET_S)

    try:
        early = watched.wait_for(ready, cap - (time.monotonic() - t0))
    except Exception:
        early = False
    waited = time.monotonic() - t0
    if not early:                                    # never less than the cap without a signal
        if waited < cap:
            sleep(cap - waited)
        waited = cap
    reason = label if early else "no early signal"
    print(f"[WindowWatch] {tag} {waited:.2f}s (cap {cap:.2f}s, {reason})")
    return waited, reason


def foreground_is(matches):
    """An ``expect`` for settle: the foreground window satisfies
    ``matches(hwnd)`` (e.g. it belongs to the app that was opened)."""
    def check():
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            return bool(hwnd) and bool(matches(hwnd))
        except Exception:
            return False

    return check


def foreground_exists():
    """An ``expect`` for settle: some window is in front."""
    return foreground_is(lambda hwnd: True)


def browser_window_present():
    """An ``expect`` for settle: MistAI's Playwright Firefox window is
    visible (looked up once per settle, then only re-validated)."""
    box = {}

    def check():
        try:
            import win32gui

            if "hwnd" not in box:
                box["hwnd"] = playwright_window() or 0
            return bool(box["hwnd"]) and bool(win32gui.IsWindowVisible(box["hwnd"]))
        except Exception:
            return False

    return check


# ============================================================
# LIVE STATE TRACKER
# ============================================================

class LiveStateTracker:
    def __init__(self):
        self._state = {
            "foreground_window": "Unknown",
            "all_windows": [],
            "running_processes": {},
            "open_apps": [],
            "clipboard": "",
            "screen_resolution": pyautogui.size(),
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "last_updated": 0,
        }
        self._lock = threading.Lock()
        self._running = True
        # Window events (WINDOW EVENTS): a foreground change re-polls at once
        # instead of up to 1 s later; the poll cross-checks the hook.
        self._kick = threading.Event()
        self.window_events = WindowEvents()
        self._hook = None
        if window_events_enabled():
            self.window_events.listener = self._kick.set
            self._hook = WinEventHook(self.window_events)
            self._hook.start()
        else:
            print("[WindowWatch] window events off (MISTAI_WINDOW_EVENTS=0) -- fixed waits")
        t = threading.Thread(target=self._loop, daemon=True, name="LiveStateTracker")
        t.start()
        time.sleep(1.2)
        print("[LiveStateTracker] Running")

    POLL_S = 1.0
    MIN_POLL_S = 0.2          # a burst of foreground events re-polls at most this often

    def _loop(self):
        while self._running:
            t0 = time.monotonic()
            try:
                self._update()
            except Exception as e:
                print(f"[LiveState] {e}")
            self._kick.wait(self.POLL_S)
            self._kick.clear()
            spent = time.monotonic() - t0
            if spent < self.MIN_POLL_S:
                time.sleep(self.MIN_POLL_S - spent)

    def _update(self):
        s = {}
        try:
            import win32gui
            fg = win32gui.GetForegroundWindow()
            # For reading: MistAI's own windows (overlays, orb, main window)
            # are never "the window in front"; the one below them is.
            hwnd, title, own_in_front = foreground_for_reading()
            s["foreground_hwnd"] = hwnd
            s["foreground_window"] = title or "Unknown"
            s["own_window_in_front"] = own_in_front
            watched = getattr(self, "window_events", None)
            if watched is not None:
                try:
                    import win32process

                    fg_pid = win32process.GetWindowThreadProcessId(fg)[1] if fg else None
                except Exception:
                    fg_pid = None
                watched.check_poll(fg, fg_pid)  # did the hook see this foreground? (the real one)
            wins = []

            def _cb(h, _):
                if win32gui.IsWindowVisible(h):
                    t = win32gui.GetWindowText(h).strip()
                    if t and len(t) > 1 and t not in wins:
                        wins.append(t)

            win32gui.EnumWindows(_cb, None)
            s["all_windows"] = wins[:40]
        except Exception:
            s["foreground_window"] = "Unknown"
            s["all_windows"] = []

        procs, apps, windowed = {}, [], set()
        try:
            visible = _visible_window_pids()
        except Exception:
            visible = None  # unknown: don't mark anything as background
        try:
            for p in psutil.process_iter(["name", "pid"]):
                n = (p.info["name"] or "").lower().replace(".exe", "")
                if is_self_process(n, p.info["pid"]):
                    continue
                if normalize_process_name(n) in TRACKED_PROCESS_NAMES:
                    procs[n] = p.info["pid"]
                    if n not in apps:
                        apps.append(n)
                    if visible is None or p.info["pid"] in visible:
                        windowed.add(n)
        except Exception:
            pass
        try:
            for app in _uwp_frame_apps():
                if not any(process_matches(app, a) for a in apps):
                    apps.append(app)
                windowed.update(a for a in apps if process_matches(app, a))
        except Exception:
            pass
        s["running_processes"] = procs
        s["open_apps"] = apps[:20]
        s["background_apps"] = [a for a in s["open_apps"] if a not in windowed]

        # Opens the clipboard only when its sequence number changed. No
        # PowerShell fallback: Get-Clipboard opens it too and only adds contention.
        s["clipboard"] = clipboard.peek_text(300)

        try:
            s["cpu_percent"] = psutil.cpu_percent(interval=None)
            s["ram_percent"] = psutil.virtual_memory().percent
        except Exception:
            s["cpu_percent"] = 0.0
            s["ram_percent"] = 0.0
        s["screen_resolution"] = pyautogui.size()
        s["last_updated"] = time.time()
        with self._lock:
            self._state.update(s)

    def refresh(self):
        """Re-read now instead of waiting for the next 1s poll, so a phase
        never starts from an Open apps list that still shows a closed app."""
        try:
            self._update()
        except Exception as e:
            print(f"[LiveState] refresh failed: {e}")

    @property
    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def stop(self):
        self._running = False
        hook = getattr(self, "_hook", None)
        if hook is not None:
            hook.stop()


# ============================================================
# WATCHERS
# ============================================================

# core/watchers.py — event SOURCES that feed the EventBus (design doc #4).
#
# StateWatcher: diffs LiveStateTracker snapshots into events. This is a
# deliberate bridge, not a cheat: LiveStateTracker already polls and works,
# so consumers become event-driven TODAY, and this one source can later be
# swapped for real Win32 hooks (SetWinEventHook, WM_CLIPBOARDUPDATE) without
# touching a single subscriber. That's the whole point of the bus.
#
# FileWatcher: real (non-polling) filesystem events via watchdog. It watches
# the user's Downloads folder by default (for DownloadCompleted) and
# auto-watches any project root the Workspace announces via ProjectSet.
#
# Both are import-guarded in the codebase's usual defensive style: a missing
# optional dependency disables only that watcher.

# ============================================================
# OPTIONAL FILE WATCHING LIBRARY
# ============================================================

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # fallback base class

_PARTIAL_SUFFIXES = (".part", ".crdownload", ".tmp", ".download", ".partial")


# ============================================================
# STATE WATCHER
# ============================================================

class StateWatcher:
    """Diffs LiveStateTracker.snapshot every `interval` seconds and publishes
    WindowFocused / WindowOpened / WindowClosed / ProcessStarted /
    ProcessExited / ClipboardChanged. First pass only primes state (no
    event storm at boot)."""

    def __init__(self, bus, live_state, interval=0.75):
        self.bus = bus
        self.live = live_state
        self.interval = interval
        self._prev = None
        self._running = True
        threading.Thread(target=self._loop, daemon=True, name="StateWatcher").start()
        print("[StateWatcher] Running")

    def _loop(self):
        while self._running:
            try:
                self._diff(self.live.snapshot)
            except Exception as e:
                print(f"[StateWatcher] {e}")
            # With window events: wake as soon as a relevant window event
            # arrives (still at most every interval), else sleep as before.
            watched = getattr(self.live, "window_events", None)
            if watched is not None and watched.healthy():
                mark = watched.mark()
                watched.wait_for(lambda: watched.changed_since(mark), self.interval)
                time.sleep(0.1)                 # let the tracker's re-poll land first
            else:
                time.sleep(self.interval)

    def _diff(self, snap):
        if self._prev is None:
            self._prev = {
                "fw": snap.get("foreground_window", ""),
                "windows": set(snap.get("all_windows", [])),
                "apps": set(snap.get("open_apps", [])),
                "clip": snap.get("clipboard", ""),
            }
            return

        prev = self._prev
        fw = snap.get("foreground_window", "")
        if fw and fw != prev["fw"]:
            self.bus.publish(events.WINDOW_FOCUSED,
                             {"title": fw, "previous": prev["fw"]}, "StateWatcher")
            prev["fw"] = fw

        windows = set(snap.get("all_windows", []))
        for t in windows - prev["windows"]:
            self.bus.publish(events.WINDOW_OPENED, {"title": t}, "StateWatcher")
        for t in prev["windows"] - windows:
            self.bus.publish(events.WINDOW_CLOSED, {"title": t}, "StateWatcher")
        prev["windows"] = windows
        # Note: a window retitling itself shows up as CLOSED(old)+OPENED(new).
        # Subscribers that care can correlate on timestamps.

        apps = set(snap.get("open_apps", []))
        for a in apps - prev["apps"]:
            self.bus.publish(events.PROCESS_STARTED, {"app": a}, "StateWatcher")
        for a in prev["apps"] - apps:
            self.bus.publish(events.PROCESS_EXITED, {"app": a}, "StateWatcher")
        prev["apps"] = apps

        clip = snap.get("clipboard", "")
        if clip != prev["clip"]:
            self.bus.publish(events.CLIPBOARD_CHANGED,
                             {"preview": clip[:80]}, "StateWatcher")
            prev["clip"] = clip

    def stop(self):
        self._running = False


# ============================================================
# FILE EVENT HANDLER
# ============================================================

if WATCHDOG_AVAILABLE:

    class _FSHandler(FileSystemEventHandler):
        """Translates watchdog callbacks into bus events, with a per-path
        debounce on 'modified' (editors and browsers spam writes)."""

        def __init__(self, bus, downloads_dir):
            self.bus = bus
            self.downloads_dir = (downloads_dir or "").lower()
            self._last_mod = {}   # path -> ts
            self._mod_debounce = 1.0

        def _is_partial(self, path):
            return path.lower().endswith(_PARTIAL_SUFFIXES)

        def _in_downloads(self, path):
            return bool(self.downloads_dir) and path.lower().startswith(self.downloads_dir)

        def on_created(self, event):
            if event.is_directory or self._is_partial(event.src_path):
                return
            self.bus.publish(events.FILE_CREATED, {"path": event.src_path}, "FileWatcher")
            if self._in_downloads(event.src_path):
                self.bus.publish(events.DOWNLOAD_COMPLETED, {"path": event.src_path}, "FileWatcher")

        def on_moved(self, event):
            # Browsers finish downloads as a rename: file.part -> file.ext
            if event.is_directory:
                return
            dest = getattr(event, "dest_path", "") or ""
            if dest and not self._is_partial(dest):
                self.bus.publish(events.FILE_CREATED, {"path": dest}, "FileWatcher")
                if self._in_downloads(dest) and self._is_partial(event.src_path):
                    self.bus.publish(events.DOWNLOAD_COMPLETED, {"path": dest}, "FileWatcher")

        def on_modified(self, event):
            if event.is_directory or self._is_partial(event.src_path):
                return
            if self._in_downloads(event.src_path):
                return  # in-progress downloads modify constantly; ignore
            now = time.time()
            if now - self._last_mod.get(event.src_path, 0) < self._mod_debounce:
                return
            self._last_mod[event.src_path] = now
            self.bus.publish(events.FILE_MODIFIED, {"path": event.src_path}, "FileWatcher")

        def on_deleted(self, event):
            if event.is_directory:
                return
            self.bus.publish(events.FILE_DELETED, {"path": event.src_path}, "FileWatcher")


# ============================================================
# FILE WATCHER
# ============================================================

class FileWatcher:
    """Watches directories and publishes FileCreated/Modified/Deleted and
    DownloadCompleted. Subscribes to ProjectSet so any project root the
    Workspace announces gets watched automatically -- the watcher itself is
    a bus subscriber, same as everything else."""

    def __init__(self, bus, watch_downloads=True):
        self.bus = bus
        self.available = WATCHDOG_AVAILABLE
        self._watched = set()
        self._observer = None
        if not self.available:
            print("[FileWatcher] watchdog not installed -- file events disabled")
            return

        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        self._handler = _FSHandler(bus, downloads if watch_downloads else "")
        self._observer = Observer()
        self._observer.daemon = True
        if watch_downloads and os.path.isdir(downloads):
            self.watch(downloads)
        self._observer.start()
        bus.subscribe(events.PROJECT_SET, self._on_project_set)
        print("[FileWatcher] Running")

    def _on_project_set(self, ev):
        root = ev.data.get("root")
        if root:
            self.watch(root)

    def watch(self, path):
        if not self.available or not path:
            return False
        path = os.path.abspath(path)
        if not os.path.isdir(path) or path in self._watched:
            return False
        try:
            self._observer.schedule(self._handler, path, recursive=True)
            self._watched.add(path)
            print(f"[FileWatcher] Watching {log_text(path)}")
            return True
        except Exception as e:
            print(f"[FileWatcher] Could not watch {log_text(path)}: "
                  f"{e if verbose_logs() else type(e).__name__}")
            return False

    def stop(self):
        if self._observer:
            try:
                self._observer.stop()
            except Exception:
                pass


# ============================================================
# WINDOW IN FRONT FOR READING
# ============================================================

# What "the window in front" means for searching and reading (the snapshot's
# foreground title, UIA searches, screen text, screen-change identity): the
# foreground window, unless it is one of MistAI's own (this process: the main
# window, a Tk overlay, the orb, a panel) -- then the first visible, titled,
# non-tool window of another process below it in z-order, i.e. the window the
# user or the agent was working in. Input guards do NOT use this: keys go to
# the real foreground, so they keep refusing when MistAI itself is in front.
GW_HWNDNEXT = 2
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
Z_ORDER_LIMIT = 512


def _window_pid(win32gui, win32process, hwnd):
    try:
        return win32process.GetWindowThreadProcessId(hwnd)[1]
    except Exception:
        return None


def foreground_for_reading(own_pid=None):
    """(hwnd, title, own_in_front) -- see above. own_in_front: the real
    foreground was MistAI's. Falls back to the raw foreground when anything
    can't be read; (0, "", False) when there is none."""
    own = os.getpid() if own_pid is None else own_pid
    try:
        import win32gui
        import win32process

        fg = win32gui.GetForegroundWindow()
    except Exception:
        return 0, "", False
    if not fg:
        return 0, "", False
    if _window_pid(win32gui, win32process, fg) != own:
        return fg, win32gui.GetWindowText(fg), False
    h = fg
    try:
        for _ in range(Z_ORDER_LIMIT):
            h = win32gui.GetWindow(h, GW_HWNDNEXT)
            if not h:
                break
            if not win32gui.IsWindowVisible(h) or win32gui.IsIconic(h):
                continue
            if _window_pid(win32gui, win32process, h) in (own, None):
                continue
            if win32gui.GetWindowLong(h, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                continue
            title = win32gui.GetWindowText(h)
            if title.strip():
                return h, title, True
    except Exception:
        pass
    return 0, "", True
