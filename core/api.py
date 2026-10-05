"""
core/api.py — the Api class exposed to the pywebview frontend. This is where
all subsystems (TTS, vision, memory, agent loop, enhanced utils, etc.) are
wired together.
"""

import json
import os
import re
import shutil
import subprocess
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
from .api_manager import APIKeyManager
from .caption import CaptionWindow
from .click_handler import CoordinateValidator, ImprovedClickHandler
from .config import (
    AGENT_MODEL,
    ENTER_CONFIRM_APPS,
    STATUS_URL,
    TRACKED_APPS,
    WAKE_WORDS,
    esc,
    fuzzy_wake,
    is_secret_file,
    is_self_process,
    normalize_process_name,
    process_matches,
    secret_output_reason,
    strip_wake_prefix,
)
from . import file_search
from .events import EventBus
from .executor import Executor
from .live_state import LiveStateTracker, window_belongs_to
from .memory import Memory
from .screen import ScreenBuffer
from .task_store import TaskStore
from .tools import ToolRegistry
from .tts import NeuralTTS
from .uia_nav import EnhancedUIANavigator
from .vision import CohereVision
from .watchers import FileWatcher, StateWatcher
from .workspace import Workspace

try:
    from . import enhanced_utils

    ENHANCED_UTILS_AVAILABLE = True
except ImportError:
    ENHANCED_UTILS_AVAILABLE = False
    enhanced_utils = None

# Memory bound for handler result text. What the model actually sees per step
# is the much smaller ToolSpec.result_chars (AgentLoop truncates).
_RESULT_MEMORY_CHARS = 20_000
# Appended wherever secret content is withheld from the model.
_WITHHELD_NOTE = (
    "You cannot see or show this content. Tell the user it was withheld "
    "because it may contain secrets; do not guess it, and do not offer to "
    "reveal it or retry another way."
)

class Api:
    _BROWSER_APP_NAMES = {"firefox", "chrome", "edge", "browser"}

    _DEFAULT_HOTKEY = "ctrl+k"  # fallback only if the LLM genuinely can't answer
    _PROJECT_MUTATION_CUES = re.compile(
        r"\b(?:convert|migrat|refactor|build|create|write|edit|modify|update|"
        r"install|add|remove|cleanup|delete)\b.*\b(?:project|website|app|react|"
        r"package(?:\.json)?|frontend|codebase|repository|repo|site)\b",
        re.IGNORECASE,
    )

    def __init__(self):
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
        self.browser = BrowserSession()
        self._pending_answer = None
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
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.12
        self.memory = Memory()
        from .task_memory import TaskMemory

        self.task_memory = TaskMemory()
        from .seed_data import DEFAULT_HOTKEYS, DEFAULT_SITE_SHORTCUTS, SEED_FACTS

        self.memory.seed_defaults(DEFAULT_HOTKEYS)
        self.memory.seed_defaults(DEFAULT_SITE_SHORTCUTS)
        self.memory.seed_facts(SEED_FACTS)
        self.tts.set_gender(self.memory.get_profile("user_gender", "none"))
        self.vision = CohereVision()
        self.screen = ScreenBuffer(fps=30)
        self.live = LiveStateTracker()
        self.click_handler = ImprovedClickHandler(
            live_state=self.live, vision_engine=self.vision, screen_buffer=self.screen
        )
        self.click_handler.target_rect_fn = self._click_target_rect
        self.uia_nav = EnhancedUIANavigator()
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
        self.captions_enabled = False
        self.caption_window = None
        self.window = None
        stats = self.memory.get_stats()
        print(
            f"[MistAI] Ready - {stats['conversations']} conversations in memory "
            f"(enhanced utils: {'on' if self.enhanced_available else 'off'})"
        )

    # -- Agent run state ------------------------------------------------------

    def is_agent_busy(self):
        """Ground truth for 'is Mist doing something': true iff an AgentLoop
        thread currently holds the run gate. The UI polls this instead of
        inferring idleness from its own request timeouts."""
        return {
            "busy": self._agent_lock.locked(),
            "task": (self._agent_task or "")[:80],
            "elapsed": round(time.time() - self._agent_started_at, 1)
            if self._agent_lock.locked() else 0,
            "stop_requested": self._stop_requested.is_set(),
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
        if self.captions_enabled and self.caption_window:
            self.caption_window.show_speech(text, 8)
        self.tts.say(text, interrupt=interrupt)

    speak_now = speak

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

    def set_api_mode(self, use_backend):
        self.api_manager.use_backend(use_backend)
        return {
            "success": True,
            "message": f"API mode: {'Backend' if use_backend else 'Direct API'}",
        }

    def minimize_window(self):
        try:
            if self.window:
                self.window.minimize()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

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
        disabled and is reused on the next enable, so it also calibrates once
        per lifetime instead of once per rapid UI toggle.
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
        rec.dynamic_energy_threshold = True
        rec.pause_threshold = 0.8
        print(f"[Wake] Google listening for: {WAKE_WORDS}")

        calibrated = False
        while not self._wake_shutdown.is_set():
            # While off, own no microphone resource. A later enable wakes this
            # same worker rather than racing a replacement worker into life.
            if not self._wake_enabled.wait(timeout=0.2):
                continue

            # Calibrate once, never over TTS, and under the same mic lock used
            # by ask_user/manual capture. This prevents two PortAudio streams
            # from opening concurrently.
            if not calibrated:
                if self.tts.speaking and not self._wait_for_tts(timeout=5):
                    continue
                if not self._wake_enabled.is_set():
                    continue
                if not self._mic_lock.acquire(blocking=False):
                    time.sleep(0.2)
                    continue
                try:
                    if self._wake_enabled.is_set():
                        with self.microphone as src:
                            rec.adjust_for_ambient_noise(src, duration=1.0)
                        print(
                            f"[Wake] Calibrated energy threshold: "
                            f"{rec.energy_threshold:.0f}"
                        )
                        calibrated = True
                except Exception as e:
                    print(f"[Wake] Calibration failed: {e}")
                    calibrated = True  # avoid a noisy failure loop
                finally:
                    self._mic_lock.release()
                continue

            if self.tts.speaking:
                self.tts.wait_until_done(timeout=1.0)
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

            try:
                text = rec.recognize_google(audio).lower()
                print(f"[Wake] Heard: '{text}'")
            except sr.UnknownValueError:
                print("[Wake] Heard something but couldn't transcribe it")
                continue
            except Exception as e:
                print(f"[Wake] {e}")
                continue

            # -- A task is running: the ONLY thing we listen for is "stop".
            # (The user said "stop" mid-task in a real session and nothing
            # happened, because the wake loop only knew about wake words.)
            if self._agent_lock.locked():
                t = self._normalize_utterance(text)
                if t in self._VOICE_STOP_WORDS:
                    # A standalone stop always interrupts the active task,
                    # including while it is waiting at a confirmation or
                    # ask_user prompt. stop_current_task() wakes that prompt
                    # with a sentinel, so it cannot be mistaken for merely a
                    # declined confirmation.
                    print("[Wake] Voice stop -- halting current task")
                    self.stop_current_task()
                    self.speak("Stopping.", interrupt=True)
                continue

            ww, cmd = fuzzy_wake(text)
            if not ww:
                continue
            print(f"[Wake] Matched wake word '{ww}', command: '{cmd}'")
            if cmd and len(cmd.split()) < 2:
                cmd = None
            # Run the conversation OFF this thread so the wake loop keeps
            # listening for a voice "stop" while the agent works.
            if not self._conversation_lock.acquire(blocking=False):
                print("[Wake] Conversation already active; ignoring duplicate wake")
                continue

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
                cmd = self._listen_once(timeout=5)
                if cmd:
                    cmd = strip_wake_prefix(cmd)
                continue

            detected = self._detect_gender_address(cmd)
            if detected:
                gender = detected

            try:
                res = self.ask_mistai(cmd, gender=gender, input_mode="voice")
                if res.get("error") == "busy":
                    # Another loop owns the machine; ask_mistai already said so.
                    break
            except Exception as e:
                print(f"[ConversationLoop] Task error: {e}")
                break

            self._wait_for_tts()

            cmd = self._listen_once(timeout=5)
            if cmd:
                cmd = strip_wake_prefix(cmd)

        print("[ConversationLoop] Ended, returning to wake-word-only mode")

    def _listen_once(self, timeout=6):
        with self._mic_lock:
            try:
                with self.microphone as src:
                    self.recognizer.pause_threshold = 1.0
                    self.recognizer.non_speaking_duration = 0.5
                    self.recognizer.adjust_for_ambient_noise(src, duration=0.3)
                    audio = self.recognizer.listen(
                        src, timeout=timeout, phrase_time_limit=15
                    )
                    return self.recognizer.recognize_google(audio)
            except Exception as e:
                print(f"[ListenOnce] {e}")
                return None

    def toggle_proactive_mode(self, enabled):
        return {"success": True}

    def get_wake_word_status(self):
        return {"active": self.wake_word_active, "wake_words": WAKE_WORDS}

    def get_proactive_status(self):
        return {"enabled": False}

    def get_captions_status(self):
        return {"enabled": self.captions_enabled}

    def set_user_gender(self, gender):
        self.memory.set_profile("user_gender", gender)
        self.tts.set_gender(gender)
        print(f"[API] Gender set to: {gender}")
        return {"success": True}

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

    def submit_answer(self, text):
        with self._answer_lock:
            self._pending_answer = text
            self._answer_event.set()
        return {"success": True}

    def _get_text_answer(self, question, timeout=60):
        with self._answer_lock:
            self._pending_answer = None
            self._answer_event.clear()
            self._awaiting_text_answer.set()
        try:
            if self.window:
                self.window.restore()
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
        # Single sources of truth: the model comes from config, the gender from
        # the saved profile (the UI dropdown writes the profile too, so text
        # and voice can no longer disagree about "sir").
        model = model or AGENT_MODEL
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
                    self.speak(msg)
                return {
                    "success": False,
                    "command": {"action": "done", "parameter": "", "speech": msg},
                    "steps": 0,
                    "error": "busy",
                    "busy": True,
                }

            self.minimize_window()
            result_holder = {}
            done_event = threading.Event()

            def _release():
                # Called from the agent thread when the loop really ends.
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
                self._notify_js(
                    f'handleAgentStep({n},"{esc(action)}","{esc(str(param))}","{esc(speech)}")'
                )

            def _on_done(result):
                result_holder.update(result)
                _release()
                done_event.set()

            def _on_error(err):
                result_holder["success"] = False
                result_holder["error"] = err
                _release()
                done_event.set()

            self.memory.add_conversation("user", message)

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
                with self._agent_state_lock:
                    self._agent_task = None
                    try:
                        self._agent_lock.release()
                    except RuntimeError:
                        pass
            return {"success": False, "error": str(e)}

    def execute_action(self, action_type, parameter, speech=""):
        if speech:
            self.speak(speech)
        return {"success": True}

    # -- Action execution (the action menu the agent picks from) ------------

    def _exec_sync(self, action, parameter):
        param = str(parameter) if not isinstance(parameter, str) else parameter
        print(f"[Exec] {action}({repr(param[:80])})")

        try:
            if action == "open_app":
                return self._act_open_app(param)
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
                result = self.analyze_screen()
                self.last_result_text = (
                    f"Window: {result['app']} | Open: {result['open_apps']} | "
                    f"{result['description']}"
                )
                return True
            elif action == "read_screen_text":
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
                return self.switch_to_app(param)
            elif action == "take_notes":
                return self.take_notes(param)
            elif action == "uia_find_input":
                return self._act_uia_find_input(param)
            elif action == "window_list":
                return self._act_window_list()
            elif action == "window_activate":
                return self._act_window_op("activate_window", param)
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
        except Exception as e:
            print(f"[Exec] Error in {action}: {e}")
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
            print(f"[ResolveApp] Registry lookup failed for '{name}': {e}")
        return None

    @staticmethod
    def _ps_single_quote(value):
        """Quote one value for a PowerShell single-quoted string literal."""
        return str(value or "").replace("'", "''")

    def _resolve_app_id(self, name):
        normalized_name = self._normalize_app_name(name)
        if len(normalized_name) < 2:
            return None
        try:
            quoted_name = self._ps_single_quote(name)
            ps_cmd = (
                f"$needle = '{quoted_name}'; Get-StartApps | Where-Object {{"
                "$_.Name -and $_.Name.IndexOf($needle, "
                "[System.StringComparison]::OrdinalIgnoreCase) -ge 0} "
                f"| Select-Object -First 1 -ExpandProperty AppID"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            app_id = result.stdout.strip()
            return app_id or None
        except Exception as e:
            print(f"[ResolveApp] Get-StartApps lookup failed for '{name}': {e}")
            return None

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
        try:
            import win32gui
        except ImportError:
            return None

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
        return matches[0] if matches else None

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
        while time.time() < deadline:
            state, _ = self.app_window_state(app_lower)
            if state in ("foreground", "visible"):
                return True
            time.sleep(0.4)
        return False

    def switch_to_app(self, app_name):
        """Switch to an app by matching its actual PROCESS identity, not
        window-title text. Returns True only if a real match was found and
        focus was verified afterward -- never lies about success."""
        app_lower = str(app_name or "").lower().strip()
        if len(self._normalize_app_name(app_lower)) < 2:
            print("[SwitchApp] Refusing an empty or one-character app target")
            return False
        try:
            import win32gui

            hwnd = self._find_hwnd_by_process_name(app_lower)
            if hwnd:
                win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE -- un-minimizes if needed
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.6)
                if win32gui.GetForegroundWindow() == hwnd:
                    return True
                # Windows refuses SetForegroundWindow from a process that
                # doesn't own the foreground (a common, silent failure).
                # A synthetic key press grants the permission; retry once.
                try:
                    pyautogui.press("alt")
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.4)
                    if win32gui.GetForegroundWindow() == hwnd:
                        return True
                except Exception:
                    pass
                print(f"[SwitchApp] Window for '{app_name}' exists but refused focus")
        except Exception as e:
            print(f"[SwitchApp] win32gui tier failed: {e}")

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
                return True
        except Exception as e:
            print(f"[SwitchApp] PowerShell AppActivate failed: {e}")

        print(f"[SwitchApp] No window found for process '{app_name}'")
        return False
    
    def _act_browser_import_cookies(self, param):
        if not param:
            self.last_result_text = "browser_import_cookies needs domain(s), e.g. 'youtube.com' or 'youtube.com|google.com'"
            return False
        try:
            counts = self.browser.import_cookies(param)
            self.last_result_text = f"Imported cookies: {counts}. Navigate/reload the page to see the logged-in state."
            return True
        except Exception as e:
            self.last_result_text = f"browser_import_cookies failed: {e}"
            return False
        
    def _act_browser_use_daily_profile(self, param):
        try:
            source, copied = self.browser.use_daily_profile_copy()
            self.last_result_text = f"Cloned logins from {source} ({', '.join(copied)}). Next browser action will use them."
            return True
        except Exception as e:
            self.last_result_text = f"browser_use_daily_profile failed: {e}"
            return False   

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

        exe_path = self._resolve_app_exe(param)
        if exe_path:
            try:
                subprocess.Popen(exe_path)
            except Exception as e:
                print(f"[OpenApp] Path launch failed: {e}")
                exe_path = None

        app_id = None
        if not exe_path:
            app_id = self._resolve_app_id(param)
            if app_id:
                try:
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
                except Exception as e:
                    print(f"[OpenApp] Start-Apps launch failed: {e}")
                    app_id = None

        # Start-menu typing is the last resort: only when there was nothing
        # to launch by path or Start-apps ID and no window has appeared.
        if not exe_path and not app_id and not self._wait_for_app_window(app_lower, timeout=1.5):
            try:
                pyautogui.press("win")
                time.sleep(0.8)
                pyautogui.write(param, interval=0.08)
                time.sleep(0.5)
                pyautogui.press("enter")
            except Exception as e:
                print(f"[OpenApp] Start-menu fallback failed: {e}")

        # A Store app launched by its AUMID shows its frame within ~1-2s;
        # waiting longer only delays the honest failure.
        if self._wait_for_app_window(app_lower, timeout=5.0 if app_id else 6.0):
            self.switch_to_app(app_lower)
            time.sleep(0.4)
            try:
                pyautogui.hotkey("win", "up")
            except Exception:
                pass
            self._last_opened_app = app_lower
            self.memory.log_app_usage(param)
            state, title = self.app_window_state(app_lower)
            self.last_result_text = f"Launched '{param}' (window '{title[:40]}', {state})"
            return True

        # Distinguish "never started" from "started but stayed in the tray"
        # so the model doesn't retry the same launch.
        if app_id:
            self.last_result_text = (
                f"'{param}' was launched via its Start-apps ID ({app_id}) but no "
                f"window appeared within 5s"
            )
        elif self.is_app_running(app_lower):
            self.last_result_text = (
                f"'{param}' is running but never showed a window -- it may live "
                f"in the system tray or be blocked by a login/update dialog"
            )
        else:
            self.last_result_text = f"'{param}' did not start"
        print(f"[OpenApp] {self.last_result_text}")
        return False

    def _wait_for_app_running(self, app_lower, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_app_running(app_lower):
                return True
            time.sleep(0.4)
        return False

    def _act_uia_click(self, param):
        """Find and click an element by its accessible name using UIA --
        reads the real UI tree instead of guessing pixel coordinates from a
        screenshot. Try this before click_on_text for anything inside a
        normal desktop app."""
        fw = self.live.snapshot.get("foreground_window", "")
        if not fw or fw == "Unknown":
            self.last_result_text = "No foreground window to search."
            return False

        element = self.uia_nav.find_element_by_name(fw, param, partial=True)
        if element:
            try:
                rect = element.rectangle()
                from .click_indicator import get_indicator

                get_indicator().show(
                    (rect.left + rect.right) // 2,
                    (rect.top + rect.bottom) // 2,
                    method="UIA",
                    label=param,
                )
            except Exception:
                pass  # debug-only; never block the real click
            ok = self.uia_nav.click_element(element)
            self.last_result_text = (
                f"Clicked '{param}' via UIA"
                if ok
                else f"Found '{param}' but click failed"
            )
            return ok

        self.last_result_text = f"'{param}' not found in the UIA element tree of '{fw}'"
        return False

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

    def _act_type_text(self, param):
        """Type into whatever app was last opened via open_app. Focuses it
        first, then clears the field and pastes via clipboard instead of
        typing character-by-character -- faster, and avoids per-character
        edge cases (special chars, autocomplete interference, dropped
        keystrokes on slow-rendering apps like Electron chat clients)."""
        report, on_target = self._ensure_input_target("text")
        if not on_target:
            self.last_result_text = report
            print(f"[TypeText] {report}")
            return False
        # Replace the field only in an app this phase opened/switched to;
        # anywhere else, never select-all + delete someone's content.
        replace = bool(getattr(self, "_last_opened_app", None))
        if not replace:
            report += " No app was opened or switched to, so the text was appended; nothing was cleared."
        prev_clipboard = None
        clipboard_staged = False
        try:
            # Stage text on the clipboard BEFORE changing the target field.
            # The old order cleared a draft with Ctrl+A/Delete and only then
            # discovered that another app had locked the clipboard.
            prev_clipboard = self._clipboard_text_full()
            if prev_clipboard is None:
                prev_clipboard = self.get_clipboard()
                if prev_clipboard == "Clipboard is empty":
                    prev_clipboard = ""
            if not self.set_clipboard(param):
                self.last_result_text = (
                    "Could not set the clipboard (locked by another app); "
                    "the existing field was left unchanged."
                )
                return False
            clipboard_staged = True

            time.sleep(0.2)
            if replace:
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.05)
                pyautogui.press("delete")
                time.sleep(0.2)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.15)
            self.last_result_text = report
            print(f"[TypeText] {report}")
            return True
        except Exception as e:
            print(f"[TypeText] Error: {e}")
            return False
        finally:
            # Preserve the user's clipboard even if UI input itself fails.
            if clipboard_staged:
                self.set_clipboard(prev_clipboard)

    def _get_app_search_hotkey(self, app):
        """Look up (and permanently cache) an app's quick-switch/search
        hotkey by asking the LLM directly -- no hardcoded table. The model
        already knows this stuff (same brain as the web version), so we
        just ask once per app and remember the answer forever."""
        app_lower = app.lower().strip()
        cached = self.memory.get_profile(f"hotkey_{app_lower}", None)
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
            r = requests.post(
                url,
                json={"message": prompt, "model": AGENT_MODEL},
                headers=headers,
                timeout=10,
            )
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

        # Sending to the wrong person is worse than failing to send. OCR/UIA
        # is unreliable in Electron chat clients, so this fast path requires
        # configured vision and exact recipient confirmation at both stages.
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
        # committing. Require ONLY the exact visible name: accepting prose,
        # prefixes (Ann/Anna), or an uncertain answer would make this a guess.
        # Vision only: these answers are compared to the name exactly, so the
        # appended accessibility-tree text of read_screen_text must not be in them.
        seen, _ = self._vision_read(
            "The quick-switcher/search box is open. Reply with ONLY the exact "
            f"name of the top highlighted conversation result. Expected: {recipient}"
        )
        if self._normalize_app_name(seen) != self._normalize_app_name(recipient):
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
        header, _ = self._vision_read(
            "Reply with ONLY the exact name shown in the header of the currently "
            f"open chat. Expected: {recipient}"
        )
        rn = self._normalize_app_name(recipient)
        hn = self._normalize_app_name(header)
        if not rn or rn != hn:
            self.last_result_text = (
                f"Opened a conversation, but its header could not be verified as "
                f"'{recipient}' (saw: {header[:60]}). NOTHING was typed or sent. "
                "Verify the exact contact name with ask_user before retrying."
            )
            return False

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
        pyautogui.press("enter")

        self.last_result_text = f"Sent to '{recipient}' in {app}"
        return True

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
                    try:
                        from .click_indicator import get_indicator

                        get_indicator().show(x, y, method="coords")
                    except Exception:
                        pass  # debug-only; never block the real click
                    pyautogui.moveTo(x, y, duration=0.2)
                    time.sleep(0.08)
                    pyautogui.click()
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
        """Run a ClickHandler click; a target-window refusal becomes the result text."""
        ok = click(*args)
        refusal = getattr(self.click_handler, "last_refusal", "")
        if not ok and refusal:
            self.last_result_text = refusal
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

    def _ensure_input_target(self, what):
        """(report, on_target) for input about to be sent. When this phase
        opened/switched to an app that is not in front, try one switch back;
        if it is still not in front, on_target is False and the caller must
        send NOTHING. No known target (no open_app/switch_app this phase):
        input goes to whatever is in front -- except a terminal/shell."""
        target = getattr(self, "_last_opened_app", None)
        if not target:
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

    def _act_press_key(self, param):
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
            return True
        except Exception as e:
            print(f"[Hotkey] Error with {param}: {e}")
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
                    print(f"[Organize] Could not move {entry}: {e}")
                    skipped.append(entry)
        except Exception as e:
            self.last_result_text = f"organize_desktop crashed: {e}"
            print(f"[Organize] Fatal error: {e}")
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
            if self.window:
                self.window.evaluate_js(js)
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
            return True
        except Exception as e:
            print(f"[URL] Error: {e}")
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
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            return None

    def get_clipboard(self):
        """Live clipboard text; the LiveState snapshot (300 chars, may be
        stale) is only a fallback."""
        content = None
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                content = ""
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    content = str(
                        win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    )
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            content = None
        if content is None:
            content = self.live.snapshot.get("clipboard", "")
        return content[:_RESULT_MEMORY_CHARS] if content else "Clipboard is empty"

    def set_clipboard(self, text):
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            win32clipboard.CloseClipboard()
            return True
        except Exception:
            return False

    def _vision_frame(self, label):
        """Frame for a vision read: captured after the last UI action
        (ScreenBuffer.frame_for_read), falling back to .frame for stand-ins."""
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
        from .uia_text import foreground_uia_text

        asked = self._asked_app(query)
        try:
            import win32gui
            import win32process

            hwnd, kind = win32gui.GetForegroundWindow(), "exact"
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
        text, _ = foreground_uia_text(hwnd)
        return text, title, native, kind, asked

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

    def read_screen_text(self, query=""):
        """Vision's reading of the screen; in a native window also the exact
        accessibility-tree text, and a NOTE when vision reports numbers the
        window does not contain. MISTAI_TRACE_READS=1 logs both side by side."""
        trace = os.environ.get("MISTAI_TRACE_READS") == "1"
        # UIA first: it is closer in time to the screenshot than the slow vision call.
        exact, title, native, kind, asked = self._exact_ui_text(query, include_browser=trace)
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

    def list_open_apps(self):
        from .live_state import describe_open_apps

        return describe_open_apps(self.live.snapshot) or "No tracked apps open"

    def remember_fact(self, fact, category="general", importance=1):
        self.memory.remember_fact(fact, category=category, importance=importance)
        return {"success": True, "stored": fact}

    def recall_facts(self, query=None, category=None):
        return self.memory.recall_facts(query=query, category=category, limit=5)

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
            print(f"[FindFile] Error: {e}")
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
    def _act_browser_open(self, param):
            try:
                self.browser.ensure_started()
                if self.browser._start_error:
                    self.last_result_text = self.browser._start_error
                    return False
                if param:
                    url, title = self.browser.navigate(param)
                    self.last_result_text = f"Opened {url} ({title})"
                else:
                    self.last_result_text = "Browser ready"
                return True
            except Exception as e:
                self.last_result_text = f"browser_open failed: {e}"
                return False

    def _page_excerpt(self, limit=900):
        """First chunk of the current page's real DOM text, for handing back
        to the model in the same observation as a navigate/search. Without
        this the model had to *know* to call browser_get_text as a second
        step -- and a small model instead re-ran the search five times."""
        try:
            text = self.browser.get_text("") or ""
            text = " ".join(text.split())
            return text[:limit] + ("..." if len(text) > limit else "")
        except Exception:
            return ""

    def _act_browser_navigate(self, param):
        if not param:
            self.last_result_text = "browser_navigate needs a URL"
            return False
        try:
            url, title = self.browser.navigate(param)
            excerpt = self._page_excerpt()
            self.last_result_text = f"Navigated to {url} ({title})."
            if excerpt:
                self.last_result_text += (
                    f" PAGE TEXT (already read -- do NOT navigate again; use "
                    f"browser_get_text with a selector only if you need more): "
                    f"{excerpt}"
                )
            self.speak(f"Navigating to {url}")
            return True
        except Exception as e:
            self.last_result_text = f"browser_navigate failed: {e}"
            return False

    def _act_browser_search(self, param):
        if not param:
            return False
        import urllib.parse
        url = "https://www.google.com/search?q=" + urllib.parse.quote(param)
        try:
            self.browser.navigate(url)
            excerpt = self._page_excerpt()
            self.last_result_text = f"Searched Google for '{param}'."
            if excerpt:
                self.last_result_text += (
                    f" RESULTS PAGE TEXT (already read -- do NOT search again; "
                    f"answer from this, or browser_click a result): {excerpt}"
                )
            self.speak(f"Searching for {param}")
            return True
        except Exception as e:
            self.last_result_text = f"browser_search failed: {e}"
            return False

    def _act_browser_close(self):
        try:
            self.browser.close()
            self.last_result_text = "Browser closed"
            self.speak("Browser closed")
            return True
        except Exception as e:
            self.last_result_text = f"browser_close failed: {e}"
            return False

    def _act_browser_click(self, param):
        if not param:
            self.last_result_text = "browser_click needs the visible text to click"
            return False
        if "|" in param:
            text, role = param.split("|", 1)
        else:
            text, role = param, None
        try:
            self.browser.click(text.strip(), role=(role.strip() if role else None))
            self.last_result_text = f"Clicked '{text.strip()}' in the browser (real DOM match)"
            return True
        except Exception as e:
            self.last_result_text = f"browser_click could not find/click '{text.strip()}': {e}"
            return False

    def _act_browser_fill(self, param):
        if "|" not in (param or ""):
            self.last_result_text = "browser_fill needs format: field label|text to type"
            return False
        field, text = param.split("|", 1)
        try:
            self.browser.fill(field.strip(), text)
            self.last_result_text = f"Filled '{field.strip()}'"
            return True
        except Exception as e:
            self.last_result_text = f"browser_fill could not find field '{field.strip()}': {e}"
            return False

    def _act_browser_get_text(self, param):
        try:
            text = self.browser.get_text(param.strip() if param else "")
            self.last_result_text = (text or "")[:_RESULT_MEMORY_CHARS]
            return True
        except Exception as e:
            self.last_result_text = f"browser_get_text failed: {e}"
            return False

    def _act_browser_press_key(self, param):
        if not param:
            return False
        try:
            self.browser.press_key(param.strip())
            self.last_result_text = f"Pressed '{param.strip()}' in the browser"
            return True
        except Exception as e:
            self.last_result_text = f"browser_press_key failed: {e}"
            return False

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
