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
#   24. LIVE STATE TRACKER
#   25. WATCHERS
#   26. OPTIONAL FILE WATCHING LIBRARY
#   27. STATE WATCHER
#   28. FILE EVENT HANDLER
#   29. FILE WATCHER
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


def _seq():
    try:
        return ctypes.windll.user32.GetClipboardSequenceNumber()
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
        hwnd = ctypes.windll.user32.GetOpenClipboardWindow()
        if not hwnd:
            return "unknown"      # not open, or opened with no window handle
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
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

def set_text(text):
    """Put text on the clipboard. False if it could not be opened or set;
    logs `[TypeText] clipboard busy holder=...` on failure."""
    global _cache, _cache_seq

    opened = []

    def _set(w):
        opened.append(True)
        w.EmptyClipboard()
        w.SetClipboardData(w.CF_UNICODETEXT, text)

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
        t = threading.Thread(target=self._loop, daemon=True, name="LiveStateTracker")
        t.start()
        time.sleep(1.2)
        print("[LiveStateTracker] Running")

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception as e:
                print(f"[LiveState] {e}")
            time.sleep(1.0)

    def _update(self):
        s = {}
        try:
            import win32gui
            fg = win32gui.GetForegroundWindow()
            s["foreground_hwnd"] = fg
            s["foreground_window"] = win32gui.GetWindowText(fg) or "Unknown"
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
