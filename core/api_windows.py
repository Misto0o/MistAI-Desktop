"""
core/api_windows.py -- window_activate / window_close, maximize, minimize

Methods of core.api.Api, moved here verbatim (Api inherits WindowActionsMixin).
"""

from .config import verbose_logs


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
