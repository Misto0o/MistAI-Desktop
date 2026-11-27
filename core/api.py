# ============================================================
# API MODULE
# ============================================================
#
# The Api class exposed to the pywebview page as js_api: it wires every
# subsystem together (voice, TTS, vision, memory, agent, browser, orb) and
# holds the action handlers, grouped into the browser, system and window
# mixin classes it inherits.
#
# SECTION INDEX
#   1. OPTIONAL MODULES AND HANDLER SETTINGS
#   2. ASYNC WINDOW SHOW
#   3. VOICE ACKNOWLEDGEMENT AND HEARTBEAT SETTINGS
#   4. STOPPABLE MICROPHONE STREAM
#   5. MICROPHONE LEVEL
#   6. BROWSER ACTIONS
#   7. BROWSER ACTION HANDLERS
#   8. SYSTEM UTILITIES
#   9. SYSTEM ACTION HANDLERS
#   10. WINDOW ACTIONS
#   11. WINDOW ACTION HANDLERS
#   12. SAVE AS DIALOG HANDLER
#   13. API CLASS
# ============================================================

"""
core/api.py — the Api class exposed to the pywebview frontend. This is where
all subsystems (TTS, vision, memory, agent loop, enhanced utils, etc.) are
wired together.
"""

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import psutil
import pyautogui
import requests
import speech_recognition as sr

from .agent import NOT_VISIBLE_ON_SCREEN, AgentLoop
from .config import (
    app_note,
    BROWSER_NOTE,
    browser_seed_profile_enabled,
    SAVE_BOX_POLL_S,
    SAVE_NAME_BOX_WAIT_S,
    spelled_key_name,
    APIKeyManager,
    describe_api_error,
    model_chain,
    model_failover_line,
    ASK_BEFORE_SENDING_CHOICES,
    ASK_BEFORE_SENDING_HELP,
    ASK_BEFORE_SENDING_LABELS,
    SEND_SWITCH_HELP,
    SEND_SWITCH_NEVER_NOTE,
    SEND_SWITCH_OFF_HELP,
    send_mode_for_switch,
    send_switch_state,
    DEFAULT_GENDER,
    DEFAULT_HOTKEYS,
    DEFAULT_MODEL,
    ENTER_CONFIRM_APPS,
    GENDER_CHOICES,
    MODEL_CHOICES,
    orb_flight_enabled,
    orb_idle_drift_enabled,
    orb_panels_enabled,
    orb_pointer_enabled,
    PTT_PAUSE_THRESHOLD,
    PTT_PHRASE_TIME_LIMIT,
    CONFIRM_LEAD_IN_SECONDS,
    PTT_THRESHOLD_MAX_AGE,
    search_routes,
    search_url,
    STATUS_URL,
    TRACKED_APPS,
    WAKE_WORDS,
    announce_verbose_logs,
    ask_before_sending_choice,
    blocked_page_reason,
    confirm_listen_profile,
    confirm_reminimize_enabled,
    esc,
    fuzzy_wake,
    wake_clip_dir,
    wake_clips_enabled,
    is_secret_file,
    is_self_process,
    log_text,
    my_facts_path,
    narration_mode,
    normalize_person_name,
    normalize_process_name,
    process_matches,
    recipient_match,
    restore_after_task_enabled,
    secret_output_reason,
    step_param_for_log,
    strip_wake_prefix,
    task_ack_enabled,
    trace_reads_enabled,
    uia_first_enabled,
    verbose_logs,
    voice_diag_enabled,
)
from .browser_session import NAV_TIMEOUT_MS
from . import browser_signins
from .caption import CaptionWindow
from .click_handler import CoordinateValidator, ImprovedClickHandler, EnhancedUIANavigator
from . import planner as conversation, workspace as file_search, live_state as clip
from .live_state import EventBus, LiveStateTracker, window_belongs_to, FileWatcher, StateWatcher
from .executor import Executor
from .floating_orb import signal_scope, waiting_scope
from .memory import Memory, recall_builtin, TaskStore, parse_fact_lines, FACTS_TEXT_MAX_CHARS
from .screen import WindowWatch, CohereVision
from .tools import ToolRegistry
from .tts import NeuralTTS, VoiceTimer, WakeCalibration, dropped_count as _tts_dropped
from .workspace import Workspace

# ============================================================
# OPTIONAL MODULES AND HANDLER SETTINGS
# ============================================================

try:
    from . import enhanced_utils

    ENHANCED_UTILS_AVAILABLE = True
except ImportError:
    ENHANCED_UTILS_AVAILABLE = False
    enhanced_utils = None

# Memory bound for handler result text. What the model actually sees per step
# is the much smaller ToolSpec.result_chars (AgentLoop truncates).
_RESULT_MEMORY_CHARS = 20_000
# quick_message recipient checks: vision is asked what is shown, never told
# the expected name or the message (an "Expected: <name>" invites the model
# to agree); the answer is compared locally (config.recipient_match).
QM_SWITCHER_QUESTION = (
    "The quick-switcher/search box is open. Reply with ONLY the exact name of "
    "the top highlighted conversation result, as displayed."
)
QM_HEADER_QUESTION = (
    "Reply with ONLY the exact name shown in the header of the currently open "
    "chat, as displayed."
)
# Remembered "Always for this person" approvals (quick_message): profile keys
# APPROVED_PREFIX + HMAC-SHA256(approval_key, app|recipient), value "1". No
# name is stored; "approval_key" is a random per-install key.
APPROVED_PREFIX = "approved:"
# The final check before Enter failed: names no person.
QM_UNCONFIRMED_REASON = "I couldn't confirm who this chat is with, so I didn't send it."
# Appended wherever secret content is withheld from the model.
_WITHHELD_NOTE = (
    "You cannot see or show this content. Tell the user it was withheld "
    "because it may contain secrets; do not guess it, and do not offer to "
    "reveal it or retry another way."
)


# ============================================================
# ASYNC WINDOW SHOW
# ============================================================

def _show_window_async(hwnd, cmd):
    """user32 ShowWindowAsync (pywin32 has no binding): posts the change to
    the window's own thread, so the caller never waits on it."""
    import ctypes

    return ctypes.windll.user32.ShowWindowAsync(hwnd, cmd)


# ============================================================
# VOICE ACKNOWLEDGEMENT AND HEARTBEAT SETTINGS
# ============================================================

# Voice tasks only: spoken once a task reaches the agent, and (narration
# "milestones") while it is still running HEARTBEAT_SECONDS after Mist last
# spoke, at most HEARTBEAT_MAX times per task. Fixed strings, never model text.
TASK_ACK_SPEECH = "On it."
HEARTBEAT_SPEECH = "Still working."
HEARTBEAT_SECONDS = 20
HEARTBEAT_MAX = 2
HEARTBEAT_POLL_SECONDS = 1  # recheck interval while Mist is speaking or listening

# What _listen_once(ptt=True) returns when a second press stopped the listen
# before any speech: the turn ends quietly, with no recognition call.
_PTT_CANCELLED = object()


# ============================================================
# STOPPABLE MICROPHONE STREAM
# ============================================================

class _StoppableStream:
    """The microphone stream of ONE push-to-talk listen. read() returns b""
    (the end of the stream, for SpeechRecognition's listen()) once `stop` is
    set, so a second press ends the listen within one chunk. It notes whether
    any chunk was loud enough to count as speech -- listen()'s own test,
    against the recognizer's current energy_threshold. close() passes
    through, so Microphone.__exit__ closes the real stream. on_level
    (optional) gets each chunk's normalized level (mic_level) and 0.0 when
    the stream closes; it is never asked to do anything else."""

    def __init__(self, stream, stop, rec, sample_width, on_level=None):
        import audioop

        self._stream, self._stop, self._rec = stream, stop, rec
        self._width, self._rms = sample_width, audioop.rms
        self._on_level = on_level
        self.speech = False

    def read(self, size):
        if self._stop.is_set():
            return b""
        buf = self._stream.read(size)
        if buf and (not self.speech or self._on_level is not None):
            try:
                rms = self._rms(buf, self._width)
            except Exception:
                rms = None
            if rms is not None:
                if not self.speech:
                    try:
                        self.speech = rms > self._rec.energy_threshold
                    except Exception:
                        pass
                self._report(mic_level(rms, self._width))
        return buf

    def _report(self, level):
        if self._on_level is not None:
            try:
                self._on_level(level)
            except Exception:
                pass

    def close(self):
        self._report(0.0)
        return self._stream.close()


# ============================================================
# MICROPHONE LEVEL
# ============================================================

MIC_LEVEL_FLOOR_DB = -50.0    # dBFS that reads as 0 (room noise)
MIC_LEVEL_TOP_DB = -15.0      # dBFS that reads as 1 (loud speech)


def mic_level(rms, sample_width):
    """A chunk's RMS as a 0..1 level: its dBFS mapped linearly from
    MIC_LEVEL_FLOOR_DB (0) to MIC_LEVEL_TOP_DB (1), clamped. A number only;
    no audio is kept."""
    try:
        rms = float(rms)
        full = float(2 ** (8 * int(sample_width) - 1))
    except (TypeError, ValueError):
        return 0.0
    if rms <= 0 or full <= 0:
        return 0.0
    db = 20.0 * math.log10(rms / full)
    level = (db - MIC_LEVEL_FLOOR_DB) / (MIC_LEVEL_TOP_DB - MIC_LEVEL_FLOOR_DB)
    return max(0.0, min(1.0, level))


# ============================================================
# BROWSER ACTIONS
# ============================================================

# core/api_browser.py -- MistAI's Playwright browser actions (browser_*)
#
# Methods of core.api.Api, moved here verbatim (Api inherits BrowserActionsMixin).

# ============================================================
# BROWSER ACTION HANDLERS
# ============================================================

NAV_LIMIT_S = NAV_TIMEOUT_MS // 1000   # browser_navigate's load limit, in seconds


class BrowserActionsMixin:
    # ============================================================
    # BROWSER RESULT HELPERS
    # ============================================================

    # A browser_click parameter that is a list number: 12, [12], #12.
    # "12", "[12]", "#12", or a list line pasted whole: '[12] link "Name"'.
    _BROWSER_REF_RX = re.compile(r'^\s*(?:\[?\s*#?\s*(\d{1,5})\s*\]?|\[(\d{1,5})\]\s+[a-z]+\s+".*)\s*$',
                                 re.S)

    @staticmethod
    def _browser_error(e):
        """One line for a failed browser call, never Playwright's call log."""
        text = str(e or "") or type(e).__name__
        if "Download is starting" in text:
            return "the URL is a file download, not a page"
        first = text.splitlines()[0].strip()
        return first[:200] + ("..." if len(first) > 200 else "")

    def _browser_note(self, text):
        """The session's one-shot note (the browser was reopened) before text."""
        take = getattr(self.browser, "take_note", None)
        note = take() if callable(take) else ""
        return f"NOTE: {note}. {text}" if note else text

    def _browser_tip(self, text):
        """config.BROWSER_NOTE once per phase, after the first element list
        (the same once-per-phase set as the APP_NOTES tips)."""
        given = self.__dict__.setdefault("_app_notes_given", set())
        if "browser" in given:
            return text
        given.add("browser")
        print("[AppNotes] browser note added")
        return f"{text}\n{BROWSER_NOTE}"

    def _elements_block(self, still_loading=None):
        """"\n" + the page's numbered element list, or "" (blank page, a stand-in
        browser without lists, or a failed read: logged as a type only)."""
        get = getattr(self.browser, "elements_text", None)
        if not callable(get):
            return ""
        try:
            text = get(still_loading) if still_loading is not None else get()
        except Exception as e:
            print(f"[Browser] element list failed ({type(e).__name__})")
            return "\n(The element list could not be read; call browser_elements.)"
        return self._browser_tip("\n" + text) if text else ""

    def _downloads_dir(self):
        """The user's Downloads folder (registry shell folder, OneDrive-aware)."""
        return self._resolve_path_placeholders("{downloads}")

    def _save_download_text(self):
        """Save the download the last browser_click/browser_navigate started
        into Downloads (numbered, never overwriting) and say where. Only
        these two actions call it: no other download is saved."""
        try:
            path = self.browser.save_download(self._downloads_dir())
        except Exception as e:
            if "timed out" in str(e):
                print("[Browser] download still running at the save wait")
                return (" It is still downloading; it will be saved to the Downloads folder "
                        "when it finishes.")
            print(f"[Browser] download not saved ({type(e).__name__})")
            return f" It could not be saved to the Downloads folder: {self._browser_error(e)}."
        print("[Browser] download saved to Downloads")
        return f" Saved it to the Downloads folder as {os.path.basename(path)}."

    def _page_text(self):
        try:
            return " ".join((self.browser.get_text("") or "").split())
        except Exception:
            return ""

    def _page_excerpt(self, limit=900, text=None):
        """First chunk of the current page's real DOM text, handed back in the
        same observation as a navigate/search (so the model need not call
        browser_get_text as a second step). The page's main content (<main>,
        the one <article>: BrowserSession.main_text) when it has one, so
        menus do not fill the excerpt; a cut one says how much is left."""
        text = self._page_text() if text is None else text
        total = len(text)
        main = getattr(self.browser, "main_text", None)
        if callable(main) and len(text) > limit:
            try:
                got = main(timeout=2.0)
            except Exception:
                got = None
            if isinstance(got, dict) and got.get("src") in ("main", "article"):
                body = " ".join(str(got.get("text") or "").split())
                if len(body) >= 40:
                    text, total = body, len(body) + max(
                        0, int(got.get("total") or 0) - len(str(got.get("text") or "")))
        if total <= limit:
            return text
        return (text[:limit] + f"... [{total - limit} more characters; use browser_get_text "
                "to read more]")

    # ============================================================
    # BROWSER ACTION HANDLERS
    # ============================================================

    def _act_browser_import_cookies(self, param):
        if not param:
            self.last_result_text = "browser_import_cookies needs domain(s), e.g. 'youtube.com' or 'youtube.com|google.com'"
            return False
        try:
            counts = self.browser.import_cookies(param)
            self.last_result_text = f"Imported cookies: {counts}. Navigate/reload the page to see the logged-in state."
            return True
        except Exception as e:
            self.last_result_text = f"browser_import_cookies failed: {self._browser_error(e)}"
            return False

    def _act_browser_use_daily_profile(self, param):
        try:
            source, copied = self.browser.use_daily_profile_copy()
            self.last_result_text = (f"Copied {len(copied)} login item(s) from the user's Firefox "
                                     "profile. The next browser action will use them.")
            return True
        except Exception as e:
            self.last_result_text = f"browser_use_daily_profile failed: {self._browser_error(e)}"
            return False

    def _act_browser_open(self, param):
        try:
            self.browser.ensure_started()
            if self.browser._start_error:
                self.last_result_text = self.browser._start_error
                return False
            if param:
                url, title = self.browser.navigate(param)
                self.last_result_text = self._browser_note(
                    f"Opened {url} ({title})") + self._elements_block()
            else:
                self.last_result_text = self._browser_note("Browser ready")
                if (getattr(self.browser, "last_url", "") or "about:blank") != "about:blank":
                    self.last_result_text += self._elements_block()
            return True
        except Exception as e:
            self.last_result_text = f"browser_open failed: {self._browser_error(e)}"
            return False

    def _act_browser_navigate(self, param):
        if not param:
            self.last_result_text = "browser_navigate needs a URL"
            return False
        try:
            url, title = self.browser.navigate(param)
            download = getattr(self.browser, "last_nav_download", None)
            if download:
                self.last_result_text = self._browser_note(
                    f"browser_navigate: that address is a file download ({str(download)[:80]}), "
                    "not a page.") + self._save_download_text()
                return True
            still = bool(getattr(self.browser, "last_nav_still_loading", False))
            stopped = bool(getattr(self.browser, "last_nav_stopped", False))
            text = self._page_text()
            reason = blocked_page_reason(url, title, text)
            if reason:
                print(f"[Browser] browser_navigate: block page ({reason})")
                self.last_result_text = (
                    f"BLOCKED: {reason}. This is a block/CAPTCHA page, not the content; "
                    f"do not try to solve it. Use browser_search for the information instead.")
                return False
            if stopped and not text.strip():
                self.last_result_text = self._browser_note(
                    f"browser_navigate: the page did not load within {NAV_LIMIT_S}s (the load "
                    "was stopped; nothing is shown yet). Try again later or use browser_search.")
                return False
            excerpt = self._page_excerpt(text=text)
            self.last_result_text = f"Navigated to {url} ({title})."
            if stopped:
                self.last_result_text += (
                    f" The page was still loading after {NAV_LIMIT_S}s; the load was stopped. "
                    "Here is what is on it.")
            elif still:
                self.last_result_text += (
                    " The page is still loading; here is what is on it so far.")
            if excerpt:
                self.last_result_text += (
                    f" PAGE TEXT (already read -- do NOT navigate again; use "
                    f"browser_get_text with a selector only if you need more): "
                    f"{excerpt}"
                )
            self.last_result_text = self._browser_note(self.last_result_text) + self._elements_block()
            self.speak(f"Navigating to {url}")
            return True
        except Exception as e:
            self.last_result_text = f"browser_navigate failed: {self._browser_error(e)}"
            return False

    def _act_browser_search(self, param):
        if not param:
            return False
        blocked = []  # (route name, fixed reason) of each route that was blocked or empty
        try:
            for name, base in search_routes():
                url, title = self.browser.navigate(search_url(base, param))
                text = self._page_text()
                reason = blocked_page_reason(url, title, text) or (
                    "the results page was empty" if not text.strip() else None)
                if reason:
                    print(f"[Browser] browser_search: {name} showed a block page ({reason})")
                    blocked.append((name, reason))
                    continue
                excerpt = self._page_excerpt(text=text)
                self.last_result_text = f"Searched {name} for '{param}'."
                if blocked:
                    self.last_result_text += (
                        f" ({', '.join(n for n, _ in blocked)} showed a block page.)")
                if excerpt:
                    self.last_result_text += (
                        f" RESULTS PAGE TEXT (already read -- do NOT search again; "
                        f"answer from this, or browser_click a result): {excerpt}"
                    )
                self.last_result_text = (self._browser_note(self.last_result_text)
                                         + self._elements_block())
                self.speak(f"Searching for {param}")
                return True
        except Exception as e:
            self.last_result_text = f"browser_search failed: {self._browser_error(e)}"
            return False
        self.last_result_text = (
            "BLOCKED: the search was blocked ("
            + "; ".join(f"{n}: {r}" for n, r in blocked)
            + "). Do not try to solve it or search another way (no browser app, no "
            "address bar); tell the user the web search was blocked.")
        return False

    def _act_browser_close(self):
        try:
            self.browser.close()
            self.last_result_text = "Browser closed"
            self.speak("Browser closed")
            return True
        except Exception as e:
            self.last_result_text = f"browser_close failed: {self._browser_error(e)}"
            return False

    def _act_browser_elements(self, param=""):
        """browser_elements: a fresh numbered list (never starts the browser)."""
        if not getattr(self.browser, "running", False):
            self.last_result_text = ("browser_elements: MistAI's browser is not open; use "
                                     "browser_navigate or browser_search first.")
            return False
        block = self._elements_block()
        if not block:
            self.last_result_text = self._browser_note(
                "browser_elements: no page is open (blank tab); use browser_navigate first.")
            return False
        self.last_result_text = self._browser_note(
            f"Elements of {getattr(self.browser, 'last_url', '') or 'the current page'}:") + block
        return True

    def _act_browser_click(self, param):
        """browser_click <number> (preferred), <text> or <text|role>. The
        result says what the click did; the fresh list follows unless nothing
        changed or a download started."""
        if not str(param or "").strip():
            self.last_result_text = ("browser_click needs an element number from the list "
                                     "(or the visible text to click)")
            return False
        m = self._BROWSER_REF_RX.match(str(param))
        try:
            if m:
                res = self.browser.click(ref=int(m.group(1) or m.group(2)))
            else:
                text, _, role = str(param).partition("|")
                res = self.browser.click(text.strip(), role=(role.strip() or None))
        except Exception as e:
            self.last_result_text = self._browser_note(
                f"browser_click failed: {self._browser_error(e)}")
            return False
        if not isinstance(res, dict):       # a stand-in browser
            self.last_result_text = f"Clicked '{str(param).strip()}' in the browser"
            return True
        text = self._browser_note(str(res.get("message") or ""))
        if res.get("ok") and getattr(self.browser, "last_download", None) is not None:
            text += self._save_download_text()
        if not res.get("ok") or res.get("changed"):
            text += self._elements_block()
        self.last_result_text = text
        return bool(res.get("ok"))

    def _act_browser_scroll(self, param):
        """browser_scroll down|up|top|bottom (default down), then the list."""
        d = str(param or "down").strip().lower() or "down"
        if d not in ("down", "up", "top", "bottom"):
            self.last_result_text = "browser_scroll needs down, up, top or bottom"
            return False
        try:
            moved, pct = self.browser.scroll(d)
        except Exception as e:
            self.last_result_text = f"browser_scroll failed: {self._browser_error(e)}"
            return False
        text = (f"Scrolled {d}; the bottom of the screen is now at about {pct}% of the page."
                if moved else f"The page did not scroll {d} (already there, or it does not scroll).")
        self.last_result_text = self._browser_note(text) + self._elements_block()
        return True

    def _act_browser_fill(self, param):
        if "|" not in (param or ""):
            self.last_result_text = "browser_fill needs format: field label or number|text to type"
            return False
        field, text = param.split("|", 1)
        try:
            done = self.browser.fill(field.strip(), text)
        except Exception as e:
            self.last_result_text = self._browser_note(f"browser_fill failed: {self._browser_error(e)}")
            return False
        what = done if isinstance(done, str) else f"'{field.strip()}'"
        self.last_result_text = self._browser_note(
            f"Filled {what} (not submitted: press Enter or click the button).")
        return True

    def _act_browser_press_key(self, param):
        if not param:
            return False
        try:
            self.browser.press_key(param.strip())
            self.last_result_text = self._browser_note(f"Pressed '{param.strip()}' in the browser")
            return True
        except Exception as e:
            self.last_result_text = f"browser_press_key failed: {self._browser_error(e)}"
            return False


# ============================================================
# SYSTEM UTILITIES
# ============================================================

# core/api_system.py -- system utilities: brightness, volume, reminders, facts, battery, locking, quick info
#
# Methods of core.api.Api, moved here verbatim (Api inherits SystemActionsMixin).

# ============================================================
# SYSTEM ACTION HANDLERS
# ============================================================

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


# ============================================================
# WINDOW ACTIONS
# ============================================================

# core/api_windows.py -- window_activate / window_close, maximize, minimize
#
# Methods of core.api.Api, moved here verbatim (Api inherits WindowActionsMixin).

# ============================================================
# WINDOW ACTION HANDLERS
# ============================================================

class WindowActionsMixin:
    def _act_window_op(self, method_name, param):
        if not self.enhanced_available:
            self.last_result_text = "Window management module isn't available."
            self.speak(self.last_result_text)
            return False
        fn = getattr(self.window_mgr, method_name)
        ok = fn(param)
        self.last_result_text = (
            f"{method_name.replace('_', ' ').title()}: {param}"
            if ok
            else f"Could not find window: {param}"
        )
        self.speak(self.last_result_text)
        return ok

    def _act_window_activate(self, param):
        """Find the window by title first, then focus it, so "not found" and
        "Windows refused focus" are reported as the different facts they are.
        One retry compares normalized titles (dashes, trailing separators)."""
        if not self.enhanced_available:
            self.last_result_text = "Window management module isn't available."
            self.speak(self.last_result_text)
            return False
        win = None
        try:
            win = (self.window_mgr.find_window(param)
                   or self.window_mgr.find_window_normalized(param))
        except Exception as e:
            print(f"[WindowActivate] lookup failed: {e if verbose_logs() else type(e).__name__}")
        hwnd = getattr(win, "_hWnd", None)
        if not hwnd:
            self.last_result_text = f"Could not find window: {param}"
            self.speak(self.last_result_text)
            return False
        title = (getattr(win, "title", "") or "")[:40]
        ok, reason = self._focus_hwnd(hwnd)
        self.last_result_text = (
            f"Activate Window: {title}" if ok
            else f"Found window '{title}' but could not bring it to the front: {reason}"
        )
        self.speak(self.last_result_text)
        return ok

# ============================================================
# SAVE AS DIALOG HANDLER
# ============================================================

    # save_file_as(path): the keyboard route through the foreground app's Save
    # As dialog, one fixed sequence: Ctrl+S, a new window of the same process,
    # the file-name box found through UIA by role and name prefix (walked by
    # the dialog's handle), select all, the full path typed once, Enter, a
    # replace prompt answered from the agent's confirmation, the file checked
    # on disk. It stops (and says where) at the first step that does not hold;
    # it never types a path when no dialog appeared. Logs: fixed labels only.
    SAVE_DIALOG_WAIT_S = 3.0      # Ctrl+S -> the dialog in front
    SAVE_CLOSE_WAIT_S = 6.0       # Enter -> the dialog gone (or a prompt)
    SAVE_FILE_WAIT_S = 2.0        # the dialog gone -> the file on disk
    SAVE_TITLE_WAIT_S = 1.0       # ... and the document's title updated
    SAVE_NAME_BOX_WAIT_S = SAVE_NAME_BOX_WAIT_S   # the dialog in front -> its file-name box
    SAVE_POLL_S = 0.1
    SAVE_PATH_MAX = 260
    SAVE_NAME_PREFIX = "file name"    # the file-name box: an Edit named "File name:"
    SAVE_NAME_ID = "1001"             # its AutomationId in the common file dialog
    SAVE_DIALOG_CLASS = "#32770"

    def _save_path_problem(self, path):
        """Why ``path`` cannot be a save target, or ''."""
        if not path:
            return "no path was given (save_file_as needs the full path with the file name)."
        if "\n" in path or "\r" in path or len(path) > self.SAVE_PATH_MAX:
            return "the path is not one line of at most 260 characters."
        if not os.path.isabs(path) or not os.path.basename(path):
            return "the path must be a full path that ends with the file name."
        if os.path.isdir(path):
            return "that path is a folder; add the file name."
        if not os.path.isdir(os.path.dirname(path)):
            return "its folder does not exist (create it first)."
        return ""

    @staticmethod
    def _save_window(hwnd):
        """(hwnd, pid, title, class) of a window; zeros when it is gone."""
        try:
            import win32gui
            import win32process

            if not hwnd or not win32gui.IsWindow(hwnd):
                return (0, 0, "", "")
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            return (hwnd, pid, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd))
        except Exception:
            return (0, 0, "", "")

    def _save_foreground(self):
        try:
            import win32gui

            return self._save_window(win32gui.GetForegroundWindow())
        except Exception:
            return (0, 0, "", "")

    @staticmethod
    def _save_window_shown(hwnd):
        try:
            import win32gui

            return bool(hwnd) and bool(win32gui.IsWindow(hwnd)) and bool(
                win32gui.IsWindowVisible(hwnd))
        except Exception:
            return False

    @staticmethod
    def _save_owner(hwnd):
        """The window that owns a dialog (the document window), or 0."""
        try:
            import win32con
            import win32gui

            return win32gui.GetWindow(hwnd, win32con.GW_OWNER) or 0
        except Exception:
            return 0

    def _save_wait(self, condition, timeout):
        """Wait for ``condition()``: woken by window events when they are
        healthy, else polled; True when it held within ``timeout`` s."""
        watched = getattr(getattr(self, "live", None), "window_events", None)
        if isinstance(watched, clip.WindowEvents) and watched.healthy():
            return watched.wait_for(condition, timeout, poll=self.SAVE_POLL_S)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if condition():
                    return True
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.SAVE_POLL_S)

    def _save_name_box(self, hwnd):
        """(element info, how) of the dialog's file-name box, or (None, '')."""
        nav = getattr(self, "uia_nav", None)
        find = getattr(nav, "find_in_window", None)
        if not callable(find):
            return None, ""
        return find(hwnd, "Edit", self.SAVE_NAME_PREFIX, self.SAVE_NAME_ID)

    @staticmethod
    def _save_field_value(element):
        """The text in an edit box through UIA (ValuePattern), or None."""
        for read in (lambda: element.get_value(),
                     lambda: element.iface_value.CurrentValue,
                     lambda: element.legacy_properties()["Value"]):
            try:
                value = read()
            except Exception:
                continue
            if value is not None:
                return str(value)
        return None

    def _save_press_button(self, hwnd, name):
        """Invoke the button named ``name`` (prefix) in window ``hwnd``."""
        nav = getattr(self, "uia_nav", None)
        find = getattr(nav, "find_in_window", None)
        if not callable(find):
            return False
        info, _how = find(hwnd, "Button", name)
        if info is None:
            return False
        try:
            info.element.invoke()
            return True
        except Exception:
            return bool(nav.click_element(info.element))

    def _save_wait_name_box(self, hwnd):
        """(element info, how, seconds waited, looks): the file-name box looked
        for again every SAVE_BOX_POLL_S (woken early by window events when they
        are healthy) until it is in the tree or SAVE_NAME_BOX_WAIT_S passed.
        The first look costs no wait."""
        started = time.monotonic()
        found = {"box": (None, ""), "looks": 0}

        def ready():
            found["looks"] += 1
            found["box"] = self._save_name_box(hwnd)
            return found["box"][0] is not None

        if not ready():
            watched = getattr(getattr(self, "live", None), "window_events", None)
            if isinstance(watched, clip.WindowEvents) and watched.healthy():
                # Re-checked on every window event and at least every poll.
                watched.wait_for(ready, self.SAVE_NAME_BOX_WAIT_S, poll=SAVE_BOX_POLL_S)
            else:
                deadline = started + self.SAVE_NAME_BOX_WAIT_S
                while time.monotonic() < deadline:
                    time.sleep(min(SAVE_BOX_POLL_S, max(0.0, deadline - time.monotonic())))
                    if ready():
                        break
        box, how = found["box"]
        return box, how, time.monotonic() - started, found["looks"]

    def _save_stop(self, label, text):
        print(f"[SaveAs] stopped: {label}")
        self.last_result_text = f"NOT SAVED: {text}"
        return False

    def _act_save_file_as(self, param):
        raw = str(param or "").strip().strip('"').strip()
        resolve = getattr(self, "_resolve_path_placeholders", None)
        path = raw
        if raw and callable(resolve):
            path = resolve(raw)
        path = os.path.expandvars(os.path.expanduser(path)) if path else ""
        problem = self._save_path_problem(path)
        if problem:
            return self._save_stop("bad path", problem + " Nothing was pressed.")
        path = os.path.normpath(path)
        name = os.path.basename(path)

        report, on_target = self._ensure_input_target("save shortcut")
        if not on_target:
            print(f"[SaveAs] {self._input_report_for_log(report, False)}")
            self.last_result_text = report
            return False

        existed = os.path.isfile(path)     # True only after the user approved replacing it
        started = time.time()
        front = self._save_foreground()
        if not front[0]:
            return self._save_stop("no window", "no window is in front. Nothing was pressed.")

        # 1. The dialog: already open (an earlier Ctrl+S), else Ctrl+S opens it.
        dialog, doc_hwnd = None, 0
        if front[3] == self.SAVE_DIALOG_CLASS and self._save_name_box(front[0])[0] is not None:
            dialog, doc_hwnd = front, self._save_owner(front[0])
            doc_title = self._save_window(doc_hwnd)[2] if doc_hwnd else ""
            print("[SaveAs] a Save As dialog was already in front")
        else:
            doc_hwnd, doc_title = front[0], front[2]
            pyautogui.hotkey("ctrl", "s")
            seen = {}

            def appeared():
                now = self._save_foreground()
                if now[0] and now[0] != front[0]:
                    seen["w"] = now
                    return True
                return False

            self._save_wait(appeared, self.SAVE_DIALOG_WAIT_S)
            new = seen.get("w")
            if new is None:
                after = self._save_window(front[0])
                saved_in_place = bool(after[0]) and after[2] != front[2]
                return self._save_stop(
                    "no dialog",
                    "no Save As dialog appeared after Ctrl+S, so nothing was typed."
                    + (" The window title changed, so the document was probably saved in place "
                       "under its existing name." if saved_in_place else "")
                    + " To save under this path, open the app's Save As (for example hotkey "
                      "ctrl+shift+s) and call save_file_as again; it uses an open dialog.")
            if new[1] != front[1]:
                return self._save_stop(
                    "other app",
                    "after Ctrl+S a window of another app came to the front instead of a Save As "
                    "dialog. Nothing was typed.")
            dialog = new
            print("[SaveAs] dialog appeared")

        # 2. The file-name box: by role and name prefix (never with its value).
        # A new dialog can be in front before its box is in the tree: look again.
        box, how, waited, looks = self._save_wait_name_box(dialog[0])
        if box is None:
            return self._save_stop(
                f"no file-name box (waited {waited:.2f}s, {looks} looks)",
                "a dialog opened, but no file-name box was found in it through the "
                "accessibility tree. Nothing was typed; the dialog is still open.")
        if looks > 1:
            print(f"[SaveAs] file-name box ready after {waited:.2f}s ({looks} looks)")
        print(f"[SaveAs] file-name box found by {how}")

        # 3. Replace its content: focus, select all, the full path typed once.
        try:
            box.element.set_focus()
        except Exception:
            return self._save_stop("focus failed",
                                   "the file-name box could not be focused. Nothing was typed; "
                                   "the dialog is still open.")
        time.sleep(0.1)
        if self._save_foreground()[0] != dialog[0]:
            return self._save_stop("dialog not in front",
                                   "the dialog is no longer in front. Nothing was typed.")
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        if not clip.send_unicode(path):
            return self._save_stop("typing failed",
                                   "typing the path into the file-name box failed; Enter was not "
                                   "pressed and the dialog is still open.")
        time.sleep(0.15)
        value = self._save_field_value(box.element)
        match = (None if value is None
                 else os.path.normcase(value.strip()) == os.path.normcase(path))
        print(f"[SaveAs] typed {len(path)} chars, read back: "
              + {None: "unreadable", True: "match", False: "mismatch"}[match])
        if match is False:
            return self._save_stop(
                "field mismatch",
                "after typing, the file-name box does not hold exactly the path (an "
                "autocomplete?). Enter was not pressed; the dialog is still open.")

        # 4. Enter; then the dialog closes, or a prompt of the same app appears.
        pyautogui.press("enter")
        outcome = {}

        def settled():
            if not self._save_window_shown(dialog[0]):
                outcome["k"] = "closed"
                return True
            now = self._save_foreground()
            if now[0] and now[0] not in (dialog[0], doc_hwnd) and now[1] == dialog[1]:
                outcome["k"], outcome["w"] = "prompt", now
                return True
            return False

        self._save_wait(settled, self.SAVE_CLOSE_WAIT_S)
        if outcome.get("k") == "prompt":
            prompt = outcome["w"]
            nav = getattr(self, "uia_nav", None)
            find = getattr(nav, "find_in_window", None)
            has_yes = callable(find) and find(prompt[0], "Button", "yes")[0] is not None
            if not has_yes:
                return self._save_stop(
                    "message",
                    "after Enter the app showed a message instead of saving (it was left open; "
                    "read it). The Save As dialog is still open.")
            if not existed:
                pressed = self._save_press_button(prompt[0], "no")
                if not pressed:
                    pyautogui.press("escape")
                print("[SaveAs] replace prompt: not approved, answered no")
                return self._save_stop(
                    "not replaced",
                    f"a file named '{name}' already exists there and replacing it was not "
                    "approved, so it was NOT replaced and nothing was overwritten. The Save As "
                    "dialog is still open.")
            print("[SaveAs] replace prompt: approved, answered yes")
            if not self._save_press_button(prompt[0], "yes"):
                return self._save_stop(
                    "replace prompt",
                    "the replace prompt's Yes button could not be pressed; nothing was replaced "
                    "and the prompt is still open.")
            self._save_wait(lambda: not self._save_window_shown(dialog[0]),
                            self.SAVE_CLOSE_WAIT_S)
            if self._save_window_shown(dialog[0]):
                return self._save_stop("dialog still open",
                                       "the Save As dialog is still open after Yes.")
        elif outcome.get("k") != "closed":
            return self._save_stop(
                "dialog still open",
                "the Save As dialog is still open after Enter. Read it before trying again.")

        # 5. Verify: the file on disk, written now; the document's title.
        def written():
            try:
                return os.path.isfile(path) and os.path.getmtime(path) >= started - 2
            except OSError:
                return False

        if not self._save_wait(written, self.SAVE_FILE_WAIT_S):
            return self._save_stop(
                "file missing",
                f"the dialog closed, but no file was written at {path}. Do not report it "
                "as saved.")
        renamed = self._save_wait(
            lambda: self._save_window(doc_hwnd)[2] not in ("", doc_title),
            self.SAVE_TITLE_WAIT_S) if doc_hwnd else False
        title = self._save_window(doc_hwnd)[2][:60] if doc_hwnd else ""
        size = os.path.getsize(path)
        print(f"[SaveAs] saved (file checked, title changed: {'yes' if renamed else 'no'})")
        self.last_result_text = (
            f"Saved to {path} (checked on disk: {size} bytes"
            + (f"; the window title is now '{title}'" if renamed
               else "; the window title did not change")
            + ")." + (" It replaced the existing file, as approved." if existed else ""))
        return True


# ============================================================
# API CLASS
# ============================================================

class Api(WindowActionsMixin, SystemActionsMixin, BrowserActionsMixin):
    _BROWSER_APP_NAMES = {"firefox", "chrome", "edge", "browser"}

    _DEFAULT_HOTKEY = "ctrl+k"  # fallback only if the LLM genuinely can't answer
    # type_text: while another app holds the clipboard, retry after short
    # jittered waits (clip.retry_delays, 1.5 s in all), then fall back to keys.
    CLIPBOARD_BUDGET_S = clip.RETRY_BUDGET_S
    _PROJECT_MUTATION_CUES = re.compile(
        r"\b(?:convert|migrat|refactor|build|create|write|edit|modify|update|"
        r"install|add|remove|cleanup|delete)\b.*\b(?:project|website|app|react|"
        r"package(?:\.json)?|frontend|codebase|repository|repo|site)\b",
        re.IGNORECASE,
    )

    def __init__(self):
        announce_verbose_logs()
        self._stop_requested = threading.Event()
        # -- Agent run gate ---------------------------------------------------
        # Exactly one AgentLoop may drive the machine at a time. The lock is
        # taken in ask_mistai() and released ONLY by the agent thread's own
        # done/error callback -- never by the caller returning -- so a UI
        # timeout, a voice turn ending, or a wake-word task starting while a
        # previous task is still executing cannot produce two loops fighting
        # over the mouse, clipboard and last_result_text.
        self._agent_lock = threading.Lock()
        # Serializes the small ownership transition around the run lock and
        # stop flag. Without it, a Stop intended for a finishing task could
        # race a just-starting task and cancel the new one instead.
        self._agent_state_lock = threading.Lock()
        self._agent_task = None          # description of the running task
        self._agent_started_at = 0.0
        self._agent_generation = 0       # bumped per run; stale callbacks are ignored
        from .browser_session import BrowserSession
        # "Use my Firefox sign-ins" is read at each browser start.
        self.browser = BrowserSession(firefox_signins=self.get_firefox_signins)
        self.browser.pointer_fn = self._orb_point_page
        self.browser.pointer_active_fn = self._orb_pointer_active
        self._pending_answer = None
        self._pending_source = None     # "button" or "typed", with _pending_answer
        self._answer_event = threading.Event()
        self._awaiting_text_answer = threading.Event()
        self._answer_lock = threading.Lock()
        os.environ["PYWINAUTO_BACKEND"] = "win32"
        self._mic_lock = threading.Lock()
        self.api_manager = APIKeyManager()
        self.last_result_text = ""
        self.last_command_result = None
        # Set for the lifetime of one agent request. It prevents a project
        # conversion from creating files before the existing project has been
        # identified from evidence.
        self._workspace_required_for_task = False
        # Roots may be opened in VS Code only when they came from the user,
        # reliable VS Code evidence, or the already-established workspace.
        # This is deliberately separate from file snapshots: a model can
        # inspect any readable file, but that must not make its parent folder
        # a trusted project root.
        self._trusted_project_roots = set()
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.is_listening = False
        self._listening_lock = threading.Lock()
        self.last_tts_error = ""
        self.tts = NeuralTTS(on_error=self._on_tts_error)
        # The Start-apps list for open_app (core/start_apps.py); main() loads
        # it in the background, so startup never waits for Get-StartApps.
        from .start_apps import StartAppsCache
        self.start_apps = StartAppsCache()
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        self.memory = Memory()
        from .memory import TaskMemory

        self.task_memory = TaskMemory()
        # Nothing is seeded: built-in hotkeys and MistAI's facts about itself
        # are read from code (config.DEFAULT_HOTKEYS, config.MISTAI_FACTS).
        self.tts.set_gender(self.memory.get_profile("user_gender", "none"))
        self._model_override = None  # live_checks --model only (current_model)
        self.vision = CohereVision()
        self.live = LiveStateTracker()
        # Frames on demand + change detection (WindowWatch), fed by the
        # tracker's window events; no capture loop.
        self.screen = WindowWatch(live=self.live)
        self.click_handler = ImprovedClickHandler(
            live_state=self.live, vision_engine=self.vision, screen_buffer=self.screen
        )
        self.click_handler.target_rect_fn = self._click_target_rect
        # The OCR mask finds MistAI's own window by handle, never by title.
        self.click_handler.own_window_fn = self._own_window_handle
        # ...and the floating orb (when shown) by its handle too.
        self.click_handler.extra_own_windows_fn = self._orb_window_handles
        # Every click path points the floating orb at its target first (a
        # no-op returning False unless the orb is shown with flight + pointer).
        self.click_handler.pointer_fn = self._orb_point
        self.uia_nav = EnhancedUIANavigator()
        self.uia_nav.pointer_fn = self._orb_point
        self.agent = AgentLoop(self, self.uia_nav)

        # -- Event-driven core: bus, workspace, tasks, tools, executor --------
        self.bus = EventBus()
        self.workspace = Workspace(self.bus)
        self.task_store = TaskStore()
        self.tools = ToolRegistry()
        self.executor = Executor(self)
        self.state_watcher = StateWatcher(self.bus, self.live)
        self.file_watcher = FileWatcher(
            self.bus
        )  # Downloads + project roots via ProjectSet

        self.enhanced_available = ENHANCED_UTILS_AVAILABLE
        if self.enhanced_available:
            self.window_mgr = enhanced_utils.WindowManager
            self.task_scheduler = enhanced_utils.TaskScheduler()
            self.screen_control = enhanced_utils.ScreenControl()
        else:
            self.window_mgr = None
            self.task_scheduler = None
            self.screen_control = None

        self.wake_word_active = False
        self._wake_thread = None
        self._wake_enabled = threading.Event()
        self._wake_shutdown = threading.Event()
        self._wake_lock = threading.Lock()
        self._conversation_lock = threading.Lock()
        # Diagnostics only: voice-turn durations, and whether a _listen_once
        # is waiting for the mic while the wake loop transcribes a clip.
        self.voice_timer = VoiceTimer()
        self._listen_waiting = threading.Event()
        # Set while a push-to-talk press waits for or holds the mic: the wake
        # loop skips taking the mic so it cannot swallow the command.
        self._ptt_waiting = threading.Event()
        # Set by a second press during a push-to-talk listen: ends it early.
        self._ptt_stop = threading.Event()
        self.captions_enabled = False
        self.caption_window = None
        self._window = None
        stats = self.memory.get_stats()
        print(
            f"[MistAI] Ready - {stats['conversations']} conversations in memory "
            f"(enhanced utils: {'on' if self.enhanced_available else 'off'})"
        )

    # -- Agent run state ------------------------------------------------------

    def is_agent_busy(self):
        """Ground truth for 'is Mist doing something': true iff an AgentLoop
        thread currently holds the run gate. The UI polls this instead of
        inferring idleness from its own request timeouts; it also reports
        whether Mist is speaking (the orb)."""
        return {
            "busy": self._agent_lock.locked(),
            "task": (self._agent_task or "")[:80],
            "elapsed": round(time.time() - self._agent_started_at, 1)
            if self._agent_lock.locked() else 0,
            "stop_requested": self._stop_requested.is_set(),
            # Mist's voice is playing (the page's orb shows "speaking").
            "speaking": bool(getattr(getattr(self, "tts", None), "speaking", False)),
        }

    def stop_current_task(self):
        """Request the running agent loop to halt at its next step boundary
        and cut off speech immediately. The loop cannot be pre-empted
        mid-LLM-call, so `busy` stays true until it actually exits -- the
        UI keeps showing 'Stopping...' until is_agent_busy() flips."""
        with self._agent_state_lock:
            was_running = self._agent_lock.locked()
            if was_running:
                self._stop_requested.set()
                # A text confirmation/question otherwise remains blocked for
                # up to its UI timeout. Wake it so AgentLoop can observe the
                # stop flag at once instead of leaving the UI falsely "busy".
                with self._answer_lock:
                    if self._awaiting_text_answer.is_set():
                        self._pending_answer = "__mistai_stop_requested__"
                        self._answer_event.set()
        self.tts.interrupt()  # drops the queue AND aborts current playback
        return {"success": True, "was_running": was_running}

    # -- Speech / Captions ----------------------------------------------------

    def _on_tts_error(self, message):
        """Surface a real speech-engine failure in the visible UI.

        Packaged builds run without a console, so a printed fallback failure is
        otherwise indistinguishable from MistAI silently ignoring the user.
        """
        self.last_tts_error = str(message or "Speech engine failed")
        if getattr(self, "captions_enabled", False) and getattr(
            self, "caption_window", None
        ):
            self.caption_window.show_error(self.last_tts_error, 8)
        self._notify_js(f'handleTtsError("{esc(self.last_tts_error)}")')

    def speak(self, text, interrupt=False):
        if not text:
            return
        self._last_spoke_at = time.monotonic()  # the heartbeat counts from here
        if self.captions_enabled and self.caption_window:
            self.caption_window.show_speech(text, 8)
        self.tts.say(text, interrupt=interrupt)

    speak_now = speak

    # -- Heartbeat ("Still working.") ------------------------------------------

    def _start_heartbeat(self, gen):
        """Start this task's heartbeat (narration "milestones" only). A chain
        of daemon one-shot timers; no thread outlives _cancel_heartbeat."""
        if narration_mode() != "milestones":
            return None
        hb = {"gen": gen, "count": 0, "since": time.monotonic(),
              "timer": None, "cancelled": False}
        self._schedule_heartbeat(hb, HEARTBEAT_SECONDS)
        return hb

    def _schedule_heartbeat(self, hb, delay):
        if hb["cancelled"]:
            return
        timer = threading.Timer(delay, self._heartbeat_tick, args=(hb,))
        timer.daemon = True
        hb["timer"] = timer
        timer.start()

    @staticmethod
    def _cancel_heartbeat(hb):
        if hb:
            hb["cancelled"] = True
            if hb["timer"] is not None:
                hb["timer"].cancel()

    def _mist_is_talking_or_listening(self):
        """Speaking, or waiting for the user (a command, conversation,
        confirmation or ask_user listen, push-to-talk, or a typed answer)."""
        if getattr(getattr(self, "tts", None), "speaking", False):
            return True
        return any(getattr(self, name, None) is not None and getattr(self, name).is_set()
                   for name in ("_listen_waiting", "_ptt_waiting", "_awaiting_text_answer"))

    def _heartbeat_tick(self, hb):
        """Runs on a timer thread; takes no lock. Speaks HEARTBEAT_SPEECH when
        the task has run HEARTBEAT_SECONDS since Mist last spoke or listened."""
        if (hb["cancelled"] or hb["gen"] != self._agent_generation
                or self._stop_requested.is_set() or not self._agent_lock.locked()):
            return
        now = time.monotonic()
        if self._mist_is_talking_or_listening():
            hb["since"] = now
            self._schedule_heartbeat(hb, HEARTBEAT_POLL_SECONDS)
            return
        due = max(hb["since"], getattr(self, "_last_spoke_at", 0.0)) + HEARTBEAT_SECONDS
        if now < due:
            self._schedule_heartbeat(hb, due - now)
            return
        if hb["cancelled"] or self._stop_requested.is_set():
            return  # the run ended or was stopped meanwhile
        hb["count"] += 1
        hb["since"] = now
        print(f"[Heartbeat] {hb['count']}/{HEARTBEAT_MAX}")
        self.speak(HEARTBEAT_SPEECH)
        if hb["count"] < HEARTBEAT_MAX:
            self._schedule_heartbeat(hb, HEARTBEAT_SECONDS)

    def _wait_for_tts(self, timeout=25):
        """Wait for Mist's own speech without letting a hung fallback engine
        hold a conversation/microphone lock forever."""
        if self.tts.wait_until_done(timeout=timeout):
            return True
        print(f"[TTS] Speech exceeded {timeout}s; interrupting it")
        self.tts.interrupt()
        return False

    def set_api_key(self, key):
        self.api_manager.set_api_key(key)
        return {"success": True, "message": "API key set."}

    def get_ui_flags(self):
        """What the Settings UI may show. Developer-only controls (the backend
        route switch) appear in a source run, or in a build with MISTAI_DEV=1.
        The route itself always comes from MISTAI_USE_BACKEND unless a
        developer switches it."""
        dev = (not getattr(sys, "frozen", False)
               or os.environ.get("MISTAI_DEV", "").strip() == "1")
        return {"is_dev": dev}

    def set_api_mode(self, use_backend):
        """Switch the backend route. Dev runs only (get_ui_flags); in a build
        it is refused and MISTAI_USE_BACKEND keeps deciding the route."""
        if not self.get_ui_flags()["is_dev"]:
            return {"success": False,
                    "error": "Switching the API route is a developer setting. "
                             "Set MISTAI_USE_BACKEND in .env instead."}
        self.api_manager.use_backend(use_backend)
        return {
            "success": True,
            "message": f"API mode: {'Backend' if use_backend else 'Direct API'}",
        }

    def minimize_window(self):
        try:
            if self._window:
                self._window.minimize()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _own_window_handle(self):
        """MistAI's own top-level window: pywebview's WinForms form
        (window.native.Handle), else the unowned top-level window of this
        process with the window's title. None when there is no window.
        Called outside _agent_state_lock (the title read messages the UI
        thread)."""
        window = getattr(self, "_window", None)
        if window is None:
            return None
        cached = getattr(self, "_own_hwnd", None)
        try:
            import win32gui

            if cached and win32gui.IsWindow(cached):
                return cached
        except Exception:
            return None
        hwnd = None
        try:
            hwnd = int(window.native.Handle.ToInt32()) or None
        except Exception:
            hwnd = None
        if hwnd is None:
            title = str(getattr(window, "title", "") or "")
            if not title:
                return None
            try:
                import win32process

                found = []

                def _cb(h, _):
                    try:
                        if (win32process.GetWindowThreadProcessId(h)[1] == os.getpid()
                                and not win32gui.GetWindow(h, 4)      # GW_OWNER
                                and win32gui.GetWindowText(h) == title):
                            found.append(h)
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(_cb, None)
                hwnd = found[0] if found else None
            except Exception:
                hwnd = None
        self._own_hwnd = hwnd
        return hwnd

    def _restore_after_task(self, gen):
        """Show MistAI's window again, WITHOUT activating it, when the run
        `gen` has really ended. Skipped when the setting is off, there is no
        window, a newer run has started or holds the gate, or a text prompt
        is open. Only async calls are made under _agent_state_lock (they post
        to the window's thread), so nothing can block while it is held. It
        never activates the window or uses pywebview's own restore."""
        try:
            if not restore_after_task_enabled():
                return
            hwnd = self._own_window_handle()
            if not hwnd:
                return
            import win32gui

            prompt = getattr(self, "_awaiting_text_answer", None)
            with self._agent_state_lock:
                if (gen != self._agent_generation or self._agent_lock.locked()
                        or (prompt is not None and prompt.is_set())):
                    return
                if not win32gui.IsIconic(hwnd):
                    return          # already shown (the user restored it)
                _show_window_async(hwnd, 4)                     # SW_SHOWNOACTIVATE
                # HWND_TOP does not lift it above the app in front; a brief
                # topmost then not-topmost puts it at the top of the normal
                # band. A window that is already topmost is left alone.
                flags = 0x4000 | 0x0010 | 0x0002 | 0x0001
                # SWP_ASYNCWINDOWPOS | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE
                if not win32gui.GetWindowLong(hwnd, -20) & 0x8:  # GWL_EXSTYLE, WS_EX_TOPMOST
                    try:
                        win32gui.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)  # HWND_TOPMOST
                    finally:
                        try:
                            win32gui.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)  # HWND_NOTOPMOST
                        except Exception as e:
                            print(f"[Window] Could not clear topmost: {type(e).__name__}")
            print("[Window] Restored and raised after the task (not activated)")
        except Exception as e:
            print(f"[Window] Restore after the task skipped: {type(e).__name__}")

    def toggle_captions(self, enabled):
        self.captions_enabled = enabled
        if enabled and not self.caption_window:
            self.caption_window = CaptionWindow()
        elif not enabled and self.caption_window:
            self.caption_window.destroy()
            self.caption_window = None
        return {"success": True}

    def show_caption(self, text, kind="system"):
        if self.captions_enabled and self.caption_window and kind != "user":
            if kind == "error":
                self.caption_window.show_error(text, 5)
            elif kind == "assistant":
                self.caption_window.show_speech(text, 8)
            else:
                self.caption_window.show_status(text, 4)
        return {"success": True}

    # -- Wake word --------------------------------------------------------------

    def start_wake_word_detection(self):
        """Enable the one long-lived wake worker.

        Toggling wake listening must never spawn a second listener while the
        old one is inside a microphone read. The worker stays dormant while
        disabled and is reused on the next enable, so it keeps its calibration
        across rapid UI toggles. It re-calibrates on its own schedule (idle,
        repeated untranscribable clips, out-of-band threshold; see
        core/wake_calibration.py).
        """
        with self._wake_lock:
            self.wake_word_active = True
            self._wake_enabled.set()
            if self._wake_thread and self._wake_thread.is_alive():
                return {"success": True, "message": "Already listening"}
            self._wake_shutdown.clear()
            self._wake_thread = threading.Thread(
                target=self._google_wake_loop, daemon=True, name="WakeWord"
            )
            self._wake_thread.start()
        return {"success": True}

    def stop_wake_word_detection(self):
        with self._wake_lock:
            self.wake_word_active = False
            self._wake_enabled.clear()
        return {"success": True}

    def shutdown_wake_word_detection(self):
        """Permanently end the wake worker during application teardown."""
        with self._wake_lock:
            self.wake_word_active = False
            self._wake_enabled.clear()
            self._wake_shutdown.set()
        return {"success": True}

    # Said on its own (or right after the wake word) while a task is running,
    # these halt the agent. Whole-utterance only -- "stop the music" is a task.
    _VOICE_STOP_WORDS = {"stop", "mist stop", "hey mist stop", "cancel",
                         "mist cancel", "stop it", "abort", "halt"}

    def _google_wake_loop(self):
        rec = sr.Recognizer()
        # Fixed threshold between calibrations. With dynamic adjustment the
        # threshold climbed during a loud "hey" until a quiet "mist" counted
        # as silence and was cut off (real logs: 'hey', 'hey miss').
        rec.dynamic_energy_threshold = False
        rec.pause_threshold = 0.8
        # Keep all trailing below-threshold audio up to the pause (the library
        # trims anything past non_speaking_duration) and a matching pre-roll.
        rec.non_speaking_duration = rec.pause_threshold
        cal = WakeCalibration()
        print(f"[Wake] Google listening for: {WAKE_WORDS}")
        save_clips = wake_clips_enabled()
        if save_clips:
            print(f"[Wake] MISTAI_WAKE_SAVE_CLIPS=1: saving raw mic clips to "
                  f"{wake_clip_dir()} (room audio; skim before sharing)")

        while not self._wake_shutdown.is_set():
            # While off, own no microphone resource. A later enable wakes this
            # same worker rather than racing a replacement worker into life.
            if not self._wake_enabled.wait(timeout=0.2):
                continue

            # Calibrate when due, never over TTS, and under the same mic lock
            # used by ask_user/manual capture. This prevents two PortAudio
            # streams from opening concurrently.
            reason = cal.due(time.monotonic())
            if reason:
                if self.tts.speaking and not self._wait_for_tts(timeout=5):
                    continue
                if not self._wake_enabled.is_set():
                    continue
                if self._ptt_wants_mic() or not self._mic_lock.acquire(blocking=False):
                    time.sleep(0.2)
                    continue
                try:
                    if self._wake_enabled.is_set():
                        with self.microphone as src:
                            rec.adjust_for_ambient_noise(src, duration=1.0)
                        measured = rec.energy_threshold
                        rec.energy_threshold = cal.calibrated(measured, time.monotonic())
                        clamped = (f" (measured {measured:.0f}, clamped to band)"
                                   if rec.energy_threshold != measured else "")
                        print(
                            f"[Wake] Calibrated energy threshold ({reason}): "
                            f"{rec.energy_threshold:.0f}{clamped}"
                        )
                except Exception as e:
                    print(f"[Wake] Calibration failed: {e}")
                    cal.failed(time.monotonic())  # retry when idle, not in a loop
                finally:
                    self._mic_lock.release()
                continue

            if self.tts.speaking:
                self.tts.wait_until_done(timeout=1.0)
                continue
            if self._ptt_wants_mic():
                time.sleep(0.1)   # a push-to-talk press is about to listen
                continue
            if not self._mic_lock.acquire(blocking=False):
                time.sleep(0.2)   # a conversation/ask_user listen owns the mic
                continue

            audio = None
            try:
                if self._wake_enabled.is_set():
                    with self.microphone as src:
                        audio = rec.listen(src, timeout=2, phrase_time_limit=6)
            except sr.WaitTimeoutError:
                pass
            except Exception as e:
                print(f"[Wake] Listen error: {e}")
            finally:
                self._mic_lock.release()

            if audio is None or self.tts.speaking or not self._wake_enabled.is_set():
                continue
            clip_end = time.monotonic()
            compete = self._mic_compete_state()

            # Threshold and clip length in the log, so it shows why a word was
            # cut off.
            threshold = rec.energy_threshold
            clip_s = self._clip_seconds(audio)
            stats = f"(threshold {threshold:.0f}, {clip_s:.1f}s)"
            text = error = None
            try:
                text = rec.recognize_google(audio).lower()
                print(f"[Wake] Heard: {log_text(text, unit='words')} {stats}")
                cal.heard(True, time.monotonic())
            except sr.UnknownValueError:
                print(f"[Wake] Heard something but couldn't transcribe it {stats}")
                cal.heard(False, time.monotonic())
            except Exception as e:
                error = type(e).__name__
                print(f"[Wake] {e}")
                cal.heard(None, time.monotonic())
            if save_clips:
                self._save_wake_clip(audio, text, threshold, clip_s, error=error)
            # A listen that started while this clip was being transcribed
            # counts too.
            compete = compete or self._mic_compete_state()
            if text is None:
                self._log_mic_compete(compete, text, "untranscribed")
                continue

            # -- A task is running: the ONLY thing we listen for is "stop".
            # (The user said "stop" mid-task in a real session and nothing
            # happened, because the wake loop only knew about wake words.)
            if self._agent_lock.locked():
                t = self._normalize_utterance(text)
                if t in self._VOICE_STOP_WORDS:
                    self._log_mic_compete(compete, text, "voice stop")
                    # A standalone stop always interrupts the active task,
                    # including while it is waiting at a confirmation or
                    # ask_user prompt. stop_current_task() wakes that prompt
                    # with a sentinel, so it cannot be mistaken for merely a
                    # declined confirmation.
                    print("[Wake] Voice stop -- halting current task")
                    self.stop_current_task()
                    self.speak("Stopping.", interrupt=True)
                else:
                    self._log_mic_compete(compete, text, "ignored (task running)")
                continue

            ww, cmd = fuzzy_wake(text)
            if not ww:
                self._log_mic_compete(compete, text, "dropped (no wake word)")
                continue
            print(f"[Wake] Matched wake word '{ww}', command: {log_text(cmd, unit='words')}")
            if cmd and len(cmd.split()) < 2:
                cmd = None
            # Run the conversation OFF this thread so the wake loop keeps
            # listening for a voice "stop" while the agent works.
            if not self._conversation_lock.acquire(blocking=False):
                self._log_mic_compete(compete, text, "dropped (conversation active)")
                print("[Wake] Conversation already active; ignoring duplicate wake")
                continue
            self._voice_start("wake", t0=clip_end)
            self._voice_mark("wake_heard")

            def _conversation_worker(command=cmd):
                try:
                    self._on_wake(command)
                finally:
                    self._conversation_lock.release()

            threading.Thread(
                target=_conversation_worker, daemon=True, name="Conversation"
            ).start()
            # Give the conversation a moment to take the mic lock (its own
            # listen) so we don't grab the same utterance twice.
            time.sleep(0.3)

    def _voice_start(self, label, t0=None):
        timer = getattr(self, "voice_timer", None)
        if timer is not None:
            timer.start(label, t0=t0)

    def _voice_mark(self, name, end=False):
        timer = getattr(self, "voice_timer", None)
        if timer is not None:
            timer.mark(name, end=end)

    def _ptt_wants_mic(self):
        ptt = getattr(self, "_ptt_waiting", None)
        return ptt is not None and ptt.is_set()

    # -- Audio levels (the orb's audio-reactive core) -------------------------
    # One float for the mic, written by the open listen's _StoppableStream
    # (each chunk) and reset to 0 when the listen ends; read by the orb pages.
    # A plain float attribute: its write and read are atomic, so no lock is
    # needed. Numbers only, never logged.

    def _set_mic_level(self, level):
        try:
            self._mic_level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self._mic_level = 0.0

    def mic_level_now(self):
        """The open listen's last chunk level 0..1; 0.0 with no listen."""
        return float(getattr(self, "_mic_level", 0.0) or 0.0)

    def audio_level(self):
        """{"mic": 0..1, "tts": 0..1, or None when the voice playing has no
        level (the pyttsx3 fallback)}; both 0.0 when idle. For the pages."""
        tts = getattr(self, "tts", None)
        level = getattr(tts, "level", None)
        try:
            spoken = level() if callable(level) else 0.0
        except Exception:
            spoken = 0.0
        return {"mic": self.mic_level_now(),
                "tts": None if spoken is None else float(spoken)}

    def _mic_compete_state(self):
        """Why a clip the wake loop is handling may have been meant for a
        _listen_once (a command, follow-up or answer), or None."""
        waiting = getattr(self, "_listen_waiting", None)
        if waiting is not None and waiting.is_set():
            return "a _listen_once was waiting for the mic"
        convo = getattr(self, "_conversation_lock", None)
        if convo is not None and convo.locked() and not self._agent_lock.locked():
            return "a conversation was active (a _listen_once may be about to start)"
        return None

    @staticmethod
    def _log_mic_compete(state, text, outcome):
        # Word count only: the utterance itself is never logged here.
        if state:
            words = len(text.split()) if text else 0
            print(f"[MicCompete] Wake loop handled a clip ({words} words) while "
                  f"{state}; outcome: {outcome}")

    @staticmethod
    def _clip_seconds(audio):
        bytes_per_s = audio.sample_rate * audio.sample_width
        return len(audio.frame_data) / bytes_per_s if bytes_per_s else 0.0

    @staticmethod
    def _save_wake_clip(audio, transcript, threshold, clip_s, error=None,
                        directory=None):
        """Write one wake-loop clip as <stamp>.wav plus <stamp>.json for the
        wake-word harness. Never raises: a full disk must not stop listening."""
        try:
            directory = directory or wake_clip_dir()
            os.makedirs(directory, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            with open(os.path.join(directory, stamp + ".wav"), "wb") as f:
                f.write(audio.get_wav_data())
            meta = {
                "wav": stamp + ".wav",
                "transcript": transcript if transcript is not None else "untranscribed",
                "energy_threshold": round(float(threshold), 1),
                "clip_seconds": round(clip_s, 2),
            }
            if error:
                meta["error"] = error
            with open(os.path.join(directory, stamp + ".json"), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        except Exception as e:
            print(f"[Wake] Could not save clip: {e}")

    def _detect_gender_address(self, text):
        """Scan heard speech for an explicit 'sir'/'ma'am' address and, if
        found, persist + apply it immediately."""
        if not text:
            return None
        t = text.lower()
        if re.search(r"\bsir\b", t):
            g = "male"
        elif re.search(r"\bma'?am\b", t):
            g = "female"
        else:
            return None
        self.memory.set_profile("user_gender", g)
        self.tts.set_gender(g)
        print(f"[Gender] Detected '{g}' address in speech -- switching voice")
        return g

    def _on_wake(self, inline_command=None):
        import random

        gender = self.memory.get_profile("user_gender", "none")

        if inline_command and len(inline_command.split()) < 2:
            inline_command = None

        resps = {
            "male": ["Yes, sir?", "Ready.", "Go ahead.", "Listening."],
            "female": ["Yes, ma'am?", "Ready.", "Go ahead.", "Listening."],
            "none": ["Yes?", "Ready.", "Listening.", "Go ahead."],
        }
        self.speak(random.choice(resps.get(gender, resps["none"])), interrupt=True)
        self._wait_for_tts()
        self._voice_mark("ack_done")

        if inline_command:
            cmd = inline_command
        else:
            cmd = self._listen_once(timeout=6)
            if not cmd:
                return
            # Users who repeat "hey mist ..." because the first wake was
            # slow end up with the wake phrase INSIDE the command. Strip it
            # (leading only) so it never reaches the planner as task text.
            cmd = strip_wake_prefix(cmd)
            if not cmd:
                return

        detected = self._detect_gender_address(cmd)
        if detected:
            gender = detected

        self._conversation_loop(cmd, gender)

    # Whole-utterance acknowledgements. These are things people say TO an
    # assistant that are not requests: "thanks" reached the planner and was
    # decomposed into a 4-phase open_application/fill_form/submit_form plan.
    _FILLER_UTTERANCES = {
        "thanks", "thank you", "thanks mist", "thank you mist", "ty", "thx",
        "ok", "okay", "k", "cool", "nice", "good", "great", "perfect",
        "awesome", "alright", "got it", "sounds good", "no thanks", "nothing",
        "nope", "no", "nah", "yes", "yeah", "yep", "sure", "bye", "goodbye",
        "see you", "later", "that's all", "thats all", "that's it", "thats it",
        "nevermind", "never mind", "stop", "cancel", "done", "im good",
        "i'm good", "all good", "you're welcome", "lol", "lmao", "hello",
        "hi", "hey",
    }
    # Explicit end-of-conversation phrases that are safe to match as a
    # SUBSTRING (multi-word, unambiguous). Single words like "stop" are only
    # matched as whole utterances above -- "stop the music" is a command.
    _STOP_SUBSTRINGS = ("that's all", "thats all", "never mind", "nevermind",
                        "that's it", "thats it", "no thanks")

    @staticmethod
    def _normalize_utterance(text):
        t = (text or "").lower().strip()
        t = re.sub(r"[^\w\s']", " ", t)
        return " ".join(t.split())

    def _is_non_actionable(self, text):
        """True for acknowledgements / filler that should never become a
        task. Whole-utterance match, or a short utterance (<=3 words) made
        entirely of filler words."""
        t = self._normalize_utterance(text)
        if not t:
            return True
        if t in self._FILLER_UTTERANCES:
            return True
        if any(p in t for p in self._STOP_SUBSTRINGS):
            return True
        words = t.split()
        if len(words) <= 3 and all(
            w in self._FILLER_UTTERANCES or w in ("mist", "please", "pls", "ok")
            for w in words
        ):
            return True
        return False

    def _is_end_of_conversation(self, text):
        t = self._normalize_utterance(text)
        return t in ("stop", "cancel", "bye", "goodbye", "that's all", "thats all",
                     "that's it", "thats it", "nevermind", "never mind",
                     "no thanks", "nothing", "im good", "i'm good", "all good",
                     "see you", "later") or any(p in t for p in self._STOP_SUBSTRINGS)

    def _conversation_loop(self, first_command, gender, max_turns=6):
        cmd = first_command
        turns = 0

        while cmd and turns < max_turns:
            turns += 1
            if self._is_end_of_conversation(cmd):
                self.speak("Okay.")
                break
            if self._is_non_actionable(cmd):
                # "thanks" / "cool" / "ok" -- acknowledge, keep listening, and
                # never hand it to the planner as if it were a goal.
                t = self._normalize_utterance(cmd)
                self.speak("You're welcome." if "thank" in t or t in ("ty", "thx")
                           else "Sure.")
                self._wait_for_tts()
                self._voice_start("followup")
                cmd = self._listen_once(timeout=5)
                if cmd:
                    cmd = strip_wake_prefix(cmd)
                continue

            detected = self._detect_gender_address(cmd)
            if detected:
                gender = detected

            try:
                res = self._voice_ask(cmd, gender)
                if res.get("error") == "busy":
                    # Another loop owns the machine; ask_mistai already said so.
                    break
            except Exception as e:
                print(f"[ConversationLoop] Task error: {e}")
                break

            self._wait_for_tts()

            self._voice_start("followup")
            cmd = self._listen_once(timeout=5)
            if cmd:
                cmd = strip_wake_prefix(cmd)

        print("[ConversationLoop] Ended, returning to wake-word-only mode")

    def _listen_once(self, timeout=6, on_listening=None, ptt=False, stop_event=None,
                     profile=None):
        # Set before waiting for the mic, so the wake loop can log a clip it
        # transcribes meanwhile ([MicCompete]). on_listening (optional) is
        # called once the mic is open, right before listening starts.
        # ptt=True: push-to-talk's own recognizer and settings (see
        # _ptt_listen_setup); every other caller is unchanged.
        # stop_event (optional): once set, the listen ends within one chunk and
        # nothing is recognized (a confirmation's Yes/No button). None (the
        # default) = no change at all.
        # profile (optional, "ptt" or "lead"): MISTAI_CONFIRM_LISTEN's set-up for
        # a confirmation or ask_user listen, on its own recognizer
        # (_confirm_listen_setup). None = the shared set-up, unchanged.
        waiting = getattr(self, "_listen_waiting", None)
        if waiting is not None:
            waiting.set()
        if profile:
            print(f"[Confirm] listen profile={profile}")
            if profile == "lead":
                self._lead_in_after_tts()
        # MISTAI_VOICE_DIAG: numbers about this listen, printed at the end.
        diag = self._voice_diag_start(ptt) if voice_diag_enabled() else None
        if diag is not None and profile:
            diag["rec"] = profile
        try:
            with self._mic_lock:
                self._voice_mark("mic_acquired")
                if diag is not None:
                    diag["mic_wait"] = time.monotonic() - diag["t0"]
                try:
                    rec = (self._ptt_recognizer() if ptt
                           else self._confirm_recognizer() if profile else self.recognizer)
                    stream = None
                    with self.microphone as src:
                        if diag is not None:
                            diag.update(since_tts=self._seconds_since_tts(),
                                        thr0=rec.energy_threshold,
                                        sr=getattr(src, "SAMPLE_RATE", None))
                        if ptt:
                            phrase_limit = self._ptt_listen_setup(rec, src)
                            # After the adjustment, so a stop never skews the
                            # stored noise threshold.
                            if getattr(src, "stream", None) is not None:
                                stream = src.stream = _StoppableStream(
                                    src.stream, self._ptt_stop_event(), rec,
                                    getattr(src, "SAMPLE_WIDTH", 2),
                                    on_level=self._set_mic_level)
                        else:
                            if profile:
                                phrase_limit = self._confirm_listen_setup(rec, src, profile)
                                if profile == "ptt":
                                    timeout = 10
                            else:
                                self.recognizer.pause_threshold = 1.0
                                self.recognizer.non_speaking_duration = 0.5
                                self.recognizer.adjust_for_ambient_noise(src, duration=0.3)
                                phrase_limit = 15
                            if (stop_event is not None
                                    and getattr(src, "stream", None) is not None):
                                src.stream = _StoppableStream(
                                    src.stream, stop_event, rec,
                                    getattr(src, "SAMPLE_WIDTH", 2),
                                    on_level=self._set_mic_level)
                        if diag is not None:
                            diag["thr1"] = rec.energy_threshold
                        self._voice_mark("listen_start")
                        if on_listening is not None:
                            try:
                                on_listening()
                            except Exception as e:
                                print(f"[ListenOnce] on_listening failed ({type(e).__name__})")
                        audio = rec.listen(
                            src, timeout=timeout, phrase_time_limit=phrase_limit
                        )
                        self._voice_mark("speech_end")
                        if diag is not None:
                            diag["audio"] = audio
                        if (stream is not None and self._ptt_stop_event().is_set()
                                and not stream.speech):
                            self._voice_mark("cancelled", end=True)
                            if diag is not None:
                                diag["result"] = "cancelled"
                            return _PTT_CANCELLED
                        if stop_event is not None and stop_event.is_set():
                            self._voice_mark("stopped", end=True)
                            if diag is not None:
                                diag["result"] = "stopped"
                            return None
                        text = rec.recognize_google(audio)
                        self._voice_mark("stt_done")
                        if diag is not None:
                            diag["result"] = "ok"
                        return text
                except Exception as e:
                    if diag is not None:
                        diag["result"] = type(e).__name__
                    if stop_event is not None and stop_event.is_set():
                        self._voice_mark("stopped", end=True)
                        return None
                    print(f"[ListenOnce] {e}")
                    self._voice_mark(f"listen_failed ({type(e).__name__})", end=True)
                    return None
        finally:
            self._set_mic_level(0.0)          # the listen is over (or failed)
            if waiting is not None:
                waiting.clear()
            if diag is not None:
                self._log_voice_diag(diag)

    def _voice_diag_start(self, ptt):
        timer = getattr(self, "voice_timer", None)
        label = (getattr(timer, "label", "") if timer is not None else "") or "listen"
        return {"t0": time.monotonic(), "label": label, "rec": "ptt" if ptt else "shared"}

    def _seconds_since_tts(self):
        """Seconds since Mist's last utterance finished playing, or None
        (never spoke, or still speaking)."""
        tts = getattr(self, "tts", None)
        at = getattr(tts, "finished_at", None)
        if at is None or getattr(tts, "speaking", False):
            return None
        return time.monotonic() - at

    def _log_voice_diag(self, d):
        """One "[VoiceDiag]" line: numbers and an exception type only, never
        the audio or any words. since_tts is measured when the mic opened,
        before the noise adjustment; thr is the threshold before -> after it."""
        def num(v, fmt):
            try:
                return format(int(v) if fmt == "d" else float(v), fmt)
            except (TypeError, ValueError):
                return "na"

        audio = d.get("audio")
        clip = rms = peak = None
        sr_ = d.get("sr")
        if audio is not None:
            try:
                import audioop

                clip = self._clip_seconds(audio)
                rms = audioop.rms(audio.frame_data, audio.sample_width)
                peak = audioop.max(audio.frame_data, audio.sample_width)
                sr_ = audio.sample_rate
            except Exception:
                pass
        print(f"[VoiceDiag] {d['label']} listen: clip={num(clip, '.2f')}s "
              f"rms={num(rms, 'd')} peak={num(peak, 'd')} sr={num(sr_, 'd')} "
              f"thr={num(d.get('thr0'), '.0f')}->{num(d.get('thr1'), '.0f')} "
              f"since_tts={num(d.get('since_tts'), '.2f')}s "
              f"mic_wait={num(d.get('mic_wait'), '.2f')}s rec={d['rec']} "
              f"result={d.get('result', 'na')}")

    def _ptt_stop_event(self):
        ev = getattr(self, "_ptt_stop", None)
        if ev is None:
            ev = self._ptt_stop = threading.Event()
        return ev

    def _ptt_recognizer(self):
        """Push-to-talk's own recognizer (created on first use), so its
        settings and noise threshold never touch the shared one."""
        rec = getattr(self, "ptt_recognizer", None)
        if rec is None:
            rec = self.ptt_recognizer = sr.Recognizer()
        return rec

    def _confirm_recognizer(self):
        """The confirmation/ask_user recognizer of MISTAI_CONFIRM_LISTEN's
        "ptt" and "lead" profiles (created on first use): the shared and the
        push-to-talk recognizers are never changed by them."""
        rec = getattr(self, "confirm_recognizer", None)
        if rec is None:
            rec = self.confirm_recognizer = sr.Recognizer()
        return rec

    def _fresh_ptt_threshold(self):
        """Push-to-talk's stored noise threshold if measured less than
        PTT_THRESHOLD_MAX_AGE (10 minutes) ago, else None. Read only."""
        at = getattr(self, "_ptt_calibrated_at", None)
        ptt_rec = getattr(self, "ptt_recognizer", None)
        if at is None or ptt_rec is None or time.monotonic() - at >= PTT_THRESHOLD_MAX_AGE:
            return None
        return ptt_rec.energy_threshold

    def _confirm_listen_setup(self, rec, src, profile):
        """Settings of a "ptt" or "lead" confirmation listen (mic open, under
        _mic_lock): a fresh stored threshold skips the 0.3 s adjustment.
        Returns the phrase limit (8 s for "ptt", 15 s as shared for "lead")."""
        rec.pause_threshold = 1.0
        rec.non_speaking_duration = 0.5
        stored = self._fresh_ptt_threshold()
        if stored is not None:
            rec.energy_threshold = stored
        else:
            rec.adjust_for_ambient_noise(src, duration=0.3)
        return 8 if profile == "ptt" else 15

    def _lead_in_after_tts(self):
        """"lead": open the mic CONFIRM_LEAD_IN_SECONDS after TTS finished."""
        tts = getattr(self, "tts", None)
        while getattr(tts, "speaking", False):
            time.sleep(0.05)
        since = self._seconds_since_tts()
        if since is not None and since < CONFIRM_LEAD_IN_SECONDS:
            time.sleep(CONFIRM_LEAD_IN_SECONDS - since)

    def _ptt_listen_setup(self, rec, src):
        """Settings for a push-to-talk listen, with the mic already open
        (called under _mic_lock). The 0.3s noise adjustment runs on the first
        press and again once the measured threshold is PTT_THRESHOLD_MAX_AGE
        old; in between the threshold is reused. Returns the phrase limit."""
        self._voice_mark("mic_open")          # mic_acquired -> mic_open: opening
        rec.pause_threshold = PTT_PAUSE_THRESHOLD
        rec.non_speaking_duration = 0.5
        at = getattr(self, "_ptt_calibrated_at", None)
        now = time.monotonic()
        if at is not None and now - at < PTT_THRESHOLD_MAX_AGE:
            print(f"[PushToTalk] Noise threshold reused ({now - at:.0f}s old)")
        else:
            rec.adjust_for_ambient_noise(src, duration=0.3)
            self._ptt_calibrated_at = time.monotonic()
        return PTT_PHRASE_TIME_LIMIT

    def _voice_ask(self, cmd, gender):
        """One voice command through the normal flow (push-to-talk and the
        wake conversation), shown in the chat like a typed turn: the heard
        words before, the result after. Words and reply go only to the page,
        escaped; they are never logged."""
        self._notify_js(f'handleVoiceTurn("{esc(cmd)}")')
        res = None
        try:
            res = self.ask_mistai(cmd, gender=gender, input_mode="voice")
            return res
        finally:
            if not isinstance(res, dict):       # ask_mistai raised
                res = {"success": False, "error": "Voice turn failed"}
            speech = (res.get("command") or {}).get("speech") or ""
            try:
                steps = int(res.get("steps") or 0)
            except (TypeError, ValueError):
                steps = 0
            self._notify_js(
                f'handleVoiceReply({"true" if res.get("success") else "false"},'
                f'"{esc(speech)}",{steps},"{esc(res.get("error") or "")}")'
            )

    # -- Push-to-talk -----------------------------------------------------------

    _PTT_BUSY = "Mist is working. Press Stop or say stop."
    _PTT_ACTIVE = "Already listening."
    _PTT_NOTHING = "Didn't catch that"

    def push_to_talk(self):
        """One press, one voice command, no wake word: listen once, then run
        the normal voice turn. Refused (mic untouched) while a task runs or a
        conversation is active. Returns at once; the turn runs on a thread and
        the page follows it through handleListenState. A press DURING a
        push-to-talk listen ends that listen early (what was said so far is
        used; nothing said: the turn ends quietly)."""
        if self._ptt_wants_mic():
            self._ptt_stop_event().set()
            return {"success": True, "stopped": True}
        if self._agent_lock.locked():
            return {"success": False, "error": "busy", "message": self._PTT_BUSY}
        if not self._conversation_lock.acquire(blocking=False):
            return {"success": False, "error": "listening", "message": self._PTT_ACTIVE}
        try:
            threading.Thread(target=self._ptt_turn, daemon=True, name="PushToTalk").start()
        except Exception:
            self._conversation_lock.release()
            raise
        return {"success": True}

    def _ptt_turn(self):
        """The push-to-talk turn. Owns _conversation_lock (taken by
        push_to_talk) and always releases it; the heard words are never logged."""
        idle_sent = False

        def idle(message=""):
            nonlocal idle_sent
            idle_sent = True
            self._notify_js(f'handleListenState("idle","{esc(message)}")')

        stop = self._ptt_stop_event()
        try:
            stop.clear()                          # a press never leaks into this turn
            self._voice_start("ptt")
            self.tts.interrupt()                  # stop Mist mid-sentence
            self._ptt_waiting.set()               # the wake loop leaves the mic alone
            self._notify_js('handleListenState("waiting","")')
            def listening():
                self._notify_js('handleListenState("listening","")')

            heard = self._listen_once(timeout=6, on_listening=listening, ptt=True)
            if heard is _PTT_CANCELLED:
                return self._ptt_cancelled(idle)
            cmd = strip_wake_prefix(heard) if heard else ""
            if heard and not cmd and not stop.is_set():
                # Only the wake phrase ("hey mist"): one silent follow-up
                # listen for the command itself, then give up. Not after a
                # second press: the user said they were done.
                heard = self._listen_once(timeout=5, on_listening=listening, ptt=True)
                if heard is _PTT_CANCELLED:
                    return self._ptt_cancelled(idle)
                cmd = strip_wake_prefix(heard) if heard else ""
            # Free the mic for the wake loop now (after the LAST listen), so
            # it can hear "stop" mid-task.
            self._ptt_waiting.clear()
            if not cmd:
                idle(self._PTT_NOTHING)           # a status line, not spoken
                return
            idle()
            gender = self.memory.get_profile("user_gender", "none")
            detected = self._detect_gender_address(cmd)
            if detected:
                gender = detected
            self._voice_ask(cmd, gender)
        except Exception as e:
            print(f"[PushToTalk] Turn failed ({type(e).__name__})")
            if not idle_sent:
                idle("Push-to-talk failed")
        finally:
            self._ptt_waiting.clear()
            stop.clear()
            self._conversation_lock.release()
            if not idle_sent:
                idle()

    def _ptt_cancelled(self, idle):
        """A second press before any speech: no recognition call, no
        "Didn't catch that"; the status simply returns."""
        self._ptt_waiting.clear()
        print("[PushToTalk] Stopped before any speech (cancelled)")
        idle()

    def toggle_proactive_mode(self, enabled):
        return {"success": True}

    def get_wake_word_status(self):
        return {"active": self.wake_word_active, "wake_words": WAKE_WORDS}

    def get_proactive_status(self):
        return {"enabled": False}

    def get_captions_status(self):
        return {"enabled": self.captions_enabled}

    def set_user_gender(self, gender):
        """Save the gender address. Anything outside GENDER_CHOICES is refused
        and changes nothing (not the saved value, not the voice)."""
        g = str(gender or "").strip().lower()
        if g not in GENDER_CHOICES:
            return {"success": False,
                    "error": f"Unknown gender address. Choose one of: {', '.join(GENDER_CHOICES)}."}
        self.memory.set_profile("user_gender", g)
        self.tts.set_gender(g)
        print(f"[API] Gender set to: {g}")
        return {"success": True, "gender": g}

    def get_user_gender(self):
        """The saved gender address, for the UI to show. Never raises: an
        unreadable, empty or unknown value gives DEFAULT_GENDER."""
        try:
            saved = str(self.memory.get_profile("user_gender", DEFAULT_GENDER) or "")
        except Exception as e:
            print(f"[API] Could not read the saved gender ({type(e).__name__})")
            return DEFAULT_GENDER
        saved = saved.strip().lower()
        return saved if saved in GENDER_CHOICES else DEFAULT_GENDER

    # -- Model (always Nova, then Flux; no picker) --------------------------------

    def current_model(self):
        """The first model of every LLM call's chain: DEFAULT_MODEL (Nova).
        A saved profile "model" value (from the old Settings picker) is never
        read or written. ``_model_override`` (a MODEL_CHOICES id) is for
        tools/live_checks.py --model only; nothing in the UI sets it."""
        model = getattr(self, "_model_override", None)
        return model if model in MODEL_CHOICES else DEFAULT_MODEL

    # -- Floating orb (Settings, default off; core/floating_orb.py) -----------

    def _floating_orb(self):
        orb = getattr(self, "_orb", None)
        if orb is None:
            from .floating_orb import FloatingOrb

            orb = self._orb = FloatingOrb(self)
        return orb

    def get_floating_orb(self):
        """The saved Floating orb setting (off unless turned on)."""
        try:
            return str(self.memory.get_profile("floating_orb", "off")) == "on"
        except Exception:
            return False

    def set_floating_orb(self, enabled):
        """Save the setting and show or hide the orb window. A window that
        cannot be opened turns the setting back off."""
        enabled = bool(enabled)
        cache = self.__dict__.setdefault("_orb_option_cache", {})
        try:
            self.memory.set_profile("floating_orb", "on" if enabled else "off")
        except Exception:
            return {"success": False, "enabled": self.get_floating_orb(),
                    "error": "Could not save the setting"}
        cache["floating_orb"] = "on" if enabled else "off"
        orb = self._floating_orb()
        if not enabled:
            orb.hide()
            return {"success": True, "enabled": False}
        if orb.show():
            return {"success": True, "enabled": True}
        try:
            self.memory.set_profile("floating_orb", "off")
        except Exception:
            pass
        cache["floating_orb"] = "off"
        return {"success": False, "enabled": False, "error": "The orb window could not be opened"}

    # ------------------------------------------------------------
    # ORB OPTIONS FROM THE FLOATING ORB SWITCH
    # ------------------------------------------------------------
    # The single "Floating orb" switch (profile "floating_orb") turns on the
    # orb AND following the task (orb_flight), pointing before clicks
    # (orb_pointer) and idle drift (orb_idle_drift); MISTAI_ORB_FLIGHT /
    # MISTAI_ORB_POINTER / MISTAI_ORB_IDLE_DRIFT stay as developer-only
    # overrides. The rows the old per-behavior switches saved are ignored
    # (left in the database). "Task panels" (orb_panels, MISTAI_ORB_PANELS)
    # keeps its own switch. Panel placement is automatic per task
    # (core/floating_orb.py OrbPanels; MISTAI_PANEL_ANCHOR pins it).
    _ORB_OPTIONS = {"orb_flight": orb_flight_enabled, "orb_panels": orb_panels_enabled,
                    "orb_pointer": orb_pointer_enabled,
                    "orb_idle_drift": orb_idle_drift_enabled}
    _ORB_SWITCH_OPTIONS = ("orb_flight", "orb_pointer", "orb_idle_drift")

    def orb_option(self, name):
        """Is this orb option on? Read four times a second by the orb, so the
        saved values are cached and refreshed by set_floating_orb /
        set_orb_option."""
        cache = self.__dict__.setdefault("_orb_option_cache", {})
        key = "floating_orb" if name in self._ORB_SWITCH_OPTIONS else name
        if key not in cache:
            try:
                cache[key] = self.memory.get_profile(key, "off")
            except Exception:
                cache[key] = "off"
        if name in self._ORB_SWITCH_OPTIONS:
            return self._ORB_OPTIONS[name](str(cache[key]) == "on")
        return self._ORB_OPTIONS[name](cache[key])

    def get_orb_options(self):
        return {name: self.orb_option(name) for name in self._ORB_OPTIONS}

    def set_orb_option(self, name, enabled):
        """Only "Task panels" has its own switch; the other options follow
        the Floating orb switch."""
        if name not in self._ORB_OPTIONS:
            return {"success": False, "error": "Unknown option"}
        if name in self._ORB_SWITCH_OPTIONS:
            return {"success": False, "enabled": self.orb_option(name),
                    "error": "This follows the Floating orb switch"}
        value = "on" if enabled else "off"
        try:
            self.memory.set_profile(name, value)
        except Exception:
            return {"success": False, "enabled": self.orb_option(name),
                    "error": "Could not save the setting"}
        self.__dict__.setdefault("_orb_option_cache", {})[name] = value
        return {"success": True, "enabled": self.orb_option(name)}

    # -- the orb pointer (core/floating_orb.py point_before_click) ------------

    def _orb_point(self, target):
        """Every click path calls this right before it sends a click: the
        orb flies beside ``target`` (a rect or point, physical px) and the
        click waits at most orb_behavior.POINT_WAIT_S. True = the orb pointed
        (skip the old click marker); False at once when the orb is hidden,
        flight is off or the pointer is off."""
        if self.__dict__.get("_orb") is None:
            return False
        from .floating_orb import point_before_click

        return point_before_click(self, target)

    def _orb_pointer_active(self):
        if self.__dict__.get("_orb") is None:
            return False
        from .floating_orb import pointer_active

        return pointer_active(self)

    def _orb_point_page(self, info):
        """browser_click: the element's viewport rect + window metrics
        (BrowserSession) mapped to the screen like the search tour; no
        pointing when the mapping can't be trusted."""
        if not self._orb_pointer_active():
            return False
        try:
            from .floating_orb import classify_page_rects
            from .live_state import playwright_window

            orb = self.__dict__.get("_orb")
            hwnd = playwright_window()
            window = orb._win.window_rect(hwnd) if hwnd and orb._win else None
            rects = classify_page_rects(info, window, 1)[0] if window else None
        except Exception:
            rects = None
        return self._orb_point(rects[0]) if rects else False

    # ------------------------------------------------------------
    # BROWSER SIGN-INS
    # ------------------------------------------------------------
    # Settings > BROWSER SIGN-INS: the switch "Let Mist use sign-ins from my
    # browser" (profile "browser_seed_profile", default off,
    # MISTAI_BROWSER_SEED_PROFILE overrides), the browser to copy from
    # (profile "browser_signins_source"; the dropdown of installed browsers,
    # core/browser_signins.py) and "Sign in inside Mist's browser". Only
    # Firefox can be copied; any other choice leaves Mist's browser on its own
    # clean profile. Read when MistAI's browser starts, so a change applies to
    # the next browser session.

    def _signins_switch(self):
        try:
            saved = self.memory.get_profile("browser_seed_profile", "off")
        except Exception:
            saved = "off"
        return browser_seed_profile_enabled(saved)

    def _signins_saved_source(self):
        try:
            return str(self.memory.get_profile("browser_signins_source", "") or "").strip().lower()
        except Exception:
            return ""

    def get_firefox_signins(self):
        """Seed MistAI's browser from Firefox? The switch on and Firefox the
        chosen browser (nothing saved yet = Firefox, the old Firefox-only
        setting). Called by BrowserSession at each start; no registry read."""
        if not self._signins_switch():
            return False
        return (self._signins_saved_source() or browser_signins.DEFAULT_SOURCE) == "firefox"

    def set_firefox_signins(self, enabled):
        """The old Firefox-only switch (kept for scripts): sets the switch only."""
        value = "on" if enabled else "off"
        try:
            self.memory.set_profile("browser_seed_profile", value)
        except Exception:
            return {"success": False, "enabled": self.get_firefox_signins(),
                    "error": "Could not save the setting"}
        print(f"[Browser] browser sign-ins: {value} (applies to the next browser start)")
        return {"success": True, "enabled": self.get_firefox_signins()}

    def _signin_choices(self):
        installed = browser_signins.installed_browsers()
        default = browser_signins.default_browser()
        return browser_signins.browser_choices(installed, default), default

    def get_browser_signins(self):
        """For Settings: {enabled, source, supported, note, browsers, explanation}."""
        choices, default = self._signin_choices()
        on = self._signins_switch()
        source = browser_signins.resolve_source(self._signins_saved_source(), choices, on, default)
        supported = browser_signins.is_supported(source)
        return {"enabled": on, "source": source, "supported": supported,
                "note": "" if supported else browser_signins.UNSUPPORTED_NOTE,
                "browsers": choices, "explanation": browser_signins.SIGNINS_EXPLANATION}

    def set_browser_signins(self, enabled=None, source=None):
        """Save the switch and/or the browser (validated against the browsers
        found on this PC). Logs the switch and the browser's family id only."""
        choices, default = self._signin_choices()
        if source is not None:
            source = str(source or "").strip().lower()
            if source not in [c["id"] for c in choices]:
                return dict(self.get_browser_signins(), success=False,
                            error="That browser was not found on this PC")
        try:
            if enabled is not None:
                self.memory.set_profile("browser_seed_profile", "on" if enabled else "off")
            if source is not None:
                self.memory.set_profile("browser_signins_source", source)
        except Exception:
            return dict(self.get_browser_signins(), success=False,
                        error="Could not save the setting")
        state = self.get_browser_signins()
        print(f"[Browser] browser sign-ins: {'on' if state['enabled'] else 'off'}, "
              f"{state['source']} ({'supported' if state['supported'] else 'not supported'}; "
              "applies to the next browser start)")
        return dict(state, success=True)

    def open_signin_browser(self):
        """"Sign in inside Mist's browser": start (or show) MistAI's own
        browser so the user can sign in to sites once; the sign-ins stay in
        MistAI's own profile. Not while a task runs (it may be using it)."""
        lock = getattr(self, "_agent_lock", None)
        if lock is not None and lock.locked():
            return {"success": False,
                    "message": "Mist is working on a task. Try again when it is done."}
        browser = getattr(self, "browser", None)
        if browser is None or not getattr(browser, "available", False):
            return {"success": False,
                    "message": "Mist's browser is not available (Playwright's Firefox is not installed)."}

        def run():
            try:
                browser.ensure_started()
                hwnd = clip.playwright_window()
                if hwnd:
                    self._focus_hwnd(hwnd)
                print("[Browser] sign-in window " + ("shown" if hwnd else "not found"))
            except Exception as e:
                print(f"[Browser] sign-in window failed ({type(e).__name__})")

        threading.Thread(target=run, daemon=True, name="SigninBrowser").start()
        return {"success": True,
                "message": "Opening Mist's browser. Sign in to your sites there; they stay "
                           "signed in inside Mist's own browser."}

    def start_floating_orb_if_enabled(self):
        if self.get_floating_orb():
            self._floating_orb().show()

    def _orb_window_handles(self):
        orb = getattr(self, "_orb", None)
        return orb.handles() if orb is not None else []

    def _orb_in_front(self):
        handles = self._orb_window_handles()
        if not handles:
            return False
        try:
            import win32gui

            return win32gui.GetForegroundWindow() in handles
        except Exception:
            return False

    # -- Ask before sending (quick_message) ------------------------------------

    def ask_before_sending_mode(self):
        """The saved ask_before_sending choice, read at call time; anything
        unreadable or unknown gives the default (config)."""
        try:
            saved = self.memory.get_profile("ask_before_sending", None)
        except Exception as e:
            print(f"[Settings] Could not read ask_before_sending ({type(e).__name__})")
            saved = None
        return ask_before_sending_choice(saved)

    def set_ask_before_sending(self, choice):
        """From the Settings page only (nothing the model outputs reaches
        it). Validated here against ASK_BEFORE_SENDING_CHOICES."""
        v = str(choice or "").strip().lower()
        if v not in ASK_BEFORE_SENDING_CHOICES:
            return {"success": False,
                    "error": f"Unknown choice. Choose one of: {', '.join(ASK_BEFORE_SENDING_CHOICES)}."}
        self.memory.set_profile("ask_before_sending", v)
        print(f"[Settings] Ask before sending: {v}")
        return {"success": True, "current": v, "label": ASK_BEFORE_SENDING_LABELS[v],
                "help": ASK_BEFORE_SENDING_HELP[v]}

    def get_ask_before_sending_options(self):
        """Every choice as {id, label, help} (the page shows label and help,
        sends ids), plus the current id."""
        return {
            "choices": [{"id": c, "label": ASK_BEFORE_SENDING_LABELS[c],
                         "help": ASK_BEFORE_SENDING_HELP[c]}
                        for c in ASK_BEFORE_SENDING_CHOICES],
            "current": self.ask_before_sending_mode(),
        }

    # ------------------------------------------------------------
    # MESSAGES SWITCH (SETTINGS)
    # ------------------------------------------------------------
    # "Ask me before sending a message for you": on = "always" (the default),
    # off = "when_unsure"; the switch never selects "never" (config MESSAGES
    # SWITCH MAPPING). The remembered approvals count rides along so the page
    # shows "Remembered approvals" / "Forget all" only when there are some.

    def get_send_confirmation(self):
        mode = self.ask_before_sending_mode()
        return {"on": send_switch_state(mode), "help": SEND_SWITCH_HELP,
                "off_help": SEND_SWITCH_OFF_HELP,
                "note": SEND_SWITCH_NEVER_NOTE if mode == "never" else "",
                "approvals": self.get_remembered_approvals_count()}

    def set_send_confirmation(self, on):
        if not isinstance(on, bool):
            return dict(self.get_send_confirmation(), success=False,
                        error="Could not save this setting")
        r = self.set_ask_before_sending(send_mode_for_switch(on))
        if not r.get("success"):
            return dict(self.get_send_confirmation(), success=False,
                        error=r.get("error") or "Could not save this setting")
        return dict(self.get_send_confirmation(), success=True)

    # -- Remembered send approvals ("Always for this person") ------------------

    def _approval_id(self, app, recipient, create=False):
        """HMAC-SHA256 of app|recipient (both normalized) under this install's
        random approval_key, hex; None without a key (unless create) or a
        name. The name itself is never stored."""
        who = normalize_person_name(recipient)
        if not who:
            return None
        try:
            key = self.memory.get_profile("approval_key", None)
            raw = bytes.fromhex(key) if key else None
        except (ValueError, TypeError):
            raw = None
        except Exception as e:
            print(f"[Confirm] approvals unreadable ({type(e).__name__})")
            return None
        if raw is None:
            if not create:
                return None
            key = secrets.token_hex(32)
            self.memory.set_profile("approval_key", key)
            raw = bytes.fromhex(key)
        msg = f"{normalize_person_name(app)}|{who}".encode("utf-8")
        return hmac.new(raw, msg, hashlib.sha256).hexdigest()

    def remember_send_approval(self, app, recipient):
        h = self._approval_id(app, recipient, create=True)
        if h:
            self.memory.set_profile(APPROVED_PREFIX + h, "1")
            print("[Confirm] approval remembered")
        return bool(h)

    def is_send_approved(self, app, recipient):
        h = self._approval_id(app, recipient)
        try:
            return bool(h) and self.memory.get_profile(APPROVED_PREFIX + h, None) is not None
        except Exception:
            return False

    def get_remembered_approvals_count(self):
        """For Settings: only a number."""
        try:
            return self.memory.count_profile_prefix(APPROVED_PREFIX)
        except Exception as e:
            print(f"[Confirm] approvals unreadable ({type(e).__name__})")
            return 0

    def forget_remembered_approvals(self):
        """Deletes the remembered approvals and nothing else (the
        approval_key and every other profile key stay)."""
        try:
            removed = self.memory.delete_profile_prefix(APPROVED_PREFIX)
        except Exception as e:
            return {"success": False, "error": f"Could not forget them ({type(e).__name__})."}
        print(f"[Confirm] remembered approvals forgotten: {removed}")
        return {"success": True, "removed": removed}

    def set_vision_api_key(self, key):
        self.vision.set_api_key(key)
        return {"success": True}

    def set_porcupine_key(self, key):
        return {"success": True}

    def check_api_status(self):
        try:
            r = requests.get(STATUS_URL, timeout=3)
            d = r.json()
            return {
                "online": d.get("status") == "online",
                "reason": d.get("down_reason", ""),
            }
        except Exception:
            return {"online": False, "reason": "Connection error"}

    def submit_answer(self, text, source=None):
        """An answer from the page: the text box, or a confirmation's Yes/No
        button (source="button")."""
        with self._answer_lock:
            self._pending_answer = text
            self._pending_source = "button" if source == "button" else "typed"
            self._answer_event.set()
        return {"success": True}

    CONFIRM_LISTEN_TIMEOUT = 10
    CONFIRM_TEXT_TIMEOUT = 60

    def ask_confirmation(self, question, input_mode, offer_remember=False):
        """Ask a yes/no question with Yes/No buttons on the page; MistAI's
        window is shown while it is pending. Voice: the question is spoken and
        answered by speech or a button -- a click ends the listen and wins.
        Typed: a button or the text box. Returns (answer, source), source
        "button", "typed" or "voice"; answer None when nothing came. A stop
        request wakes it (stop_current_task). Never logs the question.
        offer_remember: also show "Always for this person" (its click answers
        "remember" from the button)."""
        with self._answer_lock:
            self._pending_answer = self._pending_source = None
            self._answer_event.clear()
            self._awaiting_text_answer.set()
        shown = False
        waiting = waiting_scope(self, "confirm", input_mode, question)   # the orb waits
        waiting.__enter__()
        try:
            window = getattr(self, "_window", None)
            if window:
                try:
                    window.restore()
                    shown = True
                except Exception as e:
                    print(f"[Confirm] window not shown ({type(e).__name__})")
            self._notify_js(f'showConfirmPrompt("{esc(question)}"'
                            f'{",true" if offer_remember else ""})')
            heard = None
            if input_mode == "voice":
                dropped = _tts_dropped(self.tts)
                self.speak(question)
                while self.tts.speaking and not self._answer_event.is_set():
                    time.sleep(0.05)
                unheard = _tts_dropped(self.tts) != dropped
                started = time.monotonic()
                if not self._answer_event.is_set():
                    # Its own [VoiceTiming] turn: mic and STT time of the answer.
                    self._voice_start("confirm")
                    profile = confirm_listen_profile()   # MISTAI_CONFIRM_LISTEN A/B
                    heard = self._listen_once(timeout=self.CONFIRM_LISTEN_TIMEOUT,
                                              stop_event=self._answer_event,
                                              **({} if profile == "shared"
                                                 else {"profile": profile}))
                    self._voice_mark("answered", end=True)
                if unheard and not heard and not self._answer_event.is_set():
                    # No voice could speak the question: it is only on screen,
                    # so give the reader the typed answer's full time.
                    print("[Confirm] question not spoken (no voice) -- waiting for a button")
                    self._answer_event.wait(timeout=max(
                        0.0, self.CONFIRM_TEXT_TIMEOUT - (time.monotonic() - started)))
            else:
                self._answer_event.wait(timeout=self.CONFIRM_TEXT_TIMEOUT)
            with self._answer_lock:
                answered = self._answer_event.is_set()
                answer, source = self._pending_answer, self._pending_source
                self._pending_answer = self._pending_source = None
                self._answer_event.clear()
            if answered:                       # the page's answer wins over speech
                return answer, source or "typed"
            return (heard, "voice") if input_mode == "voice" else (None, "typed")
        finally:
            self._awaiting_text_answer.clear()
            waiting.__exit__(None, None, None)
            self._notify_js("closeConfirmPrompt()")
            if shown and confirm_reminimize_enabled():
                self.minimize_window()

    # ------------------------------------------------------------
    # UNHEARD QUESTION
    # ------------------------------------------------------------
    # A voice-mode ask_user question that no engine could speak (the TTS
    # dropped it) was never heard. It goes on screen with the typed prompt
    # (window restored); a spoken answer still counts during the usual listen,
    # and then the typed prompt keeps waiting for the rest of its full time,
    # so nothing is cut short because nothing was said.

    def _answer_unheard_question(self, question, timeout=60, listen_timeout=10,
                                 **listen_kwargs):
        with self._answer_lock:
            self._pending_answer = None
            self._answer_event.clear()
            self._awaiting_text_answer.set()
        print("[AskUser] question not spoken (no voice) -- shown on screen")
        started = time.monotonic()
        try:
            if getattr(self, "_window", None):
                try:
                    self._window.restore()
                except Exception as e:
                    print(f"[AskUser] window not shown ({type(e).__name__})")
            self._notify_js(f'showAskUserPrompt("{esc(question)}")')
            heard = self._listen_once(timeout=listen_timeout,
                                      stop_event=self._answer_event, **listen_kwargs)
            if heard and not self._answer_event.is_set():
                return heard
            got = self._answer_event.wait(
                timeout=max(0.0, timeout - (time.monotonic() - started)))
            with self._answer_lock:
                return self._pending_answer if got else None
        finally:
            self._awaiting_text_answer.clear()

    def _get_text_answer(self, question, timeout=60):
        with self._answer_lock:
            self._pending_answer = None
            self._answer_event.clear()
            self._awaiting_text_answer.set()
        try:
            if self._window:
                self._window.restore()
            self._notify_js(f'showAskUserPrompt("{esc(question)}")')
            got = self._answer_event.wait(timeout=timeout)
            with self._answer_lock:
                return self._pending_answer if got else None
        finally:
            self._awaiting_text_answer.clear()

    def get_memory_stats(self):
        s = self.memory.get_stats()
        snap = self.live.snapshot
        return {
            "conversation_length": s["conversations"],
            "actions_performed": s["actions"],
            "facts_remembered": s["facts"],
            "pending_reminders": s["pending_reminders"],
            "wake_word_active": self.wake_word_active,
            "proactive_mode": False,
            "captions_enabled": self.captions_enabled,
            "active_window": snap["foreground_window"],
            "api_ready": self.api_manager.has_key,
            "enhanced_utils": self.enhanced_available,
            # Real busy state, polled by the UI (see is_agent_busy).
            "agent_busy": self._agent_lock.locked(),
            "agent_task": (self._agent_task or "")[:80],
        }

    # -- Agent entry point --------------------------------------------------

    AGENT_HARD_TIMEOUT = 900  # seconds; safety ceiling, not an expected duration

    def _task_needs_project_workspace(self, message):
        return bool(self._PROJECT_MUTATION_CUES.search(str(message or "")))

    def _explicit_workspace_path(self, message):
        """Return a project folder explicitly supplied by the user.

        A direct file path is valid evidence too, but only in user text (the
        original request or an ``ask_user`` answer).  Its containing folder
        becomes the candidate root.  This method is never called on a model
        action parameter, so an arbitrary inspected file cannot promote its
        parent directory into a trusted workspace.
        """
        text = str(message or "")
        candidates = re.findall(r"\{(?:desktop|documents|home)\}[^\s\"']+", text, re.I)
        candidates += re.findall(
            r"[\"']([A-Za-z]:[\\/][^\"']+)[\"']", text
        )
        # Users commonly reply to a clarification with only an unquoted full
        # Windows path.  The older parser silently ignored that exact form.
        candidates += re.findall(r"(?<![\w:])([A-Za-z]:[\\/][^\r\n\"']+)", text)
        candidates += re.findall(
            r"\b(?:workspace|project|folder)\s*(?:at|:)?\s*"
            r"([A-Za-z]:[\\/][^\s,;]+)",
            text,
            re.I,
        )
        for candidate in candidates:
            path = self._resolve_path_placeholders(candidate.rstrip(".,;:)]}"))
            if os.path.isdir(path):
                return os.path.abspath(path)
            if os.path.isfile(path):
                return os.path.abspath(os.path.dirname(path))
        return None

    def _trust_project_root(self, root):
        """Remember one evidence-backed project root for this task."""
        root = os.path.abspath(root)
        roots = getattr(self, "_trusted_project_roots", None)
        if roots is None:
            roots = self._trusted_project_roots = set()
        roots.add(os.path.normcase(root))

    def _is_trusted_project_root(self, path):
        """Whether ``path`` is a known, existing project root (not a guess)."""
        if not path or not os.path.isdir(path):
            return False
        candidate = os.path.normcase(os.path.abspath(path))
        roots = set(getattr(self, "_trusted_project_roots", set()))
        workspace = getattr(self, "workspace", None)
        current = getattr(workspace, "project_root", None)
        if current and os.path.isdir(current):
            roots.add(os.path.normcase(os.path.abspath(current)))
        return candidate in roots

    @staticmethod
    def _file_uri_path(value):
        value = str(value or "").strip()
        if not value.lower().startswith("file:"):
            return None
        parsed = urllib.parse.urlparse(value)
        path = urllib.parse.unquote(parsed.path or "")
        # file:///C:/work is represented as /C:/work by urlparse on Windows.
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return path.replace("/", os.sep)

    def _vscode_workspace_path(self):
        """Find an actual VS Code folder/file URI, never infer from a title."""
        try:
            for proc in psutil.process_iter(["name", "cmdline"]):
                if "code" not in (proc.info.get("name") or "").lower():
                    continue
                args = list(proc.info.get("cmdline") or [])
                for index, arg in enumerate(args):
                    value = ""
                    low = str(arg).lower()
                    if low.startswith("--folder-uri=") or low.startswith("--file-uri="):
                        value = str(arg).split("=", 1)[1]
                    elif low in ("--folder-uri", "--file-uri") and index + 1 < len(args):
                        value = args[index + 1]
                    path = self._file_uri_path(value)
                    if path:
                        if low.startswith("--file-uri") or "--file-uri=" in low:
                            path = os.path.dirname(path)
                        if os.path.isdir(path):
                            return os.path.abspath(path)
        except Exception:
            pass
        # Electron worker processes normally discard the folder URI after
        # startup. VS Code's own read-only --status report still identifies
        # its open workspace folders. A folder name alone is not enough: only
        # accept it when there is exactly one direct Desktop/Documents match.
        try:
            cli = shutil.which("code")
            if cli:
                # VS Code's Windows ``code.cmd`` wrapper is the supported
                # status interface. Run it through PowerShell so a spaced
                # installation path stays one executable argument.
                quoted_cli = cli.replace("'", "''")
                status = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"& '{quoted_cli}' --status",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=8,
                ).stdout
                names = re.findall(r"(?m)^\|\s+Folder \(([^)]+)\):", status or "")
                candidates = []
                roots = [self._get_desktop_path()]
                documents = self._resolve_path_placeholders("{documents}")
                if documents not in roots:
                    roots.append(documents)
                for name in names:
                    for base in roots:
                        path = os.path.join(base, name)
                        if os.path.isdir(path):
                            candidates.append(os.path.abspath(path))
                unique = list(dict.fromkeys(candidates))
                if len(unique) == 1:
                    return unique[0]
        except Exception:
            pass
        return None

    def _establish_workspace_for_task(self, message):
        """Seed the existing Workspace only from explicit or process evidence."""
        workspace = getattr(self, "workspace", None)
        if workspace is None:
            return False
        explicit = self._explicit_workspace_path(message)
        root = explicit
        if not root:
            current = getattr(workspace, "project_root", None)
            if current and os.path.isdir(current):
                root = current
        if not root:
            root = self._vscode_workspace_path()
        if not root:
            return False
        workspace.set_project(os.path.basename(root) or root, root)
        self._trust_project_root(root)
        # A later explicit answer can resolve a task that began ambiguously;
        # do not leave the old gate latched after reliable evidence arrives.
        self._workspace_required_for_task = False
        return True

    def ask_mistai(self, message, model=None, gender=None, input_mode="text"):
        gate_taken = False      # did THIS call acquire the run gate?
        thread_started = False  # has the agent thread (which owns release) begun?
        minimized = False       # did THIS call minimize MistAI (restored at the end)?
        heartbeat = None        # this task's "Still working." timer (voice only)
        # Single sources of truth: the model is current_model() (Nova, then
        # Flux), read once here (the ``model`` argument is ignored: nothing
        # may pick a different one per call); the gender
        # comes from the saved profile (the UI dropdown writes the profile
        # too, so text and voice can no longer disagree about "sir").
        model = self.current_model()
        if not gender:
            gender = self.memory.get_profile("user_gender", "none")
        try:
            # A typed stop is an active control command, not conversational
            # filler, when another agent currently owns the desktop.
            if self._normalize_utterance(message) in self._VOICE_STOP_WORDS:
                stopped = self.stop_current_task()
                if stopped["was_running"]:
                    msg = "Stopping."
                    self.speak(msg, interrupt=True)
                    return {
                        "success": True,
                        "command": {"action": "done", "parameter": "", "speech": msg},
                        "steps": 0,
                        "error": "",
                    }

            # "Mist, resume PortfolioSite" -- intercept before anything else
            # touches the message, and route straight to the saved TaskRecord.
            resume_rec = None
            m = re.match(r"^\s*(?:mist[,!\s]+)?resume\s+(.+?)\s*$", message, re.I)
            if m:
                resume_rec = self.task_store.find_resumable(m.group(1))
                if not resume_rec:
                    msg = f"I couldn't find a saved task matching '{m.group(1)}'."
                    self.speak(msg)
                    return {
                        "success": False,
                        "command": {"action": "done", "parameter": "", "speech": msg},
                        "steps": 0,
                        "error": "no such task",
                    }

            # -- Filler / acknowledgement: answer conversationally, no agent.
            if self._is_non_actionable(message):
                t = self._normalize_utterance(message)
                reply = ("You're welcome." if "thank" in t or t in ("ty", "thx")
                         else "Hey. What do you need?" if t in ("hi", "hello", "hey")
                         else "Okay.")
                if input_mode == "voice":
                    self._voice_mark("filler_reply", end=True)
                    self.speak(reply)
                return {
                    "success": True,
                    "command": {"action": "done", "parameter": "", "speech": reply},
                    "steps": 0,
                    "error": "",
                }

            # -- Conversation layer: pure chat gets one small model call, with
            # no gate, screen, planning or tools. Any doubt or failure falls
            # through to the agent unchanged. Skipped while an agent is busy.
            if not resume_rec and not self._agent_lock.locked():
                reply = conversation.try_chat(self, message, gender, model=model)
                if input_mode == "voice":
                    self._voice_mark("router_done", end=bool(reply))
                if reply:
                    self.memory.add_conversation("user", message)
                    self.memory.add_conversation("assistant", reply)
                    if input_mode == "voice":
                        self.speak(reply)
                    return {
                        "success": True,
                        "command": {"action": "done", "parameter": "", "speech": reply},
                        "steps": 0,
                        "error": "",
                    }

            # -- Run gate: refuse (don't queue) if a loop is already driving.
            # Take the state lock while changing the stop flag/generation so a
            # concurrent Stop cannot accidentally land on the next task.
            with self._agent_state_lock:
                gate_taken = self._agent_lock.acquire(blocking=False)
                if gate_taken:
                    # A stop flag left over from the previous task must not
                    # kill this one on its first step. This is the ONLY place
                    # it is cleared, so a stop reaches every remaining phase.
                    self._stop_requested.clear()
                    self._agent_generation += 1
                    my_gen = self._agent_generation
                    self._agent_task = message
                    self._agent_started_at = time.time()

            if gate_taken:
                self._trusted_project_roots = set()
                needs_workspace = self._task_needs_project_workspace(message)
                workspace = getattr(self, "workspace", None)
                if workspace is not None:
                    workspace.start_request()
                explicit_workspace = self._explicit_workspace_path(message)
                established = (
                    self._establish_workspace_for_task(message)
                    if needs_workspace or explicit_workspace
                    else bool(getattr(workspace, "project_root", None))
                )
                self._workspace_required_for_task = needs_workspace and not established

            if not gate_taken:
                running = (self._agent_task or "a previous task")[:60]
                msg = f"I'm still working on '{running}'. Say stop, or wait for it to finish."
                print(f"[API] ask_mistai rejected -- agent busy with: {running}")
                if input_mode == "voice":
                    self._voice_mark("busy_reply", end=True)
                    self.speak(msg)
                return {
                    "success": False,
                    "command": {"action": "done", "parameter": "", "speech": msg},
                    "steps": 0,
                    "error": "busy",
                    "busy": True,
                }

            if input_mode == "voice" and not resume_rec and task_ack_enabled():
                self.speak(TASK_ACK_SPEECH)
            self.minimize_window()
            minimized = True
            result_holder = {}
            done_event = threading.Event()

            def _release():
                # Called from the agent thread when the loop really ends.
                self._cancel_heartbeat(heartbeat)
                with self._agent_state_lock:
                    if my_gen != self._agent_generation:
                        return  # stale callback from an older run; ignore
                    self._agent_task = None
                    try:
                        self._agent_lock.release()
                    except RuntimeError:
                        pass

            def _on_step(n, action, param, speech):
                if my_gen != self._agent_generation:
                    return
                if input_mode == "voice":
                    self._voice_mark("first_step", end=True)  # later steps: no-op
                self._notify_js(
                    f'handleAgentStep({n},"{esc(action)}","{esc(str(param))}","{esc(speech)}")'
                )

            def _on_done(result):
                result_holder.update(result)
                _release()
                self._restore_after_task(my_gen)
                done_event.set()

            def _on_error(err):
                result_holder["success"] = False
                result_holder["error"] = err
                _release()
                self._restore_after_task(my_gen)
                done_event.set()

            self.memory.add_conversation("user", message)

            if input_mode == "voice":
                heartbeat = self._start_heartbeat(my_gen)
            if resume_rec:
                self.agent.resume_project(
                    resume_rec,
                    model,
                    gender,
                    input_mode=input_mode,
                    on_step=_on_step,
                    on_done=_on_done,
                    on_error=_on_error,
                )
            else:
                self.agent.run_project(
                    message,
                    model,
                    gender,
                    input_mode=input_mode,
                    on_step=_on_step,
                    on_done=_on_done,
                    on_error=_on_error,
                )

            thread_started = True

            # Multi-phase plans legitimately run for minutes (planning call +
            # up to 8 phases x 15 steps, each an LLM round-trip). The old 120s
            # cap returned success=False while the loop kept going, and the
            # caller then treated Mist as idle. Wait for the real end; if a
            # hard ceiling is hit, say so explicitly rather than pretending.
            finished = done_event.wait(timeout=self.AGENT_HARD_TIMEOUT)
            if not finished:
                print(f"[API] ask_mistai: agent still running after "
                      f"{self.AGENT_HARD_TIMEOUT}s -- reporting busy, not done")
                return {
                    "success": False,
                    "command": {"action": "done", "parameter": "", "speech": ""},
                    "steps": 0,
                    "error": "still_running",
                    "busy": True,
                }

            speech = result_holder.get("speech", "")
            if speech:
                self.memory.add_conversation("assistant", speech)

            return {
                "success": result_holder.get("success", False),
                "command": {"action": "done", "parameter": "", "speech": speech},
                "steps": result_holder.get("steps", 0),
                "error": result_holder.get("error", ""),
            }
        except Exception as e:
            # Failed after taking the gate but before the agent thread (which
            # owns the release) started -> release here, or Mist is "busy"
            # forever. Once the thread is running, its own callback releases.
            if gate_taken and not thread_started:
                self._cancel_heartbeat(heartbeat)
                with self._agent_state_lock:
                    self._agent_task = None
                    try:
                        self._agent_lock.release()
                    except RuntimeError:
                        pass
                if minimized:
                    self._restore_after_task(my_gen)
            return {"success": False, "error": str(e)}

    def execute_action(self, action_type, parameter, speech=""):
        if speech:
            self.speak(speech)
        return {"success": True}

    # -- Action execution (the action menu the agent picks from) ------------

    def _exec_sync(self, action, parameter):
        param = str(parameter) if not isinstance(parameter, str) else parameter
        shown = repr(param[:80]) if verbose_logs() else step_param_for_log(action, param)
        print(f"[Exec] {action}({shown})")
        self._track_selection(action)

        try:
            if action == "open_app":
                return self._with_app_note(param, self._act_open_app(param))
            elif action == "type_text":
                return self._act_type_text(param)
            elif action == "type_search":
                return self._act_type_search(param)
            elif action == "click_coords":
                return self._act_click_coords(param)
            elif action == "click_on_text":
                return self._handler_click(self.click_handler.click_on_text, param)
            elif action == "click_nth":
                return self._act_click_nth(param)
            elif action == "uia_click":
                return self._act_uia_click(param)
            elif action == "hotkey":
                return self._act_hotkey(param)
            elif action == "press_key":
                return self._act_press_key(param)
            elif action == "scroll":
                return self._act_scroll(param)
            elif action == "check_email":
                self.last_result_text = self.check_email()
                self.speak(self.last_result_text)
                return True
            elif action == "open_url":
                return self.open_url(param)
            elif action == "screenshot":
                ok = self.screenshot_to_file(param if param else "screenshot.png")
                self.last_result_text = (
                    "Opened the Windows snipping overlay (Win+Shift+S); the user "
                    "must select a region -- nothing has been saved to disk yet."
                    if ok else "Could not open the snipping overlay"
                )
                self.speak("Screenshot saved" if ok else "Screenshot failed")
                return ok
            elif action == "find_file":
                return self._act_find_file(param)
            elif action == "get_clipboard":
                clip = self.get_clipboard()
                self.last_result_text = f"Clipboard: {clip}"
                self.speak(clip if len(clip) <= 200 else clip[:200] + "...")
                return True
            elif action == "set_clipboard":
                return self.set_clipboard(param)
            elif action == "analyze_screen":
                # Perception action: the DESCRIPTION is the result and must
                # reach the model. It used to be spoken aloud and discarded.
                with signal_scope(self, "reading"):     # the orb's reading look
                    result = self.analyze_screen()
                self.last_result_text = (
                    f"Window: {result['app']} | Open: {result['open_apps']} | "
                    f"{result['description']}"
                )
                return True
            elif action == "read_screen_text":
                with signal_scope(self, "reading"):
                    self.read_screen_text(param)
                return True
            elif action == "list_apps":
                self.last_result_text = f"Open apps: {self.list_open_apps()}"
                self.speak(self.last_result_text)
                return True
            elif action == "remember":
                self.remember_fact(param)
                self.last_result_text = f"Remembered: {param}"
                self.speak(self.last_result_text)
                return True
            elif action == "recall":
                facts = self.recall_facts(query=param)
                self.last_result_text = (
                    "Recalled facts: " + " | ".join(str(f) for f in facts[:5])
                    if facts
                    else f"No stored facts match '{param}'"
                )
                self.speak(
                    f"I remember: {facts[0]}"
                    if facts
                    else "I don't have any memories about that"
                )
                return True
            elif action == "remind_me":
                parts = param.split("|", 1)
                self.add_reminder(parts[0], parts[1] if len(parts) > 1 else None)
                self.last_result_text = f"Reminder set: {parts[0]}" + (
                    f" (due {parts[1]})" if len(parts) > 1 else ""
                )
                self.speak(f"Reminder set: {parts[0]}")
                return True
            elif action == "get_reminders":
                reminders = self.get_reminders()
                self.last_result_text = (
                    "Pending reminders: "
                    + "; ".join(
                        f"{r['text']}" + (f" (due {r['due']})" if r.get("due") else "")
                        for r in reminders[:10]
                    )
                    if reminders
                    else "No pending reminders"
                )
                if reminders:
                    self.speak(
                        "Reminders: " + "; ".join(r["text"] for r in reminders[:3])
                    )
                else:
                    self.speak("No reminders")
                return True
            elif action == "battery_status":
                self.last_result_text = self.get_battery_status()
                self.speak(self.last_result_text)
                return True
            elif action == "active_app":
                self.last_result_text = f"Current app: {self.get_active_window()}"
                self.speak(self.last_result_text)
                return True
            elif action == "lock_screen":
                self.speak("Locking screen")
                return self.lock_screen()
            elif action == "minimize_all":
                return self.minimize_all_windows()
            elif action == "switch_app":
                # The app the model wants input to go to, even if the switch
                # fails -- type_text/keys then warn instead of a silent ok.
                self._last_opened_app = param.lower().strip()
                return self._with_app_note(param, self.switch_to_app(param))
            elif action == "uia_find_input":
                return self._act_uia_find_input(param)
            elif action == "save_file_as":
                return self._act_save_file_as(param)
            elif action == "window_list":
                return self._act_window_list()
            elif action == "window_activate":
                return self._act_window_activate(param)
            elif action == "window_close":
                return self._act_window_op("close_window", param)
            elif action == "window_maximize":
                return self._act_window_op("maximize_window", param)
            elif action == "window_minimize":
                return self._act_window_op("minimize_window", param)
            elif action == "browser_open":
                return self._act_browser_open(param)
            elif action == "browser_navigate":
                return self._act_browser_navigate(param)
            elif action == "browser_search":
                return self._act_browser_search(param)
            elif action == "browser_close":
                return self._act_browser_close()
            elif action == "browser_click":
                return self._act_browser_click(param)
            elif action == "browser_elements":
                return self._act_browser_elements(param)
            elif action == "browser_scroll":
                return self._act_browser_scroll(param)
            elif action == "browser_fill":
                return self._act_browser_fill(param)
            elif action == "browser_get_text":
                return self._act_browser_get_text(param)
            elif action == "browser_press_key":
                return self._act_browser_press_key(param)
            elif action == "brightness_get":
                return self._act_brightness_get()
            elif action == "brightness_set":
                return self._act_brightness_set(param)
            elif action == "mss_screenshot":
                return self._act_mss_screenshot(param)
            elif action == "schedule_reminder":
                return self._act_schedule_reminder(param)
            elif action == "organize_desktop":
                return self._act_organize_desktop(param)
            elif action == "run_command":
                return self._act_run_command(param)
            elif action == "write_file":
                return self._act_write_file(param)
            elif action == "move_files":
                return self._act_move_files(param)
            elif action == "copy_files":
                return self._act_copy_files(param)
            elif action == "inspect_file":
                return self._act_inspect_file(param)
            elif action == "patch_json":
                return self._act_patch_json(param)
            elif action == "patch_file":
                return self._act_patch_file(param)
            elif action == "quick_message":
                return self._act_quick_message(param)
            elif action == "volume_mute":
                return self._act_volume_mute(param)
            elif action == "volume_unmute":
                return self._act_volume_unmute(param)
            elif action == "open_project_in_vscode":
                return self._act_open_project_in_vscode(param)
            elif action == "browser_import_cookies":
                return self._act_browser_import_cookies(param)
            elif action == "browser_use_daily_profile":
                return self._act_browser_use_daily_profile(param)
            elif action == "ui_elements":
                return self._act_ui_elements(param)
            elif action == "describe_action":
                from .tools import SPECS, describe_action
                self.last_result_text = describe_action(param)
                return param.strip().lower() in SPECS
        except Exception as e:
            print(f"[Exec] Error in {action}: "
                  f"{e if verbose_logs() else type(e).__name__}")  # may quote the parameter
            return False
        return False

    # -- Generic Windows app resolution ---------------------------------------

    def _resolve_app_exe(self, name):
        exe_name = name if name.lower().endswith(".exe") else name + ".exe"
        try:
            import winreg

            key_path = (
                rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
            )
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                        if path and os.path.exists(path):
                            return path
                except FileNotFoundError:
                    continue
        except Exception as e:
            print(f"[ResolveApp] Registry lookup failed for '{name}': "
                  f"{e if verbose_logs() else type(e).__name__}")
        return None

    @staticmethod
    def _ps_single_quote(value):
        """Quote one value for a PowerShell single-quoted string literal."""
        return str(value or "").replace("'", "''")

    # ------------------------------------------------------------
    # START-APPS CANDIDATES
    # ------------------------------------------------------------
    # open_app's Start-apps route: every Start menu entry matching the name,
    # ranked by core/start_apps.rank (exact > whole word > prefix >
    # substring; an inbox Windows package first within a tier), from the
    # in-memory list (StartAppsCache: loaded at startup in the background,
    # milliseconds per lookup). A parameter that IS an AppID from the list
    # (the failure message names them) selects exactly that entry.

    def _start_apps_cache(self):
        cache = self.__dict__.get("start_apps")
        if cache is None:
            from .start_apps import StartAppsCache

            cache = self.__dict__["start_apps"] = StartAppsCache()
        return cache

    def _start_app_candidates(self, name):
        """Ranked candidate dicts (start_apps.rank) for ``name``; [] for a
        name too short to match or when nothing matches."""
        if len(self._normalize_app_name(name)) < 2:
            return []
        cache = self._start_apps_cache()
        wanted = str(name).strip()
        if "!" in wanted:                             # an AppID chosen from a failure message
            exact = cache.by_app_id(wanted)
            if exact:
                return exact
        return cache.lookup(wanted)

    def _resolve_app_id(self, name):
        """The best Start-apps AppID for ``name``, or None."""
        found = self._start_app_candidates(name)
        return found[0]["app_id"] if found else None

    def _normalize_app_name(self, name):
        """Strip version numbers, bitness suffixes, and non-alphanumeric
        noise so 'OBS Studio' and 'obs64' can be recognized as the same
        app, without hardcoding a single alias -- works for any app with a
        numbered/suffixed process name (obs64, opera_gx64, discord_x64, etc)."""
        n = str(name or "").lower().strip()
        n = re.sub(r"[^a-z0-9\s]", "", n)
        n = re.sub(r"\d+$", "", n).strip()
        n = re.sub(r"\s+", "", n)
        return n

    def is_app_running(self, app_name):
        """System-level fact-check via psutil's real process list -- no
        screenshots, no title-text guessing."""
        target = self._normalize_app_name(app_name)
        if len(target) < 2:
            return False
        try:
            for p in psutil.process_iter(["name", "pid"]):
                if is_self_process(p.info["name"], p.info.get("pid")):
                    continue
                if process_matches(app_name, p.info["name"]):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _window_belongs_to(hwnd, app_lower):
        """core.live_state.window_belongs_to: process identity, plus the
        UWP frame-host case (child window, or the frame title)."""
        return window_belongs_to(hwnd, app_lower)

    def _find_hwnd_by_process_name(self, app_lower):
        """Find a visible window belonging to the app (_window_belongs_to:
        process identity, plus the UWP frame-host case)."""
        matches = self._find_hwnds_by_process_name(app_lower)
        return matches[0] if matches else None

    def _find_hwnds_by_process_name(self, app_lower):
        """Every visible, titled window of the app, in z-order."""
        try:
            import win32gui
        except ImportError:
            return []

        matches = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if not win32gui.GetWindowText(hwnd):
                return True
            try:
                if self._window_belongs_to(hwnd, app_lower):
                    matches.append(hwnd)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(callback, None)
        return matches

    def _focus_hwnd(self, hwnd):
        """Bring a window to the front. Returns (ok, reason); reason says why
        Windows refused. pywin32's SetForegroundWindow RAISES when Windows
        refuses (foreground lock), so every try is caught and every try is
        checked against the real foreground window."""
        import win32gui

        def in_front():
            try:
                return win32gui.GetForegroundWindow() == hwnd
            except Exception:
                return False

        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE, only if minimized
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.6)
        if in_front():
            return True, ""
        # Share the foreground thread's input state, which Windows accepts
        # as permission to change the foreground.
        self._attach_and_focus(hwnd)
        time.sleep(0.3)
        if in_front():
            return True, ""
        # A synthetic key press also grants the permission; retry once.
        try:
            pyautogui.press("alt")
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.4)
        if in_front():
            return True, ""
        return False, self._focus_refusal_reason()

    @staticmethod
    def _attach_and_focus(hwnd):
        try:
            import win32api
            import win32gui
            import win32process
        except ImportError:
            return
        try:
            fg = win32gui.GetForegroundWindow()
            fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
            own_thread = win32api.GetCurrentThreadId()
        except Exception:
            return
        attached = False
        try:
            if fg_thread and fg_thread != own_thread:
                win32process.AttachThreadInput(own_thread, fg_thread, True)
                attached = True
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(own_thread, fg_thread, False)
                except Exception:
                    pass

    @staticmethod
    def _focus_refusal_reason():
        """Which window kept the foreground (process name only, no title)."""
        try:
            import win32gui
            import win32process

            fg = win32gui.GetForegroundWindow()
            if not fg:
                return "Windows refused focus (no window is in front)"
            pid = win32process.GetWindowThreadProcessId(fg)[1]
            try:
                name = psutil.Process(pid).name()
            except Exception:
                name = "an unknown process"
            whose = "MistAI's own window" if is_self_process(name, pid) else "not MistAI"
            state = "minimized" if win32gui.IsIconic(fg) else "not minimized"
            return (f"Windows refused focus; the window in front belongs to "
                    f"{name} ({whose}, {state})")
        except Exception as e:
            return f"Windows refused focus ({type(e).__name__} reading the window in front)"

    @staticmethod
    def _is_own_browser_window(hwnd):
        """Does this window's process descend from MistAI (its Playwright
        browser runs as a child of the Playwright driver MistAI started)?"""
        try:
            import win32process

            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            return any(p.pid == os.getpid() for p in psutil.Process(pid).parents())
        except Exception:
            return False

    def app_window_state(self, app_name):
        """What the USER would say about this app right now.

            ("foreground",  title)  in front, usable
            ("visible",     title)  has a top-level window (maybe behind/minimised)
            ("hidden",      "")     process exists but no window -- tray/background
            ("not_running", "")     no matching process
            ("unknown",     "")     win32 unavailable

        Shared by _act_open_app (decides whether to relaunch) and by
        verify._app_visible (decides whether open_app actually worked), so
        the handler and the verifier can't disagree about the same fact."""
        app_lower = app_name.lower().strip()
        if not self.is_app_running(app_lower):
            return "not_running", ""
        try:
            import win32gui
        except ImportError:
            return "unknown", ""
        try:
            fg = win32gui.GetForegroundWindow()
            if fg:
                try:
                    if self._window_belongs_to(fg, app_lower):
                        return "foreground", win32gui.GetWindowText(fg)
                except Exception:
                    pass
            hwnd = self._find_hwnd_by_process_name(app_lower)
            if hwnd:
                return "visible", win32gui.GetWindowText(hwnd)
            return "hidden", ""
        except Exception as e:
            print(f"[AppState] {e}")
            return "unknown", ""

    def _foreground_process_matches(self, app_name):
        try:
            import win32gui

            return self._window_belongs_to(
                win32gui.GetForegroundWindow(), str(app_name or "").lower().strip()
            )
        except Exception:
            return False

    def _wait_for_app_window(self, app_lower, timeout=5.0):
        """Wait for a VISIBLE window, not merely a process -- Electron apps
        register a process well before they have anything to show."""
        deadline = time.time() + timeout
        watched = getattr(getattr(self, "live", None), "window_events", None)
        while time.time() < deadline:
            state, _ = self.app_window_state(app_lower)
            if state in ("foreground", "visible"):
                return True
            if isinstance(watched, clip.WindowEvents) and watched.healthy():
                # Wake on the next window event instead of a blind 0.4 s
                # (the same check, the same timeout).
                mark = watched.mark()
                watched.wait_for(lambda: watched.changed_since(mark),
                                 min(0.4, max(0.0, deadline - time.time())), poll=0.4)
            else:
                time.sleep(0.4)
        return False

    def switch_to_app(self, app_name):
        """Switch to an app by matching its actual PROCESS identity, not
        window-title text. Returns True only if a real match was found and
        focus was verified afterward -- never lies about success."""
        app_lower = str(app_name or "").lower().strip()
        if len(self._normalize_app_name(app_lower)) < 2:
            print("[SwitchApp] Refusing an empty or one-character app target")
            self.last_result_text = "switch_app needs a specific app name."
            return False
        hwnds, title, note, refused = [], "", "", ""
        try:
            import win32gui

            hwnds = self._find_hwnds_by_process_name(app_lower)
            if hwnds:
                hwnd = hwnds[0]
                title = (win32gui.GetWindowText(hwnd) or "")[:40]
                note = self._switch_match_note(hwnds)
                ok, refused = self._focus_hwnd(hwnd)
                if ok:
                    self.last_result_text = (
                        f"Switched to '{app_name}' (window '{title}'){note}"
                    )
                    return True
                print(f"[SwitchApp] Window for '{app_name}' exists but refused focus: "
                      f"{refused}")
        except Exception as e:
            print(f"[SwitchApp] win32gui tier failed: {e if verbose_logs() else type(e).__name__}")

        # Fallback: PowerShell's generic AppActivate (works via substring
        # match against window titles, independent of win32gui quirks).
        try:
            quoted_name = self._ps_single_quote(app_name)
            ps_cmd = (
                "(New-Object -ComObject WScript.Shell)."
                f"AppActivate('{quoted_name}')"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=5,
            )
            time.sleep(0.6)
            # AppActivate matches title text ("code" -> "Codex ..."), so only
            # accept it if the focused window's PROCESS is the requested app.
            if self._foreground_process_matches(app_name):
                self.last_result_text = f"Switched to '{app_name}' (via AppActivate){note}"
                return True
        except Exception as e:
            print(f"[SwitchApp] PowerShell AppActivate failed: "
                  f"{e if verbose_logs() else type(e).__name__}")

        if hwnds:
            self.last_result_text = (
                f"'{app_name}' has a window ('{title}'){note}, but it could not be "
                f"brought to the front: {refused or 'Windows refused focus'}"
            )
        else:
            print(f"[SwitchApp] No window found for process '{app_name}'")
            self.last_result_text = f"No window found for '{app_name}'"
        return False

    def _switch_match_note(self, hwnds):
        """How many windows matched, and whether the chosen one (or another)
        is MistAI's own automation browser. Reported, never excluded."""
        parts = []
        if len(hwnds) > 1:
            parts.append(f"1 of {len(hwnds)} matching windows")
        own = [self._is_own_browser_window(h) for h in hwnds]
        if own[0]:
            parts.append("this window is MistAI's own browser, driven by browser_* actions")
        elif any(own[1:]):
            parts.append("another matching window is MistAI's own browser")
        return f" [{'; '.join(parts)}]" if parts else ""
    

    def _act_open_app(self, param):
        app_lower = str(param or "").lower().strip()
        if len(self._normalize_app_name(app_lower)) < 2:
            self.last_result_text = "open_app needs a specific app name."
            return False

        if app_lower in self._BROWSER_APP_NAMES:
            return self._act_browser_open("")

        # -- Already has a window? Bring it forward. Never relaunch. ---------
        # (Relaunching an app that already has a window gives the user two
        # Notepads; and for single-instance apps it just no-ops slowly.)
        state, title = self.app_window_state(app_lower)
        if state in ("foreground", "visible"):
            if state == "foreground" or self.switch_to_app(app_lower):
                self._last_opened_app = app_lower
                self.last_result_text = (
                    f"'{param}' was already open -- brought its window "
                    f"('{title[:40]}') to the front"
                )
                return True
            # A window exists but Windows would not give it focus.
            self.last_result_text = (
                f"'{param}' is open (window '{title[:40]}') but could not be "
                f"brought to the front; try window_activate with that title"
            )
            return False

        # -- Running with NO window (tray/background), or not running: launch.
        # For tray apps (Discord, Spotify, Slack) launching the exe is the
        # correct, documented way to restore the window -- they are
        # single-instance and re-show themselves. For everything else this
        # is a genuine first launch.
        if state == "hidden":
            print(f"[OpenApp] '{param}' running with no window -- relaunching to restore it")

        # Numbers-only timing: path/app-id lookup, launch, window wait, focus.
        times = {"lookup": 0.0, "launch": 0.0, "window": 0.0, "focus": 0.0}
        t = time.perf_counter()
        exe_path = self._resolve_app_exe(param)
        times["lookup"] += time.perf_counter() - t
        if exe_path:
            t = time.perf_counter()
            try:
                subprocess.Popen(exe_path)
            except Exception as e:
                print(f"[OpenApp] Path launch failed: {e}")
                exe_path = None
            times["launch"] += time.perf_counter() - t

        if not exe_path:
            started = time.perf_counter()
            candidates = self._start_app_candidates(param)
            times["lookup"] += time.perf_counter() - started
            if candidates:
                cache = self._start_apps_cache()
                route = "app-id cached" if cache.last_source in ("cache", "stale cache") else "app-id"
                return self._open_start_app(param, candidates, times, route, started)

        route = "path" if exe_path else "start-menu"

        # Start-menu typing is the last resort: only when there was nothing
        # to launch by path or Start-apps ID and no window has appeared.
        if not exe_path and not self._wait_for_app_window(app_lower, timeout=1.5):
            try:
                pyautogui.press("win")
                time.sleep(0.8)
                pyautogui.write(param, interval=0.08)
                time.sleep(0.5)
                pyautogui.press("enter")
            except Exception as e:
                print(f"[OpenApp] Start-menu fallback failed: {e}")

        t = time.perf_counter()
        found = self._wait_for_app_window(app_lower, timeout=6.0)
        times["window"] += time.perf_counter() - t
        if found:
            self._finish_open(param, app_lower, times)
            self._log_open_timing(route, times, True)
            return True
        self._log_open_timing(route, times, False)

        # Distinguish "never started" from "started but stayed in the tray"
        # so the model doesn't retry the same launch.
        if self.is_app_running(app_lower):
            self.last_result_text = (
                f"'{param}' is running but never showed a window -- it may live "
                f"in the system tray or be blocked by a login/update dialog"
            )
        else:
            self.last_result_text = f"'{param}' did not start"
        print(f"[OpenApp] {self.last_result_text}")
        return False

    def _finish_open(self, param, app_lower, times, how=""):
        """A window appeared: focus and maximize it, set the input target and
        the result text."""
        t = time.perf_counter()
        self.switch_to_app(app_lower)
        time.sleep(0.4)
        try:
            pyautogui.hotkey("win", "up")
        except Exception:
            pass
        times["focus"] += time.perf_counter() - t
        self._last_opened_app = app_lower
        state, title = self.app_window_state(app_lower)
        self.last_result_text = f"Launched '{param}' (window '{title[:40]}', {state}){how}"

    # ------------------------------------------------------------
    # START-APPS LAUNCH CHAIN
    # ------------------------------------------------------------
    # The ranked candidates of the best match tier (exact before whole word
    # before prefix before substring; never a weaker tier), at most
    # OPEN_APP_MAX_CANDIDATES, each through the Start-apps folder
    # (explorer.exe shell:AppsFolder\<AppID>); then the first again through
    # ShellExecute (os.startfile, the other shell route). Each waits for a
    # window at most OPEN_APP_TRY_WAIT_S, all of it within OPEN_APP_BUDGET_S.
    # A failure names every try and the weaker matches it did not try, so
    # the model can pick one (open_app accepts the AppID) instead of
    # retrying blindly; the Start-apps list is then refreshed.

    OPEN_APP_BUDGET_S = 7.0
    OPEN_APP_TRY_WAIT_S = 3.0
    OPEN_APP_MIN_WAIT_S = 0.5
    OPEN_APP_MAX_CANDIDATES = 3

    def _open_start_app(self, param, candidates, times, route, started):
        from .start_apps import TIER_LABELS, describe_candidate

        best = candidates[0]["tier"]
        same = [c for c in candidates if c["tier"] == best][:self.OPEN_APP_MAX_CANDIDATES]
        weaker = [c for c in candidates if c["tier"] < best]
        attempts = [(c, "start-apps folder") for c in same] + [(same[0], "ShellExecute")]
        deadline = started + self.OPEN_APP_BUDGET_S
        tried = []
        for i, (cand, how) in enumerate(attempts):
            left = deadline - time.perf_counter()
            if left < self.OPEN_APP_MIN_WAIT_S:
                break
            t = time.perf_counter()
            launched = self._launch_app_id(cand["app_id"], how)
            times["launch"] += time.perf_counter() - t
            tried.append(f"{describe_candidate(cand)} via the {how}"
                         + ("" if launched else " (the launch itself failed)"))
            if not launched:
                continue
            last = i == len(attempts) - 1
            wait = left if last else min(self.OPEN_APP_TRY_WAIT_S, left)
            t = time.perf_counter()
            found = self._wait_for_app_window(cand["name"].lower(), timeout=wait)
            times["window"] += time.perf_counter() - t
            if found:
                print(f"[OpenApp] attempt {len(tried)} of {len(attempts)} showed a window "
                      f"({TIER_LABELS[best]} match, {len(same)} candidate(s))")
                note = ""
                if len(tried) > 1:
                    note = (f" -- the earlier launch(es) showed no window: "
                            f"{'; '.join(tried[:-1])}. This one was {describe_candidate(cand)}")
                self._finish_open(param, cand["name"].lower(), times, note)
                self._log_open_timing(route, times, True)
                return True
        self._log_open_timing(route, times, False)
        print(f"[OpenApp] no window after {len(tried)} attempt(s) "
              f"({TIER_LABELS[best]} match, {len(same)} candidate(s), {len(weaker)} weaker)")
        self._start_apps_cache().invalidate()
        spent = time.perf_counter() - started
        msg = (f"'{param}' showed no window within {spent:.0f}s. Tried: {'; '.join(tried)}.")
        if weaker:
            msg += (" Other Start-menu matches (not tried, weaker): "
                    + "; ".join(describe_candidate(c) for c in weaker[:3]) + ".")
        msg += (" Do not retry the same launch: open_app with one of the AppIDs above picks "
                "that exact entry, or try a different approach.")
        self.last_result_text = msg
        return False

    @staticmethod
    def _launch_app_id(app_id, how="start-apps folder"):
        """Start a Start-apps AppID through the shell (the Start-apps folder
        via explorer.exe, or ShellExecute); False if that failed."""
        target = f"shell:AppsFolder\\{app_id}"
        try:
            if how == "ShellExecute":
                os.startfile(target)
            else:
                subprocess.Popen(["explorer.exe", target])
            return True
        except Exception as e:
            print(f"[OpenApp] Start-Apps launch failed ({type(e).__name__})")
            return False

    @staticmethod
    def _log_open_timing(route, times, ok):
        """One numbers-only line per launch (fixed route label, seconds)."""
        print(f"[OpenApp] timing route={route} "
              + " ".join(f"{k} {v:.2f}s" for k, v in times.items())
              + f" {'window' if ok else 'no window'}")

    def _wait_for_app_running(self, app_lower, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_app_running(app_lower):
                return True
            time.sleep(0.4)
        return False

    # After a uia_click: re-read the window's tree after each of these waits
    # until it differs from before (a changed display, a checked box...).
    UIA_EFFECT_WAITS = (0.15, 0.25, 0.35)
    last_uia_effect = None      # True changed | False unchanged | None not checked
    last_click_effect = None    # the same for OCR / vision / coordinate clicks

    def _act_uia_click(self, param):
        """Find and click an element by its accessible name using UIA --
        reads the real UI tree instead of guessing pixel coordinates from a
        screenshot. Try this before click_on_text for anything inside a
        normal desktop app."""
        ref = self._control_ref(param)
        if ref is not None:
            return self._uia_click_number(ref)
        fw = self.live.snapshot.get("foreground_window", "")
        if not fw or fw == "Unknown":
            self.last_result_text = "No foreground window to search."
            return False

        # One or two characters: the whole name, never a substring ("7" is in
        # "Display is 37"); then, for a key, its spelled-out name ("Seven").
        # Longer names: the best rank wins (exact > whole word > prefix >
        # substring), so "Clear" never lands on "Clear all memory"; a tie
        # that stays open fails and lists the names.
        short = len(str(param or "").strip()) <= 2
        element, ambiguous = self._uia_lookup(fw, param, partial=not short)
        spelled = "" if element or ambiguous else spelled_key_name(param)
        if spelled:
            element, ambiguous = self._uia_lookup(fw, spelled, partial=False)
            print(f"[UIA] retried with the spelled-out key name: "
                  f"{'found' if element else 'not found'}")
        if ambiguous:
            self.last_result_text = (
                f"ambiguous: '{param}' matches {', '.join(ambiguous[:6])}"
                + (f" and {len(ambiguous) - 6} more" if len(ambiguous) > 6 else "")
                + " -- nothing was clicked; use the full name of the one you mean.")
            return False
        if element:
            # What the window's tree says now (the walk the lookup just made),
            # to tell afterwards whether the action changed anything.
            sig = getattr(self.uia_nav, "window_signature", None)
            before = sig(fw, fresh=False) if callable(sig) else None
            activate = getattr(self.uia_nav, "activate_element", None)
            how = activate(element) if callable(activate) else (
                "click" if self.uia_nav.click_element(element) else "")
            if how and not self._orb_pointer_active():   # the orb pointed instead
                try:
                    rect = element.rectangle()
                    from .caption import get_indicator

                    get_indicator().show(            # after the action, never before
                        (rect.left + rect.right) // 2,
                        (rect.top + rect.bottom) // 2,
                        method="UIA",
                        label=param,
                    )
                except Exception:
                    pass  # debug-only; never block the real click
            name = f"'{param}'" + (f" (the button named '{spelled}')" if spelled else "")
            if not how:
                self.last_uia_effect = None
                self.last_result_text = f"Found {name} but the UIA action failed"
                return False
            changed = None
            if before is not None:
                changed = False
                for wait in self.UIA_EFFECT_WAITS:
                    time.sleep(wait)
                    after = sig(fw, fresh=True)
                    if after is None:
                        changed = None
                        break
                    if after != before:
                        changed = True
                        break
            self.last_uia_effect = changed
            route = {"invoke": "UIA Invoke", "toggle": "UIA Toggle", "select": "UIA Select",
                     "click": "a mouse click (no UIA pattern)"}.get(how, how)
            print(f"[UIA] {how}; window text {'changed' if changed else 'unchanged' if changed is False else 'not checked'}")
            self.last_result_text = f"Clicked {name} via {route}" + (
                "; the window's accessibility text changed." if changed
                else "; but NOTHING in the window's accessibility text changed afterwards, so it "
                     "may not have taken effect -- check the result before relying on it."
                if changed is False else ".")
            return True

        self.last_result_text = f"'{param}' not found in the UIA element tree of '{fw}'"
        return False

    def _uia_lookup(self, window, name, partial):
        """(element or None, ambiguous names) through the navigator's ranked
        ``resolve_by_name``; a navigator without it (an older fake) falls back
        to ``find_element_by_name``. Logs only the rank label."""
        resolve = getattr(self.uia_nav, "resolve_by_name", None)
        if not callable(resolve):
            return self.uia_nav.find_element_by_name(window, name, partial=partial), []
        info, label, ambiguous = resolve(window, name, partial=partial)
        if label:
            print(f"[UIA] name match: {label}"
                  + (f", ambiguous ({len(ambiguous)})" if ambiguous else ""))
        return (info.element if info else None), list(ambiguous or [])

    # ------------------------------------------------------------
    # NATIVE CONTROL LIST
    # ------------------------------------------------------------
    # The numbered list of the native window in front (click_handler
    # read_controls / order_controls / format_control_list): ui_elements
    # returns it; open_app and switch_app append it when their window is
    # in front; input actions and clicks append it only when the window's
    # controls changed since its last list. Never for browsers (browser_*
    # lists the page), MistAI's own windows or a window other than the
    # phase's app (list_after). Logs counts and timing, never names.

    LIST_ALWAYS_ACTIONS = ("open_app", "switch_app")
    LIST_ON_CHANGE_ACTIONS = ("uia_click", "press_key", "type_text", "hotkey",
                              "click_on_text", "click_nth", "click_coords")

    def _control_numbers(self):
        from .click_handler import ControlNumbers

        nums = self.__dict__.get("_control_nums")
        if nums is None:
            nums = self.__dict__["_control_nums"] = ControlNumbers()
        return nums

    def _native_list_window(self, need_target=True):
        """(hwnd, title) of the native window in front that may be listed,
        else None: not a browser, not MistAI's (or its Playwright) window,
        and with need_target the app this phase opened or switched to."""
        from .executor import BROWSER_HINTS

        try:
            import win32gui
            import win32process

            hwnd = clip.foreground_for_reading()[0]
            if not hwnd:
                return None
            if need_target:
                target = getattr(self, "_last_opened_app", None)
                if not target or not self._window_belongs_to(hwnd, target):
                    return None
            title = win32gui.GetWindowText(hwnd) or ""
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if is_self_process(psutil.Process(pid).name(), pid):
                return None
            if hwnd == clip.playwright_window() or any(h in title.lower() for h in BROWSER_HINTS):
                return None
            return hwnd, title
        except Exception:
            return None

    def _read_control_list(self, hwnd, title, reader=None):
        """(list text, read dict, signature): one read, numbered and
        formatted; the window's signature is remembered for list_after."""
        from .click_handler import (control_signature, format_control_list, order_controls,
                                    read_controls)

        got = (reader or read_controls)(hwnd)
        listed, counts = order_controls(got["rows"], title=title)
        numbered = self._control_numbers().stamp(hwnd, listed)
        sig = control_signature(got["rows"])
        self.__dict__.setdefault("_control_sigs", {})[hwnd] = sig
        print(f"[UIA] list: {len(numbered)} listed, {counts['unnamed']} unnamed, "
              f"{counts['offscreen']} off-screen, {got['ms']:.0f} ms"
              + (", cut by the time budget" if got.get("timed_out") else "")
              + (f", read failed ({got['error']})" if got.get("error") else ""))
        return format_control_list(numbered, counts, title, got.get("timed_out")), got, sig

    def _act_ui_elements(self, param=""):
        """ui_elements: a fresh numbered list of the native window in front."""
        target = self._native_list_window(need_target=False)
        if not target:
            self.last_result_text = (
                "ui_elements: the window in front is not a native app window (a page in "
                "MistAI's browser is listed by browser_elements; MistAI's own window is "
                "never listed).")
            return False
        text, got, _sig = self._read_control_list(*target)
        if not got["rows"] and got.get("error"):
            self.last_result_text = (
                f"ui_elements: the window's accessibility tree could not be read "
                f"({got['error']}). Use the app's keyboard shortcuts, or vision.")
            return False
        self.last_result_text = text
        return True

    def _list_after(self, action, ok):
        """Append the control list to the result of ``action`` (see the
        section comment). Returns ok unchanged; never raises."""
        if action not in self.LIST_ALWAYS_ACTIONS + self.LIST_ON_CHANGE_ACTIONS:
            return ok
        try:
            target = self._native_list_window(need_target=True)
            if not target:
                return ok
            hwnd, title = target
            old = self.__dict__.get("_control_sigs", {}).get(hwnd)
            if action in self.LIST_ON_CHANGE_ACTIONS:
                from .click_handler import control_signature, read_controls

                got = read_controls(hwnd)
                if not got["rows"] or (old is not None
                                       and control_signature(got["rows"]) == old):
                    return ok                         # unreadable or unchanged: no list
                text, _got, _sig = self._read_control_list(hwnd, title,
                                                           reader=lambda _h: got)
            else:
                text, got, _sig = self._read_control_list(hwnd, title)
                if not got["rows"]:
                    return ok                         # nothing readable: no empty list
            self.last_result_text = (str(self.last_result_text or "") + "\n" + text).strip()
        except Exception as e:
            print(f"[UIA] list not added ({type(e).__name__})")
        return ok

    # ------------------------------------------------------------
    # CLICK EFFECT PROBE
    # ------------------------------------------------------------
    # Before/after facts for a click on the phase's app (its window in
    # front): the foreground window and title, the process's top-level
    # windows, the focused element and the controls' signature
    # (click_handler.describe_click_effect). With no known target the click
    # is not probed (nothing to compare reliably), as before.

    CLICK_EFFECT_WAITS = (0.15, 0.25, 0.35)

    def _click_probe(self, hwnd=None):
        """A probe dict of the listable window in front (or ``hwnd``), else None."""
        from .click_handler import control_signature, read_controls

        try:
            import win32gui
            import win32process

            if hwnd is None:
                target = self._native_list_window(need_target=True)
                if not target:
                    return None
                hwnd = target[0]
            fg = win32gui.GetForegroundWindow()
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            wins = []

            def cb(h, _):
                if win32gui.IsWindowVisible(h):
                    try:
                        if win32process.GetWindowThreadProcessId(h)[1] == pid:
                            wins.append(h)
                    except Exception:
                        pass
                return True

            win32gui.EnumWindows(cb, None)
            focus = None
            try:
                from pywinauto.uia_defines import IUIA

                f = IUIA().iuia.GetFocusedElement()
                focus = (tuple(f.GetRuntimeId()), f.CurrentName)
            except Exception:
                pass
            return {"hwnd": hwnd, "fg": fg, "title": win32gui.GetWindowText(fg),
                    "windows": frozenset(wins), "focus": focus,
                    "sig": control_signature(read_controls(hwnd)["rows"])}
        except Exception:
            return None

    def _click_effect(self, before):
        """(sentence, changed) after a click, polling CLICK_EFFECT_WAITS until
        something differs; ("", None) without a probe."""
        from .click_handler import describe_click_effect

        if not before:
            return "", None
        sentence, changed = "", None
        for wait in self.CLICK_EFFECT_WAITS:
            time.sleep(wait)
            sentence, changed = describe_click_effect(before, self._click_probe(before["hwnd"]))
            if changed:
                break
        return sentence, changed

    def _report_click_effect(self, before):
        """Set last_click_effect and add the effect to the result text."""
        sentence, changed = self._click_effect(before)
        self.last_click_effect = changed
        if not sentence:
            return
        print(f"[Click] effect: {'changed' if changed else 'no visible change'}")
        note = (f" Effect: {sentence}." if changed else
                f" Effect: {sentence} -- the click may have missed. Do not repeat it blindly: "
                "use ui_elements and click by number, or the app's keyboard shortcut.")
        self.last_result_text = (str(self.last_result_text or "") + note).strip()

    # ------------------------------------------------------------
    # UIA CLICK BY NUMBER
    # ------------------------------------------------------------
    # uia_click <number> from the latest control list: acts through the
    # element's UIA pattern (activate_element), then reports the effect.
    # A number from an older list, or a control that left the window, fails
    # clearly and asks for a fresh ui_elements.

    _CONTROL_REF_RX = re.compile(r"^\s*(?:\[\s*(\d+)\s*\]|#?(\d+))(?:\s+\S.*)?\s*$")

    def _control_ref(self, param):
        """The list number in a uia_click parameter ("8", "[8]", "#8", or a
        pasted list line), else None. A bare number counts only once a list
        was issued and when no listed control is literally named that
        number (a Calculator digit is a name, never a list number)."""
        m = self._CONTROL_REF_RX.match(str(param or ""))
        if not m:
            return None
        num = int(m.group(1) or m.group(2))
        bare = m.group(2) is not None and not str(param).strip().startswith("#")
        nums = self._control_numbers()
        if bare:
            if not nums.was_issued(num):
                return None
            live = getattr(nums, "_live", {})
            if any(r.get("name") == str(param).strip() for _h, r in live.values()):
                return None
        return num

    def _uia_click_number(self, num):
        from .click_handler import CONTROL_TYPES

        got = self._control_numbers().get(num)
        if got is None:
            self.last_uia_effect = None
            self.last_result_text = (
                f"[{num}] is from an older control list (the window changed since) -- call "
                "ui_elements for fresh numbers; nothing was clicked."
                if self._control_numbers().was_issued(num) else
                f"No control has number {num} -- call ui_elements for the list; nothing was "
                "clicked.")
            return False
        hwnd, row = got
        label = f'[{num}] {CONTROL_TYPES.get(row["type"], row["type"])} "{row["name"][:60]}"'
        try:
            el = row["el"]
            _ = el.CurrentName                         # raises when the control is gone
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_element_info import UIAElementInfo

            wrapper = UIAWrapper(UIAElementInfo(el))
        except Exception:
            self.last_uia_effect = None
            self.last_result_text = (f"{label} is no longer in the window -- call ui_elements "
                                     "for fresh numbers; nothing was clicked.")
            return False
        if not row.get("enabled", True):
            self.last_uia_effect = None
            self.last_result_text = f"{label} is disabled; nothing was clicked."
            return False
        before = self._click_probe(hwnd)
        activate = getattr(self.uia_nav, "activate_element", None)
        how = activate(wrapper) if callable(activate) else ""
        if not how:
            self.last_uia_effect = None
            self.last_result_text = f"Found {label} but the UIA action failed."
            return False
        try:
            rect = row.get("rect")
            if rect and not self._orb_pointer_active():      # the orb pointed instead
                from .caption import get_indicator

                get_indicator().show((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2,
                                     method="UIA", label=row["name"][:40])
        except Exception:
            pass
        route = {"invoke": "UIA Invoke", "toggle": "UIA Toggle", "select": "UIA Select",
                 "click": "a mouse click (no UIA pattern)"}.get(how, how)
        sentence, changed = self._click_effect(before)
        self.last_uia_effect = changed
        print(f"[UIA] {how} by number; effect "
              f"{'changed' if changed else 'unchanged' if changed is False else 'not checked'}")
        self.last_result_text = f"Clicked {label} via {route}" + (
            f"; effect: {sentence}." if changed else
            f"; but {sentence}, so it may not have taken effect -- check before relying on it."
            if changed is False else ".")
        return True

    def _act_uia_find_input(self, param=""):
        fw = self.live.snapshot.get("foreground_window", "")
        if not fw:
            return False
        element = self.uia_nav.find_text_input(fw)
        if element:
            return self.uia_nav.click_element(element)
        self.last_result_text = "No text input field found via UIA."
        return False

    def _act_inspect_file(self, param):
        """Read one file and register a fresh Workspace snapshot for editing.

        This is deliberately separate from ``run_command Get-Content`` so an
        overwrite can be tied to a hash of the exact file that was inspected.
        """
        path = self._resolve_path_placeholders(param or "")
        if not path or not os.path.isfile(path):
            self.last_result_text = f"inspect_file could not find a file at {path or '(empty path)'}"
            return False
        secret = is_secret_file(path)
        snapshot = self.workspace.inspect_file(
            path, return_chars=_RESULT_MEMORY_CHARS, withhold_content=secret
        )
        if not snapshot:
            self.last_result_text = f"inspect_file could not read {path}"
            return False

        header = (
            f"INSPECTED FILE: {snapshot['path']} ({snapshot['size']} bytes; "
            f"hash {snapshot['sha256'][:12]})."
        )
        if secret:
            self.last_result_text = (
                f"{header} CONTENT WITHHELD: secret-pattern file "
                f"(name, size and hash only). {_WITHHELD_NOTE}"
            )
            return True
        if snapshot.get("binary"):
            self.last_result_text = f"{header} Binary or non-UTF-8 file; content not shown."
            return True

        summary = []
        content = snapshot.get("content")
        try:
            data = json.loads(content) if content is not None else None
            if isinstance(data, dict):
                summary.append("JSON keys: " + ", ".join(sorted(map(str, data))[:20]))
                for key in ("scripts", "dependencies", "devDependencies"):
                    if isinstance(data.get(key), dict):
                        summary.append(
                            f"{key}: " + ", ".join(sorted(map(str, data[key]))[:12])
                        )
        except (TypeError, ValueError):
            pass
        if not content:
            summary.append("File is empty")
        self.last_result_text = f"{header} " + "; ".join(summary)
        if content:
            self.last_result_text += f"\nCONTENT:\n{content}"
            if snapshot.get("truncated"):
                self.last_result_text += f"\n[content cut at {_RESULT_MEMORY_CHARS} chars]"
        return True

    @staticmethod
    def _merge_json(existing, patch):
        """Recursively merge object properties without discarding siblings."""
        merged = dict(existing)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = Api._merge_json(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _act_patch_json(self, param):
        """Apply a JSON object patch to an inspected existing configuration."""
        try:
            if "|||" not in (param or ""):
                self.last_result_text = "patch_json needs format: path|||json object patch"
                return False
            raw_path, raw_patch = param.split("|||", 1)
            path = self._resolve_path_placeholders(raw_path)
            if not os.path.isfile(path):
                self.last_result_text = f"patch_json requires an existing JSON file: {path}"
                return False
            if not self.workspace.has_fresh_file_snapshot(path):
                self.last_result_text = (
                    f"REFUSED: {path} must be inspected with inspect_file immediately "
                    "before patching so existing configuration is preserved."
                )
                return False
            patch = json.loads(raw_patch)
            if not isinstance(patch, dict):
                self.last_result_text = "patch_json needs a JSON object patch"
                return False
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                self.last_result_text = "patch_json only supports top-level JSON objects"
                return False
            merged = self._merge_json(existing, patch)
            temp_path = path + ".mistai-tmp"
            with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(temp_path, path)
            self.workspace.inspect_file(path)
            self.last_result_text = (
                f"Merged JSON keys {', '.join(sorted(map(str, patch)))} into {path}; "
                "existing unrelated keys were preserved."
            )
            return True
        except Exception as e:
            self.last_result_text = f"patch_json failed: {e}"
            return False

    def _act_patch_file(self, param):
        """Replace one exact, unique fragment in an inspected text file.

        This is intentionally a small source-edit primitive, not an append
        operation or a general editor.  Requiring one unique anchor means a
        request such as adding a section can preserve the surrounding source
        instead of treating ``write_file`` as an unsafe append shortcut.
        """
        try:
            pieces = (param or "").split("|||", 2)
            if len(pieces) != 3:
                self.last_result_text = (
                    "patch_file needs format: path|||exact text to replace|||replacement text"
                )
                return False
            raw_path, find_text, replacement = pieces
            path = self._resolve_path_placeholders(raw_path)
            if not os.path.isfile(path):
                self.last_result_text = f"patch_file requires an existing file: {path}"
                return False
            if not find_text:
                self.last_result_text = "patch_file needs a non-empty exact text anchor"
                return False
            if not self.workspace.has_fresh_file_snapshot(path):
                self.last_result_text = (
                    f"REFUSED: {path} must be inspected with inspect_file immediately "
                    "before patching so existing source is preserved."
                )
                return False
            with open(path, "r", encoding="utf-8", newline="") as f:
                existing = f.read()
            matches = existing.count(find_text)
            if matches != 1:
                self.last_result_text = (
                    "patch_file requires its exact anchor to occur exactly once "
                    f"(found {matches}); source was not changed."
                )
                return False
            updated = existing.replace(find_text, replacement, 1)
            temp_path = path + ".mistai-tmp"
            with open(temp_path, "w", encoding="utf-8", newline="") as f:
                f.write(updated)
            os.replace(temp_path, path)
            self.workspace.inspect_file(path)
            self.last_result_text = (
                f"Patched one exact source fragment in {path}; surrounding content was preserved."
            )
            return True
        except Exception as e:
            self.last_result_text = f"patch_file failed: {e}"
            return False

    def _act_write_file(self, param):
        try:
            if "|||" not in param:
                self.last_result_text = "write_file needs format: path|||content"
                return False
            path, content = param.split("|||", 1)
            path = self._resolve_path_placeholders(path)
            if os.path.isfile(path) and not self.workspace.has_fresh_file_snapshot(path):
                self.last_result_text = (
                    f"REFUSED: {path} already exists. Run inspect_file on it first; "
                    "a full write replaces all existing content. Use patch_json for "
                    "a JSON/configuration property update."
                )
                return False
            # Defensive: if the content contains literal backslash-n instead of
            # a real newline (can happen if escaping gets mangled somewhere
            # upstream in the JSON/string pipeline), convert it back.
            if "\\n" in content and "\n" not in content:
                content = content.replace("\\n", "\n")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self.workspace.inspect_file(path)
            self.last_result_text = f"Wrote {len(content)} chars to {path}"
            return True
        except Exception as e:
            self.last_result_text = f"write_file failed: {e}"
            return False

    # Destination entries a move_files / copy_files result lists by name.
    TRANSFER_LIST_MAX = 60

    def _act_move_files(self, param):
        return self._transfer_files(param, copy=False)

    def _act_copy_files(self, param):
        return self._transfer_files(param, copy=True)

    def _transfer_path(self, raw):
        """A move/copy path: quotes dropped, placeholders and %VARS%/~ expanded."""
        path = str(raw or "").strip().strip("\"'").strip()
        if not path:
            return ""
        path = os.path.expandvars(os.path.expanduser(self._resolve_path_placeholders(path)))
        return os.path.normpath(path)

    def _folder_listing(self, folder):
        """'N item(s): name (size bytes), sub\\ (folder), ...': names and sizes only,
        never content (secret-pattern files included), capped at TRANSFER_LIST_MAX."""
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as e:
            return f"(could not be listed: {type(e).__name__})"
        shown = []
        for n in names[:self.TRANSFER_LIST_MAX]:
            full = os.path.join(folder, n)
            try:
                shown.append(f"{n}\\ (folder)" if os.path.isdir(full)
                             else f"{n} ({os.path.getsize(full)} bytes)")
            except OSError:
                shown.append(n)
        more = len(names) - len(shown)
        return (f"{len(names)} item(s): " + (", ".join(shown) or "nothing")
                + (f", and {more} more" if more > 0 else ""))

    def _transfer_files(self, param, copy):
        """move_files / copy_files: ``source|||destination``. The source is a file,
        a folder or a glob; the destination is always a folder, created if missing
        and refused if it exists as a file. Never overwrites: if any name already
        exists in the destination (or two sources share a name) nothing changes and
        the clashes are listed. The result lists what moved or copied and the
        destination's contents (names and sizes only)."""
        import glob

        name, verb, done_verb = (("copy_files", "copied", "Copied") if copy
                                 else ("move_files", "moved", "Moved"))
        if "|||" not in str(param or ""):
            self.last_result_text = f"{name} needs format: source|||destination folder"
            return False
        raw_src, raw_dst = str(param).split("|||", 1)
        src, dst = self._transfer_path(raw_src), self._transfer_path(raw_dst)
        if not (src and dst and os.path.isabs(src) and os.path.isabs(dst)):
            self.last_result_text = (
                f"{name} needs a full path (or a {{desktop}}-style placeholder) for both the "
                "source and the destination folder; nothing was changed.")
            return False
        if os.path.isfile(dst):
            self.last_result_text = (
                f"REFUSED: {dst} exists as a FILE, not a folder; nothing was {verb}. Choose "
                "another destination folder or ask the user what to do with that file.")
            return False
        found = sorted(glob.glob(src)) if any(c in src for c in "*?[") else (
            [src] if os.path.lexists(src) else [])
        dst_key = os.path.normcase(dst)
        # A glob such as mess\* can match the destination folder itself.
        sources = [s for s in found if os.path.normcase(os.path.normpath(s)) != dst_key]
        if not sources:
            self.last_result_text = f"Nothing matches {src}; nothing was {verb}."
            return False
        for s in sources:
            key = os.path.normcase(os.path.normpath(s))
            if os.path.isdir(s) and dst_key.startswith(key + os.sep):
                self.last_result_text = (
                    f"REFUSED: {dst} is inside {s}; a folder cannot go into itself. "
                    f"Nothing was {verb}.")
                return False
        try:
            existing = {os.path.normcase(n) for n in os.listdir(dst)} if os.path.isdir(dst) else set()
        except OSError as e:
            self.last_result_text = f"{name} could not list {dst} ({type(e).__name__}); nothing was {verb}."
            return False
        seen, clashes = set(), []
        for s in sources:
            key = os.path.normcase(os.path.basename(s))
            if key in existing or key in seen:
                clashes.append(os.path.basename(s))
            seen.add(key)
        if clashes:
            self.last_result_text = (
                f"REFUSED: nothing was {verb}, because these names already exist in {dst} "
                f"(or two sources share a name): {', '.join(clashes)}. Nothing is ever "
                "overwritten: ask the user whether to rename, skip or replace them. "
                f"{dst} contains {self._folder_listing(dst)}")
            return False
        try:
            os.makedirs(dst, exist_ok=True)
        except OSError as e:
            self.last_result_text = (f"{name} could not create the folder {dst} "
                                     f"({type(e).__name__}); nothing was {verb}.")
            return False
        done, failed = [], []
        for s in sources:
            base = os.path.basename(s)
            target = os.path.join(dst, base)
            try:
                if os.path.lexists(target):       # appeared since the check: never overwrite
                    raise FileExistsError(target)
                if not copy:
                    shutil.move(s, target)
                elif os.path.isdir(s):
                    shutil.copytree(s, target)
                else:
                    shutil.copy2(s, target)
                done.append(base)
            except Exception as e:
                failed.append(f"{base} ({type(e).__name__})")
        text = (f"{done_verb} {len(done)} of {len(sources)} item(s) into {dst}: "
                f"{', '.join(done) or 'none'}.")
        if done and copy:
            text += f" The originals remain in {src}; nothing was moved or removed."
        elif done:
            text += f" The moved item(s) are no longer in {src}."
        if failed:
            text += f" NOT {verb.upper()} (still in {src}): {', '.join(failed)}."
        self.last_result_text = f"{text} {dst} now contains {self._folder_listing(dst)}"
        return not failed

    def _with_app_note(self, app, ok):
        """open_app / switch_app: on success, the app's APP_NOTES tip is
        appended to the result text, once per phase per app (the agent resets
        ``_app_notes_given`` at phase start). Returns ok unchanged; nothing
        else about the result changes. Logs a fixed label only."""
        if not ok:
            return ok
        key, note = app_note(self._normalize_app_name(app))
        given = self.__dict__.setdefault("_app_notes_given", set())
        if note and key not in given:
            given.add(key)
            text = str(getattr(self, "last_result_text", "") or "").rstrip()
            self.last_result_text = f"{text} {note}" if text else note
            print("[AppNotes] note added")
        return ok

    def _track_selection(self, action):
        """A select-all (hotkey ctrl+a) is remembered as the window it went to
        until the next action that is not a read (perception/info): the
        action right after it sees it in ``_selection_before`` (type_text then
        says the selection was replaced), anything later does not."""
        mark = self.__dict__.pop("_select_all_mark", None)
        self._selection_before = mark
        try:
            cats = set(self.tools.get(action).categories)
        except Exception:
            cats = set()
        if mark is not None and cats & {"perception", "info"}:
            self._select_all_mark = mark          # a read changes no selection

    @staticmethod
    def _foreground_hwnd():
        try:
            import win32gui

            return win32gui.GetForegroundWindow() or None
        except Exception:
            return None

    def _act_type_text(self, param):
        """Type into whatever app was last opened via open_app. Focuses it
        first, then clears the field and pastes via clipboard instead of
        typing character-by-character -- faster, and avoids per-character
        edge cases (special chars, autocomplete interference, dropped
        keystrokes on slow-rendering apps like Electron chat clients)."""
        report, on_target = self._ensure_input_target("text")
        if not on_target:
            self.last_result_text = report
            print(f"[TypeText] {self._input_report_for_log(report, False)}")
            return False
        # Replace the field only in an app this phase opened/switched to;
        # anywhere else, never select-all + delete someone's content.
        replace = bool(getattr(self, "_last_opened_app", None))
        selected = getattr(self, "_selection_before", None)
        if not replace:
            if selected is not None and selected == self._foreground_hwnd():
                # hotkey ctrl+a went to this same window right before: the
                # paste replaces that selection.
                report += " It replaced the selected text (the ctrl+a selection just before)."
            else:
                report += (" No app was opened or switched to, so the text was appended; "
                           "nothing was cleared.")
        prev_clipboard = None
        clipboard_staged = False
        # One lock across read, stage, paste and restore: MistAI's own watcher
        # and tools never touch the clipboard in the middle of a paste.
        breaker = self.__dict__.setdefault("_clip_breaker", clip.Breaker())
        clip.lock.acquire()
        try:
            # Breaker open (the clipboard failed twice within a minute): short
            # one-line text goes straight to key events, with the same guards
            # (input target above, can_send_as_keys); nothing is staged.
            skip_clipboard = clip.can_send_as_keys(param) and breaker.is_open()
            staged = False
            if not skip_clipboard:
                # Stage text on the clipboard BEFORE changing the target field.
                # The old order cleared a draft with Ctrl+A/Delete and only then
                # discovered that another app had locked the clipboard.
                prev_clipboard = self._clipboard_text_full()
                if prev_clipboard is None:
                    prev_clipboard = self.get_clipboard()
                    if prev_clipboard == "Clipboard is empty":
                        prev_clipboard = ""
                # Another app may hold the clipboard for a moment: retry after
                # short jittered waits (1.5 s at most).
                staged = self.set_clipboard(param)
                for d in clip.retry_delays(self.CLIPBOARD_BUDGET_S):
                    if staged:
                        break
                    time.sleep(d)
                    staged = self.set_clipboard(param)
                breaker.record(staged)
            by_keys = False
            if not staged:
                if not clip.can_send_as_keys(param):
                    self.last_result_text = (
                        "Could not set the clipboard (locked by another app); "
                        "the existing field was left unchanged."
                    )
                    return False
                by_keys = True      # short one-line text: type it as key events
            clipboard_staged = staged

            time.sleep(0.2)
            if replace:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyautogui.press("delete")
                time.sleep(0.2)
            if by_keys:
                if not clip.send_unicode(param):
                    self.last_result_text = "Keyboard fallback failed; text may be incomplete."
                    return False
                report += " The clipboard was busy, so the text was entered with keyboard events."
            else:
                pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
            self.last_result_text = report
            print(f"[TypeText] {self._input_report_for_log(report, True)}"
                  + (" via=keys" if by_keys else ""))
            return True
        except Exception as e:
            print(f"[TypeText] Error: {e if verbose_logs() else type(e).__name__}")
            return False
        finally:
            # Preserve the user's clipboard even if UI input itself fails.
            if clipboard_staged:
                self.set_clipboard(prev_clipboard)
            clip.lock.release()

    def _get_app_search_hotkey(self, app):
        """Look up (and permanently cache) an app's quick-switch/search
        hotkey by asking the LLM directly -- no hardcoded table. The model
        already knows this stuff (same brain as the web version), so we
        just ask once per app and remember the answer forever."""
        app_lower = app.lower().strip()
        cached = (self.memory.get_profile(f"hotkey_{app_lower}", None)
                  or DEFAULT_HOTKEYS.get(f"hotkey_{app_lower}"))
        if cached:
            return cached

        hotkey = self._ask_llm_for_hotkey(app_lower)
        if hotkey:
            self.memory.set_profile(f"hotkey_{app_lower}", hotkey)
            print(f"[Hotkey] Learned '{app_lower}' -> {hotkey}")
            return hotkey

        print(f"[Hotkey] Could not learn hotkey for '{app_lower}', using default")
        return self._DEFAULT_HOTKEY

    def _ask_llm_for_hotkey(self, app_lower):
        """One-shot direct API call asking the model for a specific app's
        quick-switch hotkey. Validated against a strict format before being
        trusted/cached, so a garbage answer never gets saved."""
        try:
            headers = self.api_manager.get_headers()
            url = self.api_manager.get_api_url()
            prompt = (
                f"What is the keyboard shortcut on Windows to open the quick "
                f"switcher / jump-to-conversation search in the desktop app "
                f"'{app_lower}'? Reply with ONLY the shortcut in the format "
                f"'ctrl+k', nothing else. If you genuinely don't know, reply "
                f"exactly 'unknown'."
            )
            model = self.current_model()
            # model_chain: the model, then the next one once after an HTTP error.
            chain = model_chain(model)
            for attempt, attempt_model in enumerate(chain):
                r = requests.post(
                    url,
                    json={"message": prompt, "model": attempt_model},
                    headers=headers,
                    timeout=10,
                )
                if r.ok:
                    break
                print(f"[Hotkey] {describe_api_error(r)}")
                if attempt + 1 < len(chain):
                    print(model_failover_line("Hotkey", attempt_model, r.status_code,
                                              chain[attempt + 1]))
            if not r.ok:
                return None
            raw = r.json().get("response", "").strip().lower()
            if "unknown" in raw:
                return None
            if re.match(r"^((ctrl|alt|shift)\+){1,2}[a-z0-9]$", raw):
                return raw
            return None
        except Exception as e:
            print(f"[Hotkey] LLM lookup failed: {e}")
            return None

    def _act_quick_message(self, param):
        """Send a message using the target app's own quick-switch/search
        hotkey instead of hunting for the contact via OCR/UIA -- faster and
        avoids clicking the wrong thing (memes, embeds, unrelated matches).
        parameter format: "app|recipient|message"

        Every step is now checked, because this is the one action that can
        do real social damage: set_clipboard() returns False on the (common)
        Windows OpenClipboard failure, and the old version ignored that and
        pasted WHATEVER was already on the clipboard into the recipient box,
        pressed Enter, and sent. It also never confirmed which conversation
        it landed in, despite the system prompt requiring that for the
        manual flow."""
        parts = param.split("|", 2)
        if len(parts) < 3:
            self.last_result_text = "quick_message needs format: app|recipient|message"
            return False
        app, recipient, message = (
            parts[0].strip().lower(),
            parts[1].strip(),
            parts[2].strip(),
        )
        if not recipient or not message:
            self.last_result_text = (
                "quick_message needs a non-empty recipient AND message; "
                "use ask_user to get whichever is missing."
            )
            return False

        self._last_send_exact = False
        # Sending to the wrong person is worse than failing to send. OCR/UIA
        # is unreliable in Electron chat clients, so this fast path requires
        # configured vision and a recipient check at all three stages
        # (switcher result, header before typing, header before Enter).
        if not self.vision.ready:
            self.last_result_text = (
                "quick_message refused: recipient verification requires vision, "
                "which is not configured. Nothing was typed or sent."
            )
            return False

        if self.is_app_running(app):
            if not self.switch_to_app(app):
                if not self._act_open_app(app):
                    return False
        else:
            if not self._act_open_app(app):
                return False
        time.sleep(0.3)

        hotkey = self._get_app_search_hotkey(app)
        pyautogui.hotkey(*hotkey.split("+"))
        time.sleep(0.5)

        # -- Recipient: never paste unverified clipboard content ------------
        if not self.set_clipboard(recipient):
            self.last_result_text = (
                f"Could not put '{recipient}' on the clipboard (clipboard was "
                f"locked by another app). Nothing was typed or sent -- retry, "
                f"or use the manual messaging flow."
            )
            pyautogui.press("escape")
            return False
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)

        # Confirm the quick-switcher actually resolved to this person BEFORE
        # committing. Vision is asked only what is shown (never the expected
        # name) and its answer is compared here (config.recipient_match):
        # prose, prefixes (Ann/Anna) or an uncertain answer don't match.
        # Vision only: the appended accessibility-tree text of
        # read_screen_text must not be in these answers.
        found, exact_switcher, seen = self._qm_read_name(
            QM_SWITCHER_QUESTION, recipient, "switcher")
        if found != "match":
            pyautogui.press("escape")
            self.last_result_text = (
                f"The quick-switcher's top result could not be verified as "
                f"'{recipient}' (saw: {seen[:120]}). Nothing was sent. Use "
                f"ask_user to confirm the exact name, or use the manual flow."
            )
            return False

        pyautogui.press("enter")
        time.sleep(0.8)

        # -- Verify the open conversation before typing the message ---------
        found, exact_header, header = self._qm_read_name(
            QM_HEADER_QUESTION, recipient, "header")
        if found != "match":
            self.last_result_text = (
                f"Opened a conversation, but its header could not be verified as "
                f"'{recipient}' (saw: {header[:60]}). NOTHING was typed or sent. "
                "Verify the exact contact name with ask_user before retrying."
            )
            return False
        # Both checks named exactly this person (a close match is not exact).
        self._last_send_exact = bool(exact_switcher and exact_header)

        # Ask before sending, "when_unsure": AgentLoop deferred the question
        # to here, where the exactness of the match is known. It returns
        # (proceed, asked); nothing is typed unless proceed.
        hook = getattr(self, "_send_confirm", None)
        if callable(hook):
            proceed, asked = hook(self._last_send_exact)
            if not proceed:
                self.last_result_text = "Not confirmed. Nothing was typed or sent."
                return False
            if asked:
                # The Yes/No prompt raised MistAI: give the chat the keyboard back.
                if not (self.switch_to_app(app) and self._foreground_process_matches(app)):
                    self.last_result_text = (
                        "After the confirmation the app could not be brought back "
                        "to the front. Nothing was typed or sent."
                    )
                    return False
                time.sleep(0.3)

        if not self.set_clipboard(message):
            self.last_result_text = (
                f"Reached {recipient}'s conversation but could not put the "
                f"message on the clipboard. Nothing was sent."
            )
            return False
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.2)

        # -- Final check, immediately before the one keystroke that sends ----
        # One retry when the header can't be read; unreadable twice or a
        # different name -> Enter is never pressed.
        found, _, _ = self._qm_read_name(QM_HEADER_QUESTION, recipient, "final",
                                         retry_unreadable=True)
        if found != "match":
            print("[QuickMessage] final check failed; Enter not pressed")
            self.last_result_text = (
                f"{QM_UNCONFIRMED_REASON} The message may still be in the chat's "
                "text box, unsent."
            )
            return False
        pyautogui.press("enter")

        self.last_result_text = f"Sent to '{recipient}' in {app}"
        return True

    def _qm_read_name(self, question, recipient, label, retry_unreadable=False):
        """(found, exact, seen) for one quick_message recipient check: found is
        "match", "mismatch" or "unreadable" (vision failed, nothing readable,
        or NOT VISIBLE ON SCREEN). With retry_unreadable, an unreadable answer
        is read once more. Logs the label and verdict only, never a name."""
        attempts = 2 if retry_unreadable else 1
        seen, found, exact = "", "unreadable", False
        for attempt in range(attempts):
            mark = getattr(getattr(self, "screen", None), "mark_action", None)
            if callable(mark):
                mark()      # our own keystrokes changed the screen: no older frame
            seen, ok = self._vision_read(question)
            seen = str(seen or "")
            if not ok or not seen.strip() or NOT_VISIBLE_ON_SCREEN.lower() in seen.lower():
                found, exact = "unreadable", False
                print(f"[QuickMessage] {label} check: unreadable"
                      f"{' (retry)' if attempt else ''}")
                continue
            matched, exact = recipient_match(seen, recipient)
            found = "match" if matched else "mismatch"
            print(f"[QuickMessage] {label} check: "
                  f"{'exact' if exact else 'close' if matched else 'no match'}")
            break
        return found, exact, seen

    def _act_type_search(self, param):
        time.sleep(0.3)
        pyautogui.write(param, interval=0.05)
        time.sleep(0.2)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True

    def _act_click_coords(self, param):
        try:
            parts = param.replace(" ", "").split(",")
            if len(parts) >= 2:
                x, y = int(parts[0]), int(parts[1])
                if CoordinateValidator.is_valid(x, y, *pyautogui.size()):
                    self.click_handler.last_query = ""
                    refusal = self.click_handler.outside_target(x, y, "coords")
                    if refusal:
                        self.last_result_text = refusal
                        return False
                    self.last_click_effect = None
                    before = self._click_probe()
                    pointed = self._orb_point((x, y))    # the orb flies beside it first
                    pyautogui.moveTo(x, y, duration=0.2)
                    time.sleep(0.08)
                    pyautogui.click()
                    if not pointed:
                        try:
                            from .caption import get_indicator

                            get_indicator().show(x, y, method="coords")   # after the click
                        except Exception:
                            pass  # debug-only; never block the real click
                    if before:
                        self._report_click_effect(before)
                    else:
                        time.sleep(0.3)
                    return True
            return False
        except ValueError:
            print(f"[ClickCoords] Invalid format: {param}")
            return False

    def _act_click_nth(self, param):
        parts = param.split("|", 1)
        try:
            n = int(parts[0].strip())
        except Exception:
            n = 1
        item_type = parts[1].strip() if len(parts) > 1 else "item"
        return self._handler_click(self.click_handler.click_nth_element, item_type, n)

    def _handler_click(self, click, *args):
        """Run a ClickHandler click; a target-window refusal becomes the result
        text. A click that ran on the phase's app reports its effect (and
        last_click_effect False lowers the executor's confidence)."""
        self.last_click_effect = None
        before = self._click_probe()
        ok = click(*args)
        refusal = getattr(self.click_handler, "last_refusal", "")
        if not ok and refusal:
            self.last_result_text = refusal
        if ok:
            self._report_click_effect(before)
        return ok

    def _click_target_rect(self):
        """((l, t, r, b), title) of the window clicks must land in: the app
        this phase opened or switched to, while it is in front. None = no
        check (no known target, so taskbar/Start clicks stay possible; or the
        target is not in front, which type/key input already refuses)."""
        target = getattr(self, "_last_opened_app", None)
        if not target:
            return None
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not self._window_belongs_to(hwnd, target):
            print(f"[Click] {target} is not in front; window guard not applied")
            return None
        return win32gui.GetWindowRect(hwnd), win32gui.GetWindowText(hwnd)

    # Shells never receive input without an explicit target: a stray paste
    # plus Enter there runs a command (seen: text landing in an Administrator
    # PowerShell).
    _TERMINAL_PROCESSES = frozenset(
        {"windowsterminal", "openconsole", "conhost", "cmd", "powershell", "pwsh"}
    )

    def enter_target_is_safe(self):
        """May press_key('enter') skip confirmation? Only for the app this
        phase opened or switched to, while it is in front, and only when it
        is not a browser, messaging, mail or terminal app (ENTER_CONFIRM_APPS)
        nor MistAI itself. Anything unknown asks."""
        from .executor import BROWSER_HINTS

        target = getattr(self, "_last_opened_app", None)
        if not target:
            return False
        try:
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd or not self._window_belongs_to(hwnd, target):
                return False
            if win32gui.GetClassName(hwnd) in ("ConsoleWindowClass",
                                               "CASCADIA_HOSTING_WINDOW_CLASS"):
                return False
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            name = psutil.Process(pid).name()
            title = win32gui.GetWindowText(hwnd).lower()
        except Exception:
            return False
        if is_self_process(name, pid):
            return False
        if any(process_matches(app, name) or process_matches(app, target)
               or process_matches(target, app) for app in ENTER_CONFIRM_APPS):
            return False
        return not any(h in title for h in BROWSER_HINTS)

    def _foreground_terminal(self):
        """Title of the foreground window if it is a terminal/shell or
        MistAI's own console, else None."""
        try:
            import ctypes

            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            title = win32gui.GetWindowText(hwnd) or "(untitled console)"
            if hwnd == ctypes.windll.kernel32.GetConsoleWindow():
                return title
            if win32gui.GetClassName(hwnd) in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
                return title
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if normalize_process_name(psutil.Process(pid).name()) in self._TERMINAL_PROCESSES:
                return title
        except Exception:
            return None
        return None

    # ------------------------------------------------------------
    # DESTRUCTIVE GUARD HOOKS
    # ------------------------------------------------------------
    # Executor.run calls _guard_prepare, then core/guard.py reads the UI
    # through _guard_ui_context. Keyboard input goes to the phase's app, so
    # that app is brought to the front first (the same single switch
    # _ensure_input_target makes, which then finds it in front): the guard
    # reads the window that will get the keys.

    def _guard_prepare(self, action):
        from .guard import KEY_ACTIONS

        target = getattr(self, "_last_opened_app", None)
        if action in KEY_ACTIONS and target and not self._foreground_process_matches(target):
            self.switch_to_app(target)
            time.sleep(0.4)

    def _guard_ui_context(self):
        from .guard import read_ui_context

        return read_ui_context()

    def _ensure_input_target(self, what):
        """(report, on_target) for input about to be sent. When this phase
        opened/switched to an app that is not in front, try one switch back;
        if it is still not in front, on_target is False and the caller must
        send NOTHING. No known target (no open_app/switch_app this phase):
        input goes to whatever is in front -- except a terminal/shell."""
        target = getattr(self, "_last_opened_app", None)
        if not target:
            if self._orb_in_front():
                return (
                    f"NOT SENT: the {what} was not sent because the window in front is "
                    "MistAI's own floating orb and no app was opened or switched to. "
                    "Use open_app / switch_app to the intended app first.",
                    False,
                )
            shell = self._foreground_terminal()
            if shell:
                return (
                    f"NOT SENT: the {what} was not sent because the window in front is a "
                    f"terminal ('{shell[:60]}') and no app was opened or switched to. "
                    "Nothing was typed; use run_command for shell work, or open_app / "
                    "switch_app to the intended app first.",
                    False,
                )
        if target and not self._foreground_process_matches(target):
            self.switch_to_app(target)
            time.sleep(0.4)
        return self._input_target_report(what)

    def _input_target_report(self, what):
        """(report, on_target): which window is in front -- i.e. about to
        receive ``what`` -- and NOT SENT when that is not the app the agent
        last opened or switched to (the switch may have silently failed)."""
        target = getattr(self, "_last_opened_app", None)
        try:
            import win32gui

            title = win32gui.GetWindowText(win32gui.GetForegroundWindow())
        except Exception:
            title = self.live.snapshot.get("foreground_window", "")
        title = (title or "(untitled window)")[:60]
        if target and not self._foreground_process_matches(target):
            return (
                f"NOT SENT: the {what} was not sent because {target} is not in front "
                f"('{title}' is). Nothing was typed; bring {target} to the front "
                f"(switch_app) before retrying.",
                False,
            )
        return f"The {what} went to '{title}'.", True

    @staticmethod
    def _input_report_for_log(report, sent):
        """The console's view of an input report: it names the window that got
        the input (a title can hold a contact's name), so the full text prints
        only under verbose_logs(). The model's observation keeps it."""
        if verbose_logs():
            return report
        return "typed into the input target" if sent else "NOT SENT (the input target is not in front)"

    def _act_press_key(self, param):
        # pyautogui.press ignores a name it does not know ("ctrl+s") and sends
        # nothing, so the step used to report success with no key pressed.
        # Refuse it instead; a combo is NOT forwarded to hotkey, whose
        # confirmation list press_key's gate does not check.
        key = str(param or "").strip()
        known = getattr(pyautogui, "KEYBOARD_KEYS", None)
        if known is not None and len(key) > 1 and key.lower() not in known:
            combo = len(key) > 1 and "+" in key
            self.last_result_text = (
                f"NOT SENT: press_key presses ONE key; '{key[:30]}' "
                + (f"is a key combination. Use hotkey('{key[:30]}')." if combo
                   else "is not a key name (e.g. enter, tab, esc, f5, a).")
                + " Nothing was pressed.")
            return False
        report, on_target = self._ensure_input_target(f"key '{param}'")
        self.last_result_text = report
        if not on_target:
            return False
        pyautogui.press(param)
        return True

    def _act_hotkey(self, param):
        try:
            keys = [k.strip().lower() for k in param.split("+") if k.strip()]
            if not keys:
                return False
            report, on_target = self._ensure_input_target(f"hotkey '{param}'")
            self.last_result_text = report
            if not on_target:
                return False
            pyautogui.hotkey(*keys)
            time.sleep(0.3)
            if sorted(keys) == ["a", "ctrl"]:
                self._select_all_mark = self._foreground_hwnd()
            return True
        except Exception as e:
            print(f"[Hotkey] Error with {param}: {e if verbose_logs() else type(e).__name__}")
            return False

    def _act_scroll(self, param):
        try:
            parts = param.lower().split()
            direction = parts[0] if parts else "down"
            try:
                amount = int(parts[1]) if len(parts) > 1 else 5
            except ValueError:
                amount = 5
            scroll_amount = amount if direction == "up" else -amount
            pyautogui.scroll(scroll_amount)
            time.sleep(0.8)
            return True
        except Exception as e:
            print(f"[Scroll] Error: {e}")
            return False

    def _act_organize_desktop(self, param):
        import shutil

        desktop = self._get_desktop_path()
        if not os.path.isdir(desktop):
            self.last_result_text = f"Could not find Desktop folder at {desktop}"
            self.speak("Could not find your Desktop folder.")
            return False

        categories = {
            "Images": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"],
            "Documents": [".pdf", ".docx", ".doc", ".txt", ".xlsx", ".pptx"],
            "Archives": [".zip", ".rar", ".7z"],
            "Installers": [".exe", ".msi"],
            "Shortcuts": [".lnk", ".url"],
        }
        moved, skipped = {}, []

        try:
            for entry in os.listdir(desktop):
                full_path = os.path.join(desktop, entry)
                if os.path.isdir(full_path):
                    continue
                ext = os.path.splitext(entry)[1].lower()
                target_folder = next(
                    (cat for cat, exts in categories.items() if ext in exts), None
                )
                if not target_folder:
                    skipped.append(entry)
                    continue
                dest_dir = os.path.join(desktop, target_folder)
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    shutil.move(full_path, os.path.join(dest_dir, entry))
                    moved.setdefault(target_folder, []).append(entry)
                except Exception as e:
                    print(f"[Organize] Could not move {log_text(entry)}: "
                          f"{e if verbose_logs() else type(e).__name__}")
                    skipped.append(entry)
        except Exception as e:
            self.last_result_text = f"organize_desktop crashed: {e}"
            print(f"[Organize] Fatal error: {e if verbose_logs() else type(e).__name__}")
            return False

        summary = (
            ", ".join(f"{len(v)} into {k}" for k, v in moved.items())
            or "nothing to organize"
        )
        self.last_result_text = f"{summary}. {len(skipped)} items left as-is."
        self.speak(f"Desktop organized: {summary}. {len(skipped)} items left as-is.")
        return True

    def _get_desktop_path(self):
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            path, _ = winreg.QueryValueEx(key, "Desktop")
            path = os.path.expandvars(path)
            if os.path.isdir(path):
                return path
        except Exception:
            pass
        return os.path.join(os.path.expanduser("~"), "Desktop")

    def _resolve_path_placeholders(self, path):
        path = path.strip()
        if "{desktop}" in path:
            path = path.replace("{desktop}", self._get_desktop_path())
        if "{home}" in path:
            path = path.replace("{home}", os.path.expanduser("~"))
        if "{documents}" in path:
            path = path.replace("{documents}", self._shell_folder("Personal", "Documents"))
        if "{downloads}" in path:
            path = path.replace(
                "{downloads}",
                self._shell_folder("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
            )
        return path

    @staticmethod
    def _shell_folder(value_name, fallback_subdir):
        """A user shell folder from the registry (honours OneDrive/moved
        folders), falling back to ~\\<fallback_subdir>."""
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                folder, _ = winreg.QueryValueEx(key, value_name)
            if folder:
                return os.path.expandvars(folder)
        except Exception:
            pass
        return os.path.join(os.path.expanduser("~"), fallback_subdir)

    def _act_run_command(self, param):
        param = self._resolve_path_placeholders(param)
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", param],
                capture_output=True,
                text=True,
                timeout=15,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            # PowerShell exits 1 when an error was suppressed (-ErrorAction
            # SilentlyContinue): with no output at all that is "nothing found",
            # not a failure. Any other code, or any output, is a real result.
            no_results = result.returncode == 1 and not stdout and not stderr
            success = result.returncode == 0 or no_results
            warning_text = "\n".join(part for part in (stdout, stderr) if part)
            has_warnings = bool(re.search(r"(?im)^\s*(?:warning\b|warn\b)", warning_text))
            status = (
                "COMMAND RETURNED NO RESULTS"
                if no_results
                else "COMMAND COMPLETED SUCCESSFULLY"
                if success and not has_warnings
                else "COMMAND COMPLETED WITH WARNINGS"
                if success
                else "COMMAND FAILED"
            )
            self.last_command_result = {
                "command": param,
                "completed": True,
                "timed_out": False,
                "success": success,
                "exit_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "has_warnings": has_warnings,
                "no_results": no_results,
                "status": status,
            }
            details = [
                f"{status} (exit code {result.returncode}, no stdout, no stderr)."
                if no_results
                else f"{status} (exit code {result.returncode})."
            ]
            withheld = secret_output_reason(param, f"{stdout}\n{stderr}")
            if withheld:
                self.last_command_result["stdout"] = ""
                self.last_command_result["stderr"] = ""
                details.append(f"OUTPUT WITHHELD: {withheld}. {_WITHHELD_NOTE}")
            else:
                if stdout:
                    details.append(f"STDOUT: {stdout}")
                if stderr:
                    details.append(f"STDERR: {stderr}")
                if not stdout and not stderr and not no_results:
                    details.append("OUTPUT: (no output)")
            self.last_result_text = "\n".join(details)[:_RESULT_MEMORY_CHARS]
            return success
        except subprocess.TimeoutExpired as e:
            def _text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")

            stdout = _text(e.stdout).strip()
            stderr = _text(e.stderr).strip()
            self.last_command_result = {
                "command": param,
                "completed": False,
                "timed_out": True,
                "success": False,
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "has_warnings": False,
                "status": "COMMAND TIMED OUT",
            }
            details = ["COMMAND TIMED OUT (15 seconds)."]
            withheld = secret_output_reason(param, f"{stdout}\n{stderr}")
            if withheld:
                self.last_command_result["stdout"] = ""
                self.last_command_result["stderr"] = ""
                details.append(f"OUTPUT WITHHELD: {withheld}. {_WITHHELD_NOTE}")
            else:
                if stdout:
                    details.append(f"STDOUT: {stdout}")
                if stderr:
                    details.append(f"STDERR: {stderr}")
            self.last_result_text = "\n".join(details)[:_RESULT_MEMORY_CHARS]
            return False
        except Exception as e:
            self.last_command_result = {
                "command": param,
                "completed": False,
                "timed_out": False,
                "success": False,
                "exit_code": None,
                "stdout": "",
                "stderr": str(e),
                "has_warnings": False,
                "status": "COMMAND FAILED",
            }
            self.last_result_text = f"COMMAND FAILED: {e}"
            return False

    def _act_open_project_in_vscode(self, path):
        try:
            path = self._resolve_path_placeholders(path)
            if not os.path.isdir(path):
                self.last_result_text = (
                    f"REFUSED: open_project_in_vscode requires an existing folder: {path}"
                )
                return False
            if not self._is_trusted_project_root(path):
                self.last_result_text = (
                    "REFUSED: open_project_in_vscode requires a project folder "
                    "explicitly supplied by the user or established from reliable "
                    "VS Code evidence."
                )
                return False
            code_path = self._resolve_app_exe("code") or "code"
            before_count = len(
                [
                    1
                    for p in psutil.process_iter(["name"])
                    if "code" in (p.info["name"] or "").lower()
                ]
            )
            subprocess.Popen([code_path, "-n", path])
            time.sleep(2.0)
            after_count = len(
                [
                    1
                    for p in psutil.process_iter(["name"])
                    if "code" in (p.info["name"] or "").lower()
                ]
            )
            if after_count <= before_count:
                self.last_result_text = f"VS Code may have reused an existing window instead of opening a new one for {path} -- check VS Code's 'window.openFoldersInNewWindow' setting."
                return False
            self.last_result_text = f"Opened new VS Code window for {path}"
            return True
        except Exception as e:
            self.last_result_text = f"Could not open VS Code: {e}"
            return False

    def start_listening(self):
        with self._listening_lock:
            if self.is_listening:
                return {"success": False, "error": "Already listening"}
            self.is_listening = True

        def _t():
            got_mic = False
            try:
                # Do not race the wake worker, a voice confirmation, or an
                # ask_user capture for the same PortAudio device.
                got_mic = self._mic_lock.acquire(blocking=False)
                if not got_mic:
                    self._notify_js('handleVoiceError("Microphone is busy")')
                    return
                with self.microphone as src:
                    self.recognizer.adjust_for_ambient_noise(src, duration=0.4)
                    audio = self.recognizer.listen(src, timeout=5, phrase_time_limit=12)
                    text = self.recognizer.recognize_google(audio)
                    self._notify_js(f'handleVoiceResult("{esc(text)}")')
            except sr.WaitTimeoutError:
                self._notify_js('handleVoiceError("No speech detected")')
            except sr.UnknownValueError:
                self._notify_js('handleVoiceError("Could not understand")')
            except Exception as e:
                self._notify_js(f'handleVoiceError("{esc(str(e))}")')
            finally:
                if got_mic:
                    self._mic_lock.release()
                with self._listening_lock:
                    self.is_listening = False
                self._notify_js("handleVoiceEnd()")

        threading.Thread(target=_t, daemon=True).start()
        return {"success": True}

    def _notify_js(self, js):
        try:
            if self._window:
                self._window.evaluate_js(js)
        except Exception:
            pass

    # -- JARVIS utility functions --------------------------------------------

    def check_email(self):
        try:
            import webbrowser

            webbrowser.open("https://mail.google.com")
            time.sleep(3)
            snap = self.live.snapshot
            return f"Opened Gmail. Current window: {snap.get('foreground_window', 'Unknown')}"
        except Exception as e:
            return f"Could not open Gmail: {str(e)}"

    def open_url(self, url):
        try:
            if not url.startswith("http"):
                url = "https://" + url
            subprocess.Popen(["explorer.exe", url])
            time.sleep(2.5)
            # The user's own browser: no browser_* action reaches it. Say so,
            # so a task that needs the page's content switches to MistAI's.
            self.last_result_text = (
                f"Opened {url} in the user's own browser for them to look at. MistAI cannot "
                f"read or click inside that window: if the task needs this page's content, "
                f"use browser_navigate {url} (MistAI's browser), not screen reads or ask_user.")
            return True
        except Exception as e:
            print(f"[URL] Error: {e if verbose_logs() else type(e).__name__}")
            return False

    def screenshot_to_file(self, filename="screenshot.png"):
        try:
            pyautogui.hotkey("win", "shift", "s")
            time.sleep(1)
            return True
        except Exception as e:
            print(f"[Screenshot] Error: {e}")
            return False

    def _clipboard_text_full(self):
        """The whole clipboard text ("" when it holds no text), or None when
        it cannot be read. get_clipboard() truncates; restoring from it would
        cut a long clipboard short."""
        return clip.read_text()

    def get_clipboard(self):
        """Live clipboard text; the LiveState snapshot (300 chars, may be
        stale) is only a fallback."""
        content = clip.read_text()
        if content is None:
            content = self.live.snapshot.get("clipboard", "")
        return content[:_RESULT_MEMORY_CHARS] if content else "Clipboard is empty"

    def set_clipboard(self, text):
        # clip.set_text always closes the clipboard (even after a failed set)
        # and logs the holder (self|other|unknown) when it is busy.
        return clip.set_text(text)

    def _vision_frame(self, label):
        """Frame for a vision read: captured after the last UI action
        (WindowWatch.frame_for_read), falling back to .frame for stand-ins."""
        get = getattr(self.screen, "frame_for_read", None)
        return get(label) if callable(get) else self.screen.frame

    def analyze_screen(self):
        snap = self.live.snapshot
        fw = snap.get("foreground_window", "Unknown")

        description = None
        if self.vision.ready:
            try:
                frame = self._vision_frame("analyze_screen")
                if frame:
                    description = self.vision.understand_screen(frame, use_cache=False)
            except Exception as e:
                print(f"[AnalyzeScreen] Vision error: {e}")

        if not description:
            description = (
                f"Window: {fw}. Vision unavailable -- cannot read on-screen content."
            )

        return {
            "app": fw,
            "open_apps": ", ".join(snap.get("open_apps", [])[:5]),
            "cpu": f"{snap.get('cpu_percent', 0):.1f}%",
            "ram": f"{snap.get('ram_percent', 0):.1f}%",
            "description": description,
        }

    _NUMBER_RX = re.compile(r"\d+(?:[.,]\d+)*")
    _read_trace_warned = False

    def _asked_app(self, query):
        """The app a screen read is about: one open app the query names (the
        phase's target wins if the query names several), else the app this
        phase opened or switched to, else None."""
        target = getattr(self, "_last_opened_app", None)
        try:
            open_apps = list(self.live.snapshot.get("open_apps", []))
        except Exception:
            open_apps = []
        q = str(query or "").lower()
        named = [a for a in TRACKED_APPS
                 if re.search(rf"\b{re.escape(a)}\b", q)
                 and any(process_matches(a, o) for o in open_apps)]
        if target and any(process_matches(target, a) or process_matches(a, target)
                          for a in named):
            return target
        if len(named) == 1:
            return named[0]
        return target

    def _exact_ui_text(self, query="", include_browser=False):
        """(text, title, native, kind, asked) from a window's accessibility
        tree. The window is the one the read is about (_asked_app) when it
        exists, else the foreground one. kind: "exact" (the asked/foreground
        window, in front), "background" (the asked window, NOT in front) or
        "other" (only the foreground window, which is not the one asked
        about). Skipped ("") in MistAI's own window, and in browsers unless
        include_browser (the page DOM is the browser_* tools' job)."""
        from .executor import BROWSER_HINTS
        from .screen import foreground_uia_text

        asked = self._asked_app(query)
        try:
            import win32gui
            import win32process

            # The window in front for reading: never MistAI's own overlays.
            hwnd, kind = clip.foreground_for_reading()[0], "exact"
            if asked and not self._window_belongs_to(hwnd, asked):
                other = self._find_hwnd_by_process_name(asked)
                hwnd, kind = (other, "background") if other else (hwnd, "other")
            title = win32gui.GetWindowText(hwnd)
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if is_self_process(psutil.Process(pid).name(), pid):
                return "", title, False, kind, asked
        except Exception:
            return "", "", False, "exact", asked
        native = not any(h in title.lower() for h in BROWSER_HINTS)
        if not native and not include_browser:
            return "", title, False, kind, asked
        text = self._uia_text_cached(hwnd, foreground_uia_text)
        return text, title, native, kind, asked

    def _uia_text_cached(self, hwnd, walk):
        """The window's accessibility text: WindowWatch's snapshot while it
        is valid (same window and frame, no UI action or window event since),
        else a fresh walk that becomes the new snapshot. Stand-ins without
        WindowWatch always walk. Logs a fixed label and a length only."""
        watch = getattr(self, "screen", None)
        get = getattr(watch, "uia_snapshot", None)
        begin = getattr(watch, "begin_uia_snapshot", None)
        store = getattr(watch, "store_uia_snapshot", None)
        if not (callable(get) and callable(begin) and callable(store)):
            return walk(hwnd)[0]
        try:
            kept = get(hwnd)
        except Exception:
            kept = None
        if kept is not None:
            print(f"[UIA] snapshot reused (no events, same frame, {len(kept)} chars)")
            return kept
        try:
            begun = begin()
        except Exception:
            begun = None
        text = walk(hwnd)[0]
        try:
            store(hwnd, text, begun)
        except Exception:
            pass
        return text

    def _vision_read(self, query):
        """(answer, ok): the vision model's literal reading of the screen."""
        if not self.vision.ready:
            return "Vision isn't configured, so I can't read on-screen text.", False
        try:
            frame = self._vision_frame("read_screen_text")
            if not frame:
                return "No screen frame available.", False
            prompt = (
                f"Look at this screenshot and answer precisely: {query}\n"
                "Answer ONLY from what is visible in the screenshot: transcribe text "
                "and numbers exactly as displayed. Never calculate, complete, or infer "
                "a value that is not shown (a pending expression is not its result). "
                "If the answer is not visible, reply exactly: NOT VISIBLE ON SCREEN"
                if query
                else "Describe the visible text and content on this screen in detail."
            )
            result = self.vision._call(prompt, frame)
            if not result:
                return "Could not read anything meaningful from the screen.", False
            return result, True
        except Exception as e:
            return f"Error reading screen: {e}", False

    BROWSER_READ_CHARS = 3000    # page text a browser-page read returns

    def _browser_page_answer(self, query=""):
        """read_screen_text with MistAI's own browser in front and a question
        that is not about looks (MISTAI_UIA_FIRST): the page's DOM text, no
        5-8 s vision read. "" otherwise (never starts the browser)."""
        from .screen import is_visual_question

        browser = getattr(self, "browser", None)
        if not uia_first_enabled() or not getattr(browser, "running", False):
            return ""
        if query and is_visual_question(query):
            return ""
        try:
            in_front = clip.foreground_for_reading()[0]
            if not in_front or in_front != clip.playwright_window():
                return ""
            got = browser.page_text(timeout=2.0)
        except Exception:
            return ""
        if not got:
            return ""
        title, raw = got
        text = " ".join(str(raw or "").split())
        if len(text) < 40:
            return ""
        cut = text[:self.BROWSER_READ_CHARS]
        print(f"  [Read] MistAI's browser page: page text ({len(text)} chars, {len(cut)} sent), "
              "no vision call")
        more = (f" [cut, {len(text) - len(cut)} more characters; use browser_get_text to "
                "read more]" if len(text) > len(cut) else "")
        return (f"PAGE TEXT of MistAI's browser page '{str(title)[:60]}' (DOM, exact; no "
                f"screenshot was read): {cut}{more}\n(For what is clickable use browser_elements; "
                "for how the page looks, ask read_screen_text about its appearance.)")

    def read_screen_text(self, query=""):
        """In a native window whose accessibility text is sufficient and a
        question that is not about looks (MISTAI_UIA_FIRST, default on): that
        exact text alone, no vision call. Otherwise vision's reading, in a native
        window with the exact text beside it and a NOTE when vision reports
        numbers the window does not contain. MISTAI_TRACE_READS=1 logs both."""
        from .screen import is_visual_question, text_is_sufficient

        trace = trace_reads_enabled()
        page = self._browser_page_answer(query)
        if page:
            self.last_result_text = page[:_RESULT_MEMORY_CHARS]
            return self.last_result_text
        # UIA first: it is closer in time to the screenshot than the slow vision call.
        exact, title, native, kind, asked = self._exact_ui_text(query, include_browser=trace)
        if (native and kind in ("exact", "background") and uia_first_enabled()
                and text_is_sufficient(exact, title) and not is_visual_question(query)):
            # The window's own text answers it: no screenshot, no vision call.
            where = ("accessibility tree, exact" if kind == "exact" else
                     "accessibility tree, exact; this window is NOT in front")
            print("  [Read] answered from the accessibility text; no vision call")
            result = (f"EXACT UI TEXT of '{title[:60]}' ({where}): {exact}\n(No screenshot "
                      "was read. If the answer is not in this text, the window does not show "
                      "it; for how something looks, ask read_screen_text about its appearance.)")
            if trace:
                print(f"[ReadTrace] query={query[:80]!r} | vision=(skipped) | uia={exact[:300]!r}")
            self.last_result_text = result[:_RESULT_MEMORY_CHARS]
            return self.last_result_text
        answer, ok = self._vision_read(query)
        result = answer
        if native and exact and kind == "other":
            # Only some other window could be read: label it, never call it exact.
            result = (f"Vision: {answer}\nUI TEXT of the foreground window '{title[:60]}', "
                      f"not the {asked} window asked about (no {asked} window was found): "
                      f"{exact}")
        elif native and exact:
            if ok and NOT_VISIBLE_ON_SCREEN in answer:
                answer = "vision could not see it; use the EXACT UI TEXT below"
            where = ("accessibility tree, exact" if kind == "exact" else
                     "accessibility tree, exact; this window is NOT in front, so the "
                     "screenshot shows a different window")
            result = f"Vision: {answer}\nEXACT UI TEXT of '{title[:60]}' ({where}): {exact}"
            if ok:
                have = {n.replace(",", "") for n in self._NUMBER_RX.findall(exact)}
                missing = [n for n in dict.fromkeys(self._NUMBER_RX.findall(answer))
                           if n.replace(",", "") not in have]
                if missing:
                    result += (f"\nNOTE: the vision answer's number(s) {', '.join(missing)} "
                               "do not appear in the exact UI text; the exact UI text is "
                               "what the window contains.")
        if trace:
            if not Api._read_trace_warned:
                Api._read_trace_warned = True
                print("[ReadTrace] MISTAI_TRACE_READS=1: each screen read logs on-screen "
                      "text; skim these logs before sharing them.")
            print(f"[ReadTrace] query={query[:80]!r} | vision={answer[:200]!r} | "
                  f"uia={exact[:300]!r}")
        self.last_result_text = result[:_RESULT_MEMORY_CHARS]
        return self.last_result_text


    def import_my_facts(self):
        """Import the user's own facts from config.my_facts_path(): a JSON list
        of strings (or of {"content": ...}). Only when the user asks; facts
        already saved (in any category) are skipped. Never logs fact text."""
        path = my_facts_path()
        if not os.path.isfile(path):
            # Not an error: the file is optional.
            return {"success": True, "added": 0, "skipped": 0,
                    "message": f"No facts file found. To import facts, create {path} "
                               "with a JSON list of facts."}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return {"success": False, "added": 0, "skipped": 0,
                    "message": f"Could not read {path} ({type(e).__name__})."}
        items = data if isinstance(data, list) else []
        facts = [i.get("content") if isinstance(i, dict) else i for i in items]
        facts = [str(t) for t in facts if isinstance(t, str) and t.strip()]
        if not facts:
            return {"success": True, "added": 0, "skipped": 0,
                    "message": f"{path} has no facts to import (expected a JSON list)."}
        added, skipped, restored = self.memory.import_facts(facts)
        summary = (f"Imported {added} fact{'s' if added != 1 else ''} "
                   f"({skipped} already saved, {restored} restored).")
        print(f"[Memory] {summary}")                 # counts only, never fact text
        return {"success": True, "added": added, "skipped": skipped,
                "restored": restored, "message": summary}


    # ------------------------------------------------------------
    # ADD FACTS ABOUT ME (SETTINGS POPUP)
    # ------------------------------------------------------------
    # The popup's text: one fact per line, parsed by memory.parse_fact_lines
    # (bullets dropped, trimmed, deduped, size-capped) and stored through the
    # same Memory.import_facts as "Import my facts". Logs counts only.

    def add_my_facts(self, text):
        facts, info = parse_fact_lines(text)
        if info["too_big"]:
            return {"success": False, "added": 0,
                    "message": f"That is too much text at once (over {FACTS_TEXT_MAX_CHARS:,} "
                               "characters). Save it in parts."}
        if not facts:
            return {"success": False, "added": 0,
                    "message": "Nothing to save. Type at least one fact, one per line."}
        try:
            added, skipped, restored = self.memory.import_facts(facts)
        except Exception as e:
            print(f"[Memory] facts from Settings not saved ({type(e).__name__})")
            return {"success": False, "added": 0, "message": "Could not save the facts."}
        skipped += info["duplicates"]
        saved = added + restored
        parts = [f"Saved {saved} fact{'s' if saved != 1 else ''}."]
        if skipped:
            parts.append(f"{skipped} {'was' if skipped == 1 else 'were'} already saved.")
        if info["too_long"]:
            parts.append(f"{info['too_long']} {'was' if info['too_long'] == 1 else 'were'} "
                         "too long and left out.")
        if info["over"]:
            parts.append(f"{info['over']} over the limit for one save {'was' if info['over'] == 1 else 'were'} left out.")
        print(f"[Memory] facts from Settings: {added} added, {restored} restored, "
              f"{skipped} already saved, {info['too_long']} too long, {info['over']} over the cap")
        return {"success": True, "added": saved, "skipped": skipped,
                "too_long": info["too_long"], "over": info["over"], "message": " ".join(parts)}

    def find_file_roots(self):
        """Folders find_file searches, in order: the active workspace root,
        Desktop, Documents, Downloads, then the home folder (shallow)."""
        roots = []
        workspace = getattr(self, "workspace", None)
        root = getattr(workspace, "project_root", None)
        if root:
            roots.append(root)
        for ph in ("{desktop}", "{documents}", "{downloads}"):
            try:
                roots.append(self._resolve_path_placeholders(ph))
            except Exception:
                pass
        home = os.path.expanduser("~")
        roots.append(home)
        return roots, {os.path.abspath(home): file_search.HOME_DEPTH}

    def _act_find_file(self, param):
        """Ranked filename search on disk (see core/file_search.py).
        parameter = a filename or wildcard ("*.pdf"), optionally with a
        folder ("{desktop}\\proj\\.env") to search only that folder."""
        query = self._resolve_path_placeholders(param or "").strip().strip("\"'")
        if not query:
            self.last_result_text = "find_file needs a filename (e.g. 'report.pdf' or '*.env')"
            return False
        # Only ABSOLUTE paths are paths: a bare ".env" must be searched for,
        # not resolved against MistAI's own working directory.
        if os.path.isabs(query) and os.path.isfile(query):
            self.last_result_text = f"find_file: {query} exists (exact path)."
            return True
        folder, name = os.path.split(query)
        if folder and not os.path.isabs(folder):
            self.last_result_text = (
                f"find_file: '{folder}' is not an absolute folder -- use a full path "
                "or a {desktop}/{documents}/{downloads}/{home} placeholder"
            )
            return False
        if folder and os.path.isdir(folder) and name:
            roots, depths = [folder], {}
        elif folder:
            self.last_result_text = f"find_file: folder not found: {folder}"
            return False
        else:
            roots, depths = self.find_file_roots()
            name = query
        matches, info = file_search.search(name, roots, depth_limits=depths)
        self.last_result_text = file_search.describe(name, matches, info)[:_RESULT_MEMORY_CHARS]
        return bool(matches)

    def find_file(self, filename):
        """Legacy UI helper: types into Windows Search. The find_file ACTION
        uses _act_find_file (a real filesystem search) instead."""
        try:
            pyautogui.hotkey("win", "s")
            time.sleep(1.2)
            for char in filename:
                pyautogui.write(char, interval=0.06)
            time.sleep(2.0)
            return True
        except Exception as e:
            print(f"[FindFile] Error: {e if verbose_logs() else type(e).__name__}")
            return False


    # -- Enhanced-utils action handlers --------------------------------------

    def _act_window_list(self):
        if not self.enhanced_available:
            self.last_result_text = "Window management module isn't available."
            self.speak(self.last_result_text)
            return False
        windows = self.window_mgr.list_all_windows()
        titles = [w["title"] for w in windows[:40]]
        self.last_result_text = (
            (f"Found {len(windows)} windows: " + " | ".join(titles))
            if titles
            else "No windows found"
        )[:_RESULT_MEMORY_CHARS]
        self.speak(
            (f"Found {len(windows)} windows: " + ", ".join(titles[:10]))
            if titles
            else "No windows found"
        )
        return True


    def _act_browser_get_text(self, param):
        try:
            text = self.browser.get_text(param.strip() if param else "")
            self.last_result_text = (text or "")[:_RESULT_MEMORY_CHARS]
            return True
        except Exception as e:
            self.last_result_text = f"browser_get_text failed: {e}"
            return False
