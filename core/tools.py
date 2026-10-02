"""
core/tools.py — tool metadata registry (design doc #5) + learned
reliability (design doc #7, simple statistics, no ML).

Every action in Api._exec_sync gets a ToolSpec describing:
- destructive / reversible / requires_confirmation / requires_focus
- expected_runtime + settle time (replaces AgentLoop._smart_settle's table)
- base_reliability and a base confidence for successful runs
- categories, fallback_tools (drives the Executor's escalation chain)
- verifier (+ static verifier_arg) -- see core/verify.py

reliability(name) blends the spec's prior with the OBSERVED success rate
from the existing `actions` table in memory.db (which AgentLoop has been
writing all along) -- so the numbers get better the more MistAI runs,
with zero schema changes and zero new writes.
"""

import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field

# PowerShell/cmd verbs that destroy or overwrite data. These are matched only
# where PowerShell expects a command (start of a statement or after a pipe),
# rather than anywhere in the string: `Get-Help Remove-Item` is read-only and
# must not prompt merely because it mentions a destructive command.
# Aliases matter too -- `ri` IS Remove-Item.
MUTATING_COMMAND_VERBS = (
    # delete
    "remove-item",
    "ri",
    "rd",
    "rmdir",
    "del",
    "erase",
    "rm",
    "clear-content",
    "clc",
    "clear-recyclebin",
    "remove-itemproperty",
    # overwrite in place
    "set-content",
    "sc",
    "out-file",
    "add-content",
    "set-itemproperty",
    "sp",
    "new-itemproperty",
    "copy-item",
    "copy",
    "cp",
    "xcopy",
    "robocopy",
    # move / rename (destructive with -Force, and not undoable)
    "move-item",
    "mi",
    "move",
    "rename-item",
    "rni",
    "ren",
    # disk / process / power
    "format-volume",
    "format",
    "clear-disk",
    "initialize-disk",
    "stop-process",
    "kill",
    "spps",
    "stop-computer",
    "restart-computer",
    "shutdown",
    "stop-service",
)

_MUTATING_COMMAND_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*(?:&\s*)?(?:\{\s*)?("
    + "|".join(re.escape(v) for v in MUTATING_COMMAND_VERBS)
    + r")(?![\w-])(?=\s|$|[;|&])",
    re.IGNORECASE,
)
_REG_MUTATING_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*reg(?:\.exe)?\s+"
    r"(?:add|delete|restore|load|unload|save)(?:\s|$)",
    re.IGNORECASE,
)
_DYNAMIC_EVAL_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*(?:invoke-expression|iex)(?:\s|$)",
    re.IGNORECASE,
)
_CMD_WRAPPER_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*cmd(?:\.exe)?\s+/(?:c|k)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_POWERSHELL_WRAPPER_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*(?:powershell|pwsh)(?:\.exe)?\b.*?"
    r"\s-(?:command|c)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_QUOTED_INVOKE_RX = re.compile(
    r"(?:^|[;|&\r\n])\s*&\s*['\"]\s*(?:"
    + "|".join(re.escape(v) for v in MUTATING_COMMAND_VERBS)
    + r")\s*['\"](?=\s|$|[;|&])",
    re.IGNORECASE,
)
_DANGEROUS_CLICK_TEXT_RX = re.compile(
    r"\b(send|submit|post|publish|buy|purchase|pay|place\s+order|"
    r"delete|remove|discard|unsubscribe|unfollow|block|confirm)\b",
    re.IGNORECASE,
)
_DANGEROUS_HOTKEYS = {
    "alt+f4",
    "ctrl+w",
    "ctrl+shift+w",
    "alt+delete",
    "ctrl+alt+delete",
}


def _outside_quotes(command):
    """Return command text with quoted strings/comments blanked out.

    We need a deliberately small lexer here, not a PowerShell parser: it
    prevents labels and examples such as ``Get-Help Remove-Item`` from looking
    executable while still leaving statement separators and command names in
    place. Backtick escapes are PowerShell's in-string escape mechanism.
    """
    out = []
    quote = None
    i = 0
    text = command or ""
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(" " if ch not in "\r\n" else ch)
            if ch == "`" and i + 1 < len(text):
                i += 1
                out.append(" " if text[i] not in "\r\n" else text[i])
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(" ")
        elif ch == "#":
            while i < len(text) and text[i] not in "\r\n":
                out.append(" ")
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _strip_outer_quotes(text):
    """Unwrap one shell payload quote pair without interpreting it."""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def command_is_mutating(command, _depth=0):
    """Whether a shell command can overwrite, destroy, or alter state.

    This is intentionally conservative for actual executable statements, but
    it avoids flagging read-only help/search commands that merely *mention* a
    destructive verb. File redirection is state-changing even when its source
    command is read-only (``Get-Date > status.txt``), so it is handled too.
    """
    raw = str(command or "")
    source = _outside_quotes(raw)
    if ">" in source:
        return True
    if (
        _MUTATING_COMMAND_RX.search(source)
        or _REG_MUTATING_RX.search(source)
        or _DYNAMIC_EVAL_RX.search(source)
        or _QUOTED_INVOKE_RX.search(raw)
    ):
        return True

    # ``run_command`` accepts full shell wrappers too.  Inspect the payload
    # recursively rather than treating a quoted payload as documentation;
    # otherwise `cmd /c del ...` and `powershell -Command "Remove-Item ..."`
    # bypassed the confirmation gate.  A shallow cap prevents pathological
    # nested wrappers from consuming unbounded work.
    if _depth >= 3:
        return False
    for wrapper in (_CMD_WRAPPER_RX, _POWERSHELL_WRAPPER_RX):
        match = wrapper.search(raw)
        if match and command_is_mutating(_strip_outer_quotes(match.group(1)), _depth + 1):
            return True
    return False


CLICK_FAMILY = ("uia_click", "click_on_text", "click_nth", "click_coords")


@dataclass
class ToolSpec:
    name: str
    destructive: bool = False
    reversible: bool = True
    requires_confirmation: bool = False
    requires_focus: bool = False
    expected_runtime: float = 0.5  # seconds, informational for the planner
    settle: float = 0.15  # post-action settle before verification
    base_reliability: float = 0.85  # prior belief, blended with observed stats
    base_confidence: float = 0.85  # confidence assigned to a successful run
    categories: tuple = field(default_factory=tuple)
    fallback_tools: tuple = field(default_factory=tuple)
    verifier: str = ""  # key into core/verify.VERIFIERS
    verifier_arg: str = ""  # static arg ("" = derive from parameter)
    # Max chars of the handler's result text shown to the model per step.
    # >= READ_RESULT_CHARS marks a READ-type tool (see AgentLoop).
    result_chars: int = 300


READ_RESULT_CHARS = 4000


def _T(name, **kw):
    return name, ToolSpec(name=name, **kw)


SPECS = dict(
    [
        # -- Apps & windows ------------------------------------------------------
        _T(
            "open_app",
            expected_runtime=3.0,
            settle=1.5,
            base_confidence=0.85,
            categories=("app",),
            # Was "process_running": a tray-only app (Discord, Spotify) has a
            # process and no window, and verified as a 0.93-confidence success
            # while nothing appeared on screen. See verify._app_visible.
            verifier="app_visible",
        ),
        _T(
            "switch_app",
            expected_runtime=1.0,
            settle=0.6,
            base_confidence=0.85,
            categories=("app", "window"),
            fallback_tools=("open_app",),
            verifier="window_focused",
        ),
        _T(
            "open_project_in_vscode",
            expected_runtime=2.5,
            settle=1.0,
            base_confidence=0.9,
            categories=("app",),
            verifier="process_running",
            verifier_arg="code",
        ),
        _T(
            "window_list",
            base_confidence=0.98,
            categories=("window", "info"),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "window_activate",
            settle=0.6,
            categories=("window",),
            verifier="window_focused",
        ),
        _T(
            "window_close",
            destructive=True,
            reversible=False,
            requires_confirmation=True,
            categories=("window",),
        ),
        _T("window_maximize", settle=0.6, categories=("window",)),
        _T("window_minimize", settle=0.4, categories=("window",)),
        _T("minimize_all", categories=("window",)),
        _T("active_app", base_confidence=0.98, categories=("info",)),
        _T(
            "list_apps",
            base_confidence=0.98,
            categories=("info",),
            result_chars=READ_RESULT_CHARS,
        ),
        # -- Clicking (base confidence reflects each method's real trust level) --
        _T(
            "uia_click",
            settle=0.3,
            base_confidence=0.9,
            base_reliability=0.85,
            categories=("click",),
            fallback_tools=("click_on_text",),
        ),
        _T(
            "uia_find_input",
            settle=0.3,
            base_confidence=0.85,
            categories=("click", "type"),
        ),
        _T(
            "click_on_text",
            settle=0.3,
            base_confidence=0.7,
            base_reliability=0.7,
            categories=("click",),
            verifier="click_landed",
        ),
        _T(
            "click_nth",
            settle=0.3,
            base_confidence=0.6,
            categories=("click",),
            verifier="click_landed",
        ),
        _T(
            "click_coords",
            settle=0.3,
            base_confidence=0.4,
            base_reliability=0.5,
            categories=("click",),
        ),
        # -- Typing / keys -------------------------------------------------------
        _T(
            "type_text", requires_focus=True, base_confidence=0.85, categories=("type",)
        ),
        _T(
            "type_search",
            requires_focus=True,
            settle=0.6,
            base_confidence=0.8,
            categories=("type",),
        ),
        _T("hotkey", settle=0.2, base_confidence=0.9, categories=("type",)),
        _T("press_key", settle=0.2, base_confidence=0.95, categories=("type",)),
        _T("scroll", settle=0.2, base_confidence=0.9, categories=("navigation",)),
        _T(
            "quick_message",
            destructive=True,
            requires_focus=True,
            reversible=False,
            requires_confirmation=True,
            expected_runtime=4.0,
            settle=0.5,
            base_confidence=0.75,
            categories=("messaging",),
        ),
        # -- System / files (the DEFAULT tools -- high trust, real verification) --
        _T(
            "run_command",
            expected_runtime=2.0,
            base_confidence=0.97,
            base_reliability=0.95,
            categories=("system", "file"),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "write_file",
            base_confidence=0.96,
            base_reliability=0.95,
            categories=("file",),
            verifier="file_written",
        ),
        _T(
            "inspect_file",
            expected_runtime=0.4,
            base_confidence=0.98,
            categories=("file", "info"),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "patch_json",
            base_confidence=0.96,
            base_reliability=0.95,
            categories=("file",),
            verifier="file_written",
        ),
        _T(
            "patch_file",
            base_confidence=0.96,
            base_reliability=0.95,
            categories=("file",),
            verifier="file_written",
        ),
        _T(
            "find_file",
            expected_runtime=2.0,
            settle=0.0,
            base_confidence=0.95,
            categories=("file", "info"),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "organize_desktop",
            destructive=True,
            requires_confirmation=True,
            expected_runtime=3.0,
            categories=("file",),
        ),
        _T("screenshot", categories=("system",)),
        _T("mss_screenshot", categories=("system",)),
        _T(
            "get_clipboard",
            base_confidence=0.98,
            categories=("info",),
            result_chars=READ_RESULT_CHARS,
        ),
        _T("set_clipboard", base_confidence=0.95, categories=("system",)),
        _T(
            "lock_screen",
            destructive=True,
            reversible=False,
            requires_confirmation=True,
            categories=("system",),
        ),
        _T("battery_status", base_confidence=0.98, categories=("info",)),
        _T("brightness_get", base_confidence=0.95, categories=("info",)),
        _T("brightness_set", base_confidence=0.9, categories=("system",)),
        _T("volume_mute", base_confidence=0.95, categories=("system",)),
        _T("volume_unmute", base_confidence=0.95, categories=("system",)),
        _T(
            "browser_open",
            expected_runtime=2.0,
            settle=1.0,
            base_confidence=0.9,
            categories=("browser",),
        ),
        _T(
            "browser_navigate",
            expected_runtime=2.0,
            settle=1.0,
            base_confidence=0.92,
            categories=("browser",),
            result_chars=1200,
        ),
        _T(
            "browser_search",
            expected_runtime=2.0,
            settle=1.0,
            base_confidence=0.92,
            categories=("browser",),
            result_chars=1200,
        ),
        _T("browser_close", categories=("browser",)),
        _T(
            "browser_click",
            settle=0.3,
            base_confidence=0.95,
            base_reliability=0.9,
            categories=("browser", "click"),
        ),
        _T(
            "browser_fill",
            settle=0.2,
            base_confidence=0.93,
            categories=("browser", "type"),
        ),
        _T(
            "browser_get_text",
            base_confidence=0.98,
            categories=("browser", "perception"),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "browser_press_key",
            settle=0.2,
            base_confidence=0.9,
            categories=("browser", "type"),
        ),
        _T(
            "open_url",
            expected_runtime=3.0,
            settle=1.5,
            base_confidence=0.85,
            categories=("browser",),
        ),
        _T(
            "check_email",
            expected_runtime=4.0,
            settle=1.0,
            categories=("browser",),
            result_chars=1200,
        ),
        # -- Perception (LLM/vision-backed -- inherently softer) -----------------
        _T(
            "analyze_screen",
            expected_runtime=3.0,
            base_confidence=0.7,
            categories=("perception",),
            result_chars=2000,
        ),
        _T(
            "read_screen_text",
            expected_runtime=3.0,
            base_confidence=0.7,
            categories=("perception",),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "browser_import_cookies",
            expected_runtime=1.0,
            base_confidence=0.75,
            base_reliability=0.7,
            categories=("browser",),
        ),
        _T(
            "browser_use_daily_profile",
            expected_runtime=3.0,
            base_confidence=0.8,
            base_reliability=0.75,
            categories=("browser",),
        ),
        # -- Memory / reminders --------------------------------------------------
        _T("remember", base_confidence=0.98, categories=("memory",)),
        _T("recall", base_confidence=0.9, categories=("memory",), result_chars=1500),
        _T("remind_me", base_confidence=0.95, categories=("memory",)),
        _T(
            "get_reminders",
            base_confidence=0.98,
            categories=("memory", "info"),
            result_chars=1500,
        ),
        _T("schedule_reminder", base_confidence=0.9, categories=("memory",)),
        _T(
            "take_notes",
            requires_focus=True,
            expected_runtime=3.0,
            categories=("type",),
        ),
        # -- Meta (never routed through the Executor, listed for completeness) ---
        _T("ask_user", categories=("meta",)),
        _T("done", categories=("meta",)),
    ]
)

_GENERIC = ToolSpec(name="unknown", base_confidence=0.6, base_reliability=0.6)


class ToolRegistry:
    """Metadata lookups + observed-reliability blending. Opens its own
    read-only-ish connection to memory.db (same established pattern as
    core/task_memory.py) so core/memory.py needs no changes."""

    OBS_MIN_SAMPLES = 5
    OBS_WEIGHT = 0.65

    def __init__(self, db_path=None):
        self.specs = dict(SPECS)
        self._lock = threading.Lock()
        self._conn = None
        try:
            if db_path is None:
                from .config import memory_db_path

                db_path = memory_db_path()
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA busy_timeout=3000")
        except Exception as e:
            print(f"[ToolRegistry] stats db unavailable: {e}")

    def get(self, name):
        return self.specs.get(name, _GENERIC)

    def settle(self, name):
        return self.get(name).settle

    def fallbacks(self, name):
        return self.get(name).fallback_tools

    def is_destructive(self, action, parameter="", resolve_path=None):
        """Single source of truth for the confirmation gate. Mirrors (and
        can replace) AgentLoop._is_destructive."""
        if action == "run_command":
            return command_is_mutating(parameter)
        if action in ("write_file", "patch_json", "patch_file"):
            # Writing a NEW file is harmless; silently replacing an existing
            # one is not, and nothing else in the system checks this.
            try:
                path = str(parameter or "").split("|||", 1)[0].strip()
                if not path:
                    return False
                if callable(resolve_path):
                    path = resolve_path(path)
                return os.path.exists(os.path.expandvars(os.path.expanduser(path)))
            except Exception:
                return False
        normalized_key = "+".join(
            p.strip().lower() for p in str(parameter or "").split("+") if p.strip()
        )
        if action == "hotkey":
            return normalized_key in _DANGEROUS_HOTKEYS
        if action in ("press_key", "browser_press_key"):
            return normalized_key in {"enter", "return", "delete"}
        if action == "click_coords":
            # Raw pixels provide no semantic label from which to distinguish
            # a harmless control from Send/Delete/Buy. It is explicitly a
            # last-resort tool, so require confirmation at this boundary.
            return True
        if action in CLICK_FAMILY + ("browser_click",):
            # Text-targeted irreversible controls are safety-critical even
            # when a model chooses the manual flow instead of quick_message.
            text = str(parameter or "").split("|", 1)[0]
            return bool(_DANGEROUS_CLICK_TEXT_RX.search(text))
        return self.get(action).destructive

    def requires_confirmation(self, action, parameter="", resolve_path=None):
        """Whether AgentLoop must ask before running this action.

        Most confirmation-worthy operations are destructive dynamically (for
        example only overwriting, not creating, a file). Some irreversible
        operations such as sending a message are metadata-driven. Keeping the
        decision here gives the loop one authoritative safety boundary.
        """
        spec = self.get(action)
        return spec.requires_confirmation or self.is_destructive(
            action, parameter, resolve_path=resolve_path
        )

    # -- Learned reliability (design doc #7: statistics, not ML) -------------

    def observed(self, name):
        """(sample_count, success_rate) from the existing actions table."""
        if not self._conn:
            return 0, 0.0
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(success),0) FROM actions "
                    "WHERE action=?",
                    (name,),
                ).fetchone()
            n, s = row or (0, 0)
            return int(n), (float(s) / n if n else 0.0)
        except Exception:
            return 0, 0.0

    def reliability(self, name):
        """Prior blended with observed history once there's enough data.
        This is what makes 'strategy X keeps failing, prefer Y' emerge
        from real usage instead of hardcoding."""
        spec = self.get(name)
        n, rate = self.observed(name)
        if n < self.OBS_MIN_SAMPLES:
            return spec.base_reliability
        w = self.OBS_WEIGHT
        return round((1 - w) * spec.base_reliability + w * rate, 3)

    def describe(self, name):
        """One-line metadata summary, usable in prompts or logs."""
        s = self.get(name)
        flags = [
            f
            for f, on in (
                ("destructive", s.destructive),
                ("irreversible", not s.reversible),
                ("needs-confirm", s.requires_confirmation),
                ("needs-focus", s.requires_focus),
            )
            if on
        ]
        return (
            f"{name}: rel={self.reliability(name)} run~{s.expected_runtime}s "
            f"[{','.join(s.categories) or '-'}]"
            + (f" ({', '.join(flags)})" if flags else "")
            + (f" fallback->{'/'.join(s.fallback_tools)}" if s.fallback_tools else "")
        )
