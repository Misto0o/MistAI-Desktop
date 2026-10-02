"""
core/enhanced_utils.py — MistAI Enhanced Utilities.

Window management, browser automation, task scheduling, file monitoring,
screen control, and config management. Each capability group is imported
defensively so a missing optional dependency only disables that one group
instead of breaking the whole module.
"""

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── WINDOW & APP MANAGEMENT ───────────────────────────────────────────────────
try:
    import pygetwindow as gw
    from pywinauto import Desktop
    WINDOW_MGMT_AVAILABLE = True
except ImportError:
    WINDOW_MGMT_AVAILABLE = False

try:
    import uiautomation as auto
    UIAUTOMATION_AVAILABLE = True
except ImportError:
    UIAUTOMATION_AVAILABLE = False

# ── KEYBOARD & MOUSE CONTROL ──────────────────────────────────────────────────
try:
    from pynput.keyboard import Controller as KeyboardController, Key, Listener
    from pynput.mouse import Button, Controller as MouseController
    import keyboard
    INPUT_CONTROL_AVAILABLE = True
except ImportError:
    INPUT_CONTROL_AVAILABLE = False

# ── BROWSER AUTOMATION ────────────────────────────────────────────────────────
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── TASK SCHEDULING ───────────────────────────────────────────────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    import schedule as schedule_lib
    SCHEDULING_AVAILABLE = True
except ImportError:
    SCHEDULING_AVAILABLE = False

# ── FILE MONITORING ───────────────────────────────────────────────────────────
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # fallback base class

# ── SCREEN CAPTURE & BRIGHTNESS ───────────────────────────────────────────────
try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

try:
    from screen_brightness_control import get_brightness, set_brightness
    BRIGHTNESS_AVAILABLE = True
except ImportError:
    BRIGHTNESS_AVAILABLE = False

# ── CONFIG FILE PARSING ───────────────────────────────────────────────────────
import configparser
try:
    import toml
    TOML_AVAILABLE = True
except ImportError:
    TOML_AVAILABLE = False

# Overall flag the main app can check before instantiating anything here.
ENHANCED_UTILS_AVAILABLE = True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WINDOW MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class WindowManager:
    """Advanced window management with pygetwindow, pywinauto, and win32gui."""

    available = WINDOW_MGMT_AVAILABLE

    @staticmethod
    def find_window(title: str, partial: bool = True) -> Optional[object]:
        if not WINDOW_MGMT_AVAILABLE:
            return None
        windows = gw.getWindowsWithTitle(title)
        if windows:
            return windows[0] if partial else (windows[0] if windows[0].title == title else None)
        return None

    @staticmethod
    def find_window_uia(title: str, partial: bool = True) -> Optional[object]:
        if not UIAUTOMATION_AVAILABLE:
            return None
        try:
            if partial:
                for w in auto.GetTopLevelWindows():
                    if title in w.Name:
                        return w
            else:
                return auto.GetTopLevelWindowByName(title)
        except Exception:
            pass
        return None

    @staticmethod
    def list_all_windows() -> List[Dict[str, Any]]:
        if not WINDOW_MGMT_AVAILABLE:
            return []
        return [{"title": w.title} for w in gw.getAllWindows() if w.title]

    @staticmethod
    def activate_window(window_title: str) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.activate()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def get_active_window() -> Optional[str]:
        if not WINDOW_MGMT_AVAILABLE:
            return None
        try:
            window = gw.getActiveWindow()
            return window.title if window else None
        except Exception:
            return None

    @staticmethod
    def close_window(window_title: str) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.close()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def maximize_window(window_title: str) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.maximize()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def minimize_window(window_title: str) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.minimize()
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def move_window(window_title: str, x: int, y: int) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.moveTo(x, y)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def resize_window(window_title: str, width: int, height: int) -> bool:
        try:
            window = WindowManager.find_window(window_title)
            if window:
                window.resizeTo(width, height)
                return True
        except Exception:
            pass
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADVANCED INPUT CONTROL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class InputController:
    """Advanced keyboard and mouse control, including global hotkeys."""

    available = INPUT_CONTROL_AVAILABLE

    def __init__(self):
        if not INPUT_CONTROL_AVAILABLE:
            self.mouse = None
            self.keyboard = None
        else:
            self.mouse = MouseController()
            self.keyboard = KeyboardController()
        self.hotkeys = {}

    def register_hotkey(self, hotkey: str, callback: Callable) -> None:
        if not INPUT_CONTROL_AVAILABLE:
            return
        try:
            keyboard.add_hotkey(hotkey, callback)
            self.hotkeys[hotkey] = callback
            logger.info(f"Hotkey registered: {hotkey}")
        except Exception as e:
            logger.error(f"Failed to register hotkey {hotkey}: {e}")

    def unregister_hotkey(self, hotkey: str) -> bool:
        if not INPUT_CONTROL_AVAILABLE:
            return False
        try:
            keyboard.remove_hotkey(hotkey)
            self.hotkeys.pop(hotkey, None)
            return True
        except Exception:
            return False

    def get_mouse_position(self) -> tuple:
        if not self.mouse:
            return (0, 0)
        return self.mouse.position

    def move_mouse(self, x: int, y: int, duration: float = 0.5) -> None:
        if self.mouse:
            self.mouse.position = (x, y)

    def click(self, button: str = "left", count: int = 1) -> None:
        if not self.mouse:
            return
        btn = Button.left if button == "left" else Button.right
        for _ in range(count):
            self.mouse.click(btn)

    def type_text(self, text: str, interval: float = 0.05) -> None:
        if not self.keyboard:
            return
        for char in text:
            self.keyboard.type(char)
            time.sleep(interval)

    def press_key(self, key_name: str) -> None:
        if not self.keyboard:
            return
        try:
            key = getattr(Key, key_name, None) or key_name
            self.keyboard.press(key)
            self.keyboard.release(key)
        except Exception:
            pass

    def key_sequence(self, keys: List[str]) -> None:
        for key in keys:
            self.press_key(key)
            time.sleep(0.1)

    def listen_for_keys(self, on_press: Callable = None, on_release: Callable = None):
        if not INPUT_CONTROL_AVAILABLE:
            return None
        listener = Listener(on_press=on_press, on_release=on_release)
        listener.start()
        return listener


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BROWSER AUTOMATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BrowserAutomation:
    """Selenium-based browser automation."""

    available = SELENIUM_AVAILABLE

    def __init__(self, headless: bool = False):
        self.driver = None
        self.headless = headless

    def start_browser(self, url: str = "about:blank") -> None:
        if not SELENIUM_AVAILABLE:
            logger.error("Selenium not installed; browser automation disabled")
            return
        try:
            options = webdriver.ChromeOptions()
            if self.headless:
                options.add_argument("--headless")
            self.driver = webdriver.Chrome(options=options)
            self.driver.get(url)
        except Exception as e:
            logger.error(f"Failed to start browser: {e}")

    def navigate_to(self, url: str) -> None:
        if self.driver:
            self.driver.get(url)

    def find_element(self, selector: str, by: str = "css") -> Optional[Any]:
        if not self.driver:
            return None
        try:
            by_type = By.CSS_SELECTOR if by == "css" else By.XPATH
            return self.driver.find_element(by_type, selector)
        except Exception:
            return None

    def click_element(self, selector: str) -> bool:
        try:
            element = self.find_element(selector)
            if element:
                element.click()
                return True
        except Exception:
            pass
        return False

    def fill_input(self, selector: str, text: str) -> bool:
        try:
            element = self.find_element(selector)
            if element:
                element.clear()
                element.send_keys(text)
                return True
        except Exception:
            pass
        return False

    def search_google(self, query: str) -> None:
        self.navigate_to(f"https://www.google.com/search?q={query}")

    def close_browser(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TASK SCHEDULING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TaskScheduler:
    """Advanced task scheduling with APScheduler + the `schedule` library."""

    available = SCHEDULING_AVAILABLE

    def __init__(self):
        if SCHEDULING_AVAILABLE:
            self.scheduler = BackgroundScheduler()
            self.scheduler.start()
            self.simple_scheduler = schedule_lib
        else:
            self.scheduler = None
            self.simple_scheduler = None

    def schedule_at_time(self, hour: int, minute: int, func: Callable, args: tuple = ()) -> None:
        if not self.scheduler:
            return
        try:
            self.scheduler.add_job(func, "cron", hour=hour, minute=minute, args=args)
        except Exception as e:
            logger.error(f"Failed to schedule task: {e}")

    def schedule_every(self, interval: int, unit: str, func: Callable) -> None:
        if not self.simple_scheduler:
            return
        job = getattr(self.simple_scheduler.every(interval), unit)
        job.do(func)

    def schedule_once(self, delay_seconds: int, func: Callable, args: tuple = ()) -> None:
        timer = threading.Timer(delay_seconds, func, args=args)
        timer.start()

    def stop_scheduler(self) -> None:
        if self.scheduler:
            self.scheduler.shutdown()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE MONITORING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FileMonitor(FileSystemEventHandler):
    """Monitor file system changes."""

    available = WATCHDOG_AVAILABLE

    def __init__(self, on_modified: Callable = None, on_created: Callable = None,
                 on_deleted: Callable = None):
        self.on_modified_callback = on_modified
        self.on_created_callback = on_created
        self.on_deleted_callback = on_deleted
        self.observer = Observer() if WATCHDOG_AVAILABLE else None

    def on_modified(self, event):
        if self.on_modified_callback and not event.is_directory:
            self.on_modified_callback(event.src_path)

    def on_created(self, event):
        if self.on_created_callback and not event.is_directory:
            self.on_created_callback(event.src_path)

    def on_deleted(self, event):
        if self.on_deleted_callback and not event.is_directory:
            self.on_deleted_callback(event.src_path)

    def start_monitoring(self, path: str, recursive: bool = True) -> None:
        if not self.observer:
            logger.error("watchdog not installed; file monitoring disabled")
            return
        try:
            self.observer.schedule(self, path, recursive=recursive)
            self.observer.start()
            logger.info(f"Monitoring: {path}")
        except Exception as e:
            logger.error(f"Failed to start monitoring: {e}")

    def stop_monitoring(self) -> None:
        if self.observer:
            self.observer.stop()
            self.observer.join()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SCREEN CAPTURE & CONTROL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ScreenControl:
    """Screen capture (MSS) and brightness control."""

    @staticmethod
    def take_screenshot(save_path: Optional[str] = None) -> Any:
        if not MSS_AVAILABLE:
            logger.error("mss not installed; screenshot disabled")
            return None
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                screenshot = sct.grab(monitor)
                if save_path:
                    from PIL import Image
                    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
                    img.save(save_path)
                    return save_path
                return screenshot
        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return None

    @staticmethod
    def get_brightness() -> Optional[int]:
        if not BRIGHTNESS_AVAILABLE:
            return None
        try:
            return get_brightness()[0]
        except Exception:
            return None

    @staticmethod
    def set_brightness_level(level: int) -> bool:
        if not BRIGHTNESS_AVAILABLE:
            return False
        try:
            set_brightness(level)
            return True
        except Exception:
            return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURATION MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ConfigManager:
    """Configuration file management (INI & TOML)."""

    @staticmethod
    def load_ini(filepath: str) -> Optional[dict]:
        try:
            config = configparser.ConfigParser()
            config.read(filepath)
            return {s: dict(config.items(s)) for s in config.sections()}
        except Exception as e:
            logger.error(f"Failed to load INI: {e}")
            return None

    @staticmethod
    def load_toml(filepath: str) -> Optional[dict]:
        if not TOML_AVAILABLE:
            return None
        try:
            with open(filepath, "r") as f:
                return toml.load(f)
        except Exception as e:
            logger.error(f"Failed to load TOML: {e}")
            return None

    @staticmethod
    def save_ini(filepath: str, data: dict) -> bool:
        try:
            config = configparser.ConfigParser()
            for section, values in data.items():
                config.add_section(section)
                for key, value in values.items():
                    config.set(section, key, str(value))
            with open(filepath, "w") as f:
                config.write(f)
            return True
        except Exception as e:
            logger.error(f"Failed to save INI: {e}")
            return False

    @staticmethod
    def save_toml(filepath: str, data: dict) -> bool:
        if not TOML_AVAILABLE:
            return False
        try:
            with open(filepath, "w") as f:
                toml.dump(data, f)
            return True
        except Exception as e:
            logger.error(f"Failed to save TOML: {e}")
            return False
