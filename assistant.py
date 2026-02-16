"""
MistAI Desktop Assistant
Working wake word detection
Proper action verification & delays
User captions removed
Clean imports (removed unused)
Production API endpoints

Requirements:
    pip install pywebview pyautogui requests SpeechRecognition pyttsx3 pyaudio psutil pytesseract Pillow pywin32
"""

# ==========================================
# CRITICAL: UTF-8 SETUP MUST BE FIRST
# ==========================================
import sys
# ==========================================
# NOW IMPORT EVERYTHING ELSE
# ==========================================
import os
import warnings
import logging

# Suppress warnings AFTER importing
warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

# Suppress pywebview spam - DO THIS ONCE ONLY
logging.basicConfig(level=logging.CRITICAL)
logging.getLogger("pywebview").setLevel(logging.CRITICAL)

# Now import the rest
import webview
import pyautogui
import requests
import speech_recognition as sr
import pyttsx3
import time
import threading
import json
from datetime import datetime
import psutil
from queue import Queue
import tkinter as tk
from tkinter import font as tkfont
import difflib

# Optional OCR imports
try:
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image
    OCR_AVAILABLE = True
except:
    OCR_AVAILABLE = False

VERSION = "1.5.1"

def show_splash_screen():
    """Show splash screen before launching assistant"""
    try:
        from tkinter import Tk, Label
        from PIL import Image, ImageTk
        
        root = Tk()
        root.overrideredirect(True)
        root.configure(bg="black")
        
        # Load splash image
        try:
            img_path = get_resource_path("splash.png")
            img = Image.open(img_path)
            image_width, image_height = img.size
            photo = ImageTk.PhotoImage(img)
        except Exception as e:
            print(f"[Splash] Could not load splash.png: {e}")
            root.destroy()
            return
        
        # Center window
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width // 2) - (image_width // 2)
        y = (screen_height // 2) - (image_height // 2)
        root.geometry(f"{image_width}x{image_height}+{x}+{y}")
        
        # Display image
        label = Label(root, image=photo, bg="black")
        label.pack()
        label.image = photo  # Keep reference
        
        # Fade in
        root.attributes("-alpha", 0)
        
        def fade_in(alpha=0):
            alpha += 0.05
            if alpha > 1:
                alpha = 1
            root.attributes("-alpha", alpha)
            if alpha < 1:
                root.after(50, fade_in, alpha)
            else:
                root.after(3000, root.destroy)  # Close after 3 seconds
        
        fade_in()
        root.mainloop()
        
    except Exception as e:
        print(f"[Splash] Error: {e}")
        # If splash fails, just continue

def setup_bundled_tesseract():
    """Setup Tesseract - checks bundled version first, then system install"""
    if not OCR_AVAILABLE:
        return False
    
    # Detect if running as PyInstaller bundle
    if getattr(sys, 'frozen', False):
        # Running as compiled .exe - use bundled Tesseract
        base_path = sys._MEIPASS
        bundled_tesseract_exe = os.path.join(base_path, 'Tesseract-OCR', 'tesseract.exe')
        bundled_tessdata = os.path.join(base_path, 'Tesseract-OCR', 'tessdata')
        
        print(f"Looking for bundled Tesseract at: {bundled_tesseract_exe}")
        
        if os.path.exists(bundled_tesseract_exe):
            # Set Tesseract executable path
            pytesseract.pytesseract.tesseract_cmd = bundled_tesseract_exe
            
            # Set tessdata path environment variable
            os.environ['TESSDATA_PREFIX'] = os.path.join(base_path, 'Tesseract-OCR')
            
            print(f"Using bundled Tesseract")
            print(f"EXE: {bundled_tesseract_exe}")
            print(f"TESSDATA: {bundled_tessdata}")
            
            # Verify tessdata exists
            if os.path.exists(bundled_tessdata):
                eng_file = os.path.join(bundled_tessdata, 'eng.traineddata')
                if os.path.exists(eng_file):
                    print(f"English language data found")
                else:
                    print(f"WARNING: eng.traineddata not found")
            
            return True
        else:
            print(f"Bundled Tesseract not found at expected location")
            print(f"Falling back to system installation...")
    
    # Not bundled OR bundled version not found - check system installation
    print("Checking for system Tesseract installation...")

    possible_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(os.getenv('LOCALAPPDATA', ''), 'Programs', 'Tesseract-OCR', 'tesseract.exe'),
        os.path.join(os.getenv('PROGRAMFILES', ''), 'Tesseract-OCR', 'tesseract.exe'),
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            print(f"Using system Tesseract: {path}")
            return True
    
    return False

# Initialize Tesseract
if OCR_AVAILABLE:
    tesseract_found = setup_bundled_tesseract()
    if not tesseract_found:
        OCR_AVAILABLE = False

# Configuration - PRODUCTION URLS
API_URL = "https://mist-ai.fly.dev/api/chat"
STATUS_URL = "https://mist-ai.fly.dev/api/status"
MODEL = "gemini"
DEBUG_MODE = False  # Set to False for production

# Wake words with phonetic alternatives
WAKE_WORDS = ["mist", "hey mist", "mistai", "mist ai"]
WAKE_WORD_ALTERNATIVES = {
    "mist": ["missed", "miss", "midst", "myst", "mest", "messed"],
    "hey mist": [
        "hey miss",
        "hey missed",
        "hey midst",
        "a mist",
        "hey mest",
        "i missed",
    ],
    "mistai": ["miss ai", "missed ai", "miss tie", "misty", "misty ai"],
    "mist ai": ["miss ai", "missed ai", "midst ai", "mess ai"],
}


def fuzzy_match_wake_word(text):
    """Check if text contains a wake word or close phonetic match"""
    text_lower = text.lower()

    # Direct match first
    for wake_word in WAKE_WORDS:
        if wake_word in text_lower:
            return wake_word, text_lower.split(wake_word, 1)[-1].strip()

    # Fuzzy match - check alternatives
    for wake_word, alternatives in WAKE_WORD_ALTERNATIVES.items():
        for alt in alternatives:
            if alt in text_lower:
                print(f"   Fuzzy match: '{alt}' -> '{wake_word}'")
                # Extract command after the alternative word
                command = text_lower.split(alt, 1)[-1].strip()
                return wake_word, command

    # Similarity check for single words (for "mist" variations)
    words = text_lower.split()
    for word in words:
        # Check similarity to "mist"
        if len(word) >= 3 and len(word) <= 6:
            similarity = difflib.SequenceMatcher(None, word, "mist").ratio()
            if similarity >= 0.75:  # 75% similar
                print(f"   Similarity match: '{word}' -> 'mist' ({similarity:.0%})")
                word_index = words.index(word)
                command = " ".join(words[word_index + 1 :])
                return "mist", command

    return None, None

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

class SimpleCaptionWindow:
    """Simple, reliable Tkinter caption overlay"""

    def __init__(self):
        self.root = None
        self.label = None
        self.is_ready = False
        self.message_queue = Queue()
        self.running = True

        self.thread = threading.Thread(target=self._run_tk, daemon=True)
        self.thread.start()

        # Wait for window to be ready
        timeout = 5
        start = time.time()
        while not self.is_ready and time.time() - start < timeout:
            time.sleep(0.1)

    def _run_tk(self):
        """Run Tkinter in its own thread"""
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg="black")

        if os.name == "nt":
            self.root.attributes("-transparentcolor", "black")

        # Position at bottom of screen
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = int(sw * 0.7)
        h = 100
        x = (sw - w) // 2
        y = sh - 200
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Create label
        font_ = tkfont.Font(family="Segoe UI", size=15, weight="bold")
        self.label = tk.Label(
            self.root,
            text="",
            font=font_,
            fg="white",
            bg="#1a1a2e",
            wraplength=w - 40,
            padx=20,
            pady=15,
            justify="center",
        )
        self.label.pack(fill="both", expand=True)

        self.is_ready = True
        self.root.after(50, self._process_queue)
        self.root.mainloop()

    def _process_queue(self):
        """Process messages from queue"""
        try:
            while not self.message_queue.empty():
                text, kind, duration = self.message_queue.get_nowait()
                self._display(text, kind, duration)
        except:
            pass

        if self.running:
            self.root.after(50, self._process_queue)

    def _display(self, text, kind, duration):
        """Display a caption"""
        colors = {
            "assistant": ("#10b981", "white"),
            "system": ("#fbbf24", "black"),
            "suggestion": ("#8b5cf6", "white"),
        }
        bg, fg = colors.get(kind, ("#1a1a2e", "white"))

        self.label.config(text=text, bg=bg, fg=fg)
        self.root.attributes("-alpha", 0.95)
        self.root.update_idletasks()

        if duration > 0:
            self.root.after(int(duration * 1000), self._hide)

    def _hide(self):
        """Hide the window"""
        try:
            self.root.attributes("-alpha", 0.0)
            self.root.update_idletasks()
        except:
            pass

    def show(self, text, kind="system", duration=6):
        """Show a caption (thread-safe)"""
        if self.is_ready:
            self.message_queue.put((text, kind, duration))

    def destroy(self):
        """Clean up"""
        self.running = False
        if self.root:
            try:
                self.root.quit()
            except:
                pass


class Api:
    """Backend API for MistAI Desktop Assistant"""

    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        self.is_listening = False

        # Initialize TTS
        self.engine = pyttsx3.init()
        self.engine.setProperty("rate", 175)
        self.engine.setProperty("volume", 1.0)

        # Speech queue
        self.speech_queue = Queue()
        self.speech_thread = threading.Thread(target=self._speech_worker, daemon=True)
        self.speech_thread.start()

        # Test PyAutoGUI
        print("\nTESTING PYAUTOGUI:")
        try:
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0.2
            pos = pyautogui.position()
            print(f"PyAutoGUI working! Mouse: {pos} | Screen: {pyautogui.size()}")
        except Exception as e:
            print(f"PYAUTOGUI ERROR: {e}")

        # Memory system
        self.conversation_active = False
        self.last_interaction_time = 0
        self.conversation_timeout = 45  # seconds
        self.conversation_history = []
        self.actions_performed = []
        self.opened_apps = set()
        self.active_window = None
        self.last_screenshot_text = ""
        self.context = {
            "last_action": None,
            "last_app_opened": None,
            "session_start": datetime.now().isoformat(),
        }

        # Wake word detection
        self.wake_word_active = False
        self.wake_word_thread = None
        self.stop_wake_word = threading.Event()

        # Proactive mode
        self.proactive_mode = False
        self.proactive_thread = None
        self.stop_proactive = threading.Event()
        self.last_screen_check = time.time()
        self.last_suggestion_time = time.time()
        self.suggestion_cooldown = 45
        self.last_suggestion = ""

        # Caption system
        self.captions_enabled = False
        self.caption_window = None
        self.tts_lock = threading.Lock()

    def minimize_window(self):
        """Minimize MistAI window"""
        try:
            if hasattr(self, "window") and self.window:
                self.window.minimize()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _speech_worker(self):
        """Background thread for TTS"""
        while True:
            text = self.speech_queue.get()
            if text:
                if self.captions_enabled and self.caption_window:
                    self.caption_window.show(f"🤖 {text}", "assistant", duration=8)

                with self.tts_lock:
                    self.engine.say(text)
                    self.engine.runAndWait()

            self.speech_queue.task_done()

    def speak_now(self, text, interrupt=False):
        """Queue text to be spoken"""
        if not text:
            return

        if interrupt:
            while not self.speech_queue.empty():
                try:
                    self.speech_queue.get_nowait()
                except:
                    break

        self.speech_queue.put(text)

    # ============================================
    # CAPTION SYSTEM
    # ============================================

    def toggle_captions(self, enabled):
        """Toggle caption system"""
        self.captions_enabled = enabled

        if enabled:
            if not self.caption_window:
                self.caption_window = SimpleCaptionWindow()
                self.caption_window.show("📺 Captions Enabled", "system", duration=3)
        else:
            if self.caption_window:
                self.caption_window.destroy()
                self.caption_window = None

        return {"success": True}

    def show_caption(self, text, kind="system"):
        """Show caption - blocks user captions"""
        if kind == "user":
            return {"success": True}

        if self.captions_enabled and self.caption_window:
            prefix = (
                "🤖 "
                if kind == "assistant"
                else "💡 " if kind == "suggestion" else "ℹ️ "
            )
            self.caption_window.show(f"{prefix}{text}", kind, duration=6)
        return {"success": True}

    # ============================================
    # WAKE WORD DETECTION
    # ============================================

    def start_wake_word_detection(self):
        """Start continuous wake word listening"""
        if self.wake_word_active:
            return {"success": False, "message": "Already active"}

        self.wake_word_active = True
        self.stop_wake_word.clear()

        self.wake_word_thread = threading.Thread(
            target=self._wake_word_loop, daemon=True
        )
        self.wake_word_thread.start()

        print("[Mic] Wake word detection started")
        return {"success": True, "message": "Wake word detection active"}

    def stop_wake_word_detection(self):
        """Stop wake word detection"""
        if not self.wake_word_active:
            return {"success": False, "message": "Not active"}

        self.wake_word_active = False
        self.stop_wake_word.set()

        print("[Mic] Wake word detection stopped")
        return {"success": True, "message": "Wake word detection stopped"}

    def _wake_word_loop(self):
        print(f"[Ear] Listening for wake words: {WAKE_WORDS}")

        wake_recognizer = sr.Recognizer()
        wake_recognizer.energy_threshold = 550
        wake_recognizer.dynamic_energy_threshold = True
        wake_recognizer.pause_threshold = 0.5

        while not self.stop_wake_word.is_set():
            try:
                with self.microphone as source:
                    wake_recognizer.adjust_for_ambient_noise(source, duration=0.2)
                    audio = wake_recognizer.listen(
                        source, timeout=1, phrase_time_limit=5
                    )

                try:
                    text = wake_recognizer.recognize_google(audio).lower()
                    print(f"[Ear] Heard: {text}")

                    wake_word, command_after_wake = fuzzy_match_wake_word(text)

                    if wake_word:
                        self.conversation_active = True
                        self.last_interaction_time = time.time()
                        print(f"[Check] Wake word detected: {wake_word}")
                        self._notify_wake_word_detected()

                        gender = self.get_user_gender()
                        if gender == "male":
                            greeting = "Yes, sir?"
                        elif gender == "female":
                            greeting = "Yes, ma'am?"
                        else:
                            greeting = "Yes?"

                        if command_after_wake:
                            print(f"[Target] Executing command: '{command_after_wake}'")
                            self._execute_wake_command(command_after_wake)
                        else:
                            self.speak_now(greeting, interrupt=True)
                            follow_up = self._listen_for_command(timeout=5)
                            if follow_up:
                                print(f"[Target] Follow-up command: '{follow_up}'")
                                self._execute_wake_command(follow_up)
                            else:
                                print("[Timer] Waiting for next command...")

                    elif self.conversation_active and text:
                        time_since_last = time.time() - self.last_interaction_time

                        if time_since_last < self.conversation_timeout:
                            print(
                                f"[Speech] Continuous conversation: '{text}' (timeout in {self.conversation_timeout - time_since_last:.0f}s)"
                            )
                            self.last_interaction_time = time.time()
                            self._execute_wake_command(text)
                        else:
                            self.conversation_active = False
                            print("[Sleep] Conversation timed out - back to wake word mode")

                except sr.UnknownValueError:
                    pass
                except Exception as e:
                    print(f"Wake word error: {e}")

            except sr.WaitTimeoutError:
                pass
            except Exception as e:
                print(f"Wake loop error: {e}")
                time.sleep(0.2)

    def _listen_for_command(self, timeout=5):
        """Listen for a command after wake word"""
        try:
            with self.microphone as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.3)
                audio = self.recognizer.listen(
                    source, timeout=timeout, phrase_time_limit=10
                )
                command = self.recognizer.recognize_google(audio)
                print(f"[Target] Command received: '{command}'")
                return command
        except sr.WaitTimeoutError:
            print("[Timer] No command received")
            return None
        except sr.UnknownValueError:
            print("[X] Could not understand command")
            return None
        except Exception as e:
            print(f"[Warning] Listen error: {e}")
            return None

    def _execute_wake_command(self, command):
        """Execute command from wake word - non-blocking"""

        def execute_in_background():
            try:
                print(f"\n[Rocket] WAKE COMMAND START: '{command}'")
                print(f"   [Satellite] Calling API at {API_URL}...")

                result = self.ask_mistai(command, MODEL, self.get_user_gender())

                print(f"   [Inbox] API Response received")
                print(f"   Success: {result.get('success')}")

                if result.get("success") and result.get("command"):
                    cmd = result["command"]
                    print(
                        f"   [Clapperboard] Executing: {cmd.get('action')} | {cmd.get('parameter')}"
                    )
                    print(f"   [Speech] Speech: {cmd.get('speech')}")

                    speech = cmd.get("speech", "")
                    if speech:
                        self.speak_now(speech, interrupt=False)
                        print(f"   [Speaker] Speaking: '{speech}'")

                    self.execute_action(
                        cmd.get("action"),
                        cmd.get("parameter"),
                        "",
                    )

                    self._notify_command_executed(command, speech)
                    print(f"   [Check] WAKE COMMAND COMPLETE")
                else:
                    error = result.get("error", "Unknown error")
                    print(f"   [X] API Error: {error}")
                    self.speak_now("Sorry, I couldn't process that.")

            except requests.exceptions.Timeout:
                print(f"   [Timer] API TIMEOUT")
                self.speak_now("Sorry, that took too long.")
            except requests.exceptions.ConnectionError:
                print(f"   [Plug] CONNECTION ERROR")
                self.speak_now("Sorry, I can't connect to my brain.")
            except Exception as e:
                print(f"   [X] EXCEPTION: {e}")
                import traceback

                traceback.print_exc()
                self.speak_now("Sorry, something went wrong.")

        threading.Thread(target=execute_in_background, daemon=True).start()
        print(f"   [Arrows] Wake command queued for execution")

    def _notify_wake_word_detected(self):
        """Notify UI that wake word was detected"""
        try:
            if hasattr(self, "window") and self.window:
                self.window.evaluate_js("handleWakeWordDetected()")
        except:
            pass

    def _notify_command_executed(self, command, response):
        """Notify UI that command was executed"""
        try:
            if hasattr(self, "window") and self.window:
                command_escaped = command.replace('"', '\\"').replace("'", "\\'")
                response_escaped = response.replace('"', '\\"').replace("'", "\\'")
                self.window.evaluate_js(
                    f'handleWakeCommand("{command_escaped}", "{response_escaped}")'
                )
        except:
            pass

    # ============================================
    # PROACTIVE MODE
    # ============================================

    def toggle_proactive_mode(self, enabled):
        """Toggle proactive screen monitoring"""
        self.proactive_mode = enabled

        if enabled:
            if not self.proactive_thread or not self.proactive_thread.is_alive():
                self.stop_proactive.clear()
                self.proactive_thread = threading.Thread(
                    target=self._proactive_loop, daemon=True
                )
                self.proactive_thread.start()
                print("[Eye] Proactive mode enabled")
                return {"success": True, "message": "Proactive mode enabled"}
        else:
            self.stop_proactive.set()
            print("[Eye] Proactive mode disabled")
            return {"success": True, "message": "Proactive mode disabled"}

    def _proactive_loop(self):
        """Continuous screen monitoring"""
        print("[Eye] Proactive monitoring started")

        while not self.stop_proactive.is_set() and self.proactive_mode:
            try:
                current_time = time.time()

                if current_time - self.last_screen_check >= 5:
                    self.last_screen_check = current_time

                    screen_text = self.read_screen_text()
                    active_window = self.get_active_window()

                    if (
                        current_time - self.last_suggestion_time
                        >= self.suggestion_cooldown
                    ):
                        suggestion = self._generate_suggestion(
                            screen_text, active_window
                        )

                        if suggestion:
                            self.last_suggestion_time = current_time
                            self._notify_suggestion(suggestion)

                time.sleep(1)

            except Exception as e:
                time.sleep(2)

        print("[Eye] Proactive monitoring stopped")

    def _generate_suggestion(self, screen_text, active_window):
        """Generate intelligent suggestion"""
        try:
            context = f"""You are MistAI in Proactive Mode. You're monitoring the user's screen.

Current window: {active_window}
Screen content (OCR): {screen_text[:500]}

Based on what you see, suggest ONE helpful action the user might want to take.
Your suggestion should be:
- Brief (1 sentence)
- Actionable
- Relevant to what's on screen
- Natural and helpful

If nothing interesting is happening, return "none".

Respond with ONLY your suggestion text, or "none"."""

            response = requests.post(
                API_URL,
                json={"message": context, "model": MODEL, "mode": "suggestion"},
                timeout=10,
            )

            if response.ok:
                data = response.json()
                suggestion = data.get("response", "").strip()

                if suggestion.lower() in ["none", "no suggestion", ""]:
                    return None

                if len(suggestion) > 150:
                    suggestion = suggestion[:147] + "..."

                if suggestion == self.last_suggestion:
                    return None

                self.last_suggestion = suggestion
                return suggestion

            return None

        except Exception as e:
            return None

    def _notify_suggestion(self, suggestion):
        """Notify UI and show caption for proactive suggestion"""
        try:
            self.show_caption(suggestion, "suggestion")

            if hasattr(self, "window") and self.window:
                suggestion_escaped = suggestion.replace('"', '\\"').replace("'", "\\'")
                self.window.evaluate_js(
                    f'handleProactiveSuggestion("{suggestion_escaped}")'
                )

        except Exception as e:
            pass

    # ============================================
    # UTILITY METHODS
    # ============================================

    def get_user_gender(self):
        return getattr(self, "user_gender", "none")

    def set_user_gender(self, gender):
        self.user_gender = gender
        return {"success": True}

    def get_wake_word_status(self):
        return {"active": self.wake_word_active, "wake_words": WAKE_WORDS}

    def get_proactive_status(self):
        return {"enabled": self.proactive_mode}

    def get_captions_status(self):
        return {"enabled": self.captions_enabled}

    def get_mistai_window_rect(self):
        try:
            import win32gui

            hwnd = win32gui.FindWindow(None, "MistAI Desktop Assistant")
            if hwnd:
                return win32gui.GetWindowRect(hwnd)
        except:
            pass
        return None

    # ============================================
    # OCR & VISION METHODS
    # ============================================

    def find_buttons_on_screen(self):
        """Find all button-like regions on screen using computer vision"""
        if not OCR_AVAILABLE:
            return []
        
        try:
            screenshot = pyautogui.screenshot()
            screenshot_np = np.array(screenshot)
            screenshot_np = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)
            
            mistai_rect = self.get_mistai_window_rect()
            if mistai_rect:
                left, top, right, bottom = mistai_rect
                if left > -10000:
                    screenshot_np[top:bottom, left:right] = 0
            
            screenshot_np[:80, :] = 0
            
            gray = cv2.cvtColor(screenshot_np, cv2.COLOR_BGR2GRAY)
            
            edges1 = cv2.Canny(gray, 30, 100)
            edges2 = cv2.Canny(gray, 100, 200)
            edges = cv2.bitwise_or(edges1, edges2)
            
            kernel = np.ones((3,3), np.uint8)
            edges = cv2.dilate(edges, kernel, iterations=2)
            
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            buttons = []
            
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                
                if w < 60 or w > 600 or h < 20 or h > 120:
                    continue
                
                aspect_ratio = w / h
                if aspect_ratio < 1.2 or aspect_ratio > 10:
                    continue
                
                button_region = screenshot_np[y:y+h, x:x+w]
                button_gray = cv2.cvtColor(button_region, cv2.COLOR_BGR2GRAY)
                
                if np.mean(button_gray) < 128:
                    button_gray = cv2.bitwise_not(button_gray)
                
                button_gray = cv2.resize(button_gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
                button_gray = cv2.normalize(button_gray, None, 0, 255, cv2.NORM_MINMAX)
                button_gray = cv2.adaptiveThreshold(
                    button_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                    cv2.THRESH_BINARY, 11, 2
                )
                
                text = pytesseract.image_to_string(
                    button_gray, 
                    config="--oem 3 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz "
                ).strip()
                
                if text and len(text) >= 2:
                    buttons.append((x, y, w, h, text))
            
            return buttons
            
        except Exception as e:
            print(f"   [X] Button detection error: {e}")
            return []

    def find_text_on_screen(self, search_text, confidence=45, save_debug=None):
        """HYBRID text finder with multiple OCR strategies"""
        if save_debug is None:
            save_debug = DEBUG_MODE
            
        if not OCR_AVAILABLE:
            print("   [X] OCR not available")
            return None

        try:
            screenshot = pyautogui.screenshot()
            screenshot_np = np.array(screenshot)
            screenshot_np = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2BGR)
            
            original_screenshot = screenshot_np.copy() if save_debug else None

            print(f"   [Search] HYBRID search for: '{search_text}'")
            
            print(f"   [Circle] Step 1: Detecting UI buttons...")
            buttons = self.find_buttons_on_screen()
            
            if buttons:
                print(f"   [List] Found {len(buttons)} button(s):")
                for i, (x, y, w, h, text) in enumerate(buttons[:5], 1):
                    print(f"      {i}. '{text}' at ({x},{y})")
                
                search_lower = search_text.lower()
                best_match = self._match_button(buttons, search_lower)
                
                if best_match:
                    x, y, w, h, text = best_match
                    print(f"   [Check] BUTTON MATCH: '{text}'")
                    if save_debug:
                        self._save_debug_screenshot(original_screenshot, buttons, best_match, search_text, "button")
                    return (x, y, w, h)

            print(f"   [Document] Step 2: Trying enhanced OCR...")
            
            mistai_rect = self.get_mistai_window_rect()
            if mistai_rect:
                left, top, right, bottom = mistai_rect
                if left > -10000:
                    screenshot_np[top:bottom, left:right] = 0
            screenshot_np[:80, :] = 0
            
            best_match = None
            best_score = 0
            best_strategy = ""
            
            gray1 = cv2.cvtColor(screenshot_np, cv2.COLOR_BGR2GRAY)
            match1, score1 = self._ocr_search(gray1, search_text, confidence)
            if score1 > best_score:
                best_match, best_score, best_strategy = match1, score1, "light"
            
            gray2 = cv2.bitwise_not(gray1)
            match2, score2 = self._ocr_search(gray2, search_text, confidence)
            if score2 > best_score:
                best_match, best_score, best_strategy = match2, score2, "inverted"
            
            gray3 = cv2.normalize(gray1, None, 0, 255, cv2.NORM_MINMAX)
            gray3 = cv2.convertScaleAbs(gray3, alpha=1.5, beta=0)
            match3, score3 = self._ocr_search(gray3, search_text, confidence)
            if score3 > best_score:
                best_match, best_score, best_strategy = match3, score3, "contrast"
            
            MIN_SCORE = 70 if len(search_text.split()) == 1 else 85
            
            if best_match and best_score >= MIN_SCORE:
                print(f"   [Check] OCR MATCH: score={best_score} strategy={best_strategy}")
                if save_debug:
                    x, y, w, h = best_match
                    self._save_debug_screenshot(
                        original_screenshot, [], 
                        (x, y, w, h, search_text), 
                        search_text, "ocr"
                    )
                return best_match
            
            print(f"   [X] NOT FOUND (best score: {best_score}, needed: {MIN_SCORE})")
            
            if save_debug:
                self._save_debug_screenshot(original_screenshot, buttons, None, search_text, "failed")
            
            return None
            
        except Exception as e:
            print(f"   [X] Error: {e}")
            import traceback
            traceback.print_exc()
            return None
        
    def _match_button(self, buttons, search_text):
        """Match search text to detected buttons"""
        search_lower = search_text.lower()
        search_words = search_lower.split()
        
        best_button = None
        best_score = 0
        
        for x, y, w, h, text in buttons:
            text_lower = text.lower()
            score = 0
            
            if text_lower == search_lower:
                score = 100
            elif all(word in text_lower for word in search_words):
                score = 95
            elif search_lower in text_lower:
                score = 85
            else:
                similarity = difflib.SequenceMatcher(None, text_lower, search_lower).ratio()
                score = int(similarity * 100)
            
            if score > best_score:
                best_score = score
                best_button = (x, y, w, h, text)
        
        if best_button and best_score >= 60:
            return best_button
        return None

    def _ocr_search(self, gray_image, search_text, confidence):
        """Perform OCR search on preprocessed image"""
        try:
            gray = cv2.resize(gray_image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            
            ocr_data = pytesseract.image_to_data(
                gray,
                output_type=pytesseract.Output.DICT,
                config="--oem 3 --psm 11"
            )
            
            search_lower = search_text.lower()
            best_match = None
            best_score = 0
            
            for i, word in enumerate(ocr_data["text"]):
                if not word.strip() or len(word) < 2:
                    continue
                
                word_lower = word.lower()
                conf = int(ocr_data["conf"][i])
                
                if conf < confidence:
                    continue
                
                score = 0
                if word_lower == search_lower:
                    score = 100
                elif search_lower in word_lower:
                    score = 85
                elif word_lower in search_lower:
                    score = 75
                else:
                    similarity = difflib.SequenceMatcher(None, word_lower, search_lower).ratio()
                    score = int(similarity * 100)
                
                if score > best_score:
                    x = int(ocr_data["left"][i] / 2)
                    y = int(ocr_data["top"][i] / 2)
                    w = int(ocr_data["width"][i] / 2)
                    h = int(ocr_data["height"][i] / 2)
                    
                    best_match = (x, y, w, h)
                    best_score = score
            
            return best_match, best_score
            
        except Exception as e:
            print(f"   [Warning] OCR strategy error: {e}")
            return None, 0

    def _save_debug_screenshot(self, screenshot, buttons, matched_element, search_text, mode="button"):
        """Save debug screenshot with auto-cleanup"""
        if not DEBUG_MODE:
            return
        try:
            debug_dir = os.path.join(os.path.expanduser("~"), "MistAI", "ocr_debug")   
                    
            if not os.path.exists(debug_dir):
                os.makedirs(debug_dir)
                print(f"   [Folder] Created debug dir: {debug_dir}")
            
            try:
                debug_files = [f for f in os.listdir(debug_dir) if f.endswith('.png')]
                if debug_files:
                    debug_files.sort(key=lambda f: os.path.getmtime(os.path.join(debug_dir, f)))
                    
                    if len(debug_files) > 50:
                        for old_file in debug_files[:-50]:
                            try:
                                os.remove(os.path.join(debug_dir, old_file))
                            except:
                                pass
            except Exception as cleanup_error:
                print(f"   [Warning] Cleanup warning: {cleanup_error}")
            
            timestamp = datetime.now().strftime("%H%M%S")
            debug_image = screenshot.copy()
            
            if buttons:
                for bx, by, bw, bh, btn_text in buttons:
                    cv2.rectangle(debug_image, (bx, by), (bx+bw, by+bh), (255, 100, 0), 2)
                    cv2.putText(debug_image, btn_text[:20], (bx, by-5), 
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 100, 0), 1)
            
            if matched_element:
                x, y, w, h, text = matched_element
                cv2.rectangle(debug_image, (x, y), (x+w, y+h), (0, 255, 0), 4)
                cv2.putText(debug_image, f"MATCH: {text}", (x, y-10), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            text_y = debug_image.shape[0] - 30
            cv2.rectangle(debug_image, (0, text_y - 25), (debug_image.shape[1], debug_image.shape[0]), (0, 0, 0), -1)
            cv2.putText(debug_image, f"Searched: '{search_text}' | Mode: {mode}", (10, text_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            
            safe_search = "".join(c for c in search_text if c.isalnum() or c in (' ', '_'))[:30]
            safe_search = safe_search.replace(' ', '_')
            filename = os.path.join(debug_dir, f"{mode}_{timestamp}_{safe_search}.png")
            
            success = cv2.imwrite(filename, debug_image)
            if success:
                print(f"   [Disk] Debug saved: {filename}")
            else:
                print(f"   [X] Failed to save debug screenshot")
            
        except Exception as e:
            print(f"   [Warning] Debug screenshot error: {e}")

    def read_screen_text(self):
        """Fast screen reading"""
        if not OCR_AVAILABLE:
            return "OCR not available"
        
        try:
            screenshot = pyautogui.screenshot()
            width, height = screenshot.size
            
            cropped = screenshot.crop((
                width // 3, 
                height // 3 + 50,
                2 * width // 3, 
                2 * height // 3
            ))

            image_np = np.array(cropped)
            image_np = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)

            text, _, _ = self.auto_psm_ocr(image_np, action="read", enhance=False)
            
            self.last_screenshot_text = text
            return text if text.strip() else "No readable text"
            
        except Exception as e:
            return f"OCR error: {str(e)}"

    def click_on_text(self, search_text):
        """Click with button detection"""
        coords = self.find_text_on_screen(search_text)
        if coords:
            x, y, w, h = coords
            click_x = x + w // 2
            click_y = y + h // 2
            
            print(f"   [Mouse] Clicking at ({click_x}, {click_y})")
            pyautogui.moveTo(click_x, click_y, duration=0.3)
            time.sleep(0.1)
            pyautogui.click()
            
            self.track_action(f"clicked '{search_text}'")
            return True
        return False

    def get_active_window(self):
        try:
            import win32gui

            window = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(window)
            self.active_window = title
            return title
        except:
            return "Unknown"

    def auto_psm_ocr(self, image, action="read", enhance=True):
        """Fast OCR with optional enhancement"""
        try:
            if len(image.shape) == 3 and image.shape[2] == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image

            if enhance:
                gray = cv2.resize(gray, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
                gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
                gray = cv2.bilateralFilter(gray, 5, 50, 50)

            if action == "button":
                psm = 11
            elif action == "line":
                psm = 7
            elif action == "read":
                psm = 6
            else:
                psm = 3

            config = f"--oem 3 --psm {psm}"
            text = pytesseract.image_to_string(gray, config=config)

            return text, 0, psm

        except Exception as e:
            print(f"   [X] OCR error: {e}")
            return "", 0, 3

    def get_running_apps(self):
        try:
            apps = []
            targets = [
                "firefox",
                "chrome",
                "discord",
                "spotify",
                "vscode",
                "code",
                "excel",
                "word",
                "notepad",
                "explorer",
                "steam",
                "edge",
            ]
            for proc in psutil.process_iter(["name"]):
                try:
                    name = proc.info["name"].lower()
                    if name.endswith(".exe"):
                        name = name[:-4]
                    if any(t in name for t in targets) and name not in apps:
                        apps.append(name)
                except:
                    continue
            return apps[:10]
        except:
            return []

    def get_context_summary(self):
        summary_parts = []
        active = self.get_active_window()
        if active and active != "Unknown":
            summary_parts.append(f"Current window: {active}")
        if self.actions_performed:
            recent = list(self.actions_performed)[-5:]
            summary_parts.append(f"Recent actions: {', '.join(recent)}")
        if self.opened_apps:
            summary_parts.append(f"Apps opened: {', '.join(self.opened_apps)}")
        if self.context.get("last_action"):
            summary_parts.append(f"Last action: {self.context['last_action']}")
        running = self.get_running_apps()
        if running:
            summary_parts.append(f"Running apps: {', '.join(running[:5])}")
        return " | ".join(summary_parts) if summary_parts else "No context"

    def focus_app_windows(self, app_name):
        """Focus an application window"""
        try:
            import win32gui, win32con

            app_name = app_name.lower()

            def callback(hwnd, windows):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd).lower()
                    if app_name in title:
                        windows.append((hwnd, title))
                return True

            windows = []
            win32gui.EnumWindows(callback, windows)

            if windows:
                windows.sort(key=lambda x: len(x[1]))
                hwnd = windows[0][0]

                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)

                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.3)

                return True

            return False
        except Exception as e:
            print(f"Focus error: {e}")
            return False

    def add_to_history(self, role, message):
        self.conversation_history.append(
            {"role": role, "message": message, "timestamp": datetime.now().isoformat()}
        )
        if len(self.conversation_history) > 20:
            self.conversation_history = self.conversation_history[-20:]

    def track_action(self, action):
        self.actions_performed.append(action)
        self.context["last_action"] = action
        if len(self.actions_performed) > 10:
            self.actions_performed = self.actions_performed[-10:]

    def get_conversation_context(self):
        if not self.conversation_history:
            return "No previous conversation"
        return "\n".join(
            [f"{m['role']}: {m['message']}" for m in self.conversation_history[-8:]]
        )

    def get_memory_stats(self):
        return {
            "conversation_length": len(self.conversation_history),
            "actions_performed": len(self.actions_performed),
            "apps_opened": len(self.opened_apps),
            "active_window": self.active_window or "Unknown",
            "wake_word_active": self.wake_word_active,
            "proactive_mode": self.proactive_mode,
            "captions_enabled": self.captions_enabled,
        }

    def sync_opened_apps(self):
        try:
            for app in self.get_running_apps():
                self.opened_apps.add(app)
        except:
            pass

    def check_api_status(self):
        try:
            response = requests.get(STATUS_URL, timeout=2)
            data = response.json()
            return {
                "online": data.get("status") == "online",
                "reason": data.get("down_reason", ""),
            }
        except:
            return {"online": False, "reason": "Connection error"}

    def ask_mistai(self, message, model="gemini", gender="none"):
        self.sync_opened_apps()
        try:
            context_summary = self.get_context_summary()
            active_window = self.get_active_window()

            conversation_context = (
                "\n".join(
                    [
                        f"{msg['role']}: {msg['message']}"
                        for msg in self.conversation_history[-5:]
                    ]
                )
                if self.conversation_history
                else "No previous conversation"
            )

            screen_context = ""
            if OCR_AVAILABLE:
                print("   [Camera] Taking fresh screenshot for context...")
                screen_text = self.read_screen_text()
                screen_context = f"\nVISIBLE ON SCREEN RIGHT NOW: {screen_text[:500]}"
                
                buttons = self.find_buttons_on_screen()
                if buttons:
                    button_texts = [btn[4] for btn in buttons[:15]]
                    screen_context += f"\nVISIBLE BUTTONS: {', '.join(button_texts)}"

            system_prompt = f"""You are MistAI, a Jarvis-like desktop AI assistant created by Kristian. You control the user's computer through vision and actions.

IDENTITY & PERSONALITY:
You are confident, capable, and natural — like a skilled digital partner who actually *sees* what's happening on screen and can take action. You're not a robotic yes-bot; you're proactive, observant, and occasionally witty.

- Speak naturally and conversationally — "Got it", "Opening that now", "I see you're on YouTube"
- Show awareness of what you observe: "I can see Firefox is already open", "Looks like the page loaded"
- Be efficient: don't over-explain unless asked
- Use light humor when appropriate, but stay focused on the task
- If something goes wrong, admit it plainly: "Couldn't find that text on screen" or "Firefox didn't open — want me to try again?"

COMMUNICATION STYLE:
- Keep responses SHORT (1-2 sentences in "speech" field)
- Sound human: "Sure thing" not "Affirmative, executing command"
- Be direct: "Searching YouTube for robotics tutorials" not "I will now proceed to search..."
- React to context: If they just asked you to do something similar, acknowledge it
- NO excessive enthusiasm or emoji spam (one emoji MAX if it fits)

CURRENT SITUATION:
Active Window: {active_window}
Running Apps: {', '.join(self.get_running_apps())}
Context: {context_summary}

WHAT YOU CAN SEE RIGHT NOW (FRESH OCR):
{screen_context}

RECENT CONVERSATION:
{conversation_context}

CRITICAL RULES FOR CLICKING:
1. **If user asks to click on something**: 
- Check if it's in "VISIBLE ON SCREEN RIGHT NOW" or "VISIBLE BUTTONS"
- If YES -> use click_on_text action
- If NO -> use click_on_text anyway and let OCR try harder (it can see more than the preview)
- NEVER say "I don't see it" without trying click_on_text first

2. **Trust your vision system**:
- The screen context shows a PREVIEW (first 500 chars)
- OCR can see the ENTIRE screen when you use click_on_text
- Always attempt the action first, apologize only if it fails

3. **Example responses**:
WRONG: {{"action": "none", "speech": "I don't see that username"}}
RIGHT: {{"action": "click_on_text", "parameter": "Fulvex", "speech": "Looking for Fulvex"}}

AVAILABLE ACTIONS:
- open_app: Open/focus application (firefox, chrome, discord, notepad, etc.)
- type_search: Type text into active field and press enter
- click_on_text: Find visible text on screen via OCR and click it <- USE THIS WHEN USER ASKS TO CLICK
- press_key: Press a keyboard key (enter, escape, tab, etc.)
- click: Click at current mouse position
- scroll: Scroll up or down
- volume: Control system volume (up, down, mute)
- fullscreen: Toggle fullscreen (F11)
- maximize: Maximize current window (Win + Up)
- multi_step: Chain multiple actions together
- none: Just talk, no action needed (ONLY use this for questions/chat, NOT for click requests)

USER REQUEST:
"{message}"

RESPONSE FORMAT (JSON only, no markdown):
{{"action": "...", "parameter": "...", "speech": "..."}}

REMEMBER:
- When user says "click on X" -> ALWAYS try click_on_text first
- The screen preview is LIMITED - OCR can see more than what's shown
- Be ACTION-FIRST, not cautious
- You're a DO-er, not a "let me check first"-er"""

            response = requests.post(
                API_URL,
                json={"message": system_prompt, "model": model, "mode": "assistant"},
                timeout=30,
            )

            if response.ok:
                ai_response = response.json().get("response", "")
                try:
                    json_str = ai_response
                    if "```json" in json_str:
                        json_str = json_str.split("```json")[1].split("```")[0]
                    elif "```" in json_str:
                        json_str = json_str.split("```")[1].split("```")[0]
                    command = json.loads(json_str.strip())
                    self.add_to_history("user", message)
                    self.add_to_history("assistant", command.get("speech", ""))
                    return {"success": True, "command": command}
                except:
                    self.add_to_history("user", message)
                    self.add_to_history("assistant", ai_response)
                    return {
                        "success": True,
                        "command": {
                            "action": "none",
                            "parameter": "",
                            "speech": ai_response,
                        },
                    }
            return {"success": False, "error": "API failed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def execute_action(self, action_type, parameter, speech=""):
        """Execute action with FULL caption support"""

        def run():
            try:
                print(f"\n[Wrench] EXECUTING ACTION: {action_type} | Param: {parameter}")

                if action_type == "multi_step":
                    if isinstance(parameter, list):
                        if self.captions_enabled:
                            self.show_caption(f"Starting multi-step task...", "assistant")
                        
                        for i, step in enumerate(parameter):
                            if isinstance(step, str):
                                step = {"action": step, "parameter": ""}
                            elif not isinstance(step, dict):
                                continue

                            action_name = step.get("action", "none")
                            action_param = step.get("parameter", "")

                            print(f"\n[Pin] Step {i+1}/{len(parameter)}: {action_name} - {action_param}")
                            
                            if self.captions_enabled:
                                step_caption = f"Step {i+1}/{len(parameter)}: {self._get_action_caption(action_name, action_param)}"
                                self.show_caption(step_caption, "assistant")

                            success = self.execute_action_sync(action_name, action_param)

                            print(f"   {'[Check]' if success else '[X]'} Step {i+1}")

                            if not success and action_name in ["open_app", "click_on_text", "type_search"]:
                                if self.captions_enabled:
                                    self.show_caption(f"❌ Step {i+1} failed, stopping", "assistant")
                                if speech:
                                    self.speak_now("Sorry, I couldn't complete that task.")
                                return

                            if action_name == "open_app":
                                time.sleep(2.0)
                                if OCR_AVAILABLE:
                                    self.last_screenshot_text = self.read_screen_text()

                            elif action_name == "click_on_text":
                                time.sleep(1.5)
                                if OCR_AVAILABLE:
                                    self.last_screenshot_text = self.read_screen_text()

                            elif action_name == "type_search":
                                time.sleep(1.3)

                            elif action_name in ["press_key", "click"]:
                                time.sleep(0.5)
                            else:
                                time.sleep(0.8)

                        if self.captions_enabled:
                            self.show_caption("✅ Task completed!", "assistant")
                        
                        if speech:
                            self.speak_now(speech, interrupt=False)
                        return

                self.execute_action_sync(action_type, parameter)

                if speech and action_type != "multi_step":
                    self.speak_now(speech, interrupt=False)

            except Exception as e:
                print(f"[X] Action error: {e}")
                if self.captions_enabled:
                    self.show_caption(f"❌ Error: {str(e)[:50]}", "assistant")

        threading.Thread(target=run, daemon=True).start()
        return {"success": True}

    def execute_action_sync(self, action_type, parameter):
        """Execute single action with MistAI-powered recovery"""
        try:
            print(f"\n[Target] SYNC ACTION: {action_type} | Param: {parameter}")
            
            if self.captions_enabled:
                action_caption = self._get_action_caption(action_type, parameter)
                self.show_caption(action_caption, "assistant")

            if action_type == "click_on_text":
                print(f"[Mouse] Clicking on text: {parameter}")
                
                if OCR_AVAILABLE:
                    print(f"   [Search] Searching for text...")
                    
                    result = self.click_on_text(parameter)
                    
                    if result:
                        print(f"   [Check] Found and clicked '{parameter}'")
                        if self.captions_enabled:
                            self.show_caption(f"✅ Clicked '{parameter}'", "assistant")
                        time.sleep(0.8)
                        return True
                    
                    print(f"   [X] Could not find '{parameter}'")
                    if self.captions_enabled:
                        self.show_caption(f"❌ Can't find '{parameter}', thinking...", "assistant")
                    
                    screen_text = self.read_screen_text()
                    buttons = self.find_buttons_on_screen()
                    button_texts = [btn[4] for btn in buttons] if buttons else []
                    
                    print(f"   [Brain] Asking MistAI for recovery strategy...")
                    
                    recovery_result = self._ask_for_recovery(
                        failed_action="click_on_text",
                        failed_parameter=parameter,
                        screen_text=screen_text,
                        available_buttons=button_texts
                    )
                    
                    if recovery_result and recovery_result.get("success"):
                        recovery_cmd = recovery_result.get("command", {})
                        recovery_action = recovery_cmd.get("action")
                        recovery_param = recovery_cmd.get("parameter")
                        recovery_speech = recovery_cmd.get("speech", "")
                        
                        print(f"   [Cycle] MistAI suggests: {recovery_action} | {recovery_param}")
                        
                        if self.captions_enabled and recovery_speech:
                            self.show_caption(recovery_speech, "assistant")
                        
                        if recovery_action == "click_on_text" and recovery_param != parameter:
                            print(f"   [Cycle] Trying alternative: '{recovery_param}'")
                            alt_result = self.click_on_text(recovery_param)
                            if alt_result:
                                print(f"   [Check] Recovery successful!")
                                if self.captions_enabled:
                                    self.show_caption(f"✅ Found it as '{recovery_param}'", "assistant")
                                return True
                        
                        elif recovery_action == "scroll":
                            print(f"   [Cycle] Scrolling {recovery_param}...")
                            if self.captions_enabled:
                                self.show_caption(f"Scrolling {recovery_param}...", "assistant")
                            
                            pyautogui.scroll(300 if recovery_param == "up" else -300)
                            time.sleep(1.5)
                            
                            retry_result = self.click_on_text(parameter)
                            if retry_result:
                                print(f"   [Check] Found after scrolling!")
                                if self.captions_enabled:
                                    self.show_caption(f"✅ Found '{parameter}' after scrolling", "assistant")
                                return True
                        
                        elif recovery_action == "none":
                            print(f"   [Warning] MistAI advises giving up")
                            if self.captions_enabled:
                                self.show_caption("❌ Couldn't complete this action", "assistant")
                            return False
                    
                    print(f"   [X] Recovery failed")
                    if self.captions_enabled:
                        self.show_caption(f"❌ Couldn't find '{parameter}'", "assistant")
                    return False
                
                else:
                    result = self.click_on_text(parameter)
                    return result

            elif action_type == "open_app":
                print(f"[Rocket] Opening app: {parameter}")
                if self.captions_enabled:
                    self.show_caption(f"Opening {parameter}...", "assistant")
                
                running_apps = [app.lower() for app in self.get_running_apps()]
                param_lower = parameter.lower()

                if param_lower in running_apps:
                    print(f"   App already running, focusing...")
                    if self.captions_enabled:
                        self.show_caption(f"Focusing {parameter}...", "assistant")
                    
                    if self.focus_app_windows(parameter):
                        self.track_action(f"focused {parameter}")
                        self.context["last_app_opened"] = parameter
                        
                        time.sleep(1.0)
                        active = self.get_active_window().lower()
                        if param_lower in active:
                            print(f"   [Check] VERIFIED: {parameter} is active")
                            if self.captions_enabled:
                                self.show_caption(f"✅ {parameter} is ready", "assistant")
                            
                            time.sleep(0.3)
                            pyautogui.hotkey("win", "up")
                        return True

                print(f"   Opening via Start menu...")
                pyautogui.press("win")
                time.sleep(0.7)
                
                pyautogui.write(parameter, interval=0.06)
                time.sleep(0.7)
                
                pyautogui.press("enter")
                time.sleep(2.8)
                
                active_window = self.get_active_window().lower()
                if param_lower in active_window:
                    print(f"   [Check] VERIFIED: {parameter} launched")
                    if self.captions_enabled:
                        self.show_caption(f"✅ {parameter} opened", "assistant")
                else:
                    print(f"   [Warning] App might not have opened")
                    if self.captions_enabled:
                        self.show_caption(f"⚠️ {parameter} might not have opened", "assistant")

                self.opened_apps.add(parameter)
                self.track_action(f"opened {parameter}")
                self.context["last_app_opened"] = parameter
                return True

            elif action_type == "type_search":
                print(f"[Keyboard] Typing: {parameter}")
                if self.captions_enabled:
                    self.show_caption(f"Typing: {parameter}", "assistant")
                
                time.sleep(0.7)
                pyautogui.write(str(parameter), interval=0.06)
                
                active_window_lower = self.get_active_window().lower()
                if "calculator" not in active_window_lower:
                    time.sleep(0.3)
                    pyautogui.press("enter")
                    time.sleep(1.2)
                
                self.track_action(f"typed '{parameter}'")
                
                if self.captions_enabled:
                    self.show_caption(f"✅ Typed '{parameter}'", "assistant")
                
                return True

            elif action_type == "scroll":
                scroll_amount = 300 if parameter == "up" else -300
                print(f"[Cycle] Scrolling {parameter}...")
                if self.captions_enabled:
                    self.show_caption(f"Scrolling {parameter}...", "assistant")
                
                pyautogui.scroll(scroll_amount)
                self.track_action(f"scrolled {parameter}")
                
                if self.captions_enabled:
                    self.show_caption(f"✅ Scrolled {parameter}", "assistant")
                return True

            elif action_type == "volume":
                print(f"[Speaker] Volume {parameter}...")
                if self.captions_enabled:
                    self.show_caption(f"Volume {parameter}", "assistant")
                
                pyautogui.press(f"volume{parameter}")
                self.track_action(f"volume {parameter}")
                
                if self.captions_enabled:
                    self.show_caption(f"✅ Volume {parameter}", "assistant")
                return True

            elif action_type == "press_key":
                print(f"[Keyboard] Pressing key: {parameter}")
                if self.captions_enabled:
                    self.show_caption(f"Pressing {parameter}...", "assistant")
                
                pyautogui.press(parameter)
                self.track_action(f"pressed {parameter}")
                
                if self.captions_enabled:
                    self.show_caption(f"✅ Pressed {parameter}", "assistant")
                return True

            elif action_type == "maximize":
                print(f"[Square] Maximizing window...")
                if self.captions_enabled:
                    self.show_caption("Maximizing window...", "assistant")
                
                pyautogui.hotkey("win", "up")
                self.track_action("maximized window")
                
                if self.captions_enabled:
                    self.show_caption("✅ Window maximized", "assistant")
                return True

            elif action_type == "fullscreen":
                print(f"[Window] Toggling fullscreen...")
                if self.captions_enabled:
                    self.show_caption("Toggling fullscreen...", "assistant")
                
                pyautogui.press("f11")
                self.track_action("toggled fullscreen")
                
                if self.captions_enabled:
                    self.show_caption("✅ Fullscreen toggled", "assistant")
                return True

            return True

        except Exception as e:
            print(f"[X] Action sync error: {e}")
            if self.captions_enabled:
                self.show_caption(f"❌ Error: {str(e)[:50]}", "assistant")
            return False
        
    def _get_action_caption(self, action_type, parameter):
        """Get user-friendly caption for action"""
        captions = {
            "open_app": f"Opening {parameter}...",
            "click_on_text": f"Looking for '{parameter}'...",
            "type_search": f"Typing '{parameter}'...",
            "scroll": f"Scrolling {parameter}...",
            "volume": f"Adjusting volume {parameter}...",
            "press_key": f"Pressing {parameter}...",
            "maximize": "Maximizing window...",
            "fullscreen": "Toggling fullscreen...",
        }
        return captions.get(action_type, f"Executing {action_type}...")

    def _ask_for_recovery(self, failed_action, failed_parameter, screen_text, available_buttons):
        """Ask MistAI for recovery strategy when action fails"""
        try:
            recovery_prompt = f"""You are MistAI. An action just FAILED and you need to suggest a recovery strategy.

FAILED ACTION: {failed_action}
FAILED PARAMETER: {failed_parameter}
REASON: Could not find this text/button on screen

CURRENT SCREEN STATE:
Available buttons detected: {', '.join(available_buttons[:10]) if available_buttons else 'None'}
Screen text (OCR): {screen_text[:400]}

RECOVERY OPTIONS:
1. **Try alternative text**: If you see a similar button/text, suggest clicking it instead
Example: User wanted "Open Discord" but you see "OpenDiscord" -> suggest that

2. **Scroll**: If the element might be off-screen, suggest scrolling up or down

3. **Give up**: If there's no viable alternative, return action: "none"

RESPONSE FORMAT (JSON only):
{{"action": "click_on_text", "parameter": "alternative text", "speech": "Trying 'alternative text' instead"}}
OR
{{"action": "scroll", "parameter": "down", "speech": "Scrolling to find it"}}
OR
{{"action": "none", "parameter": "", "speech": "Can't find that on screen"}}

Be smart and practical. What's the best recovery strategy?"""

            response = requests.post(
                API_URL,
                json={"message": recovery_prompt, "model": MODEL, "mode": "recovery"},
                timeout=10,
            )

            if response.ok:
                ai_response = response.json().get("response", "")
                try:
                    json_str = ai_response
                    if "```json" in json_str:
                        json_str = json_str.split("```json")[1].split("```")[0]
                    elif "```" in json_str:
                        json_str = json_str.split("```")[1].split("```")[0]
                    command = json.loads(json_str.strip())
                    return {"success": True, "command": command}
                except:
                    return {"success": False}
            
            return {"success": False}
            
        except Exception as e:
            print(f"   [X] Recovery request error: {e}")
            return {"success": False}

    def start_listening(self):
        if self.is_listening:
            return {"success": False}

        def listen_thread():
            self.is_listening = True
            self.recognizer = sr.Recognizer()
            try:
                with self.microphone as source:
                    self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                    audio = self.recognizer.listen(
                        source, timeout=5, phrase_time_limit=10
                    )
                    text = self.recognizer.recognize_google(audio)
                    self.window.evaluate_js(f'handleVoiceResult("{text}")')
            except sr.WaitTimeoutError:
                self.window.evaluate_js('handleVoiceError("No speech detected")')
            except sr.UnknownValueError:
                self.window.evaluate_js('handleVoiceError("Could not understand")')
            except Exception as e:
                self.window.evaluate_js(f'handleVoiceError("{str(e)}")')
            finally:
                self.is_listening = False
                self.window.evaluate_js("handleVoiceEnd()")

        threading.Thread(target=listen_thread, daemon=True).start()
        return {"success": True}

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MistAI Desktop Assistant</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            color: #fff; height: 100vh; overflow: hidden;
        }
        .container { display: flex; flex-direction: column; height: 100vh; padding: 20px; }
        .header { text-align: center; margin-bottom: 15px; }
        .logo-container { position: relative; width: 80px; height: 80px; margin: 0 auto 10px; }
        .ai-orb {
            width: 60px; height: 60px;
            background: linear-gradient(135deg, #667eea, #764ba2);
            border-radius: 50%; position: absolute; top: 50%; left: 50%;
            transform: translate(-50%, -50%);
            box-shadow: 0 0 30px rgba(102, 126, 234, 0.6);
            animation: float 3s ease-in-out infinite;
        }
        @keyframes float {
            0%, 100% { transform: translate(-50%, -50%) translateY(0); }
            50% { transform: translate(-50%, -50%) translateY(-10px); }
        }
        .ai-orb.active { animation: float 3s ease-in-out infinite, glow 1.5s ease-in-out infinite; }
        .ai-orb.listening {
            background: linear-gradient(135deg, #10b981, #059669);
            box-shadow: 0 0 50px rgba(16, 185, 129, 1);
        }
        @keyframes glow {
            0%, 100% { box-shadow: 0 0 30px rgba(102, 126, 234, 0.6); }
            50% { box-shadow: 0 0 50px rgba(102, 126, 234, 1); }
        }
        .title {
            font-size: 1.8em; font-weight: 700;
            background: linear-gradient(135deg, #667eea, #764ba2);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .subtitle { color: rgba(255,255,255,0.7); font-size: 0.8em; margin-top: 5px; }
        .memory-badge {
            display: inline-block; background: rgba(102, 126, 234, 0.2);
            border: 1px solid rgba(102, 126, 234, 0.4);
            padding: 4px 12px; border-radius: 15px;
            font-size: 0.7em; margin-top: 5px; color: #667eea;
        }
        .status-bar {
            display: flex; justify-content: space-between; align-items: center;
            background: rgba(255,255,255,0.05); padding: 10px 16px;
            border-radius: 10px; margin-bottom: 12px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .status-indicator { display: flex; align-items: center; gap: 8px; }
        .status-dot {
            width: 10px; height: 10px; border-radius: 50%;
            background: #fbbf24; animation: pulse-dot 1.5s ease-in-out infinite;
        }
        .status-dot.online { background: #10b981; animation: none; }
        @keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
        .settings-row { display: flex; gap: 8px; align-items: center; }
        .model-select, .gender-select {
            background: rgba(255,255,255,0.1); color: #fff;
            border: 1px solid rgba(255,255,255,0.2);
            padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 0.85em;
        }
        .gender-select {
            background: rgba(118, 75, 162, 0.2);
            border: 1px solid rgba(118, 75, 162, 0.4);
        }
        
        .toggle-container {
            display: flex; gap: 10px; align-items: center;
            background: rgba(255,255,255,0.05); padding: 8px 12px;
            border-radius: 10px; margin-bottom: 12px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .toggle-item {
            display: flex; align-items: center; gap: 8px;
            flex: 1;
        }
        .toggle-label {
            font-size: 0.75em;
            color: rgba(255,255,255,0.8);
        }
        .toggle-switch {
            position: relative;
            width: 40px;
            height: 22px;
            background: rgba(255,255,255,0.2);
            border-radius: 11px;
            cursor: pointer;
            transition: background 0.3s;
        }
        .toggle-switch.active {
            background: linear-gradient(135deg, #10b981, #059669);
        }
        .toggle-switch::after {
            content: '';
            position: absolute;
            width: 16px;
            height: 16px;
            background: white;
            border-radius: 50%;
            top: 3px;
            left: 3px;
            transition: left 0.3s;
        }
        .toggle-switch.active::after {
            left: 21px;
        }
        
        .chat-container {
            flex: 1; background: rgba(0,0,0,0.2); border-radius: 15px;
            padding: 15px; overflow-y: auto; margin-bottom: 12px;
            border: 1px solid rgba(255,255,255,0.1);
        }
        .chat-container::-webkit-scrollbar { width: 6px; }
        .chat-container::-webkit-scrollbar-thumb { background: rgba(102, 126, 234, 0.5); border-radius: 10px; }
        .message {
            margin-bottom: 10px; padding: 8px 12px; border-radius: 10px;
            max-width: 80%; word-wrap: break-word; animation: slideIn 0.3s ease; font-size: 0.9em;
        }
        @keyframes slideIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
        .message.user { background: linear-gradient(135deg, #667eea, #764ba2); margin-left: auto; text-align: right; }
        .message.assistant { background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); }
        .message.system { background: rgba(251,191,36,0.2); border: 1px solid rgba(251,191,36,0.4); text-align: center; margin: 0 auto; max-width: 100%; }
        .message.wake-word {
            background: rgba(16, 185, 129, 0.2);
            border: 1px solid rgba(16, 185, 129, 0.4);
            text-align: center;
            margin: 0 auto;
            max-width: 100%;
        }
        .message.suggestion {
            background: rgba(139, 92, 246, 0.2);
            border: 1px solid rgba(139, 92, 246, 0.4);
            margin-left: auto;
            font-style: italic;
        }
        .input-container { display: flex; gap: 8px; }
        .input-wrapper { flex: 1; display: flex; gap: 8px; }
        #text-input {
            flex: 1; background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2); color: #fff;
            padding: 10px 14px; border-radius: 10px; font-size: 0.9em;
        }
        #text-input:focus { outline: none; border-color: #667eea; }
        .btn {
            width: 40px; height: 40px; border-radius: 50%; border: none;
            cursor: pointer; display: flex; align-items: center; justify-content: center;
            transition: transform 0.3s; color: #fff; font-size: 18px;
        }
        .btn:hover:not(:disabled) { transform: scale(1.1); }
        .btn-voice { background: linear-gradient(135deg, #10b981, #059669); }
        .btn-voice.listening { background: linear-gradient(135deg, #ef4444, #dc2626); animation: pulse-btn 1s ease-in-out infinite; }
        @keyframes pulse-btn { 0%, 100% { box-shadow: 0 0 0 0 rgba(239,68,68,0.7); } 50% { box-shadow: 0 0 0 15px rgba(239,68,68,0); } }
        .btn-send { background: linear-gradient(135deg, #667eea, #764ba2); }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .welcome-message { text-align: center; padding: 30px 15px; color: rgba(255,255,255,0.5); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo-container"><div class="ai-orb" id="ai-orb"></div></div>
            <h1 class="title">MistAI Desktop Assistant</h1>
            <p class="subtitle">Your Jarvis-like AI companion</p>
            <div class="memory-badge" id="memory-badge">🧠 Loading...</div>
        </div>
        
        <div class="toggle-container">
            <div class="toggle-item">
                <span class="toggle-label">🎤 Wake</span>
                <div class="toggle-switch" id="wake-word-toggle"></div>
            </div>
            <div class="toggle-item">
                <span class="toggle-label">👁️ Proactive</span>
                <div class="toggle-switch" id="proactive-toggle"></div>
            </div>
            <div class="toggle-item">
                <span class="toggle-label">📺 Captions</span>
                <div class="toggle-switch" id="captions-toggle"></div>
            </div>
        </div>
        
        <div class="status-bar">
            <div class="status-indicator">
                <div class="status-dot" id="status-dot"></div>
                <span id="status-text" style="font-size: 0.85em;">Checking...</span>
            </div>
            <div class="settings-row">
                <select class="gender-select" id="gender-select">
                    <option value="none">Prefer not to say</option>
                    <option value="male">Male (Sir)</option>
                    <option value="female">Female (Ma'am)</option>
                </select>
                <select class="model-select" id="model-select">
                    <option value="gemini" selected>Gemini</option>
                    <option value="cohere">Cohere</option>
                    <option value="mistral">Mistral</option>
                </select>
            </div>
        </div>
        
        <div class="chat-container" id="chat-container">
            <div class="welcome-message">
                <div style="font-size: 2.5em; margin-bottom: 12px;">✨ MistAI Ready</div>
                <p><strong>All systems operational</strong></p>
                <p style="font-size: 0.85em; margin-top: 8px;">Wake word, Captions, & Proactive mode available</p>
            </div>
        </div>
        
        <div class="input-container">
            <div class="input-wrapper">
                <input type="text" id="text-input" placeholder="Ask me anything..." autocomplete="off"/>
                <button class="btn btn-send" id="send-btn">▶</button>
            </div>
        </div>
    </div>
    
   <script>
    let currentModel = 'gemini', isProcessing = false, userGender = 'none';
    let wakeWordActive = false, proactiveMode = false, captionsEnabled = false;
    let pywebviewReady = false;
    
    const chat = document.getElementById('chat-container');
    const input = document.getElementById('text-input');
    const sendBtn = document.getElementById('send-btn');
    const modelSelect = document.getElementById('model-select');
    const genderSelect = document.getElementById('gender-select');
    const statusDot = document.getElementById('status-dot');
    const statusText = document.getElementById('status-text');
    const aiOrb = document.getElementById('ai-orb');
    const memoryBadge = document.getElementById('memory-badge');
    const wakeWordToggle = document.getElementById('wake-word-toggle');
    const proactiveToggle = document.getElementById('proactive-toggle');
    const captionsToggle = document.getElementById('captions-toggle');

    window.addEventListener('pywebviewready', async function() {
        console.log('[Check] Pywebview ready event fired');
        pywebviewReady = true;
        await init();
    });

    async function init() {
        console.log('[Cycle] Initializing...');
        
        if (!window.pywebview || !window.pywebview.api) {
            console.error('[X] pywebview.api not available!');
            addMessage('❌ System initialization failed', 'system');
            return;
        }
        
        console.log('[Check] pywebview.api available');
        
        const savedGender = localStorage.getItem('userGender');
        if (savedGender) {
            userGender = savedGender;
            genderSelect.value = savedGender;
            await pywebview.api.set_user_gender(savedGender);
        }
        
        checkStatus();
        updateMemoryBadge();
        setInterval(checkStatus, 30000);
        setInterval(updateMemoryBadge, 5000);
    }

    sendBtn.addEventListener('click', handleSend);
    
    modelSelect.addEventListener('change', (e) => {
        currentModel = e.target.value;
        addMessage(`Switched to ${e.target.options[e.target.selectedIndex].text}`, 'system');
    });
    
    genderSelect.addEventListener('change', async (e) => {
        if (!pywebviewReady) return;
        userGender = e.target.value;
        localStorage.setItem('userGender', userGender);
        await pywebview.api.set_user_gender(userGender);
        addMessage(`Gender preference set`, 'system');
    });
    
    input.addEventListener('keypress', (e) => {
        if (e.key === 'Enter' && !isProcessing) handleSend();
    });
    
    wakeWordToggle.addEventListener('click', async () => {
        if (!pywebviewReady) {
            addMessage('⚠️ System not ready yet', 'system');
            return;
        }
        wakeWordActive = !wakeWordActive;
        wakeWordToggle.classList.toggle('active', wakeWordActive);
        if (wakeWordActive) {
            await pywebview.api.start_wake_word_detection();
            addMessage('🎤 Wake word detection enabled', 'system');
            aiOrb.classList.add('listening');
        } else {
            await pywebview.api.stop_wake_word_detection();
            addMessage('🎤 Wake word detection disabled', 'system');
            aiOrb.classList.remove('listening');
        }
    });
    
    proactiveToggle.addEventListener('click', async () => {
        if (!pywebviewReady) {
            addMessage('⚠️ System not ready yet', 'system');
            return;
        }
        proactiveMode = !proactiveMode;
        proactiveToggle.classList.toggle('active', proactiveMode);
        await pywebview.api.toggle_proactive_mode(proactiveMode);
        addMessage(proactiveMode ? '👁️ Proactive mode enabled' : '👁️ Proactive mode disabled', 'system');
    });
    
    captionsToggle.addEventListener('click', async () => {
        if (!pywebviewReady) {
            addMessage('⚠️ System not ready yet', 'system');
            return;
        }
        captionsEnabled = !captionsEnabled;
        captionsToggle.classList.toggle('active', captionsEnabled);
        await pywebview.api.toggle_captions(captionsEnabled);
        addMessage(captionsEnabled ? '📺 Captions enabled (bot/system only)' : '📺 Captions disabled', 'system');
    });

    async function checkStatus() {
        if (!pywebviewReady || !window.pywebview) return;
        try {
            const result = await pywebview.api.check_api_status();
            statusDot.className = `status-dot ${result.online ? 'online' : ''}`;
            statusText.textContent = result.online ? 'Connected' : 'Offline';
            if (!wakeWordActive) {
                aiOrb.classList.toggle('active', result.online);
            }
        } catch (error) { 
            console.error('Status check error:', error);
            statusDot.className = 'status-dot';
            statusText.textContent = 'Error';
        }
    }

    async function updateMemoryBadge() {
        if (!pywebviewReady || !window.pywebview) return;
        try {
            const stats = await pywebview.api.get_memory_stats();
            let badges = `🧠 ${stats.conversation_length} | ${stats.actions_performed} acts`;
            if (stats.wake_word_active) badges += ' | 🎤';
            if (stats.proactive_mode) badges += ' | 👁️';
            if (stats.captions_enabled) badges += ' | 📺';
            memoryBadge.textContent = badges;
        } catch (e) {
            console.error('Memory badge error:', e);
        }
    }

    async function handleSend() {
        if (!pywebviewReady) {
            addMessage('⚠️ System not ready - please wait', 'system');
            return;
        }
        
        if (!window.pywebview || !window.pywebview.api) {
            addMessage('❌ System error - pywebview not available', 'system');
            return;
        }
        
        if (typeof pywebview.api.ask_mistai !== 'function') {
            addMessage('❌ ask_mistai method not found', 'system');
            return;
        }
        
        const msg = input.value.trim();
        if (!msg || isProcessing) return;
        
        try {
            await pywebview.api.minimize_window();
        } catch (e) {
            console.error('Minimize error:', e);
        }
        
        input.value = '';
        isProcessing = true;
        sendBtn.disabled = input.disabled = true;

        addMessage(msg, 'user');

        try {
            const result = await pywebview.api.ask_mistai(msg, currentModel, userGender);
            
            if (result.success && result.command) {
                const cmd = result.command;
                await pywebview.api.execute_action(cmd.action, cmd.parameter, cmd.speech);
                if (cmd.speech) {
                    addMessage(cmd.speech, 'assistant');
                }
            } else {
                addMessage(`Error: ${result.error || 'Unknown error'}`, 'system');
            }
            updateMemoryBadge();
        } catch (e) {
            console.error('[X] Error in handleSend:', e);
            addMessage(`Error: ${e}`, 'system');
        } finally {
            isProcessing = false;
            sendBtn.disabled = input.disabled = false;
            input.focus();
        }
    }
    
    function handleWakeWordDetected() {
        addMessage('🎤 Wake word detected', 'wake-word');
    }
    
    function handleWakeCommand(command, response) {
        addMessage(command, 'user');
        if (response) addMessage(response, 'assistant');
    }
    
    function handleProactiveSuggestion(suggestion) {
        addMessage('💡 ' + suggestion, 'suggestion');
    }

    function addMessage(text, type) {
        const w = chat.querySelector('.welcome-message');
        if (w) w.remove();
        const div = document.createElement('div');
        div.className = `message ${type}`;
        div.textContent = text;
        chat.appendChild(div);
        chat.scrollTop = chat.scrollHeight;
    }
</script>
</body>
</html>
"""

def main():
    """Main entry point for the assistant"""
    # Show splash screen first (only if display available)
    if os.name == 'nt' or os.environ.get('DISPLAY'):
        print("[Splash] Showing splash screen...")
        show_splash_screen()
    
    print(f"\n[Rocket] Starting MistAI v{VERSION}...")
    
    api = Api()

    window = webview.create_window(
        f"MistAI Desktop Assistant v{VERSION}",
        html=HTML_CONTENT,
        js_api=api,
        width=800,
        height=700,
        resizable=True,
    )
    api.window = window

    webview.start(debug=False, gui="edgechromium")

if __name__ == "__main__":
    main()