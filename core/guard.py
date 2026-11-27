# ============================================================
# DESTRUCTIVE-ACTION GUARD MODULE
# ============================================================
#
# One check, run by Executor.run right before an action's input is sent:
# would this delete, erase or overwrite something? It reads the window in
# front, its dialog text and buttons and the focused control (UIA), and the
# action's own target (a numbered control's name, a click's text, a
# command's paths). A held action asks through the existing confirmation
# flow; a protected location asks a second time naming the path; nobody to
# ask (unattended, no prompt) refuses. A refused action sends nothing.
#
# SECTION INDEX
#   1. WORDING
#   2. UI CONTEXT
#   3. DECISION
#   4. PROTECTED LOCATIONS
#   5. PATHS IN A COMMAND OR PARAMETER
#   6. CLASSIFY: UI INPUT
#   7. CLASSIFY: FILES AND COMMANDS
#   8. CLASSIFY
#   9. READING THE UI (REAL DESKTOP)
#   10. GATE
# ============================================================

"""
core/guard.py — destructive-action guard (see CLAUDE.md "Destructive guard").

Pure classification (classify) is separate from the OS probe
(read_ui_context) so tests feed it fake contexts.
"""

import fnmatch
import os
import re
import string
import threading
import time
from dataclasses import dataclass, field

from .config import (
    PROTECTED_KNOWN_FOLDERS, PROTECTED_NAME_PREFIXES, extra_protected_paths,
    memory_db_path, unattended_mode,
)
from .tools import GIT_DESTRUCTIVE_RX, command_is_mutating


# ============================================================
# WORDING
# ============================================================
# Control names that delete/erase/overwrite when activated, and dialog text
# that asks to. Plain "Clear" (Calculator's C key), "Format" (a menu), or
# "Replace" (find and replace) are not destructive alone.

DESTRUCTIVE_NAME_RX = re.compile(
    r"\b(?:delete|deleting|remove|erase|uninstall|discard|wipe|destroy|purge|shred"
    r"|permanently|overwrite|recycle|trash|don'?t\s+save|do\s+not\s+save|reset"
    r"|format\s+(?:the\s+)?(?:disk|drive|volume|partition)"
    r"|empty\s+(?:the\s+)?(?:recycle\s*bin|trash|bin|folder)"
    r"|clear\s+(?:all\s+)?(?:browsing\s+|site\s+|app\s+|user\s+)?data"
    r"|clear\s+(?:everything|cache|cookies|storage)"
    r"|replace\s+(?:the\s+)?(?:files?|existing|in\s+the\s+destination))\b",
    re.IGNORECASE,
)
DESTRUCTIVE_TEXT_RX = re.compile(
    r"\b(?:delete|deleting|deletion|remove|removing|erase|erasing|uninstall|wipe|wiping"
    r"|permanently|and\s+(?:all\s+)?its\s+contents|recycle\s*bin|move\s+to\s+(?:the\s+)?trash"
    r"|cannot\s+be\s+undone|can'?t\s+be\s+undone|irreversible|overwrite|discard"
    r"|replace\s+the\s+files?|format(?:ting)?\s+(?:the\s+|this\s+)?(?:disk|drive|volume|partition)"
    r"|all\s+data\s+on)\b",
    re.IGNORECASE,
)
# Buttons that back out: never held, even on a delete dialog.
SAFE_NAME_RX = re.compile(
    r"^\s*(?:cancel|no|close|keep(?:\s+\w+){0,2}|back|not\s+now|never\s?mind|skip|abort"
    r"|(?:don'?t|do\s+not)\s+(?:delete|remove|erase|uninstall|discard|overwrite|replace))\s*[.!]?\s*$",
    re.IGNORECASE,
)
VERBS = ("delete", "remove", "erase", "uninstall", "discard", "overwrite", "replace",
         "format", "empty", "wipe", "reset", "clear", "move")

# UIA control types: a file/list/tree item (Delete there deletes files) vs a
# text field or document (Delete there edits text).
ITEM_TYPES = frozenset({"ListItem", "TreeItem", "DataItem", "List", "Tree", "DataGrid", "Table"})
TEXT_TYPES = frozenset({"Edit", "Document", "ComboBox"})
BUTTON_TYPES = frozenset({"Button", "SplitButton", "MenuItem", "Hyperlink"})

UI_INPUT_ACTIONS = ("type_text", "type_search", "press_key", "hotkey",
                    "uia_click", "click_on_text", "click_nth", "click_coords")
KEY_ACTIONS = ("type_text", "type_search", "press_key", "hotkey")
FILE_WRITE_ACTIONS = ("write_file", "patch_json", "patch_file", "save_file_as")
# Keys that only move focus or back out: never held on a dialog.
NAV_KEYS = frozenset({"escape", "esc", "tab", "up", "down", "left", "right", "home", "end",
                      "pageup", "pagedown", "pgup", "pgdn", "shift", "ctrl", "alt"})
ACTIVATE_KEYS = frozenset({"enter", "return", "space"})
DELETE_KEYS = frozenset({"delete", "del"})


def is_destructive_name(name):
    return bool(name) and not is_safe_name(name) and bool(DESTRUCTIVE_NAME_RX.search(str(name)))


def is_safe_name(name):
    return bool(SAFE_NAME_RX.match(str(name or "")))


def destructive_sentence(text):
    """The sentence of ``text`` that asks to delete/erase/overwrite, else ""."""
    for part in re.split(r"(?<=[.?!])\s+|\n+", " ".join(str(text or "").split("\n"))):
        if DESTRUCTIVE_TEXT_RX.search(part):
            return " ".join(part.split())[:160]
    return ""


def quoted_names(text):
    """Names in quotes in ``text`` ('Mist.AI', "notes.txt", 'x')."""
    return [a or b or c for a, b, c in
            re.findall(r"'([^'\n]{1,120})'|\"([^\"\n]{1,120})\"|[‘“]([^’”\n]{1,120})[’”]",
                       str(text or ""))]


def plain_what(sentence, fallback_name=""):
    """A short plain description: "delete 'Mist.AI' and its contents"."""
    s = str(sentence or "")
    low = s.lower()
    verb = next((v for v in VERBS if re.search(rf"\b{v}", low)), "delete")
    if "recycle" in low and verb == "move":
        verb = "delete"
    names = quoted_names(s) or ([fallback_name] if fallback_name else [])
    what = f"{'permanently ' if 'permanently' in low else ''}{verb}"
    if names:
        what += f" '{names[0]}'"
    if "contents" in low:
        what += " and its contents"
    elif not names and s:
        what = s.rstrip("?.! ")[:120]
    return what


# ============================================================
# UI CONTEXT
# ============================================================

@dataclass
class UIContext:
    """What the guard knows about the window in front. known=False: nothing
    could be read (every UI rule then needs other evidence)."""
    known: bool = False
    hwnd: int = 0
    title: str = ""
    cls: str = ""
    is_dialog: bool = False
    dialog_text: str = ""            # title + text and button names of a dialog
    buttons: tuple = ()               # button names of a dialog
    focus_type: str = ""              # UIA control type of the focused element
    focus_name: str = ""
    focus_password: bool = False

    @property
    def dialog_destructive(self):
        """The dialog's own text asks to delete/erase/overwrite (button names
        are not counted: a "Don't save" button does not make Save destructive)."""
        lines = self.dialog_text.splitlines()
        text = " ".join(t for t in lines if t.strip() not in self.buttons)
        return self.is_dialog and bool(DESTRUCTIVE_TEXT_RX.search(text))

    @property
    def destructive_buttons(self):
        return [b for b in self.buttons if is_destructive_name(b)] if self.is_dialog else []

    @property
    def focus_is_item(self):
        return self.focus_type in ITEM_TYPES

    @property
    def focus_is_text(self):
        return self.focus_type in TEXT_TYPES


# ============================================================
# DECISION
# ============================================================

@dataclass
class Decision:
    verdict: str = "allow"           # allow | confirm | refuse
    reason: str = ""                 # fixed label (console, audit)
    what: str = ""                   # plain: "delete 'Mist.AI' and its contents"
    question: str = ""               # the confirmation question
    refusal: str = ""                # model-facing text when refused outright
    protected: str = ""              # the protected location involved
    window: str = ""                 # for the audit
    control: str = ""
    password: bool = False           # typing goes into a password field (audit: length only)

    @property
    def held(self):
        return self.verdict != "allow"


def _confirm(reason, what, ctx=None, control="", question=""):
    what = str(what or "").strip()
    q = question or (what[:1].upper() + what[1:] + "?" if what else "Allow this action?")
    return Decision("confirm", reason, what, q, window=getattr(ctx, "title", "") or "",
                    control=control)


def blocked_text(decision, why="the user did not approve it"):
    """The model's observation for a held action that did not run."""
    if decision.refusal:
        return decision.refusal
    what = decision.what or "delete or overwrite something"
    extra = (f" {decision.protected} is a protected location." if decision.protected else "")
    return (f"BLOCKED: this would {what}; {why}. Nothing was sent or changed.{extra} "
            "Ask the user (ask_user) or stop; do not try another way to do the same thing.")


# ============================================================
# PROTECTED LOCATIONS
# ============================================================
# (path, kind): "tree" = the folder and everything in it; "root" = the
# folder itself and anything above it (not files inside it). Built at call
# time, cached for PROTECTED_CACHE_S (the workspace root is added per call).

PROTECTED_CACHE_S = 30.0
_cache = {"at": -1e9, "entries": ()}


def _norm(path):
    try:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(path).strip().strip('"\''))))
    except Exception:
        return ""
    if re.fullmatch(r"[A-Za-z]:", p):
        p += "\\"
    p = os.path.normcase(p)
    return p if re.fullmatch(r"[a-z]:\\", p) else p.rstrip("\\/")


def _letters(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _drive_roots():
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [f"{c}:\\" for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]
    except Exception:
        return []


def _shell_folders():
    out = []
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
            for name in PROTECTED_KNOWN_FOLDERS:
                try:
                    out.append(winreg.QueryValueEx(key, name)[0])
                except OSError:
                    pass
    except Exception:
        pass
    home = os.path.expanduser("~")
    out += [os.path.join(home, d) for d in ("Desktop", "Documents", "Downloads", "Pictures",
                                            "Music", "Videos")]
    out += [os.environ[v] for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
            if os.environ.get(v)]
    return out


def _mistai_siblings(root):
    parent = os.path.dirname(root)
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    return [os.path.join(parent, n) for n in names
            if any(_letters(n).startswith(p) for p in PROTECTED_NAME_PREFIXES)]


def static_protected_entries():
    trees = [repo_root(), *_mistai_siblings(repo_root()),
             os.path.dirname(os.path.join(os.path.expanduser("~"), "MistAI", "x")),
             *extra_protected_paths()]
    db_dir = os.path.dirname(memory_db_path())
    if os.path.basename(db_dir).lower() == "mistai":
        trees.append(db_dir)
    roots = [os.path.expanduser("~"), *_drive_roots(), *_shell_folders(), os.getcwd()]
    out, seen = [], set()
    for kind, paths in (("tree", trees), ("root", roots)):
        for p in paths:
            n = _norm(p)
            if n and n not in seen:
                seen.add(n)
                _DISPLAY[n] = os.path.abspath(os.path.expandvars(os.path.expanduser(str(p))))
                out.append((n, kind))
    return tuple(out)


_DISPLAY = {}


def display(entry):
    """A protected entry as the user would write it (its original case)."""
    return _DISPLAY.get(entry, entry)


def protected_entries(api=None):
    now = time.monotonic()
    if now - _cache["at"] > PROTECTED_CACHE_S:
        _cache["entries"], _cache["at"] = static_protected_entries(), now
    entries = list(_cache["entries"])
    root = getattr(getattr(api, "workspace", None), "project_root", None)
    if root and isinstance(root, str):
        n = _norm(root)
        if n and all(n != e for e, _ in entries):
            entries.append((n, "root"))
    return entries


def protected_hit(path, entries, deleting=True):
    """The protected entry ``path`` touches, else "". Inside a tree always
    counts; the entry itself or an ancestor of one counts when deleting
    (or moving) -- a write next to a root does not."""
    raw = str(path or "").strip().strip('"\'')
    if not raw:
        return ""
    if any(ch in raw for ch in "*?"):
        return _wildcard_hit(raw, entries, deleting)
    n = _norm(raw)
    if not n:
        return ""
    for e, kind in entries:
        if kind == "tree" and (n == e or n.startswith(e.rstrip("\\") + "\\")):
            return e
    if deleting:
        for e, _kind in entries:
            if n == e:
                return e
        for e, _kind in entries:
            if e.startswith(n.rstrip("\\") + "\\"):
                return e
    return ""


def _wildcard_hit(raw, entries, deleting):
    parts = re.split(r"[\\/]", raw)
    i = next(k for k, p in enumerate(parts) if "*" in p or "?" in p)
    base = _norm("\\".join(parts[:i]) or ".") if i else _norm(".")
    pattern = parts[i].lower()
    inside = protected_hit(base, [(e, k) for e, k in entries if k == "tree"], deleting=False)
    if inside:
        return inside
    if not deleting:
        return ""
    prefix = base.rstrip("\\") + "\\"
    for e, _kind in entries:
        if e.startswith(prefix):
            first = e[len(prefix):].split("\\")[0]
            if fnmatch.fnmatch(first, pattern):
                return e
    return ""


def protected_name_hit(name, entries):
    """A protected entry whose folder name is ``name`` (a dialog's quoted
    name, a selected item), else "". Drive roots have no name."""
    key = os.path.normcase(str(name or "").strip().rstrip("\\/"))
    if not key:
        return ""
    for e, _kind in entries:
        if not re.fullmatch(r"[a-z]:\\", e) and os.path.basename(e) == key:
            return e
    return ""


def protected_in_text(text, entries):
    """A protected entry named in ``text`` by full path or quoted name."""
    low = os.path.normcase(str(text or ""))
    for e, kind in entries:
        if kind == "tree" and e in low:
            return e                       # anything inside a tree
        if kind == "root" and re.search(re.escape(e.rstrip("\\")) + r"\\?(?![\\\w])", low):
            return e                       # the root itself, not a path inside it
    for name in quoted_names(text):
        hit = protected_hit(name, entries) if re.match(r"[A-Za-z]:[\\/]|\\\\", name) else \
            protected_name_hit(name, entries)
        if hit:
            return hit
    return ""


# ============================================================
# PATHS IN A COMMAND OR PARAMETER
# ============================================================
# Path-like arguments of a shell command: quoted strings and tokens with a
# drive, separator, ~, $env:/$HOME or %VAR%; a bare name only when it exists
# in the working directory. Flags (-Recurse, /s) are skipped.

_TOKEN_RX = re.compile(r'"([^"]*)"|\'([^\']*)\'|([^\s;|&(){},]+)')
_DELETE_VERB_RX = re.compile(
    r"(?<![\w-])(?:remove-item|ri|rd|rmdir|del|erase|rm|clear-content|clc|clear-recyclebin"
    r"|format|format-volume|clear-disk|initialize-disk|move-item|mi|move|mv|remove-[a-z]+"
    r"|clear-item|sdelete|sdelete64|cipher|rmtree|unlink|rmsync)(?![\w-])"
    r"|robocopy[^;|&]*/(?:mir|purge)\b",
    re.IGNORECASE,
)


def _expand(token):
    t = token
    t = re.sub(r"(?i)\$env:(\w+)", lambda m: os.environ.get(m.group(1), m.group(0)), t)
    t = re.sub(r"(?i)\$home\b", lambda m: os.path.expanduser("~"), t)
    return os.path.expandvars(os.path.expanduser(t))


def command_paths(command, cwd=None):
    cwd = cwd or os.getcwd()
    out = []
    for a, b, c in _TOKEN_RX.findall(str(command or "")):
        tok = a or b or c
        if (not tok or tok.startswith("-") or re.fullmatch(r"/[A-Za-z?]{1,10}(?::\S*)?", tok)
                or re.match(r"[A-Za-z][\w+.-]*://", tok)):
            continue
        t = _expand(tok)
        pathish = bool(re.match(r"[A-Za-z]:|\\\\|~|\.{1,2}[\\/]|\.{1,2}$", t)) or "\\" in t or "/" in t \
            or any(ch in t for ch in "*?")
        if not pathish:
            if not os.path.exists(os.path.join(cwd, t)):
                continue
        if not re.match(r"[A-Za-z]:|\\\\", t):
            t = os.path.join(cwd, t)
        out.append(t)
    return out


# ============================================================
# CLASSIFY: UI INPUT
# ============================================================

def _key(param):
    return "+".join(p.strip().lower() for p in str(param or "").split("+") if p.strip())


def _click_text(action, param):
    text = str(param or "")
    if action in ("click_on_text", "click_nth", "uia_click"):
        text = text.split("|", 1)[0]
    return text.strip()


def _dialog_what(ctx):
    sentence = destructive_sentence(ctx.dialog_text) or destructive_sentence(ctx.title)
    what = plain_what(sentence) if sentence else (
        f"confirm '{ctx.title[:60]}'" if ctx.title else "confirm a delete dialog")
    return what


def classify_ui(action, param, ctx, row=None):
    """Decision for one UI input action. ``row``: the numbered control a
    uia_click targets ({"type", "name", "popup"}), when it is a number."""
    key = _key(param) if action in ("press_key", "hotkey") else ""
    target_name = (row or {}).get("name") or (_click_text(action, param) if action in (
        "uia_click", "click_on_text", "click_nth") else "")

    # (a) type_text into a file/list/tree item: its Ctrl+A + Delete (or the
    # paste) would act on the selected items, not on a text field.
    if action in ("type_text", "type_search") and ctx.known:
        if ctx.focus_is_item:
            return Decision("refuse", "typing into a list item", window=ctx.title,
                            control=ctx.focus_name, refusal=(
                f"NOT SENT: keyboard focus is on a {ctx.focus_type} ('{ctx.focus_name[:60]}'), not "
                "a text field; typing there would select all items and press Delete. Nothing "
                "was typed. Click the text field first (ui_elements, then uia_click its number)."))
        if ctx.focus_type in BUTTON_TYPES and is_destructive_name(ctx.focus_name):
            return Decision("refuse", "typing onto a destructive button", window=ctx.title,
                            control=ctx.focus_name, refusal=(
                f"NOT SENT: keyboard focus is on the '{ctx.focus_name[:60]}' button; typing "
                "could press it. Nothing was typed."))

    # (c) a dialog that asks to delete/erase/overwrite: anything but backing out.
    if ctx.known and ctx.dialog_destructive:
        what = _dialog_what(ctx)
        backing_out = (
            (action == "press_key" and (key in NAV_KEYS or (
                key in ACTIVATE_KEYS and ctx.focus_type in BUTTON_TYPES and is_safe_name(ctx.focus_name))))
            or (action == "hotkey" and key in ("alt+f4", "shift+tab"))
            or (action in ("uia_click", "click_on_text", "click_nth") and is_safe_name(target_name))
        )
        if not backing_out:
            return _confirm("delete dialog in front", what, ctx, control=target_name or ctx.focus_name)

    # (b) Enter/Space on a dialog with a destructive button, when that button
    # has focus or the focus is unknown (Enter may press the default one).
    if (action == "press_key" and key in ACTIVATE_KEYS and ctx.known and ctx.destructive_buttons
            and not (ctx.focus_type in BUTTON_TYPES and not is_destructive_name(ctx.focus_name))):
        name = ctx.focus_name if is_destructive_name(ctx.focus_name) else ctx.destructive_buttons[0]
        return _confirm("destructive dialog button", f"press '{name[:60]}' in '{ctx.title[:50]}'",
                        ctx, control=name,
                        question=f"Press '{name[:60]}' in '{ctx.title[:50]}'? It may delete or "
                                 "discard something.")

    # (b) activating a control whose name deletes/erases/overwrites.
    if action in ("uia_click", "click_on_text", "click_nth") and is_destructive_name(target_name):
        return _confirm("destructive control", f"click '{target_name[:60]}'"
                        + (f" in '{ctx.title[:50]}'" if ctx.title else ""), ctx, control=target_name,
                        question=f"Click '{target_name[:60]}'? It may delete or remove something.")
    if (action == "press_key" and key in ACTIVATE_KEYS and ctx.focus_type in BUTTON_TYPES
            and is_destructive_name(ctx.focus_name)):
        return _confirm("destructive control focused", f"press '{ctx.focus_name[:60]}'", ctx,
                        control=ctx.focus_name,
                        question=f"Press '{ctx.focus_name[:60]}'? It may delete or remove something.")

    # (a) Delete keys on a file/list/tree item (never in a text field).
    if action in ("press_key", "hotkey"):
        parts = key.split("+")
        if parts and parts[-1] in DELETE_KEYS:
            mods = set(parts[:-1])
            permanent = "shift" in mods
            if ctx.known and ctx.focus_is_item:
                name = ctx.focus_name[:60]
                what = (f"{'permanently ' if permanent else ''}delete the selected item(s)"
                        + (f" ('{name}')" if name else ""))
                return _confirm("delete key on a list item", what, ctx, control=ctx.focus_name)
            if action == "hotkey" and permanent and not (ctx.known and ctx.focus_is_text):
                return _confirm("permanent delete key", "permanently delete the selection "
                                "(Shift+Delete)", ctx, control=ctx.focus_name)
    return Decision(window=ctx.title, control=target_name or ctx.focus_name)


# ============================================================
# CLASSIFY: FILES AND COMMANDS
# ============================================================

def classify_paths(action, param, entries, resolve=None, cwd=None):
    """Protected-location check for commands and file tools; Decision."""
    text = str(param or "")
    if callable(resolve):
        try:
            text = resolve(text)
        except Exception:
            pass
    if action == "run_command":
        if not command_is_mutating(text):
            return Decision()
        deleting = bool(_DELETE_VERB_RX.search(text) or GIT_DESTRUCTIVE_RX.search(text))
        paths = command_paths(text, cwd)
        git = GIT_DESTRUCTIVE_RX.search(text)
        if git:
            m = re.search(r"(?<![\w-])git(?:\.exe)?\s+-C\s+(\"[^\"]*\"|'[^']*'|\S+)", text, re.I)
            paths.append(_expand(m.group(1).strip('"\'')) if m else (cwd or os.getcwd()))
        for p in paths:
            hit = display(protected_hit(p, entries, deleting=deleting))
            if hit:
                d = _confirm("protected location", f"{'delete' if deleting else 'change'} files in "
                             f"the protected location {hit}")
                d.protected = hit
                return d
        return Decision()
    if action in FILE_WRITE_ACTIONS:
        path = text.split("|||", 1)[0].strip().strip('"')
        if path and os.path.exists(_expand(path)):
            hit = display(protected_hit(path, entries, deleting=False))
            if hit:
                d = _confirm("protected location", f"overwrite {path} in the protected location {hit}")
                d.protected = hit
                return d
        return Decision()
    if action == "move_files":
        src = text.split("|", 1)[0]
        for p in [s.strip() for s in re.split(r"[;\n]", src) if s.strip()]:
            hit = display(protected_hit(p, entries, deleting=True))
            if hit:
                d = _confirm("protected location", f"move files out of the protected location {hit}")
                d.protected = hit
                return d
    return Decision()


# ============================================================
# CLASSIFY
# ============================================================

def classify(action, param, ctx=None, row=None, entries=(), resolve=None, cwd=None):
    """One Decision for an action about to run. A UI hold that names a
    protected location (dialog text, selected item, target name) is marked
    protected too."""
    ctx = ctx or UIContext()
    if action in UI_INPUT_ACTIONS:
        d = classify_ui(action, param, ctx, row)
        if d.held and entries:
            hit = protected_in_text(ctx.dialog_text, entries) if ctx.dialog_destructive else ""
            if not hit and d.reason in ("delete key on a list item", "permanent delete key"):
                hit = protected_name_hit(ctx.focus_name, entries)
            d.protected = d.protected or (display(hit) if hit else "")
        return d
    return classify_paths(action, param, entries, resolve=resolve, cwd=cwd)


# ============================================================
# READING THE UI (REAL DESKTOP)
# ============================================================
# The foreground window (handle, title, class, size, owner), the focused
# element (UIA type, name, password) and, for a dialog-like window (class
# #32770, an owned window, or a small one), the names of its text and
# buttons. In a thread with PROBE_BUDGET_S; anything unread stays empty.

PROBE_BUDGET_S = 0.8
DIALOG_MAX_SIZE = (900, 500)
DIALOG_TEXT_CHARS = 2000


def read_ui_context(budget=PROBE_BUDGET_S):
    box = {}

    def run():
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            owned = bool(win32gui.GetWindow(hwnd, 4))           # GW_OWNER
            # A small window counts only without a maximize box (a dialog,
            # not a narrow app window such as a resized Notepad).
            style = win32gui.GetWindowLong(hwnd, -16)            # GWL_STYLE
            small = ((r - l) <= DIALOG_MAX_SIZE[0] and (b - t) <= DIALOG_MAX_SIZE[1]
                     and not style & 0x00010000)                 # WS_MAXIMIZEBOX
            ctx = UIContext(known=True, hwnd=hwnd, title=title, cls=cls,
                            is_dialog=cls == "#32770" or owned or small)
            box["ctx"] = ctx
            try:
                from .click_handler import _uia

                iuia = _uia()
                f = iuia.iuia.GetFocusedElement()
                ctx.focus_type = iuia.known_control_type_ids.get(f.CurrentControlType, "")
                ctx.focus_name = " ".join(str(f.CurrentName or "").split())[:200]
                ctx.focus_password = bool(f.CurrentIsPassword)
            except Exception:
                pass
            if ctx.is_dialog:
                from .click_handler import read_controls

                rows = read_controls(hwnd, budget=max(0.2, budget - 0.3)).get("rows") or []
                names = [title] + [r["name"] for r in rows if r.get("name")
                                   and r.get("type") in ("Text", "Button", "SplitButton", "Edit",
                                                         "Document", "Pane", "Window", "Group")]
                ctx.dialog_text = " \n".join(dict.fromkeys(names))[:DIALOG_TEXT_CHARS]
                ctx.buttons = tuple(r["name"] for r in rows
                                    if r.get("type") in ("Button", "SplitButton") and r.get("name"))
        except Exception as e:
            box["error"] = type(e).__name__

    th = threading.Thread(target=run, daemon=True, name="GuardProbe")
    th.start()
    th.join(budget + 0.3)
    return box.get("ctx") or UIContext()


# ============================================================
# GATE
# ============================================================
# gate(api, action, param) -> (Decision, outcome, text). outcome: allow,
# approved, denied, refused, unattended, no-prompt, stopped; text is the
# model's observation when the action must not run (else "").
#
# The question goes through api._guard_ask(question) -> "yes" | "no" |
# "unclear" | None (stopped), which AgentLoop sets around each executor
# call (its _ask_confirmation: Yes/No prompt, voice in voice mode, one retry
# on unclear). api._guard_preapproved == (action, param) when the loop's
# own gate already asked about this step with the guard's question: the
# first question is not asked twice. A protected location always asks its
# second question. Logs fixed labels only.

def _row_for(api, action, param):
    if action != "uia_click":
        return None
    ref = getattr(api, "_control_ref", None)
    nums = getattr(api, "_control_numbers", None)
    if not (callable(ref) and callable(nums)):
        return None
    try:
        num = ref(param)
        got = nums().get(num) if num is not None else None
        return got[1] if got else None
    except Exception:
        return None


def preview(api, action, param):
    """The Decision for an action, reading the UI now (never asks)."""
    ctx = UIContext()
    if action in UI_INPUT_ACTIONS:
        reader = getattr(api, "_guard_ui_context", None)
        try:
            ctx = reader() if callable(reader) else UIContext()
        except Exception:
            ctx = UIContext()
    resolve = getattr(api, "_resolve_path_placeholders", None)
    try:
        entries = protected_entries(api)
    except Exception:
        entries = []
    try:
        d = classify(action, param, ctx, row=_row_for(api, action, param), entries=entries,
                     resolve=resolve if callable(resolve) else None)
        d.password = bool(ctx.focus_password and action in KEY_ACTIONS)
        return d
    except Exception as e:
        print(f"[Guard] check failed ({type(e).__name__}); allowed")
        return Decision()


def _ask(api, question):
    ask = getattr(api, "_guard_ask", None)
    if not callable(ask):
        return "no-prompt"
    try:
        return ask(question)
    except Exception:
        return "no"


def gate(api, action, param):
    d = preview(api, action, param)
    if not d.held:
        return d, "allow", ""
    print(f"[Guard] held ({d.reason}){' protected' if d.protected else ''}")
    if d.verdict == "refuse":
        print("[Guard] refused: nothing sent")
        return d, "refused", blocked_text(d)
    if unattended_mode():
        print("[Guard] blocked (unattended: nobody to ask)")
        return d, "unattended", blocked_text(d, "nobody can approve it in an unattended run")
    pre = getattr(api, "_guard_preapproved", None) == (action, str(param))
    if not pre:
        verdict = _ask(api, d.question)
        if verdict != "yes":
            outcome = {"no-prompt": "no-prompt", None: "stopped"}.get(verdict, "denied")
            print(f"[Guard] blocked ({outcome})")
            why = {"no-prompt": "there was no way to ask the user",
                   "stopped": "the task was stopped"}.get(outcome, "the user did not approve it")
            return d, outcome, blocked_text(d, why)
    if d.protected:
        verdict = _ask(api, f"{d.protected} is a protected location. Really {d.what}? "
                            "Say yes again only if you are sure.")
        if verdict != "yes":
            outcome = {"no-prompt": "no-prompt", None: "stopped"}.get(verdict, "denied")
            print(f"[Guard] blocked ({outcome}, protected location)")
            return d, outcome, blocked_text(d, "the user did not confirm the protected location")
    print("[Guard] approved: runs once")
    return d, "approved", ""
