# ============================================================
# WORKSPACE MODULE
# ============================================================
#
# Files and projects: the live workspace state (project root, files, last
# command) and the bounded filename search behind find_file.
#
# SECTION INDEX
#   1. WORKSPACE LIMITS
#   2. FILE INFO
#   3. WORKSPACE STATE
#   4. FILE SEARCH
#   5. SEARCH LIMITS AND SKIPPED FOLDERS
#   6. JUNCTION CHECK
#   7. RESULT RANKING
#   8. FILENAME SEARCH
#   9. SEARCH RESULT TEXT
# ============================================================

"""
core/workspace.py — live workspace state (design doc #1).

Instead of rediscovering the desktop every phase, MistAI maintains ONE
authoritative Workspace object:

- current project + directory tree
- known files (+ cached contents when small)
- open windows / active app
- last terminal command + output
- last browser destination
- task outputs and execution history

It stays fresh by SUBSCRIBING to the event bus (WindowFocused, FileCreated,
ActionExecuted, ...) rather than polling, and the Executor records every
action into it. The agent's system prompt embeds context_block(), so the
planner reads from the Workspace first before re-querying the system with
analyze_screen / read_screen_text / run_command.
"""

import os
import hashlib
import json
import threading
import time
from collections import OrderedDict, deque
import fnmatch

from . import live_state as events


# ============================================================
# WORKSPACE LIMITS
# ============================================================

_MAX_KNOWN_FILES = 400
_MAX_CACHED_CONTENT = 50_000  # chars per file
_MAX_RETURNED_CONTENT = 20_000  # chars handed back by inspect_file
_HISTORY = 100


# ============================================================
# FILE INFO
# ============================================================

class FileInfo:
    __slots__ = ("path", "size", "mtime", "content")

    def __init__(self, path, size=0, mtime=0.0, content=None):
        self.path = path
        self.size = size
        self.mtime = mtime
        self.content = content


# ============================================================
# WORKSPACE STATE
# ============================================================

class Workspace:
    def __init__(self, bus):
        self.bus = bus
        self._lock = threading.RLock()

        self.current_project = None
        self.project_root = None
        self.known_files = OrderedDict()   # path -> FileInfo
        self.open_windows = []
        self.active_window = ""
        self.active_app = ""
        self.clipboard_preview = ""
        self.browser = {"last_url": ""}
        self.terminal = {"last_command": "", "last_output": "", "ok": None}
        self.task_outputs = {}             # task_id -> {node_id: summary}
        self.execution_history = deque(maxlen=_HISTORY)
        # A destructive replacement is only safe when it is based on a recent
        # read of the actual file, not on a model's guess about its contents.
        # ``path -> snapshot`` stores that evidence without creating another
        # project-state system beside Workspace.
        self.file_snapshots = {}

        # Keep fresh via events. Handlers must stay FAST (bus dispatch thread).
        bus.subscribe(events.WINDOW_FOCUSED, self._on_focus)
        bus.subscribe(events.WINDOW_OPENED, self._on_win_open)
        bus.subscribe(events.WINDOW_CLOSED, self._on_win_close)
        bus.subscribe(events.CLIPBOARD_CHANGED, self._on_clip)
        bus.subscribe(events.FILE_CREATED, self._on_file_touch)
        bus.subscribe(events.FILE_MODIFIED, self._on_file_touch)
        bus.subscribe(events.FILE_DELETED, self._on_file_gone)

    def start_request(self):
        """Forget the previous request's run (last command, action history,
        phase outputs) so a new user request isn't planned around it. The
        project, known files, file snapshots and browser URL are kept."""
        with self._lock:
            self.terminal = {"last_command": "", "last_output": "", "ok": None}
            self.task_outputs.clear()
            self.execution_history.clear()

    def reset_task_state(self):
        """Forget everything learned from earlier tasks (project, files,
        last command, history, snapshots). Live window/clipboard state is
        kept: watchers keep it current. Used between independent sessions,
        e.g. by the scenario harness."""
        self.start_request()
        with self._lock:
            self.current_project = None
            self.project_root = None
            self.known_files.clear()
            self.browser = {"last_url": ""}
            self.file_snapshots.clear()

    # -- Event handlers ------------------------------------------------------

    def _on_focus(self, ev):
        with self._lock:
            self.active_window = ev.data.get("title", "")

    def _on_win_open(self, ev):
        t = ev.data.get("title", "")
        with self._lock:
            if t and t not in self.open_windows:
                self.open_windows.append(t)
                del self.open_windows[:-40]

    def _on_win_close(self, ev):
        t = ev.data.get("title", "")
        with self._lock:
            try:
                self.open_windows.remove(t)
            except ValueError:
                pass

    def _on_clip(self, ev):
        with self._lock:
            self.clipboard_preview = ev.data.get("preview", "")

    def _on_file_touch(self, ev):
        path = ev.data.get("path", "")
        if path:
            self.note_file(path)  # stat only -- no content read on the bus thread

    def _on_file_gone(self, ev):
        with self._lock:
            self.known_files.pop(ev.data.get("path", ""), None)

    # -- Project -------------------------------------------------------------

    def set_project(self, name, root=None):
        """Declare the active project. Publishes ProjectSet so the
        FileWatcher starts watching the root, and pre-scans the tree so
        the planner knows what already exists without a run_command."""
        with self._lock:
            self.current_project = name
            self.project_root = os.path.abspath(root) if root else None
        if root:
            self.scan_tree(root)
        self.bus.publish(events.PROJECT_SET,
                         {"name": name, "root": self.project_root}, "Workspace")

    def scan_tree(self, root, max_files=200):
        """Cheap bounded walk to seed known_files (no contents)."""
        try:
            count = 0
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in ("node_modules", ".git", "__pycache__", "venv")]
                for f in filenames:
                    self.note_file(os.path.join(dirpath, f))
                    count += 1
                    if count >= max_files:
                        return count
            return count
        except Exception as e:
            print(f"[Workspace] scan_tree: {e}")
            return 0

    def directory_tree(self, max_entries=40):
        """Compact relative listing of the current project's known files."""
        with self._lock:
            root = self.project_root
            paths = list(self.known_files.keys())
        if not root:
            return []
        rel = sorted(p[len(root):].lstrip("\\/") for p in paths if p.startswith(root))
        return rel[:max_entries]

    def project_clues(self):
        """Small configuration summary for migration planning, not a file dump."""
        with self._lock:
            root = self.project_root
        if not root:
            return ""
        package = os.path.join(root, "package.json")
        if not os.path.isfile(package):
            return ""
        content = self.get_file(package)
        try:
            data = json.loads(content or "")
        except (TypeError, ValueError):
            return "package.json exists but could not be parsed"
        if not isinstance(data, dict):
            return "package.json exists but is not a JSON object"
        parts = ["package.json"]
        for key in ("scripts", "dependencies", "devDependencies"):
            value = data.get(key)
            if isinstance(value, dict) and value:
                parts.append(f"{key}: {', '.join(sorted(map(str, value))[:10])}")
        return "; ".join(parts)

    # -- Files ---------------------------------------------------------------

    def note_file(self, path, content=None):
        """Record that a file exists (and optionally cache its content).
        Called by watchers on FS events and by the Executor after write_file."""
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            return
        with self._lock:
            info = self.known_files.get(path) or FileInfo(path)
            info.size, info.mtime = size, mtime
            if content is not None and len(content) <= _MAX_CACHED_CONTENT:
                info.content = content
            self.known_files[path] = info
            self.known_files.move_to_end(path)
            while len(self.known_files) > _MAX_KNOWN_FILES:
                self.known_files.popitem(last=False)

    def get_file(self, path):
        """Cached content if still fresh (mtime match), else re-read + cache.
        Returns None if the file doesn't exist. This is the 'read from
        Workspace first' path for planner decisions."""
        try:
            st = os.stat(path)
        except OSError:
            with self._lock:
                self.known_files.pop(path, None)
            return None
        with self._lock:
            info = self.known_files.get(path)
            if info and info.content is not None and abs(info.mtime - st.st_mtime) < 0.001:
                return info.content
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_MAX_CACHED_CONTENT + 1)
        except Exception:
            return None
        self.note_file(path, content if len(content) <= _MAX_CACHED_CONTENT else None)
        return content

    def inspect_file(
        self, path, max_chars=_MAX_CACHED_CONTENT, return_chars=_MAX_RETURNED_CONTENT,
        withhold_content=False,
    ):
        """Read and fingerprint one existing text file for a later safe edit.

        The hash is recomputed by ``has_fresh_file_snapshot`` immediately
        before a mutation, so a model cannot inspect an old package.json and
        overwrite a version the user edited in the meantime.

        Binary / non-UTF-8 files and ``withhold_content`` (secret files) get
        size and hash only. The stored snapshot caches up to ``max_chars``;
        the returned copy carries at most ``return_chars`` of content.
        """
        path = os.path.abspath(path)
        try:
            st = os.stat(path)
            if not os.path.isfile(path):
                return None
            with open(path, "rb") as f:
                raw = f.read()
            binary = b"\0" in raw
            content = None
            if not binary and not withhold_content:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    binary = True
            snapshot = {
                "path": path,
                "size": st.st_size,
                "mtime_ns": getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "inspected_at": time.time(),
                "content": content if content is not None and len(content) <= max_chars else None,
                "truncated": content is not None and len(content) > max_chars,
                "binary": binary,
                "withheld": bool(withhold_content),
            }
        except (OSError, UnicodeError):
            return None
        with self._lock:
            self.file_snapshots[path] = snapshot
        # Size/mtime are registered even when content is withheld.
        self.note_file(path, snapshot["content"])
        returned = dict(snapshot)
        if content is not None:
            returned["content"] = content[:return_chars]
            returned["truncated"] = len(content) > return_chars
        return returned

    def has_fresh_file_snapshot(self, path, max_age=300):
        """True only if the path was inspected recently and is unchanged."""
        path = os.path.abspath(path)
        with self._lock:
            snapshot = self.file_snapshots.get(path)
        if not snapshot or time.time() - snapshot["inspected_at"] > max_age:
            return False
        try:
            st = os.stat(path)
            if st.st_size != snapshot["size"]:
                return False
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            return digest == snapshot["sha256"]
        except (OSError, UnicodeError):
            return False

    def phase_evidence(self, since=0.0, limit=4):
        """Compact, grounded evidence for a planner phase summary."""
        with self._lock:
            entries = [
                h for h in self.execution_history
                if h["ts"] >= since and h["success"]
            ][-limit:]
        return "; ".join(
            f"{h['action']}({h['parameter'][:45]})"
            for h in entries
        )

    def file_exists(self, path):
        exists = os.path.exists(path)
        if exists:
            self.note_file(path)
        return exists

    # -- Execution history ---------------------------------------------------

    def record_execution(self, action, parameter, success, confidence, detail=""):
        entry = {
            "action": action,
            "parameter": str(parameter)[:80],
            "success": bool(success),
            "confidence": round(float(confidence), 2),
            "ts": time.time(),
        }
        with self._lock:
            self.execution_history.append(entry)
            if action == "run_command":
                self.terminal = {"last_command": str(parameter)[:120],
                                 "last_output": (detail or "")[:300],
                                 "ok": bool(success)}
            elif action in ("browser_navigate", "browser_open", "browser_search", "open_url"):
                self.browser["last_url"] = str(parameter)[:200]
            elif action == "write_file" and success and "|||" in str(parameter):
                path = str(parameter).split("|||", 1)[0]
                content = str(parameter).split("|||", 1)[1]
                # note_file takes its own lock path; do it outside ours
        if action == "write_file" and success and "|||" in str(parameter):
            p, c = str(parameter).split("|||", 1)
            self.note_file(p.strip(), c)

    def record_task_output(self, task_id, node_id, summary):
        with self._lock:
            self.task_outputs.setdefault(task_id, {})[node_id] = (summary or "")[:300]

    # -- The read path for the LLM ------------------------------------------

    def context_block(self, max_files=12):
        """Compact text block for the agent's system prompt: what MistAI
        already knows, so the model doesn't burn steps rediscovering it."""
        with self._lock:
            lines = []
            if self.current_project:
                lines.append(f"- Project: {self.current_project}"
                             + (f" ({self.project_root})" if self.project_root else ""))
            tree = self.directory_tree(max_files) if self.project_root else []
            if tree:
                lines.append("- Project files (already exist -- do NOT recreate): "
                             + ", ".join(tree))
            clues = self.project_clues()
            if clues:
                lines.append(f"- Project configuration clues: {clues}")
            recent = [f for f in reversed(self.known_files.values())][:max_files] \
                if not tree else []
            if recent:
                names = ", ".join(os.path.basename(f.path) for f in recent[:max_files])
                lines.append(f"- Recently seen files: {names}")
            if self.terminal["last_command"]:
                ok = "ok" if self.terminal["ok"] else ("FAILED" if self.terminal["ok"] is False else "?")
                out = self.terminal["last_output"][:120].replace("\n", " ")
                lines.append(f"- Last command ({ok}): {self.terminal['last_command']}"
                             + (f" -> {out}" if out else ""))
            if self.browser["last_url"]:
                lines.append(f"- Browser last sent to: {self.browser['last_url']}")
            hist = list(self.execution_history)[-5:]
            if hist:
                acts = "; ".join(
                    f"{h['action']}({h['parameter'][:30]})"
                    f"{'ok' if h['success'] else 'FAIL'}@{h['confidence']}"
                    for h in hist)
                lines.append(f"- Recent actions: {acts}")
            return "\n        ".join(lines) if lines else "(nothing recorded yet)"

    def snapshot(self):
        """Debug dump."""
        with self._lock:
            return {
                "project": self.current_project,
                "root": self.project_root,
                "known_files": len(self.known_files),
                "active_window": self.active_window,
                "terminal": dict(self.terminal),
                "browser": dict(self.browser),
                "history": list(self.execution_history)[-10:],
            }


# ============================================================
# FILE SEARCH
# ============================================================

# core/file_search.py — bounded, ranked filename search for the find_file action.
#
# Searches real folders (no Windows Search index, no fuzzy matching), skips
# dependency/build/cache trees, ranks exact filename matches first, and caps
# both the work done and the number of results returned.

# ============================================================
# SEARCH LIMITS AND SKIPPED FOLDERS
# ============================================================

# Directory names never descended into (compared lowercase).
EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "site-packages",
        ".next",
        ".nuxt",
        ".gradle",
        ".tox",
        ".idea",
        "appdata",
        "$recycle.bin",
    }
)

MAX_RESULTS = 25
MAX_DIRS = 20_000
TIME_BUDGET = 4.0  # seconds across all roots
HOME_DEPTH = 3  # the home folder itself is only searched this deep

RANK_EXACT, RANK_PREFIX, RANK_CONTAINS = 0, 1, 2
_RANK_LABEL = {RANK_EXACT: "exact", RANK_PREFIX: "prefix", RANK_CONTAINS: "partial"}


def is_excluded_dir(name):
    n = name.lower()
    return n in EXCLUDED_DIRS or n.endswith("cache") or n.endswith("_cache")


# ============================================================
# JUNCTION CHECK
# ============================================================

def _is_junction(path):
    # Legacy profile junctions ("Local Settings", "Application Data", ...)
    # loop back into AppData; never follow them.
    isjunction = getattr(os.path, "isjunction", None)
    try:
        return os.path.islink(path) or bool(isjunction and isjunction(path))
    except OSError:
        return True


# ============================================================
# RESULT RANKING
# ============================================================

def _rank(query, name):
    """Lower is better; None = no match. Wildcard queries are exact-or-nothing."""
    q, n = query.lower(), name.lower()
    if any(c in q for c in "*?["):
        return RANK_EXACT if fnmatch.fnmatchcase(n, q) else None
    if n == q:
        return RANK_EXACT
    stem = os.path.splitext(n)[0]
    if stem == q or n.startswith(q):
        return RANK_PREFIX
    # A dotfile query (".env") matching inside an unrelated name
    # ("jest.environment.js") is noise, not a partial match.
    if not q.startswith(".") and q in n:
        return RANK_CONTAINS
    return None


# ============================================================
# FILENAME SEARCH
# ============================================================

def search(query, roots, max_results=MAX_RESULTS, max_dirs=MAX_DIRS,
           time_budget=TIME_BUDGET, depth_limits=None):
    """Returns (matches, info). matches: [(rank, path)] best first (rank,
    then root order, then shorter path), at most max_results.
    info: {"roots", "dirs", "truncated", "total"}. Roots listed in
    depth_limits are shallow sweeps (e.g. home) that also skip dot-folders."""
    depth_limits = depth_limits or {}
    deadline = time.monotonic() + time_budget
    found, seen_roots, dirs = {}, [], 0
    truncated = False
    for root_idx, root in enumerate(roots):
        if not root or not os.path.isdir(root):
            continue
        root = os.path.abspath(root)
        if any(os.path.normcase(root) == os.path.normcase(r) for r in seen_roots):
            continue
        seen_roots.append(root)
        limit = depth_limits.get(root)
        base_depth = root.rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            dirs += 1
            if dirs > max_dirs or time.monotonic() > deadline:
                truncated = True
                break
            depth = dirpath.rstrip("\\/").count(os.sep) - base_depth
            # Don't re-walk another root nested inside this one.
            dirnames[:] = [
                d for d in dirnames
                if not is_excluded_dir(d)
                and not (limit is not None and (depth >= limit or d.startswith(".")))
                and not _is_junction(os.path.join(dirpath, d))
                and not any(
                    os.path.normcase(os.path.join(dirpath, d)) == os.path.normcase(r)
                    for r in roots if r
                )
            ]
            for name in filenames + dirnames:
                r = _rank(query, name)
                if r is not None:
                    path = os.path.join(dirpath, name)
                    found.setdefault(path, (r, root_idx))
        if truncated:
            break
    ordered = sorted(found.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0].lower()))
    return (
        [(rank, path) for path, (rank, _root) in ordered[:max_results]],
        {"roots": seen_roots, "dirs": dirs, "truncated": truncated, "total": len(found)},
    )


# ============================================================
# SEARCH RESULT TEXT
# ============================================================

def describe(query, matches, info):
    """Result text for the model."""
    roots = "; ".join(info["roots"]) or "(no searchable folders)"
    head = f"find_file '{query}' searched: {roots}."
    if not matches:
        tail = " Stopped early (search budget hit) -- try an absolute folder." if info["truncated"] else ""
        return f"{head} NO MATCHES.{tail}"
    lines = [
        f"{head} {info['total']} match(es)"
        + (f", showing best {len(matches)}" if info["total"] > len(matches) else "")
        + (" (stopped early: search budget hit)" if info["truncated"] else "")
        + ". Exact filename matches first:"
    ]
    for rank, path in matches:
        kind = "dir" if os.path.isdir(path) else "file"
        lines.append(f"[{_RANK_LABEL[rank]} {kind}] {path}")
    return "\n".join(lines)
