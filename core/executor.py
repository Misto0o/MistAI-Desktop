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

from . import events, verify
from .tools import CLICK_FAMILY

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
        try:
            ok = bool(api._exec_sync(action, param))
        except Exception as e:
            ok = False
            api.last_result_text = f"executor: {e}"

        detail = getattr(api, "last_result_text", "") or ""
        api.last_result_text = ""

        # Settle from tool metadata (replaces AgentLoop._smart_settle) --
        # the UI must catch up BEFORE verification looks at ground truth.
        time.sleep(reg.settle(action))
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
            api.memory.add_action(res.action, res.parameter, res.success)
        except Exception:
            pass
        try:
            api.task_memory.record(res.action, res.parameter, res.success,
                                   res.detail)
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
            reason = " | reason: " + why.encode("ascii", "backslashreplace").decode("ascii")
        print(f"[Executor] {res.short()}{reason}")
