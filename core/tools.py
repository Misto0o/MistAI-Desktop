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

# PowerShell/cmd verbs that destroy or overwrite data. They count ANYWHERE in a
# command as a whole token (see command_is_mutating): inside { } script blocks,
# ( ) and $( ) sub-expressions, loops and quoted strings, which PowerShell can
# run as well. A confirmation beats a miss, so `Get-Help Remove-Item` asks too.
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

_MUTATING_VERB_SET = frozenset(MUTATING_COMMAND_VERBS)
_DYNAMIC_EVAL_TOKENS = frozenset({"invoke-expression", "iex"})
# A token ends at whitespace or a PowerShell/cmd operator or quote character.
_TOKEN_END_RX = re.compile(r"[\s;|&(){}\[\],=+'\"`<>@$!]+")
_PATH_SEP_RX = re.compile(r"[\\/]")
_PROGRAM_EXT_RX = re.compile(r"\.(?:exe|com|bat|cmd)$")
_REG_MUTATING_RX = re.compile(
    r"(?<![\w-])reg(?:\.exe)?\s+(?:add|delete|restore|load|unload|save)(?![\w-])",
    re.IGNORECASE,
)
# & applied to a string, a variable or an expression (what it runs is only known at
# run time); `& { ... }` script blocks are scanned like any other text.
_CALL_ON_VALUE_RX = re.compile(r"(?<!&)&\s*['\"$(]")
# .NET file-system calls: [IO.File]::Move, [System.IO.Directory]::Delete,
# New-Object System.IO.StreamWriter, and FileSystemInfo mutators on an object
# ((Get-Item x).Delete(), $_.MoveTo(...), `% Delete`).
_DOTNET_IO_RX = re.compile(r"\[\s*(?:system\s*\.\s*)?io\s*\.|(?<![\w.])system\.io\.", re.IGNORECASE)
_FS_MUTATOR_RX = re.compile(
    r"(?:\.|(?:^|[\s;|{(])(?:%|foreach-object)\s+(?:-membername\s+)?)"
    r"(?:delete|moveto|copyto)(?![\w-])",
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


def _command_tokens(text):
    """Lower-cased tokens of a command, quotes and comments included. A path
    token is reduced to its last segment and a program extension is dropped,
    so C:\\Windows\\System32\\robocopy.exe and Microsoft.PowerShell.Management\\
    Remove-Item count; a verb only inside a longer word or file name
    (copyright.txt, Format-Table, report-copy.docx) never does."""
    for tok in _TOKEN_END_RX.split(text.lower()):
        if tok:
            yield _PROGRAM_EXT_RX.sub("", _PATH_SEP_RX.split(tok)[-1])


def command_is_mutating(command, _depth=0):
    """Whether a shell command can overwrite, destroy, or alter state. Fails
    closed with one scan of the whole text (no PowerShell parser): mutating if a
    known verb is any token anywhere (script blocks, sub-expressions, loops and
    quoted strings included, since PowerShell can run those), iex/Invoke-Expression
    is used, & is applied to a string/variable/expression, a .NET System.IO call
    or FileSystemInfo mutator appears, `reg add|delete|...` runs, or output is
    redirected outside quotes (``Get-Date > status.txt``). A command made only of
    read-only verbs and arguments stays read-only.
    """
    raw = str(command or "")
    if ">" in _outside_quotes(raw):
        return True
    for tok in _command_tokens(raw):
        if tok in _MUTATING_VERB_SET or tok in _DYNAMIC_EVAL_TOKENS:
            return True
    if (
        _REG_MUTATING_RX.search(raw)
        or _CALL_ON_VALUE_RX.search(raw)
        or _DOTNET_IO_RX.search(raw)
        or _FS_MUTATOR_RX.search(raw)
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
    # Earliest end of that settle with window events (core/live_state.py
    # settle, SETTLE_FLOORS below); None = always the full settle, as before.
    settle_floor: object = None
    base_reliability: float = 0.85  # prior belief, blended with observed stats
    base_confidence: float = 0.85  # confidence assigned to a successful run
    categories: tuple = field(default_factory=tuple)
    fallback_tools: tuple = field(default_factory=tuple)
    verifier: str = ""  # key into core/verify.VERIFIERS
    verifier_arg: str = ""  # static arg ("" = derive from parameter)
    # Max chars of the handler's result text shown to the model per step.
    # >= READ_RESULT_CHARS marks a READ-type tool (see AgentLoop).
    result_chars: int = 300
    # The model's playbook (filled from _PLAYBOOK below): what it does in one
    # line, the exact parameter format (separators verbatim), longer details
    # and an optional example. The prompt's action menu is generated from it.
    purpose: str = ""
    args: str = ""
    limits: str = ""
    example: str = ""


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
        # Moves and copies that never overwrite; the result lists the destination.
        _T(
            "move_files",
            destructive=True,
            reversible=False,
            requires_confirmation=True,
            expected_runtime=1.0,
            settle=0.0,
            base_confidence=0.96,
            base_reliability=0.95,
            categories=("file",),
            result_chars=READ_RESULT_CHARS,
        ),
        _T(
            "copy_files",
            destructive=True,
            reversible=False,
            requires_confirmation=True,
            expected_runtime=1.0,
            settle=0.0,
            base_confidence=0.96,
            base_reliability=0.95,
            categories=("file",),
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
        # Read-only: returns an action's playbook entry (no screen, no input).
        _T(
            "describe_action",
            settle=0.0,
            base_confidence=0.99,
            categories=("info",),
            result_chars=1500,
        ),
        # -- Meta (never routed through the Executor, listed for completeness) ---
        _T("ask_user", categories=("meta",)),
        _T("done", categories=("meta",)),
    ]
)

# The model's playbook: name -> (purpose, args, limits[, example]). purpose is
# the one short line the menu always shows; args is the exact parameter format
# ("" = no parameter, separators verbatim); limits holds the longer details that
# describe_action returns. "{submit_hint}" is filled by action_menu().
_PLAYBOOK = {
    "open_app": ("Open an app (switches to it if already running)", "app name"),
    "switch_app": ("Switch to a running app", "app name",
                   'Preferred over open_app when listed in "Open apps".'),
    "open_project_in_vscode": (
        "Open an EXISTING project folder in a NEW VS Code window", "folder path",
        "Use it instead of open_app/switch_app('code'), which reuse an unrelated window. "
        "The folder must come from the user or reliable VS Code evidence; it never "
        "creates or edits files."),
    "window_list": ("List all open windows", ""),
    "window_activate": ("Bring a window into focus", "window title"),
    "window_close": ("Close a window", "window title"),
    "window_maximize": ("Maximize a window", "window title"),
    "window_minimize": ("Minimize a window", "window title"),
    "minimize_all": ("Minimize all windows (ONLY when the user explicitly asks)", ""),
    "active_app": ("Show the active application", ""),
    "list_apps": ("List running applications", ""),
    "uia_click": ("Click a native-app element by name via the accessibility tree",
                  "element name",
                  'If an edit control field lacks an exact text match, try control type "Edit".'),
    "uia_find_input": ("Click into a native app's text input, chat field or search bar "
                       "to focus it", ""),
    "click_on_text": ("Find and click text via OCR/Vision", "text"),
    "click_nth": ("Click the Nth item of a type", "N|type"),
    "click_coords": ("Click raw pixels (LAST RESORT ONLY)", "x,y",
                     "Use exact coordinates verified via read_screen_text."),
    "type_text": (
        "Type into the app you opened/switched to, replacing the field (with no such app "
        "it only appends). It does NOT submit: follow it with press_key('enter'){submit_hint}",
        "text",
        "For a command, expression, or form, submit it with press_key('enter') or end the "
        "text with the app's submit key."),
    "type_search": ("Type text and press Enter", "text"),
    "hotkey": ("Press a key combo", "ctrl+t"),
    "press_key": ("Press one key (a combo is hotkey)", "key"),
    "scroll": ("Scroll the window", "up or down"),
    "quick_message": (
        "Send a message to a named person (try this first)", "app|recipient|message",
        "Uses the app's own quick-switch/search hotkey. ALWAYS TRY THIS FIRST for messaging "
        "a SPECIFIC, CLEARLY NAMED person; if it fails, use the manual flow.",
        "discord|Alex|running 5 minutes late"),
    "run_command": ("Run PowerShell, get exact stdout/stderr (your DEFAULT tool for system "
                    "tasks)", "the command"),
    "write_file": (
        "Create a file; on an existing file it REPLACES THE ENTIRE FILE (NEVER appends)",
        "path|||complete content",
        "It NEVER merges. On an existing file, inspect_file is required first and user "
        'confirmation is required before execution. Never use write_file(existing_file, "a '
        'new section") expecting an append: that would discard the old file. The path MUST '
        'include a filename, never just "{desktop}|||hello" or a literal C:\\Users\\... path.',
        "{desktop}\\status.txt|||hello"),
    "inspect_file": ("Read and fingerprint an existing file before changing it", "file path",
                     "REQUIRED before replacing a pre-existing file with write_file or "
                     "patch_json."),
    "patch_json": ("Merge JSON properties into an inspected existing JSON/config file",
                   "path|||json object",
                   "Use this for package.json changes so scripts and dependencies are "
                   "preserved."),
    "patch_file": (
        "Replace ONE exact, unique fragment in an inspected existing text file",
        "path|||exact existing text|||replacement text",
        'Use this for a source edit such as adding a section: replace a unique anchor like '
        '"</main>" with the new section plus "</main>". It refuses missing or ambiguous '
        "anchors and does NOT append automatically.",
        "{documents}\\site\\index.html|||</main>|||<section>About</section></main>"),
    "find_file": (
        "Find a file by name; with several results, act only on the one whose filename "
        "matches what was asked, after checking its path",
        'filename, wildcard like "*.pdf", or "folder\\name"',
        "Searches the workspace root, Desktop, Documents, Downloads, home (skips "
        "node_modules/.git/build/cache)."),
    "move_files": (
        "Move a file, folder or glob (*.txt) into a folder, created if missing; never "
        "overwrites (any name clash moves nothing)",
        "source|||destination folder",
        "The destination is always a folder; if it exists as a FILE the move is refused. "
        "Any name that already exists there means nothing moves and the clashes are "
        "listed. The result lists what moved and the destination's contents (name and "
        "size). Use full paths or the {desktop}-style placeholders.",
        "{downloads}\\*.pdf|||{documents}\\PDFs"),
    "copy_files": (
        "Copy a file, folder or glob (*.txt) into a folder, created if missing; never "
        "overwrites (any name clash copies nothing)",
        "source|||destination folder",
        "Same rules as move_files; the source stays where it is.",
        "{desktop}\\report.docx|||{documents}\\Backup"),
    "organize_desktop": ("Move loose desktop files into categorized folders", ""),
    "screenshot": ("Take a screenshot", "filename"),
    "mss_screenshot": ("Fast full-screen capture (MSS)", ""),
    "get_clipboard": ("Read the clipboard", ""),
    "set_clipboard": ("Write text to the clipboard", "text"),
    "lock_screen": ("Lock the workstation", ""),
    "battery_status": ("Battery percentage", ""),
    "brightness_get": ("Read screen brightness", ""),
    "brightness_set": ("Set screen brightness", "0-100"),
    "volume_mute": ("Mute system audio", "",
                    "Windows Core Audio API -- deterministic, no toggle guessing."),
    "volume_unmute": ("Unmute system audio", "",
                      "Windows Core Audio API -- deterministic, no toggle guessing."),
    "browser_open": ("Start MistAI's browser", "optional starting URL"),
    "browser_navigate": ("Go to a URL in MistAI's browser", "url",
                         "A channel is youtube.com/@handle; a YouTube search is "
                         "youtube.com/results?search_query=YOUR+TERMS (one step instead "
                         "of six)."),
    "browser_search": ("Web search (DuckDuckGo) in one step", "search query"),
    "browser_close": ("Close MistAI's browser", ""),
    "browser_click": (
        "Click by visible text in MistAI's browser",
        'text, or text|role (e.g. "Subscribe|button")',
        "Use text|role for precision when plain text matches more than one element "
        "(role examples: button, link, checkbox, textbox). Talks to the real page "
        "structure -- raises a real error if nothing matches, never silently clicks the "
        "wrong thing."),
    "browser_fill": ("Fill a form field in MistAI's browser",
                     "field label or placeholder|text to type"),
    "browser_get_text": (
        "Read the page's exact DOM text (trust it, don't re-verify)",
        "optional CSS selector, empty = whole page",
        "Not a vision guess: use this instead of read_screen_text/analyze_screen for "
        "anything inside a browser page."),
    "browser_press_key": (
        "Press a key in MistAI's browser page", 'key name, e.g. "k", "f", "Escape", "Enter"',
        "For a video player use keyboard shortcuts rather than clicking: k or space = "
        "play/pause, f = fullscreen, m = mute, j/l = back/forward 10s, arrow keys = "
        "seek/volume, c = captions. Click once on the video area with browser_click first "
        "to give the player focus."),
    "browser_import_cookies": (
        "Import one domain's cookies from the user's real Firefox (rarely needed)",
        "domain, or domain1|domain2",
        "MistAI's browser AUTO-SEEDS from the user's real Firefox logins on its own "
        'first-ever launch -- do NOT call this for a normal "am I logged in" / "use my '
        "cookies\" request. Only for ONE SPECIFIC domain that wasn't part of the original "
        "seed."),
    "browser_use_daily_profile": (
        "Re-seed MistAI's browser from the user's real Firefox (rarely needed)", "",
        "Only if the user wants to REFRESH with a login added since the original "
        "auto-seed. Not needed on a normal first use."),
    "open_url": ("Open a website in the user's own browser", "url"),
    "check_email": ("Check unread emails", ""),
    "analyze_screen": ("General visual description of the screen", ""),
    "read_screen_text": ("Answer a SPECIFIC question about content VISIBLE on screen",
                         "your question",
                         "Use it to check a conversation header or other UI text you "
                         "need; not to confirm an action that reported success."),
    "remember": ("Store a long-term fact", "fact text"),
    "recall": ("Retrieve stored facts", "search query"),
    "remind_me": ("Add a reminder", "text|due_time"),
    "get_reminders": ("List pending reminders", ""),
    "schedule_reminder": ("Remind N seconds from now", "seconds|text"),
    "describe_action": ("An action's full details: format, limits, example", "action name"),
    "ask_user": ("Ask the user and wait for the answer", "your question"),
    "done": ("End the task (its speech is the answer)", ""),
}
for _name, _doc in _PLAYBOOK.items():
    _doc = tuple(_doc) + ("",) * (4 - len(_doc))
    SPECS[_name].purpose, SPECS[_name].args, SPECS[_name].limits, SPECS[_name].example = _doc

# Settle floors (window events, core/live_state.py settle): the settle above
# stays the maximum; it may end after the floor once the expected window is
# in front and the desktop has been quiet for QUIET_S. Only app launches and
# switches, browser navigation and the UI input actions; every other action
# (and quick_message, open_url, check_email: their window is unknown or the
# step is safety-sensitive) keeps its fixed settle.
SETTLE_FLOORS = {
    "open_app": 0.4, "switch_app": 0.25,
    "window_activate": 0.25, "window_maximize": 0.25, "window_minimize": 0.2,
    "browser_navigate": 0.2, "browser_open": 0.2, "browser_search": 0.2,
    "click_on_text": 0.15, "click_nth": 0.15, "click_coords": 0.15, "uia_click": 0.15,
    "uia_find_input": 0.15, "type_search": 0.3,
    "press_key": 0.1, "hotkey": 0.1, "scroll": 0.1, "type_text": 0.1,
}
for _name, _floor in SETTLE_FLOORS.items():
    SPECS[_name].settle_floor = min(_floor, SPECS[_name].settle)

# Structural parameter checks for the actions with a fixed format. A mismatch
# never blocks the action; the observation then carries the action's entry.
_ARG_RX = {
    "write_file": r"[^|]*\S[^|]*\|\|\|",
    "patch_json": r"[^|]*\S[^|]*\|\|\|",
    "patch_file": r"[^|]*\S[^|]*\|\|\|.+?\|\|\|",
    "move_files": r"[^|]*\S[^|]*\|\|\|\s*\S",
    "copy_files": r"[^|]*\S[^|]*\|\|\|\s*\S",
    "quick_message": r"[^|]*\S[^|]*\|[^|]*\S[^|]*\|\s*\S",
    "browser_fill": r"[^|]*\S[^|]*\|",
    "click_nth": r"\s*\d+\s*\|\s*\S",
    "click_coords": r"\s*\d+\s*,\s*\d+\s*$",
    "brightness_set": r"\s*(100|\d{1,2})\s*%?\s*$",
    "schedule_reminder": r"\s*\d+\s*(\||$)",
    "scroll": r"(?i)\s*(up|down)\b",
}


def _fill(text, submit_hint):
    return text.replace("{submit_hint}", submit_hint)


def action_line(name, submit_hint=""):
    """The menu's one line for an action: name, <parameter format>, purpose."""
    s = SPECS[name]
    return f"- {name}" + (f" <{s.args}>" if s.args else "") + ": " + _fill(s.purpose, submit_hint)


def action_entry(name, submit_hint=""):
    """One action's full playbook entry (describe_action, format notes)."""
    s = SPECS[name]
    text = action_line(name, submit_hint)
    text += "" if text.endswith(".") else "."
    if s.limits:
        text += " " + _fill(s.limits, submit_hint)
    if s.example:
        text += f" Example parameter: {s.example}"
    return text


def action_menu(submit_hint=""):
    """The prompt's AVAILABLE ACTIONS list, generated from SPECS: every action,
    one line each."""
    return "\n".join(action_line(n, submit_hint) for n in SPECS)


def closest_actions(name, n=3):
    """Registered action names closest to ``name`` (spelling only)."""
    import difflib

    return difflib.get_close_matches(str(name or "").strip().lower(), list(SPECS), n=n, cutoff=0.5)


def describe_action(name):
    """describe_action's result: the full entry, or the closest names."""
    key = str(name or "").strip().lower()
    if key in SPECS:
        return action_entry(key)
    close = closest_actions(key)
    return (f"No action named '{str(name)[:40]}'."
            + (f" Closest: {', '.join(close)}." if close else " See AVAILABLE ACTIONS."))


def arg_problem(name, parameter):
    """True when ``parameter`` does not fit ``name``'s fixed format."""
    rx = _ARG_RX.get(name)
    return bool(rx) and not re.match(rx, str(parameter or ""), re.S)


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

    def is_destructive(self, action, parameter="", resolve_path=None, enter_safe=False):
        """Single source of truth for the confirmation gate. Mirrors (and
        can replace) AgentLoop._is_destructive. enter_safe: press_key Enter
        goes to an app this phase opened, in front, outside the
        browser/messaging/mail/terminal categories (Api.enter_target_is_safe)."""
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
            if action == "press_key" and enter_safe and normalized_key in {"enter", "return"}:
                return False
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

    def requires_confirmation(self, action, parameter="", resolve_path=None, enter_safe=False):
        """Whether AgentLoop must ask before running this action.

        Most confirmation-worthy operations are destructive dynamically (for
        example only overwriting, not creating, a file). Some irreversible
        operations such as sending a message are metadata-driven. Keeping the
        decision here gives the loop one authoritative safety boundary.
        """
        spec = self.get(action)
        return spec.requires_confirmation or self.is_destructive(
            action, parameter, resolve_path=resolve_path, enter_safe=enter_safe
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
