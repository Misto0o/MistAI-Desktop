"""
core/api_system.py -- system utilities: brightness, volume, reminders, facts, battery, locking, quick info

Methods of core.api.Api, moved here verbatim (Api inherits SystemActionsMixin).
"""

import subprocess
import time
from datetime import datetime

import psutil
import pyautogui

from .memory import recall_builtin


class SystemActionsMixin:
    def list_open_apps(self):
        from .live_state import describe_open_apps

        return describe_open_apps(self.live.snapshot) or "No tracked apps open"

    def remember_fact(self, fact, category="general", importance=1):
        self.memory.remember_fact(fact, category=category, importance=importance)
        return {"success": True, "stored": fact}

    def recall_facts(self, query=None, category=None):
        """The user's saved facts; when none match a query, MistAI's facts
        about itself (from code)."""
        facts = self.memory.recall_facts(query=query, category=category, limit=5)
        if not facts and query and category is None:
            facts = recall_builtin(query, limit=5)
        return facts

    def add_reminder(self, text, due_time=None):
        self.memory.add_reminder(text, due_time=due_time)
        return {"success": True, "reminder": text}

    def get_reminders(self):
        reminders = self.memory.get_pending_reminders()
        return [{"id": r[0], "text": r[1], "due": r[2]} for r in reminders]

    def complete_reminder(self, reminder_id):
        self.memory.complete_reminder(reminder_id)
        return {"success": True}

    def get_battery_status(self):
        try:
            battery = psutil.sensors_battery()
            if battery:
                plugged = "plugged in" if battery.power_plugged else "unplugged"
                return f"Battery at {battery.percent}% ({plugged})"
            return "Battery info unavailable"
        except Exception:
            return "Could not get battery status"

    def get_active_window(self):
        return self.live.snapshot.get("foreground_window", "Unknown")

    def lock_screen(self):
        try:
            pyautogui.hotkey("win", "l")
            return True
        except Exception:
            return False

    def minimize_all_windows(self):
        try:
            pyautogui.hotkey("win", "d")
            return True
        except Exception:
            return False

    def take_notes(self, note_text):
        try:
            subprocess.Popen("notepad.exe")
            time.sleep(1.5)
            pyautogui.write(note_text, interval=0.02)
            return True
        except Exception:
            return False

    def press_and_hold(self, key, duration=0.5):
        try:
            pyautogui.keyDown(key)
            time.sleep(duration)
            pyautogui.keyUp(key)
            return True
        except Exception:
            return False

    def type_slowly(self, text, delay=0.1):
        try:
            for char in text:
                pyautogui.write(char, interval=delay)
            return True
        except Exception:
            return False

    def get_frequently_used_apps(self):
        apps = self.memory.get_frequent_apps(limit=5)
        return ", ".join(apps) if apps else "No usage history"

    def get_system_info(self):
        snap = self.live.snapshot
        return {
            "time": datetime.now().strftime("%I:%M %p"),
            "date": datetime.now().strftime("%A, %B %d"),
            "foreground_app": snap.get("foreground_window", "Unknown"),
            "open_apps": snap.get("open_apps", [])[:5],
            "cpu": f"{snap.get('cpu_percent', 0):.1f}%",
            "ram": f"{snap.get('ram_percent', 0):.1f}%",
        }

    def _act_brightness_get(self):
        if not self.enhanced_available:
            self.speak("Brightness control isn't available.")
            return False
        level = self.screen_control.get_brightness()
        self.last_result_text = (
            f"Current brightness: {level}%"
            if level is not None
            else "Could not read brightness"
        )
        self.speak(self.last_result_text)
        return level is not None

    def _act_brightness_set(self, param):
        if not self.enhanced_available:
            self.speak("Brightness control isn't available.")
            return False
        try:
            level = int(param)
            ok = self.screen_control.set_brightness_level(level)
            self.last_result_text = (
                f"Brightness set to {level}%" if ok else "Could not set brightness"
            )
            self.speak(self.last_result_text)
            return ok
        except Exception:
            return False

    def _act_mss_screenshot(self, param):
        if not self.enhanced_available:
            return self.screenshot_to_file(param or "screenshot.png")
        path = self.screen_control.take_screenshot(param if param else "screenshot.png")
        self.last_result_text = f"Screenshot saved to {path}" if path else "Screenshot failed"
        self.speak("Screenshot saved" if path else "Screenshot failed")
        return bool(path)

    def _act_schedule_reminder(self, param):
        if not self.enhanced_available or not self.task_scheduler:
            self.speak("Scheduling module isn't available.")
            return False
        try:
            parts = param.split("|", 1)
            seconds = int(parts[0].strip())
            text = parts[1].strip() if len(parts) > 1 else "Reminder"

            def _fire():
                self.add_reminder(text)
                self.speak(f"Reminder: {text}")

            self.task_scheduler.schedule_once(seconds, _fire)
            self.last_result_text = f"Scheduled reminder '{text}' in {seconds}s"
            self.speak(f"I'll remind you about '{text}' in {seconds} seconds")
            return True
        except Exception as e:
            print(f"[Schedule] Error: {e}")
            return False

    def _act_volume_mute(self, param=""):
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMute(1, None)
            self.last_result_text = "Muted"
            return True
        except Exception as e:
            self.last_result_text = f"Mute failed: {e}"
            return False

    def _act_volume_unmute(self, param=""):
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
            volume.SetMute(0, None)
            self.last_result_text = "Unmuted"
            return True
        except Exception as e:
            self.last_result_text = f"Unmute failed: {e}"
            return False
