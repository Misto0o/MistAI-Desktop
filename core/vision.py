"""
core/vision.py — Cohere multimodal vision: screen understanding & element finding.
"""

import io
import os
import threading
import time

import pyautogui
from PIL import Image, ImageGrab

try:
    import cohere
    COHERE_AVAILABLE = True
except ImportError:
    COHERE_AVAILABLE = False


class CohereVision:
    MODEL = "command-a-vision-07-2025"

    def __init__(self):
        self._client = None
        self._lock = threading.Lock()
        self._cache = ""
        self._cache_time = 0
        self._cache_ttl = 3.0
        key = os.environ.get("COHERE_API_KEY", "")
        if key and COHERE_AVAILABLE:
            self._init(key)

    def _init(self, key):
        try:
            self._client = cohere.ClientV2(key)
            print(f"[Vision] Cohere {self.MODEL} ready")
        except Exception as e:
            print(f"[Vision] Init error: {e}")
            self._client = None

    def set_api_key(self, key):
        if COHERE_AVAILABLE and key:
            self._init(key)

    @property
    def ready(self):
        return self._client is not None

    def _b64(self, frame):
        import base64
        buf = io.BytesIO()
        w, h = frame.size
        if w > 1920:
            frame = frame.resize((1920, int(h * 1920 / w)), Image.LANCZOS)
        frame.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _call(self, prompt, frame=None):
        if not self.ready:
            return ""
        try:
            content = (
                [
                    {"type": "image", "image": f"data:image/png;base64,{self._b64(frame)}"},
                    {"type": "text", "text": prompt},
                ]
                if frame
                else prompt
            )
            resp = self._client.chat(
                model=self.MODEL,
                messages=[{"role": "user", "content": content}],
                max_tokens=600,
            )
            return resp.message.content[0].text.strip()
        except Exception as e:
            print(f"[Vision] call error: {e}")
            return ""

    def understand_screen(self, frame=None, use_cache=True):
        if not self.ready:
            return "Vision engine not ready"

        now = time.time()
        if use_cache:
            with self._lock:
                if self._cache and (now - self._cache_time) < self._cache_ttl:
                    return self._cache

        shot = frame or ImageGrab.grab()

        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                r = self._call(
                    "You are looking at a Windows desktop. IGNORE any window titled "
                    "'MistAI' or showing text related to a MistAI chat interface -- that "
                    "is the assistant's own control panel, never a target to interact "
                    "with. Describe the OTHER application(s) visible for automation:\n"
                    "1. What application is open?\n"

                    "2. Is there a dialog box (Save, Open, etc)? If yes, what are the fields?\n"
                    "3. What buttons are visible?\n"
                    "4. What text/input is visible?\n"
                    "Focus on: dialogs, buttons, input fields, important text.",
                    shot,
                )
                if r and len(r) > 5:
                    with self._lock:
                        self._cache = r
                        self._cache_time = now
                    print(f"[Vision] Screen understood ({len(r)} chars)")
                    return r
            except Exception as e:
                print(f"[Vision] Attempt {attempt + 1} failed: {e}")
                time.sleep(0.5)

        return "Screen captured but Vision unavailable. Recommend using OCR or UIA fallback."

    def find_element_coords(self, description, frame=None):
        if not self.ready:
            return None
        shot = frame or ImageGrab.grab()
        sw, sh = pyautogui.size()
        result = self._call(
            f"Find '{description}' on this screen.\n"
            f"Screen resolution: {sw}x{sh}.\n"
            "Reply with ONLY: x,y (pixel coordinates of the center of that element).\n"
            "If not found, reply: NOT_FOUND\n"
            "No explanation. Just coordinates or NOT_FOUND.",
            shot,
        )
        if not result or "NOT_FOUND" in result.upper():
            return None
        try:
            parts = result.strip().replace(" ", "").split(",")
            x, y = int(parts[0]), int(parts[1])
            if 0 <= x <= sw and 0 <= y <= sh:
                return (x, y)
        except Exception:
            pass
        return None

    def find_nth_element_coords(self, item_type, n, frame=None):
        if not self.ready:
            return None, None
        shot = frame or ImageGrab.grab()
        sw, sh = pyautogui.size()
        ordinals = [
            "first", "second", "third", "fourth", "fifth",
            "sixth", "seventh", "eighth", "ninth", "tenth",
        ]
        ordinal = ordinals[n - 1] if 1 <= n <= len(ordinals) else f"#{n}"
        result = self._call(
            f"Find the {ordinal} {item_type} on this screen.\n"
            f"Screen resolution: {sw}x{sh}.\n"
            "Reply with TWO lines:\n"
            "TITLE: <exact full title of the item>\n"
            "COORDS: x,y\n"
            "If not found, reply: NOT_FOUND",
            shot,
        )
        if not result or "NOT_FOUND" in result.upper():
            return None, None
        title, coords = None, None
        for line in result.splitlines():
            line = line.strip()
            if line.upper().startswith("TITLE:"):
                title = line.split(":", 1)[1].strip()
            elif line.upper().startswith("COORDS:"):
                try:
                    parts = line.split(":", 1)[1].strip().replace(" ", "").split(",")
                    x, y = int(parts[0]), int(parts[1])
                    if 0 <= x <= sw and 0 <= y <= sh:
                        coords = (x, y)
                except Exception:
                    pass
        return title, coords

    def is_screen_ready(self, context, frame=None):
        if not self.ready:
            return True
        shot = frame or ImageGrab.grab()
        r = self._call(
            f"Context: {context}\n"
            "Is this screen fully loaded? Look for loading spinners, progress bars, or 'Loading...' text.\n"
            "Reply with ONLY: READY or LOADING",
            shot,
        )
        return "READY" in r.upper() if r else True
