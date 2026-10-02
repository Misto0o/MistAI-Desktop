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
    def __init__(self, fps=4):
        self._pil = self._cv = None
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
                pil = ImageGrab.grab()
                cv = None
                if OCR_AVAILABLE:
                    cv = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
                with self._lock:
                    self._pil, self._cv = pil, cv
            except Exception as e:
                print(f"[ScreenBuffer] {e}")
            time.sleep(iv)

    @property
    def frame(self):
        with self._lock:
            return self._pil

    @property
    def cv_frame(self):
        with self._lock:
            return self._cv

    def stop(self):
        self._running = False
