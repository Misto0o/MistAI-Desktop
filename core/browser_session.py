"""
core/browser_session.py — real browser automation via Playwright, replacing
OCR/Vision pixel-guessing for anything inside a web page.

Why this exists: every recurring browser-automation bug this project hit
(OCR blind to GPU-composited video, DPI-mismatched mask rects, "subscribe"
vs "subscribers" false matches, caption-bar false positives, 5+ second
scans) was a symptom of the same root problem -- guessing what's clickable
from a screenshot. A real browser exposes its actual structure through the
DOM; Playwright talks to that directly. No pixels, no coordinate math, no
ambiguity about whether something is a button or a caption.

Why Playwright launches its OWN Firefox instead of attaching to your daily
one: confirmed directly against Playwright's current docs -- CDP attach
(the feature that lets Playwright hook into an already-running, visible
browser) is Chromium-only. Firefox and WebKit don't support it. So this is
a SEPARATE Firefox process, not your regular browser tabs.

What softens that: launch_persistent_context() keeps a real profile on
disk (~/MistAI/browser_profile) that survives restarts -- sign into
YouTube/whatever once in MistAI's browser, and it's still signed in next
time, same as a normal browser profile. It's a different Firefox than your
daily one, but it isn't a fresh, empty one every single time.

Threading: Playwright's sync API is NOT safe to call from a different
thread than the one that created it, but MistAI's agent actions run on
whichever worker thread happens to be running that task. So this class
owns exactly ONE dedicated background thread that runs the entire
Playwright session; every public method here marshals its work onto that
thread via a queue and blocks for the result. Callers never touch a
Playwright object directly, from any thread -- same pattern already used
successfully for PhoneManager's asyncio loop and ClickIndicator's Tk
thread elsewhere in this codebase.
"""

import os
import queue
import threading
import time

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

try:
    import browser_cookie3

    COOKIE_IMPORT_AVAILABLE = True
except ImportError:
    COOKIE_IMPORT_AVAILABLE = False

import configparser
import shutil

# Only the pieces that actually carry logins/sessions -- deliberately NOT a
# full profile copy (cache2/, storage/default's non-essential origins, etc
# can be gigabytes and add nothing but copy time). storage/ is included
# because many modern auth flows (Google among them) keep session state in
# IndexedDB, not just cookies.sqlite.
_PROFILE_ESSENTIAL_FILES = (
    "cookies.sqlite",
    "logins.json",
    "key4.db",
    "cert9.db",
    "permissions.sqlite",
)
_PROFILE_ESSENTIAL_DIRS = ("storage",)


def find_default_firefox_profile():
    """Locate the user's REAL, everyday Firefox profile by parsing
    profiles.ini -- the exact file Firefox itself reads to decide which
    profile to open next. No folder-name guessing. Read-only: this
    function never opens or modifies anything in that profile."""
    base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")
    ini_path = os.path.join(base, "profiles.ini")
    if not os.path.isfile(ini_path):
        return None
    cp = configparser.ConfigParser()
    cp.read(ini_path)

    # Modern Firefox: an [InstallXXXX] section's Default= is authoritative
    # -- it's literally what Firefox uses on next launch.
    for section in cp.sections():
        if section.startswith("Install") and cp.has_option(section, "Default"):
            path = os.path.join(base, cp.get(section, "Default"))
            if os.path.isdir(path):
                return path

    # Older format: a [ProfileN] section explicitly marked Default=1, or
    # the only profile that exists at all.
    profile_sections = [
        s for s in cp.sections() if s.startswith("Profile") and cp.has_option(s, "Path")
    ]
    for section in profile_sections:
        marked_default = cp.has_option(section, "Default") and cp.getboolean(
            section, "Default", fallback=False
        )
        if marked_default or len(profile_sections) == 1:
            is_relative = cp.getboolean(section, "IsRelative", fallback=True)
            raw = cp.get(section, "Path")
            path = os.path.join(base, raw) if is_relative else raw
            if os.path.isdir(path):
                return path
    return None


def clone_daily_profile(dest_dir):
    """Copy just the login-carrying pieces of the user's real Firefox
    profile into dest_dir. The source is ONLY ever read from (shutil.copy2
    / copytree, never opened as a live profile) -- it can't be corrupted
    by this, worst case a copy silently doesn't happen for one file (e.g.
    if it's momentarily locked by a live Firefox write) and that's logged,
    not fatal to the rest.

    Returns (source_profile_path, [copied item names])."""
    source = find_default_firefox_profile()
    if not source:
        raise RuntimeError(
            "Could not locate your Firefox profile "
            "(profiles.ini not found at the expected path)"
        )

    os.makedirs(dest_dir, exist_ok=True)
    copied = []
    for fname in _PROFILE_ESSENTIAL_FILES:
        src = os.path.join(source, fname)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(dest_dir, fname))
                copied.append(fname)
            except Exception as e:
                print(f"[Browser] Could not copy {fname} (skipped, " f"non-fatal): {e}")
    for dname in _PROFILE_ESSENTIAL_DIRS:
        src = os.path.join(source, dname)
        dst = os.path.join(dest_dir, dname)
        if os.path.isdir(src):
            try:
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                copied.append(dname)
            except Exception as e:
                print(
                    f"[Browser] Could not copy {dname}/ (skipped, " f"non-fatal): {e}"
                )
    if not copied:
        raise RuntimeError(
            f"Found your Firefox profile at {source} but "
            f"couldn't copy anything from it -- is Firefox "
            f"currently open and actively writing to it?"
        )
    return source, copied


START_TIMEOUT = 30  # cold browser launch can be slow
DEFAULT_ACTION_TIMEOUT = 20
CLICK_TIMEOUT_MS = 8000
FILL_TIMEOUT_MS = 6000
TEXT_TIMEOUT_MS = 6000


def _default_profile_dir():
    """The daily-profile-seeded path is now the STANDARD default, not an
    opt-in extra step -- first-ever launch auto-clones from the user's
    real Firefox (see ensure_started), every launch after that just
    reuses the same already-seeded profile like any persistent profile."""
    return os.path.join(os.path.expanduser("~"), "MistAI", "browser_profile_from_daily")


def _sanitize_expires(raw):
    """Playwright accepts EXACTLY -1 (session cookie) or a positive unix
    timestamp in SECONDS -- confirmed directly against real Playwright.

    Two real, confirmed-via-testing failure modes, not just theory:
    1. Any negative value that isn't precisely -1 (e.g. a leftover garbage
       expiry in a long-used profile) fails the ENTIRE cookie batch.
    2. A microsecond-scale value (some cookie-extraction paths read the
       wrong column/unit off Firefox's cookies.sqlite) is still technically
       "positive" but absurdly large, and Playwright rejects it with the
       SAME error message as case 1 -- reproduced directly: a real seconds
       timestamp 400 years out (14.4 billion) is accepted fine, but a
       microsecond-scale one (~10^15) is rejected. Rather than just
       clamping that to -1 (which would silently downgrade every cookie to
       session-only and defeat the entire point of importing them), detect
       the unit mismatch and correct it back to seconds -- recovers the
       REAL expiry instead of losing persistence."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return -1
    if val <= 0:
        return -1
    # Nothing legitimate needs an expiry >100 billion seconds out (~3170
    # CE). A value past that is almost certainly seconds mistaken for a
    # finer unit -- microseconds is the specific case confirmed here.
    SANE_MAX = 100_000_000_000
    if val > SANE_MAX:
        corrected = val / 1_000_000
        if 0 < corrected <= SANE_MAX:
            return corrected
        return -1  # still absurd even after correction -- safest fallback
    return val


class BrowserSession:
    """The single source of truth for 'the browser' -- lazily started on
    first use, kept alive and reused across actions (not relaunched every
    call), one persistent Firefox profile for the life of the app."""

    def __init__(self, profile_dir=None):
        self.profile_dir = profile_dir or _default_profile_dir()
        self._req_q = queue.Queue()
        self._ready = threading.Event()
        self._start_error = ""
        self._page = None  # ONLY ever touched on the worker thread
        self._context = None
        self._thread = None
        self.last_url = ""  # mirrored for Workspace's context_block

    @property
    def available(self):
        return PLAYWRIGHT_AVAILABLE

    def ensure_started(self):
        """Lazy start -- an app with no browser task pays zero cost."""
        if self._thread is not None:
            return
        if not PLAYWRIGHT_AVAILABLE:
            self._start_error = "Playwright not installed (`pip install playwright` + `playwright install firefox`)"
            self._ready.set()
            return

        # First-ever launch: auto-seed from the user's real Firefox so
        # logins carry over with zero explicit action needed -- this used
        # to require saying "use my daily profile" every time, which
        # defeats the point of a PERSISTENT profile. Best-effort: if the
        # clone fails for any reason (no Firefox found, profiles.ini
        # missing, whatever), fall through to a normal fresh profile at
        # this same path rather than blocking browser use entirely on a
        # convenience feature.
        if not os.path.isdir(self.profile_dir):
            try:
                source, copied = clone_daily_profile(self.profile_dir)
                print(
                    f"[Browser] Auto-seeded profile from {source} "
                    f"({', '.join(copied)})"
                )
            except Exception as e:
                print(
                    f"[Browser] Could not auto-seed from daily Firefox "
                    f"({e}) -- starting with a fresh profile instead"
                )

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="BrowserSession"
        )
        self._thread.start()
        if not self._ready.wait(timeout=START_TIMEOUT):
            self._start_error = f"Browser did not start within {START_TIMEOUT}s"

    def _run(self):
        try:
            os.makedirs(self.profile_dir, exist_ok=True)
            with sync_playwright() as p:
                self._context = p.firefox.launch_persistent_context(
                    self.profile_dir,
                    headless=False,  # visible -- this is the whole point
                    viewport=None,  # use the OS window's natural size
                    accept_downloads=True,
                    color_scheme="dark",  # tells every site prefers-color-
                    # scheme is dark (same signal an
                    # OS dark theme sends) -- sites
                    # like GitHub auto-render dark
                    # with zero per-site settings.
                    firefox_user_prefs={
                        # dom.webdriver.enabled is Firefox's OWN native
                        # preference controlling navigator.webdriver --
                        # confirmed via bot.sannysoft.com to be the exact
                        # signal Google's automation detection was keying
                        # on (every cookie-import/profile-clone approach
                        # failed identically, which only makes sense if the
                        # browser itself was being flagged before session
                        # data was ever checked). This is the real,
                        # W3C-spec-mandated control point for that flag --
                        # not a third-party spoof, Firefox's own switch.
                        "dom.webdriver.enabled": False,
                        # Older Selenium-era flag some sites still probe
                        # for; harmless to set even if Playwright's own
                        # automation layer doesn't use it.
                        "useAutomationExtension": False,
                    },
                )
                self._page = (
                    self._context.pages[0]
                    if self._context.pages
                    else self._context.new_page()
                )
                self._ready.set()
                print(f"[Browser] Firefox started (profile: {self.profile_dir})")

                while True:
                    item = self._req_q.get()
                    if item is None:  # shutdown sentinel
                        break
                    fn, result_q = item
                    try:
                        result_q.put(("ok", fn()))
                    except Exception as e:
                        result_q.put(("err", e))

                self._context.close()
        except Exception as e:
            self._start_error = f"Browser thread crashed: {e}"
            print(f"[Browser] {self._start_error}")
            self._ready.set()

    def _call(self, fn, timeout=DEFAULT_ACTION_TIMEOUT):
        self.ensure_started()
        if self._start_error:
            raise RuntimeError(self._start_error)
        result_q = queue.Queue()
        self._req_q.put((fn, result_q))
        try:
            status, value = result_q.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(
                f"Browser action timed out after {timeout}s "
                f"(page may be stuck loading)"
            )
        if status == "err":
            raise value
        return value

    # -- Public actions -------------------------------------------------------
    # Each wraps a closure over self._page and marshals it onto the worker
    # thread via _call(). Playwright's own exceptions (Error/TimeoutError)
    # propagate up as-is -- these are real, specific, honest failure
    # signals ("no element found", "timed out waiting"), not a coordinate
    # guess that might silently be wrong.

    def navigate(self, url):
        def _do():
            full = url if url.startswith(("http://", "https://")) else f"https://{url}"
            # domcontentloaded fires as soon as the raw HTML parses -- for a
            # heavily JS-rendered page (YouTube, Claude.ai, most modern
            # SPAs) that's an empty shell; the real content gets painted in
            # AFTER by JS, and a read right after domcontentloaded catches
            # nothing. Confirmed directly: a page whose content loads 300ms
            # after the initial HTML returns empty text under both
            # domcontentloaded AND load, but the real content under
            # networkidle.
            #
            # networkidle's own risk: a site with persistent connections
            # (live polling, analytics beacons -- YouTube is a classic
            # case) can technically never go fully network-idle, which
            # would otherwise hang the WHOLE navigation. Bound it to a
            # sub-timeout and treat only-networkidle-timing-out as "good
            # enough, proceed" rather than a real navigation failure --
            # domcontentloaded already succeeded by this point, so there
            # IS a usable page, just with some background chatter still
            # running.
            self._page.goto(full, wait_until="domcontentloaded", timeout=20000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                print(
                    f"[Browser] Page never went fully idle (likely "
                    f"background polling/analytics, not a real problem) "
                    f"-- proceeding anyway: {e}"
                )
            self.last_url = self._page.url
            return self._page.url, self._page.title()

        url_out, title = self._call(_do)
        return url_out, title

    def click(self, target, role=None):
        """target: visible text to click. role: optional ARIA role
        ('button', 'link', 'checkbox', ...) for precise disambiguation
        when plain text matches more than one element."""

        def _do():
            loc = (
                self._page.get_by_role(role, name=target)
                if role
                else self._page.get_by_text(target, exact=False)
            )
            loc.first.click(timeout=CLICK_TIMEOUT_MS)
            return True

        return self._call(_do)

    def fill(self, field, text):
        """field: the input's visible label or placeholder text."""

        def _do():
            try:
                loc = self._page.get_by_label(field, exact=False)
                loc.first.fill(text, timeout=FILL_TIMEOUT_MS)
            except (PlaywrightError, PlaywrightTimeout):
                # Not every input has an associated <label> -- placeholder
                # text is the other common way real forms identify a field.
                loc = self._page.get_by_placeholder(field, exact=False)
                loc.first.fill(text, timeout=FILL_TIMEOUT_MS)
            return True

        return self._call(_do)

    def get_text(self, selector=""):
        """Real DOM text -- replaces read_screen_text/OCR for anything
        inside the browser. Essentially always accurate, since it's not a
        guess, it's the actual rendered text content."""

        def _do():
            return self._page.inner_text(selector or "body", timeout=TEXT_TIMEOUT_MS)

        return self._call(_do)

    def press_key(self, key):
        def _do():
            self._page.keyboard.press(key)
            return True

        return self._call(_do)

    def current_url(self):
        def _do():
            return self._page.url

        try:
            return self._call(_do, timeout=5)
        except Exception:
            return self.last_url

    def import_cookies(self, domains):
        """Pull cookies for the given domain(s) out of the user's REAL,
        daily Firefox profile and inject them into this (separate,
        Playwright-controlled) profile -- carries over an existing login
        instead of making them sign in again by hand.

        Returns (per_domain_counts, failures) where failures is a list of
        (cookie_name, real_error_reason) tuples -- the caller decides what
        "success" means (e.g. api.py treats it as a failure if literally
        everything failed, even though this method itself doesn't raise).

        Genuinely more fragile than everything else in this module: it's
        reading another browser's cookie store out from under it (via a
        lock-safe copy, courtesy of browser_cookie3), and some sites
        actively invalidate sessions that show up in an unfamiliar browser
        context/fingerprint. If a site logs the imported session right back
        out, that's the site's own anti-fraud heuristics, not a bug here --
        the honest fallback is always just logging in by hand once."""
        if not COOKIE_IMPORT_AVAILABLE:
            raise RuntimeError(
                "browser_cookie3 not installed " "(`pip install browser_cookie3`)"
            )

        domain_list = [d.strip() for d in domains.split("|") if d.strip()]
        if not domain_list:
            raise ValueError("import_cookies needs at least one domain")

        playwright_cookies = []
        per_domain_counts = {}
        for domain in domain_list:
            jar = browser_cookie3.firefox(domain_name=domain)
            count = 0
            for c in jar:
                # http.cookiejar predates the SameSite attribute, so the
                # real value isn't recoverable here -- default to "Lax",
                # which is correct for our actual use case (the user
                # directly navigating to the site) and avoids Playwright
                # rejecting a "None" cookie that isn't also marked secure.
                pc = {
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain,
                    "path": c.path or "/",
                    "secure": bool(c.secure),
                    "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
                    "sameSite": "Lax",
                    "expires": _sanitize_expires(c.expires),
                }
                playwright_cookies.append(pc)
                count += 1
            per_domain_counts[domain] = count

        def _do():
            if not playwright_cookies:
                return per_domain_counts, []
            try:
                self._context.add_cookies(playwright_cookies)
                return per_domain_counts, []
            except Exception as e:
                print(
                    f"[Browser] Batch cookie import failed ({e}) -- "
                    f"retrying individually to isolate the real cause"
                )
                failures = []  # (name, reason) -- the actual reason, not just the name
                for pc in playwright_cookies:
                    try:
                        self._context.add_cookies([pc])
                    except Exception as ce:
                        failures.append((pc["name"], str(ce)))
                if failures:
                    # Group by error message so 150 identical failures show
                    # as ONE reason, not 150 unhelpful repeats.
                    by_reason = {}
                    for name, reason in failures:
                        by_reason.setdefault(reason, []).append(name)
                    print(
                        f"[Browser] {len(failures)} cookie(s) could not be "
                        f"imported:"
                    )
                    for reason, names in by_reason.items():
                        print(f"  ({len(names)}x) {reason} -- e.g. {names[:3]}")
                return per_domain_counts, failures

        return self._call(_do)

    def use_daily_profile_copy(self):
        """The daily-profile-seeded directory is now the DEFAULT profile
        (auto-cloned on first ever launch, see ensure_started) -- so this
        action's job is REFRESHING it with whatever's newer in the user's
        real Firefox since that first clone (a login added since then,
        etc.), not switching to it for the first time.

        Must close the current session first: Playwright launches against
        whatever profile_dir is set at start time, so re-seeding an
        already-in-use profile means a fresh launch, not a live swap."""
        self.close()
        source, copied = clone_daily_profile(self.profile_dir)
        return source, copied

    def close(self):
        """Closes the browser process. The profile on disk (and therefore
        logins/history) survives -- next action just relaunches against
        the same persistent profile."""
        if self._thread is None:
            return
        try:
            self._req_q.put(None)
            self._thread.join(timeout=10)
        except Exception:
            pass
        self._thread = None
        self._ready = threading.Event()
        self._page = None
        self._context = None
