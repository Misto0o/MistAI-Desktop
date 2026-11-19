# ============================================================
# SCREEN PERCEPTION MODULE
# ============================================================
#
# How MistAI sees the screen: the frame buffer, Cohere vision, and the
# exact accessibility-tree text of native windows.
#
# SECTION INDEX
#   1. OPTIONAL CAPTURE LIBRARY
#   2. FRAME BUFFER
#   3. VISION
#   4. OPTIONAL VISION LIBRARY
#   5. COHERE VISION CLIENT
#   6. ACCESSIBILITY TEXT
#   7. ACCESSIBILITY TEXT SETTINGS
#   8. ACCESSIBILITY TREE WALK
#   9. WINDOW PROCESS NAME
#   10. SLOW WINDOW MEMORY
#   11. VISUAL QUESTIONS
#   12. SUFFICIENT TEXT CHECK
#   13. FOREGROUND WINDOW TEXT
# ============================================================

"""
core/screen.py — continuously-updated screen capture buffer.
"""

import threading
import time
import io
import os
import re

import pyautogui
from PIL import Image, ImageGrab

from .config import secret_text_reason, verbose_logs


# ============================================================
# OPTIONAL CAPTURE LIBRARY
# ============================================================

try:
    import cv2
    import numpy as np
    from PIL import ImageGrab
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False
    from PIL import ImageGrab


# ============================================================
# FRAME BUFFER
# ============================================================

class ScreenBuffer:
    """Captures the screen continuously. A vision read asks frame_for_read(),
    which only hands out a buffered frame captured AFTER the last UI action
    (or phase start, see mark_action) and otherwise takes a fresh capture --
    a frame from before the action shows a screen that no longer exists."""

    def __init__(self, fps=4):
        self._pil = self._cv = None
        self._time = 0.0  # monotonic time the buffered frame's capture started
        self.last_action_at = 0.0  # mark_action(): last UI action / phase start
        self._lock = threading.Lock()
        self._running = True
        self._fps = fps
        t = threading.Thread(target=self._loop, daemon=True, name="ScreenBuffer")
        t.start()
        for _ in range(40):
            if self._pil:
                break
            time.sleep(0.05)
        print(f"[ScreenBuffer] {fps}fps")

    def _loop(self):
        iv = 1.0 / max(self._fps, 1)
        while self._running:
            try:
                t0 = time.monotonic()
                pil = ImageGrab.grab()
                cv = None
                if OCR_AVAILABLE:
                    cv = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
                with self._lock:
                    self._pil, self._cv, self._time = pil, cv, t0
            except Exception as e:
                print(f"[ScreenBuffer] {e}")
            time.sleep(iv)

    @property
    def frame(self):
        with self._lock:
            return self._pil

    def mark_action(self):
        """Something may have changed the screen: frames captured before now
        are not handed to vision reads any more."""
        self.last_action_at = time.monotonic()

    def frame_for_read(self, label="vision"):
        """The frame for a vision read: the buffered one if it was captured
        after the last mark_action(), else a fresh capture (~20-30ms at
        1080p). None if a fresh capture fails -- never a frame from before
        the action. Logs the age of whatever is used."""
        with self._lock:
            pil, t = self._pil, self._time
        now = time.monotonic()
        age = "none buffered" if pil is None else f"buffered frame {now - t:.2f}s old"
        if pil is not None and t > self.last_action_at:
            print(f"[Frame] {label}: {age} (newer than the last action)")
            return pil
        t0 = time.monotonic()
        try:
            fresh = ImageGrab.grab()
        except Exception as e:
            print(f"[Frame] {label}: fresh capture failed ({e}); {age}, older than the "
                  "last action -- not used")
            return None
        print(f"[Frame] {label}: fresh capture ({(time.monotonic() - t0) * 1000:.0f}ms); "
              f"{age}, older than the last action")
        return fresh

    @property
    def cv_frame(self):
        with self._lock:
            return self._cv

    def stop(self):
        self._running = False


# ============================================================
# VISION
# ============================================================

# core/vision.py — Cohere multimodal vision: screen understanding & element finding.

# ============================================================
# OPTIONAL VISION LIBRARY
# ============================================================

try:
    import cohere
    COHERE_AVAILABLE = True
except ImportError:
    COHERE_AVAILABLE = False


# ============================================================
# COHERE VISION CLIENT
# ============================================================

class CohereVision:
    MODEL = "command-a-vision-07-2025"
    # Frames wider than this are scaled down before they are sent; coordinate
    # answers are in the pixels of the image sent and are scaled back.
    MAX_SEND_WIDTH = 1920

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
        size = self._sent_size(frame)
        if size != frame.size:
            frame = frame.resize(size, Image.LANCZOS)
        frame.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    def _sent_size(self, frame):
        """(w, h) of the image _b64 sends for this frame; None if the frame
        has no size."""
        try:
            w, h = frame.size
        except Exception:
            return None
        if w > self.MAX_SEND_WIDTH:
            return self.MAX_SEND_WIDTH, int(h * self.MAX_SEND_WIDTH / w)
        return w, h

    def _coord_space(self, frame):
        """(image_w, image_h, screen_w, screen_h, prompt line) for a
        coordinate question. The model is told the size of the image it gets
        (unchanged wording when that is the screen size)."""
        sw, sh = pyautogui.size()
        iw, ih = self._sent_size(frame) or (sw, sh)
        if (iw, ih) == (sw, sh):
            line = f"Screen resolution: {sw}x{sh}."
        else:
            line = f"Image size: {iw}x{ih} pixels (give coordinates in this image's pixels)."
        return iw, ih, sw, sh, line

    @staticmethod
    def _to_screen(x, y, iw, ih, sw, sh):
        """An answer in image pixels -> screen pixels (x * screen / image);
        None when it lies outside the image."""
        if not (0 <= x <= iw and 0 <= y <= ih):
            return None
        if (iw, ih) == (sw, sh):
            return (x, y)
        return (round(x * sw / iw), round(y * sh / ih))

    def _call(self, prompt, frame=None):
        if not self.ready:
            return ""
        try:
            t0 = time.perf_counter()
            image = self._b64(frame) if frame else ""
            t1 = time.perf_counter()
            content = (
                [
                    {"type": "image", "image": f"data:image/png;base64,{image}"},
                    {"type": "text", "text": prompt},
                ]
                if frame
                else prompt
            )
            try:
                resp = self._client.chat(
                    model=self.MODEL,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=600,
                )
            finally:
                if frame:
                    # Numbers only: sent size, base64 KB, encode and call (upload + reply) ms.
                    w, h = self._sent_size(frame) or (0, 0)
                    print(f"[Vision] sent {w}x{h} png {len(image) // 1024} KB "
                          f"encode {(t1 - t0) * 1000:.0f}ms call {(time.perf_counter() - t1) * 1000:.0f}ms")
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
                    print(f"[Vision] Screen understood ({len(r)} chars, attempts {attempt + 1})")
                    return r
            except Exception as e:
                print(f"[Vision] Attempt {attempt + 1} failed: {e}")
                time.sleep(0.5)

        print(f"[Vision] no usable answer, attempts {max_attempts}")
        return "Screen captured but Vision unavailable. Recommend using OCR or UIA fallback."

    def find_element_coords(self, description, frame=None):
        if not self.ready:
            return None
        shot = frame or ImageGrab.grab()
        iw, ih, sw, sh, size_line = self._coord_space(shot)
        result = self._call(
            f"Find '{description}' on this screen.\n"
            f"{size_line}\n"
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
            return self._to_screen(x, y, iw, ih, sw, sh)
        except Exception:
            pass
        return None

    def find_nth_element_coords(self, item_type, n, frame=None):
        if not self.ready:
            return None, None
        shot = frame or ImageGrab.grab()
        iw, ih, sw, sh, size_line = self._coord_space(shot)
        ordinals = [
            "first", "second", "third", "fourth", "fifth",
            "sixth", "seventh", "eighth", "ninth", "tenth",
        ]
        ordinal = ordinals[n - 1] if 1 <= n <= len(ordinals) else f"#{n}"
        result = self._call(
            f"Find the {ordinal} {item_type} on this screen.\n"
            f"{size_line}\n"
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
                    coords = self._to_screen(x, y, iw, ih, sw, sh)
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


# ============================================================
# ACCESSIBILITY TEXT
# ============================================================

# core/uia_text.py — exact on-screen text from the accessibility tree.
#
# read_screen_text puts this next to the vision answer in native windows: UIA
# reports what a control actually contains (a Calculator display, an Edit box),
# where vision has been seen to report a value that was never displayed.
#
# Password fields are skipped, and a snapshot that looks like it holds a secret
# (private key block, credential-looking token) is withheld entirely. A process
# whose walk ran past the budget once is skipped for the rest of the session.

# ============================================================
# ACCESSIBILITY TEXT SETTINGS
# ============================================================

TEXT_CONTROL_TYPES = ("Text", "Edit", "Document")
TEXT_CAP = 300  # chars foreground_uia_text returns by default
UIA_VALUE_PROPERTY_ID = 30045  # UIA_ValueValuePropertyId
UIA_IS_PASSWORD_PROPERTY_ID = 30019  # UIA_IsPasswordPropertyId
MAX_ELEMENTS = 2000

_slow_processes = set()  # process names whose walk timed out this session
_slow_lock = threading.Lock()


def _is_password(info):
    try:
        return bool(info.element.GetCurrentPropertyValue(UIA_IS_PASSWORD_PROPERTY_ID))
    except Exception:
        return False


# ============================================================
# ACCESSIBILITY TREE WALK
# ============================================================

def _walk_text(hwnd, deadline, cap):
    """(text, timed_out): names/values of the Text/Edit/Document elements
    under ``hwnd`` in tree order, deduplicated, joined with ' | ' and cut to
    ``cap`` chars. Stops early at ``cap``; timed_out = the deadline arrived
    with elements still unvisited (a big window like VS Code)."""
    from pywinauto.uia_element_info import UIAElementInfo

    parts, seen, total, visited = [], set(), 0, 0
    queue = [UIAElementInfo(hwnd)]
    timed_out = False
    while queue and total < cap and visited < MAX_ELEMENTS:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        info = queue.pop(0)
        visited += 1
        try:
            queue.extend(info.children())
        except Exception:
            pass
        try:
            if info.control_type not in TEXT_CONTROL_TYPES or _is_password(info):
                continue
            texts = [info.name or ""]
            try:
                texts.append(str(info.element.GetCurrentPropertyValue(UIA_VALUE_PROPERTY_ID) or ""))
            except Exception:
                pass
        except Exception:
            continue
        for t in texts:
            t = " ".join(t.split())
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
                total += len(t) + 3
    return " | ".join(parts)[:cap], timed_out


# ============================================================
# WINDOW PROCESS NAME
# ============================================================

def _process_name(hwnd):
    try:
        import psutil
        import win32process

        return psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name()
    except Exception:
        return ""


# ============================================================
# SLOW WINDOW MEMORY
# ============================================================

def _mark_slow(proc):
    if not proc:
        return
    with _slow_lock:
        if proc in _slow_processes:
            return
        _slow_processes.add(proc)
    print(f"[UIA] skipping {proc}: too slow")


# ============================================================
# VISUAL QUESTIONS
# ============================================================

# Questions about how things look, not what text they hold: a screenshot is
# needed. General words only, never app names.
_VISUAL_RX = re.compile(
    r"\b(colou?rs?|red|green|blue|yellow|orange|purple|pink|gr[ae]y|images?|pictures?|"
    r"photos?|icons?|logos?|thumbnails?|layout|positions?|arranged|aligned|highlighted|"
    r"bold|italic|underlined|fonts?|shapes?|charts?|graphs?|diagrams?|maps?|"
    r"looks? like|appearance|screenshot|where on)\b", re.I)


def is_visual_question(query):
    """True when the question is empty (a general description) or about
    something visual (colours, images, layout...)."""
    q = str(query or "").strip()
    return not q or bool(_VISUAL_RX.search(q))


# ============================================================
# SUFFICIENT TEXT CHECK
# ============================================================

def text_is_sufficient(text, title="", cap=TEXT_CAP):
    """UIA text that can answer a read on its own: it holds at least one
    element besides the window's own title, and the walk was not cut at
    ``cap`` (a cut walk may have dropped what the question is about)."""
    text = str(text or "")
    if not text.strip() or len(text) >= cap - 3:
        return False
    own = " ".join(str(title or "").split()).lower()
    return any(p.strip() and p.strip().lower() != own for p in text.split(" | "))


# ============================================================
# FOREGROUND WINDOW TEXT
# ============================================================

def foreground_uia_text(hwnd=None, budget=1.5, cap=TEXT_CAP):
    """(text, title) for the foreground window (or ``hwnd``); text is "" when
    UIA found nothing, ran past ``budget`` seconds, the process is on the
    too-slow list, or the text looks like it holds a secret. Never raises."""
    title = ""
    try:
        import win32gui

        hwnd = hwnd or win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return "", title
    if not hwnd:
        return "", title
    proc = _process_name(hwnd)
    with _slow_lock:
        if proc and proc in _slow_processes:
            return "", title
    box = []

    def run():
        try:
            box.append(_walk_text(hwnd, time.monotonic() + budget, cap))
        except Exception:
            box.append(("", False))

    # ponytail: an over-budget walk is abandoned, not cancelled (UIA can't be
    # interrupted); it stops at its own deadline and its result is dropped.
    t = threading.Thread(target=run, daemon=True, name="UIAText")
    t.start()
    t.join(budget + 0.25)
    if not box or box[0][1]:
        _mark_slow(proc)
        return "", title
    text = box[0][0]
    reason = secret_text_reason(text)
    if reason:
        whose = f"'{title[:60]}'" if verbose_logs() else "the window"  # a title is content
        print(f"[UIA] withheld the text of {whose}: {reason}")
        return "", title
    return text, title
