"""
core/wake_calibration.py — when the wake loop re-measures room noise.

The wake loop runs with a fixed energy threshold (dynamic adjustment raised
the bar during a loud "hey" until a quiet "mist" counted as silence). A fixed
threshold goes stale, so it is re-calibrated:
  * at startup,
  * after WAKE_RECAL_IDLE_S with no clip heard,
  * after WAKE_RECAL_AFTER_FAILURES untranscribable clips in a row (noise is
    opening clips: the threshold is too low),
  * once, WAKE_RECAL_RETRY_S after a result outside the band (it is clamped
    meanwhile; a second out-of-band result is kept, clamped, to avoid a loop).
Pure bookkeeping: the caller owns the microphone, TTS and lock checks.
"""

from .config import (
    WAKE_ENERGY_MAX,
    WAKE_ENERGY_MIN,
    WAKE_RECAL_AFTER_FAILURES,
    WAKE_RECAL_IDLE_S,
    WAKE_RECAL_RETRY_S,
)


class WakeCalibration:
    def __init__(self, low=WAKE_ENERGY_MIN, high=WAKE_ENERGY_MAX,
                 idle_s=WAKE_RECAL_IDLE_S, max_failures=WAKE_RECAL_AFTER_FAILURES,
                 retry_s=WAKE_RECAL_RETRY_S):
        self.low, self.high = low, high
        self.idle_s, self.max_failures, self.retry_s = idle_s, max_failures, retry_s
        self._forced = "startup"
        self._retry_at = None
        self._last_activity = None
        self._failures = 0

    def due(self, now):
        """The reason a calibration is due now, or None."""
        if self._forced:
            return self._forced
        if self._retry_at is not None and now >= self._retry_at:
            return "threshold out of band"
        if self._last_activity is not None and now - self._last_activity >= self.idle_s:
            return f"idle {self.idle_s:.0f}s"
        return None

    def calibrated(self, measured, now):
        """Record a finished calibration; return the threshold to use."""
        was_retry = self._retry_at is not None
        threshold = min(max(measured, self.low), self.high)
        self._forced = None
        self._failures = 0
        self._last_activity = now
        self._retry_at = (now + self.retry_s
                          if threshold != measured and not was_retry else None)
        return threshold

    def failed(self, now):
        """A calibration raised: keep the old threshold, retry when idle."""
        self._forced = None
        self._retry_at = None
        self._last_activity = now

    def heard(self, transcribed, now):
        """A clip ended. transcribed: True, False (no speech found) or None
        (recognizer/network error, which says nothing about the threshold)."""
        self._last_activity = now
        if transcribed is True:
            self._failures = 0
        elif transcribed is False:
            self._failures += 1
            if self._failures >= self.max_failures:
                self._forced = f"{self.max_failures} untranscribed clips"
                self._failures = 0
