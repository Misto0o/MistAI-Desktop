# ============================================================
# START APPS MODULE
# ============================================================
#
# The Start menu's app list (Get-StartApps: display name + AppID) for
# open_app: one in-memory copy loaded in the background, and a ranked
# match of an app name against it.
#
# SECTION INDEX
#   1. LOADING THE LIST
#   2. RANKING A NAME
#   3. THE CACHE
# ============================================================

"""
core/start_apps.py -- Start-apps lookup for open_app.

Get-StartApps costs ~1.3 s, plus ~1.5 s to start PowerShell, so the list is
read once in the background (StartAppsCache.preload, from main()) and kept
in memory: a lookup is then a few milliseconds. It is refreshed when a
lookup finds nothing, when it is older than CACHE_TTL_S (the stale copy
still answers at once while a background refresh runs), and after a launch
that showed no window (invalidate).

rank() orders every entry whose name matches: exact > whole word > prefix >
substring, and within one tier an inbox Windows package (its AppID starts
with Microsoft.Windows) before anything else, then the list's own order.
Nothing about a particular app is hardcoded.
"""

import json
import re
import subprocess
import threading
import time


# ============================================================
# LOADING THE LIST
# ============================================================

GET_START_APPS_PS = ("Get-StartApps | Select-Object Name, AppID | "
                     "ConvertTo-Json -Compress")
LOAD_TIMEOUT_S = 15


def load_start_apps(timeout=LOAD_TIMEOUT_S):
    """[(name, app_id), ...] in Get-StartApps order. Raises on failure."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", GET_START_APPS_PS],
        capture_output=True, text=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(result.stdout or "[]")
    if isinstance(data, dict):        # one app: ConvertTo-Json gives an object
        data = [data]
    out = []
    for row in data:
        name, app_id = str(row.get("Name") or "").strip(), str(row.get("AppID") or "").strip()
        if name and app_id:
            out.append((name, app_id))
    return out


# ============================================================
# RANKING A NAME
# ============================================================

TIER_EXACT, TIER_WORD, TIER_PREFIX, TIER_SUBSTRING = 4, 3, 2, 1
TIER_LABELS = {TIER_EXACT: "exact", TIER_WORD: "whole word", TIER_PREFIX: "prefix",
               TIER_SUBSTRING: "substring"}


def _norm(text):
    return " ".join(re.sub(r"[^\w]+", " ", str(text or "").lower()).split())


def match_tier(needle, name):
    """How well an app name matches a display name: TIER_* or 0."""
    n, h = _norm(needle), _norm(name)
    if len(n) < 2 or not h:
        return 0
    if n == h:
        return TIER_EXACT
    if re.search(rf"(?:^| ){re.escape(n)}(?: |$)", h):
        return TIER_WORD
    if h.startswith(n):
        return TIER_PREFIX
    if n.replace(" ", "") in h.replace(" ", ""):
        return TIER_SUBSTRING
    return 0


def is_inbox_package(app_id):
    """An app that ships with Windows: a packaged AppID of a
    Microsoft.Windows* package (Microsoft.Windows.Photos_...!App,
    Microsoft.WindowsCamera_...!App, Microsoft.WindowsCalculator_...)."""
    family = str(app_id or "").split("!", 1)[0]
    return "!" in str(app_id or "") and family.lower().startswith("microsoft.windows")


def rank(needle, entries):
    """Matching entries, best first: dicts {name, app_id, tier, inbox}.
    Order: tier, then inbox Windows package first, then the list order."""
    scored = []
    for i, (name, app_id) in enumerate(entries or ()):
        tier = match_tier(needle, name)
        if tier:
            scored.append((-tier, 0 if is_inbox_package(app_id) else 1, i,
                           {"name": name, "app_id": app_id, "tier": tier,
                            "inbox": is_inbox_package(app_id)}))
    scored.sort(key=lambda s: s[:3])
    return [s[3] for s in scored]


def describe_candidate(c):
    return f"'{c['name']}' ({c['app_id']}, {TIER_LABELS[c['tier']]} match)"


# ============================================================
# THE CACHE
# ============================================================

CACHE_TTL_S = 300.0          # older: answer from it, refresh in the background
MISS_REFRESH_GAP_S = 10.0    # a miss reloads at most this often


class StartAppsCache:
    """The Start-apps list in memory. Thread-safe; loader and clock are
    injectable (tests). Never raises from lookup()."""

    def __init__(self, loader=load_start_apps, clock=time.monotonic, ttl=CACHE_TTL_S):
        self._loader, self._clock, self.ttl = loader, clock, ttl
        self._lock = threading.Lock()
        self._entries = None        # None until the first load finished
        self._loaded_at = None
        self._stale = False
        self._loading = None        # threading.Event of a load in progress
        self.last_source = ""       # "cache" | "stale cache" | "loaded" | "refreshed" | "failed"

    # -- loading ------------------------------------------------------------

    def _load(self):
        """Load now (one at a time); a second caller waits for the first."""
        with self._lock:
            pending = self._loading
            if pending is None:
                pending = self._loading = threading.Event()
                mine = True
            else:
                mine = False
        if not mine:
            pending.wait(LOAD_TIMEOUT_S + 1)
            return self._entries is not None
        t0 = time.perf_counter()
        ok = False
        try:
            entries = self._loader()
            with self._lock:
                self._entries, self._loaded_at, self._stale = entries, self._clock(), False
            ok = True
            print(f"[StartApps] loaded {len(entries)} apps in {time.perf_counter() - t0:.2f}s")
        except Exception as e:
            print(f"[StartApps] load failed ({type(e).__name__})")
        finally:
            with self._lock:
                self._loading = None
            pending.set()
        return ok

    def preload(self):
        """Load in a daemon thread (startup); returns at once."""
        threading.Thread(target=self._load, daemon=True, name="StartAppsLoad").start()

    def invalidate(self):
        """The next lookup refreshes the list in the background."""
        with self._lock:
            self._stale = True

    @property
    def loaded(self):
        return self._entries is not None

    # -- lookup -------------------------------------------------------------

    def lookup(self, needle):
        """Ranked candidates for needle (rank()); [] when nothing matches or
        the list cannot be read."""
        try:
            return self._lookup(needle)
        except Exception as e:
            print(f"[StartApps] lookup failed ({type(e).__name__})")
            self.last_source = "failed"
            return []

    def by_app_id(self, app_id):
        """[candidate] for an exact AppID in the list (tier exact), else []."""
        if self._entries is None:
            self._load()
        want = str(app_id or "").strip().lower()
        return [{"name": n, "app_id": a, "tier": TIER_EXACT, "inbox": is_inbox_package(a)}
                for n, a in (self._entries or []) if a.lower() == want][:1]

    def _lookup(self, needle):
        if self._entries is None:
            self.last_source = "loaded" if self._load() else "failed"
            return rank(needle, self._entries or [])
        age = self._clock() - (self._loaded_at or 0.0)
        if self._stale or age > self.ttl:
            # Answer from the old copy now; refresh behind it.
            self.last_source = "stale cache"
            if self._loading is None:
                self.preload()
        else:
            self.last_source = "cache"
        found = rank(needle, self._entries)
        if not found and age > MISS_REFRESH_GAP_S:
            # A newly installed app is missing from an older copy.
            if self._load():
                self.last_source = "refreshed"
                found = rank(needle, self._entries)
        return found
