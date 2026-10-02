"""
core/watchers.py — event SOURCES that feed the EventBus (design doc #4).

StateWatcher: diffs LiveStateTracker snapshots into events. This is a
deliberate bridge, not a cheat: LiveStateTracker already polls and works,
so consumers become event-driven TODAY, and this one source can later be
swapped for real Win32 hooks (SetWinEventHook, WM_CLIPBOARDUPDATE) without
touching a single subscriber. That's the whole point of the bus.

FileWatcher: real (non-polling) filesystem events via watchdog. It watches
the user's Downloads folder by default (for DownloadCompleted) and
auto-watches any project root the Workspace announces via ProjectSet.

Both are import-guarded in the codebase's usual defensive style: a missing
optional dependency disables only that watcher.
"""

import os
import threading
import time

from . import events

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # fallback base class

_PARTIAL_SUFFIXES = (".part", ".crdownload", ".tmp", ".download", ".partial")


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
            print(f"[FileWatcher] Watching {path}")
            return True
        except Exception as e:
            print(f"[FileWatcher] Could not watch {path}: {e}")
            return False

    def stop(self):
        if self._observer:
            try:
                self._observer.stop()
            except Exception:
                pass