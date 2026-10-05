"""
core/screen.py — continuously-updated screen capture buffer.
"""

import threading
import time

try:
    import cv2
    import numpy as np
    from PIL import ImageGrab
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False
    from PIL import ImageGrab


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
