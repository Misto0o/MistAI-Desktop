"""
core/events.py — internal event bus (design doc #4).

Components publish and subscribe to named events instead of polling each
other's state. Dispatch happens on ONE dedicated thread so:
- publish() never blocks the caller (it's just a queue put)
- a crashing subscriber can never take down the publisher
- event ordering is deterministic

Rules for subscribers: callbacks must be FAST (<~10ms). Anything heavy
(vision calls, disk walks) should hand off to its own thread.

Event names are plain strings; the canonical set lives below so every
name is grep-able. Unknown names are allowed (forward compatible) --
USBInserted / NetworkConnected are reserved here even though no watcher
emits them yet.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from queue import Empty, Queue

# -- Canonical event names ---------------------------------------------------

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

TASK_STARTED = "TaskStarted"
TASK_PHASE_DONE = "TaskPhaseDone"
TASK_COMPLETED = "TaskCompleted"
TASK_FAILED = "TaskFailed"
TASK_PAUSED = "TaskPaused"

PROJECT_SET = "ProjectSet"

USB_INSERTED = "USBInserted"            # reserved -- no watcher yet
NETWORK_CONNECTED = "NetworkConnected"  # reserved -- no watcher yet


@dataclass
class Event:
    name: str
    data: dict = field(default_factory=dict)
    source: str = ""
    ts: float = field(default_factory=time.time)

    def __repr__(self):
        keys = ",".join(list(self.data.keys())[:4])
        return f"Event({self.name}, {{{keys}}}, src={self.source})"


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