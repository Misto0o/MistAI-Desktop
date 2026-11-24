# ============================================================
# EXECUTOR MODULE
# ============================================================
#
# The one path every action takes (Executor.run): dispatch, settle, the
# ground-truth verifiers, confidence scoring and fallback escalation.
#
# SECTION INDEX
#   1. GROUND-TRUTH VERIFIERS
#   2. PATH RESOLUTION FOR CHECKS
#   3. FILE WRITTEN CHECK
#   4. FILE AND FOLDER EXISTENCE CHECKS
#   5. PROCESS RUNNING CHECK
#   6. WINDOW FOCUSED CHECK
#   7. APP VISIBLE CHECK
#   8. TOGGLE TARGETS
#   9. CLICK LANDED CHECK
#   10. VERIFIER REGISTRY
#   11. RUNNING A VERIFIER
#   12. METHOD CONFIDENCE AND BROWSER HINTS
#   13. EXECUTION RESULT
#   14. SETTLE AFTER AN ACTION
#   15. ACTION EXECUTOR
# ============================================================

"""
core/executor.py — the ONE path every action takes (design doc #6, #8, #10):

    metadata lookup -> _exec_sync -> settle -> verify -> confidence
        -> (escalate through fallback chain if weak) -> record everywhere

What each step buys:
- verify: ground-truth check via core/verify.py. If verification CONTRADICTS
  a claimed success, the action is downgraded to a failure -- the agent
  never gets to assume.
- confidence: every ExecutionResult carries a 0..1 score. Click-family
  actions inherit the score of the method that actually resolved them
  (UIA 0.92 > OCR 0.74 > Vision 0.45), read from click_handler.last_method.
- escalation: below-threshold or failed results automatically walk the
  tool's fallback chain (e.g. uia_click -> click_on_text) BEFORE giving up.
  When the chain is exhausted, escalate_to_user=True is surfaced in the
  observation so the MODEL (not hardcoded Python) decides to ask_user --
  preserving the "LLM makes decisions" philosophy.
- record: one place updates Memory.actions, TaskMemory patterns, the
  Workspace, and publishes ActionExecuted/ActionFailed on the bus. (The
  matching lines in AgentLoop._loop are removed by the integration patch,
  so nothing double-records.)

Deliberately NOT here: the destructive-confirmation gate. That stays in
AgentLoop (it owns the voice/text conversation with the user); the
Executor only refuses to auto-escalate INTO a destructive fallback.
"""

import time
from dataclasses import dataclass, field
import os

from . import executor as verify, live_state as events
from .config import verbose_logs, normalize_window_title
from .tools import CLICK_FAMILY


# ============================================================
# GROUND-TRUTH VERIFIERS
# ============================================================

# core/verify.py — post-action verification (design doc #8): the agent
# verifies instead of assuming.
#
# Each verifier answers "did that action ACTUALLY take effect?" against
# ground truth (disk, process list, foreground window) -- never against a
# screenshot guess. Return contract:
#
#     (True,  note)  -> independently confirmed
#     (False, note)  -> independently CONTRADICTED (executor downgrades the
#                       action to a failure even if the handler returned ok)
#     (None,  note)  -> cannot verify (unknown != success; confidence stays
#                       at the tool's base level)
#
# Verifiers are looked up by ToolSpec.verifier; ToolSpec.verifier_arg can pin
# a static target (e.g. browser_* always verifies the 'firefox' process),
# otherwise the target derives from the action parameter.

# ============================================================
# PATH RESOLUTION FOR CHECKS
# ============================================================

def _resolve(api, path):
    try:
        return api._resolve_path_placeholders(path)
    except Exception:
        return path


# ============================================================
# FILE WRITTEN CHECK
# ============================================================

def _file_written(api, action, param, arg):
    if "|||" not in (param or ""):
        return None, "no path to verify"
    path = _resolve(api, param.split("|||", 1)[0].strip())
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            api.workspace.note_file(path)
            return True, f"file exists ({os.path.getsize(path)} bytes)"
        return False, f"file missing or empty at {path}"
    except Exception as e:
        return None, f"verify error: {e}"


# ============================================================
# FILE AND FOLDER EXISTENCE CHECKS
# ============================================================

def _file_exists(api, action, param, arg):
    path = _resolve(api, (arg or param or "").strip())
    if not path:
        return None, "no path"
    return (os.path.isfile(path), f"checked {path}")


def _folder_exists(api, action, param, arg):
    path = _resolve(api, (arg or param or "").strip())
    if not path:
        return None, "no path"
    return (os.path.isdir(path), f"checked {path}")


# ============================================================
# PROCESS RUNNING CHECK
# ============================================================

def _process_running(api, action, param, arg):
    target = (arg or param or "").strip()
    if not target:
        return None, "no target process"
    try:
        running = api.is_app_running(target)
        return (running, f"process '{target}' {'found' if running else 'NOT found'}")
    except Exception as e:
        return None, f"verify error: {e}"


# ============================================================
# WINDOW FOCUSED CHECK
# ============================================================

def _window_focused(api, action, param, arg):
    target = (arg or param or "").strip().lower()
    if not target:
        return None, "no target window"
    try:
        fw = api.live.snapshot.get("foreground_window", "").lower()
        # Compare normalized forms too: a title quoted back with '-' for its
        # em dash must not contradict a switch that actually worked.
        t, f = normalize_window_title(target), normalize_window_title(fw)
        ok = bool(fw) and (target in fw or fw in target
                           or bool(f and t) and (t in f or f in t))
        return (ok, f"foreground is '{fw[:40]}'")
    except Exception as e:
        return None, f"verify error: {e}"


# ============================================================
# APP VISIBLE CHECK
# ============================================================

def _app_visible(api, action, param, arg):
    """open_app's real intent: the app is on screen and usable -- not merely
    that a process by that name exists. Discord minimised to the tray has a
    process and no window; that used to verify as a success (conf 0.93)
    while VS Code stayed in front of the user.

        foreground  -> True   (it's in front of the user)
        visible     -> True   (has a top-level window; user can reach it)
        hidden      -> False  (running, but nothing to interact with)
        not running -> False
        can't tell  -> None   (win32 unavailable; falls back to base confidence)

    Uses Api.app_window_state(), the same helper _act_open_app decides with,
    so handler and verifier can never disagree about the same fact.
    """
    target = (arg or param or "").strip()
    if not target:
        return None, "no target app"
    try:
        state, title = api.app_window_state(target)
    except Exception as e:
        return None, f"verify error: {e}"
    if state == "foreground":
        return True, f"'{target}' is the foreground window ('{title[:40]}')"
    if state == "visible":
        return (
            True,
            f"'{target}' has a visible window ('{title[:40]}') but is not in front",
        )
    if state == "hidden":
        return (
            False,
            f"'{target}' is running but has NO visible window (tray/background only)",
        )
    if state == "not_running":
        return False, f"process '{target}' NOT found"
    return None, "could not determine window state"


# ============================================================
# TOGGLE TARGETS
# ============================================================

# Words whose own label is expected to CHANGE the instant a click lands
# (Subscribe -> Subscribed, Follow -> Following, ...). Outside this list we
# stay silent (None) rather than guess -- most clicked text (links, menu
# items) is SUPPOSED to still be readable somewhere after a correct click,
# so "the word is still there" isn't evidence of failure in general.
_TOGGLE_TARGETS = {
    "subscribe": "subscribed",
    "follow": "following",
    "like": "liked",
    "save": "saved",
    "mute": "muted",
    "block": "blocked",
    "join": "joined",
    "unsubscribe": "subscribe",
    "unfollow": "follow",
    "unlike": "like",
    "unsave": "save",
    "unmute": "mute",
    "unblock": "block",
    "leave": "join",
}


# ============================================================
# CLICK LANDED CHECK
# ============================================================

def _click_landed(api, action, param, arg):
    """Sanity check for click_on_text/click_nth on known toggle labels.

    Requires the EXPECTED post-toggle word (Subscribe -> Subscribed), not
    just "any different text at that spot" -- a click that misses the real
    button and lands on some unrelated element will usually find DIFFERENT
    text there too (it's a different element!), which a naive before/after
    diff would wrongly read as a successful toggle. Only a match against
    the actual expected target word counts as confirmed success; anything
    else stays unverified (None) rather than a false positive."""
    try:
        handler = api.click_handler
    except Exception:
        return None, "no click handler"
    query = (handler.last_query or "").strip().lower()
    target = _TOGGLE_TARGETS.get(query)
    if not target:
        return None, "not a recognized toggle target"
    after = handler.recheck_text_near_last_click()
    if not after:
        return None, "OCR recheck found nothing"
    after_words = {w.strip(".,!?:;").lower() for w in after.split()}
    if target in after_words:
        return (
            True,
            f"label now reads '{after.strip()[:40]}' -- matches expected '{target}'",
        )
    if query in after_words:
        return (
            False,
            f"label still reads '{after.strip()[:40]}' -- click did not toggle it",
        )
    # Different text, but not the expected target word -- most likely the
    # click landed on the WRONG element entirely (a different label exists
    # there for unrelated reasons). Don't count this as confirmation.
    return None, (
        f"label reads '{after.strip()[:40]}', neither the original "
        f"nor the expected '{target}' -- click may have missed "
        f"the actual button"
    )


# ============================================================
# VERIFIER REGISTRY
# ============================================================

VERIFIERS = {
    "file_written": _file_written,
    "file_exists": _file_exists,
    "folder_exists": _folder_exists,
    "process_running": _process_running,
    "window_focused": _window_focused,
    "app_visible": _app_visible,
    "click_landed": _click_landed,
}


# ============================================================
# RUNNING A VERIFIER
# ============================================================

def run(name, api, action, param, arg=""):
    """Dispatch a verifier by name. Unknown/missing names -> (None, '')."""
    fn = VERIFIERS.get(name)
    if not fn:
        return None, ""
    try:
        return fn(api, action, param, arg)
    except Exception as e:
        return None, f"verifier crashed: {e}"


# ============================================================
# METHOD CONFIDENCE AND BROWSER HINTS
# ============================================================

# Confidence assigned per click-resolution method (design doc #6's example).
METHOD_CONFIDENCE = {"UIA": 0.92, "OCR": 0.74, "Vision": 0.45}

# Browsers render their content into a canvas/DOM that UIA sees only as a
# useless generic tree ("document", "group", "Empty"), so UIA-first clicking
# wastes a step and then lands on the wrong element. Inside a browser, OCR
# (pytesseract) reads what is ACTUALLY painted on screen, which is both
# faster and more accurate here. These actions get rerouted automatically.
BROWSER_HINTS = ("firefox", "chrome", "chromium", "edge", "opera", "brave",
                 "vivaldi", "mozilla", "youtube", "— mozilla firefox")
UIA_ACTIONS = ("uia_click", "uia_find_input")


# ============================================================
# EXECUTION RESULT
# ============================================================

@dataclass
class ExecutionResult:
    action: str
    parameter: str
    success: bool
    confidence: float
    method: str = "direct"
    verified: object = None          # True / False / None (unverifiable)
    detail: str = ""
    duration: float = 0.0
    escalated_from: str = ""
    escalate_to_user: bool = False

    def short(self):
        # Diagnostics must be printable in Windows consoles still using a
        # legacy code page (such as cp1252); logging cannot break an action.
        v = "OK" if self.verified else ("NO" if self.verified is False else "?")
        return (f"{self.action} -> {'ok' if self.success else 'FAIL'} "
                f"conf={self.confidence:.2f} verify={v} via {self.method}")

    def reliability_note(self):
        """Qualitative phrasing for the agent's OBSERVATION. Deliberately not
        a number: shown the raw float, the model treated it as a threshold it
        could raise ('I'll retry with a higher confidence threshold'), which
        it cannot -- confidence only DESCRIBES how the action resolved."""
        if not self.success:
            return ""
        if self.verified is True:
            return " Confirmed by a direct system check."
        if self.method == "Vision":
            return (" Resolved by vision (least precise of the click methods) "
                    "-- if the next screen looks unchanged, prefer a different "
                    "approach over repeating this click.")
        if self.method == "OCR":
            return " Resolved by reading on-screen text (OCR)."
        if self.method == "UIA":
            return " Resolved via the accessibility tree."
        return ""


# ============================================================
# SETTLE AFTER AN ACTION
# ============================================================

# The settle after an action: ToolSpec.settle is the maximum; with window
# events (core/live_state.py settle) an action with a ToolSpec.settle_floor
# may end it after the floor once the window it expects is in front and the
# desktop is quiet: the app it opened or switched to, MistAI's browser
# window after a navigation, else any window. Stand-ins without window
# events, and every action without a floor, sleep the full settle as before.

BROWSER_NAVIGATION = ("browser_navigate", "browser_open", "browser_search")


def settle_after(api, action, param, spec, cap):
    floor = getattr(spec, "settle_floor", None)
    watched = getattr(getattr(api, "live", None), "window_events", None)
    if floor is None or not isinstance(watched, events.WindowEvents):
        time.sleep(cap)
        return
    if action in ("open_app", "switch_app"):
        app = str(param or "").lower().strip()
        expect = events.foreground_is(lambda hwnd: api._window_belongs_to(hwnd, app))
        label = "window+quiet"
    elif action in BROWSER_NAVIGATION:
        expect, label = events.browser_window_present(), "browser window+quiet"
    else:
        expect, label = events.foreground_exists(), "quiet"
    events.settle(watched, cap, floor, expect, label)


# ============================================================
# ACTION EXECUTOR
# ============================================================

class Executor:
    CONFIDENCE_THRESHOLD = 0.6
    MAX_ESCALATION_DEPTH = 2

    def __init__(self, api):
        self.api = api  # bus/tools/workspace looked up lazily -> no init-order coupling

    # -- Public entry --------------------------------------------------------

    def run(self, action, parameter, _depth=0):
        api = self.api
        reg = api.tools
        param = str(parameter) if not isinstance(parameter, str) else parameter

        # -- OCR-first routing inside browsers ------------------------------
        # Not a fallback AFTER failure -- a redirect BEFORE it, because in a
        # browser the UIA attempt is near-guaranteed to miss or mis-hit.
        rerouted_from = ""
        if action in UIA_ACTIONS and self._in_browser():
            if action == "uia_click" and param:
                rerouted_from, action = action, "click_on_text"
                print(f"[Executor] Browser detected -- routing "
                      f"{rerouted_from} through OCR (click_on_text)")
            elif action == "uia_find_input":
                # No OCR equivalent for "find the input"; clicking the visible
                # search/message box by its label is the model's job instead.
                print("[Executor] Browser detected -- uia_find_input is "
                      "unreliable here; prefer click_on_text on the field's "
                      "visible placeholder text")

        spec = reg.get(action)
        t0 = time.time()
        t_start = time.perf_counter()
        try:
            ok = bool(api._exec_sync(action, param))
        except Exception as e:
            ok = False
            api.last_result_text = f"executor: {e}"

        detail = getattr(api, "last_result_text", "") or ""
        api.last_result_text = ""
        t_handler = time.perf_counter()

        # Settle from tool metadata (replaces AgentLoop._smart_settle) --
        # the UI must catch up BEFORE verification looks at ground truth.
        settle_after(api, action, param, spec, reg.settle(action))
        t_settle = time.perf_counter()
        # Anything but a read may have changed the screen: later vision reads
        # must use a frame captured after this point.
        if not set(spec.categories) & {"perception", "info"}:
            mark = getattr(getattr(api, "screen", None), "mark_action", None)
            if callable(mark):
                mark()

        verified, vnote = (None, "")
        if spec.verifier:
            verified, vnote = verify.run(spec.verifier, api, action, param,
                                         spec.verifier_arg)
            if vnote:
                detail = f"{detail} [{vnote}]" if detail else f"[{vnote}]"
        print(f"[Timing] exec split: handler={t_handler - t_start:.2f}s "
              f"settle={t_settle - t_handler:.2f}s verify={time.perf_counter() - t_settle:.2f}s")

        # Verification contradicting a claimed success wins.
        if ok and verified is False:
            ok = False
            detail = (detail + " -- handler reported success but verification "
                      "contradicted it").strip()

        method = "direct"
        if action in ("click_on_text", "click_nth"):
            method = getattr(api.click_handler, "last_method", None) or "direct"
        elif action == "uia_click":
            method = "UIA"

        confidence = self._confidence(spec, action, method, ok, verified)
        res = ExecutionResult(
            action=action, parameter=param, success=ok, confidence=confidence,
            method=method, verified=verified, detail=detail,
            duration=round(time.time() - t0, 2),
            escalated_from=rerouted_from,
        )

        # Record THIS attempt before any escalation. Previously, a failure
        # that a fallback rescued returned early via `return sub` and never
        # reached _finalize -- so e.g. switch_app's failures were invisible to
        # ToolRegistry.reliability() and to TaskMemory, and every tool with a
        # fallback looked more reliable than it actually is.
        self._finalize(res, spec)

        # -- Escalation (design doc #6) --------------------------------------
        # ONLY a real failure escalates. Confidence describes how much to
        # TRUST a result; it never overturns one. A Vision-resolved click
        # (0.45) that worked is still a click that worked -- treating it as a
        # failure made the agent re-click things it had already clicked.
        # The single exception is verification actively contradicting the
        # handler, which is already folded into `ok` above.
        if not res.success:
            for fb in spec.fallback_tools:
                if _depth >= self.MAX_ESCALATION_DEPTH:
                    break
                if reg.is_destructive(fb, param):
                    continue  # never auto-escalate INTO something destructive
                fb_param = self._map_param(action, fb, param)
                if fb_param is None:
                    continue
                print(f"[Executor] Escalating {action} -> {fb} "
                      f"(conf {res.confidence:.2f})")
                sub = self.run(fb, fb_param, _depth + 1)
                if sub.success:
                    sub.escalated_from = action
                    sub.detail = (f"(escalated from {action}) " + sub.detail).strip()
                    return sub  # sub already finalized itself
            if spec.fallback_tools or action in CLICK_FAMILY:
                # Chain exhausted: hand the DECISION back to the model.
                res.escalate_to_user = True
        return res

    # -- Internals -----------------------------------------------------------

    def _in_browser(self):
        """Is the foreground window a web browser? Drives OCR-first routing."""
        try:
            fw = (self.api.live.snapshot.get("foreground_window", "") or "").lower()
        except Exception:
            return False
        return any(h in fw for h in BROWSER_HINTS)

    def _confidence(self, spec, action, method, ok, verified):
        base = METHOD_CONFIDENCE.get(method, spec.base_confidence)
        if not ok:
            return round(min(0.2, base * 0.25), 2)
        c = base
        if verified is True:
            c = max(c, 0.93)
        elif verified is False:
            c = min(c, 0.3)
        return round(c, 2)

    def _map_param(self, from_action, to_action, param):
        """Parameter translation between a tool and its fallback.
        Return None to skip that fallback for this parameter."""
        if not param and to_action in ("click_on_text", "uia_click"):
            return None  # nothing to search for
        return param     # chains defined so far share the same parameter shape

    def _finalize(self, res, spec):
        api = self.api
        # Recording (single writer -- the patched AgentLoop no longer records)
        try:
            api.memory.add_action(res.action, res.success)    # never the parameter
        except Exception:
            pass
        try:
            api.task_memory.record(res.action, res.parameter, res.success)
        except Exception:
            pass
        try:
            api.workspace.record_execution(res.action, res.parameter,
                                           res.success, res.confidence,
                                           res.detail)
        except Exception:
            pass
        # Anything that changed the UI invalidates the cached UIA tree.
        try:
            if res.success and set(spec.categories) & {"click", "type", "window", "app"}:
                api.uia_nav.invalidate_cache()
        except Exception:
            pass
        try:
            api.bus.publish(
                events.ACTION_EXECUTED if res.success else events.ACTION_FAILED,
                {
                    "action": res.action,
                    "parameter": res.parameter[:120],
                    "success": res.success,
                    "confidence": res.confidence,   
                    "verified": res.verified,
                    "method": res.method,
                    "detail": res.detail[:300],
                    "duration": res.duration,
                },
                "Executor",
            )
        except Exception:
            pass
        reason = ""
        if not res.success:
            why = " ".join((res.detail or "no detail").split())[:300]
            # The detail can quote command output, a click query or a window title.
            reason = " | reason: " + (why.encode("ascii", "backslashreplace").decode("ascii")
                                      if verbose_logs() else f"<{len(why)} chars>")
        print(f"[Executor] {res.short()}{reason}")
