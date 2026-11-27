# ============================================================
# AUDIT TRAIL MODULE
# ============================================================
#
# The local audit trail: one JSON line per executed or blocked action, with
# its full arguments, so a later question ("what did it do?") has an answer.
# Local only: never uploaded, never printed to the console, never copied
# into the harness's shared logs.
#
# SECTION INDEX
#   1. ENTRY FORMAT
#   2. ROTATION
#   3. APPEND
#   4. READING THE LAST ENTRIES
#   5. COMMAND LINE
# ============================================================

"""
core/audit.py — append-only local audit trail (config.audit_path()).

    python -m core.audit -n 20      # the last 20 entries, oldest first
"""

import json
import os
import sys
import threading
from datetime import datetime, timedelta

if __package__ in (None, ""):          # `python core/audit.py` as well as -m
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.config import AUDIT_MAX_BYTES, AUDIT_MAX_DAYS, audit_path
else:
    from .config import AUDIT_MAX_BYTES, AUDIT_MAX_DAYS, audit_path

_lock = threading.Lock()


# ============================================================
# ENTRY FORMAT
# ============================================================
# {"ts", "tool", "args", "window", "control", "result", "detail", "guard",
# "ms"}: ts is local ISO time; result is ok | failed | blocked | refused;
# guard is {"decision", "reason", "what", "protected"} (decision: allow,
# approved, denied, refused, unattended, no-prompt, stopped). Text fields are
# cut (MAX_FIELD_CHARS) so one line stays small; args keep MAX_ARGS_CHARS.

MAX_FIELD_CHARS = 500
MAX_ARGS_CHARS = 4000


def _cut(value, limit):
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + f"...[+{len(text) - limit} chars]"


def make_entry(tool, args, result, window="", control="", detail="", guard=None, ms=None):
    entry = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "tool": str(tool or ""),
        "args": _cut(args, MAX_ARGS_CHARS),
        "window": _cut(window, MAX_FIELD_CHARS),
        "control": _cut(control, MAX_FIELD_CHARS),
        "result": str(result or ""),
        "detail": _cut(detail, MAX_FIELD_CHARS),
        "guard": {k: _cut(v, MAX_FIELD_CHARS) for k, v in (guard or {"decision": "allow"}).items()},
    }
    if ms is not None:
        entry["ms"] = int(ms)
    return entry


# ============================================================
# ROTATION
# ============================================================
# Two files: <name>.jsonl (current) and <name>.1.jsonl (older). The current
# one is moved over the older one once it is past half the size limit or its
# first entry is older than half the day limit, so together they hold about
# AUDIT_MAX_BYTES and AUDIT_MAX_DAYS. Only these two MistAI-made files are
# ever replaced.

def older_path(path):
    root, ext = os.path.splitext(path)
    return f"{root}.1{ext or '.jsonl'}"


def _first_ts(path):
    try:
        with open(path, encoding="utf-8") as f:
            line = f.readline()
        return datetime.fromisoformat(json.loads(line)["ts"])
    except Exception:
        return None


def needs_rotation(path, max_bytes=None, max_days=None, now=None):
    max_bytes = AUDIT_MAX_BYTES if max_bytes is None else max_bytes
    max_days = AUDIT_MAX_DAYS if max_days is None else max_days
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size == 0:
        return False
    if size >= max_bytes / 2:
        return True
    first = _first_ts(path)
    return first is not None and (now or datetime.now()) - first > timedelta(days=max_days / 2)


def rotate(path):
    try:
        os.replace(path, older_path(path))
        return True
    except OSError:
        return False


# ============================================================
# APPEND
# ============================================================

def append(entry, path=None):
    """Write one entry (a dict from make_entry). Never raises; returns
    whether it was written."""
    path = path or audit_path()
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            if os.path.isdir(path):
                return False                  # never rotate or write over a folder
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if needs_rotation(path):
                rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception:
        return False


def record(tool, args, result, **kw):
    """make_entry + append."""
    return append(make_entry(tool, args, result, **kw))


# ============================================================
# READING THE LAST ENTRIES
# ============================================================

def tail(n=20, path=None):
    """The last n entries (oldest first), across the older and current
    files. Unreadable lines are skipped."""
    path = path or audit_path()
    lines = []
    for p in (older_path(path), path):
        try:
            with open(p, encoding="utf-8") as f:
                lines += f.readlines()
        except OSError:
            continue
    out = []
    for line in lines[-max(0, int(n)):] if n else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def format_entry(e):
    g = e.get("guard") or {}
    decision = g.get("decision", "")
    head = f"{e.get('ts', '')}  {e.get('tool', ''):<14} {e.get('result', ''):<8} guard={decision}"
    if g.get("reason"):
        head += f" ({g['reason']})"
    lines = [head]
    if e.get("window") or e.get("control"):
        lines.append(f"    window: {e.get('window', '')!r}  control: {e.get('control', '')!r}")
    lines.append(f"    args: {str(e.get('args', ''))[:300]!r}")
    if g.get("what"):
        lines.append(f"    what: {g['what']}" + (f"  PROTECTED: {g['protected']}" if g.get("protected") else ""))
    if e.get("detail"):
        lines.append(f"    detail: {str(e['detail'])[:200]!r}")
    return "\n".join(lines)


# ============================================================
# COMMAND LINE
# ============================================================

def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Show the last entries of MistAI's local audit trail.")
    ap.add_argument("-n", type=int, default=20, help="how many entries (default 20)")
    ap.add_argument("--json", action="store_true", help="print raw JSON lines")
    ap.add_argument("--path", default=None, help="audit file (default: config.audit_path())")
    args = ap.parse_args(argv)
    path = args.path or audit_path()
    entries = tail(args.n, path)
    if not entries:
        print(f"No audit entries in {path}")
        return 0
    print(f"Last {len(entries)} audit entries ({path}); local only, may contain typed text:")
    for e in entries:
        print(json.dumps(e, ensure_ascii=False) if args.json else format_entry(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
