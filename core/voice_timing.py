"""
core/voice_timing.py — [VoiceTiming] log lines for one spoken turn.

A turn starts when the wake clip ends (or when a follow-up listen opens).
Each mark logs the seconds since the previous mark and since the start, and
the turn ends at the first agent step, a chat reply, or a failed listen.
Durations and mark names only, never transcript text.
"""

import threading
import time


class VoiceTimer:
    def __init__(self, clock=time.monotonic, log=print):
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._label = ""
        self._t0 = None
        self._last = None

    @property
    def active(self):
        with self._lock:
            return self._t0 is not None

    def start(self, label, t0=None):
        with self._lock:
            self._label = label
            self._t0 = self._clock() if t0 is None else t0
            self._last = self._t0

    def mark(self, name, end=False):
        """Log one step of the active turn; a no-op when none is active."""
        with self._lock:
            if self._t0 is None:
                return
            now = self._clock()
            delta, total = now - self._last, now - self._t0
            self._last = now
            label = self._label
            if end:
                self._t0 = None
        self._log(f"[VoiceTiming] {label}: {name} +{delta:.2f}s (total {total:.2f}s)")
