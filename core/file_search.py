"""
core/file_search.py — bounded, ranked filename search for the find_file action.

Searches real folders (no Windows Search index, no fuzzy matching), skips
dependency/build/cache trees, ranks exact filename matches first, and caps
both the work done and the number of results returned.
"""

import fnmatch
import os
import time

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


def _is_junction(path):
    # Legacy profile junctions ("Local Settings", "Application Data", ...)
    # loop back into AppData; never follow them.
    isjunction = getattr(os.path, "isjunction", None)
    try:
        return os.path.islink(path) or bool(isjunction and isjunction(path))
    except OSError:
        return True


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
