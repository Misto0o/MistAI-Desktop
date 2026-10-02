"""
core/live_state.py — tracks foreground window, open apps, clipboard, CPU/RAM.
"""

import subprocess
import threading
import time

import psutil
import pyautogui

from .config import TRACKED_PROCESS_NAMES, is_self_process, normalize_process_name


def describe_open_apps(snap, limit=20):
    """'code, discord (background), notepad' -- "background" = the app has a
    process but no visible window (tray apps, Win11 Notepad's broker), so
    switch_app has nothing to focus."""
    background = set(snap.get("background_apps", []))
    apps = snap.get("open_apps", [])[:limit]
    return ", ".join(f"{a} (background)" if a in background else a for a in apps)


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
        s["running_processes"] = procs
        s["open_apps"] = apps[:20]
        s["background_apps"] = [a for a in s["open_apps"] if a not in windowed]

        clip = ""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                clip = str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))[:300]
            except Exception:
                pass
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            try:
                r = subprocess.run(
                    ["powershell", "-command", "Get-Clipboard"],
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                clip = r.stdout.strip()[:300]
            except Exception:
                pass
        s["clipboard"] = clip

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
