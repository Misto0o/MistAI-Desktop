# ============================================================
# BROWSER REAL CHECK
# ============================================================
#
# A REAL-browser check of the browser_* element list and verified clicks on
# public pages (no logged-in sites): Wikipedia (control), DuckDuckGo results,
# YouTube search results ("the first video"), python.org. It drives MistAI's
# own Playwright Firefox through the real Api handlers, in a fresh EMPTY
# profile folder it creates itself; it never copies or reads the user's
# Firefox profile and never turns on "Use my Firefox sign-ins".
#
#   python tools/browser_real_check.py --yes [--headless] [--keep-profile]
#
# SECTION INDEX
#   1. EMPTY PROFILE GUARD
#   2. HANDLER HARNESS
#   3. CHECKS
#   4. MAIN
# ============================================================

import argparse
import os
import re
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PROFILE_PREFIX = "mist_browser_check_"


# ============================================================
# EMPTY PROFILE GUARD
# ============================================================

def make_empty_profile():
    """A new, empty profile folder under %TEMP% (this script's own)."""
    return tempfile.mkdtemp(prefix=PROFILE_PREFIX)


def require_empty_profile(path):
    """Refuse to start unless ``path`` exists and is empty (never a copy of a
    real profile). Raises SystemExit with the reason."""
    if not path or not os.path.isdir(path):
        raise SystemExit(f"refusing to start: the profile folder does not exist ({path!r})")
    if os.listdir(path):
        raise SystemExit("refusing to start: the profile folder is not empty (it may be a copy "
                         "of a real profile); pass a new empty folder")
    if os.environ.get("MISTAI_BROWSER_SEED_PROFILE", "").strip() not in ("", "0"):
        raise SystemExit("refusing to start: MISTAI_BROWSER_SEED_PROFILE is set; unset it")


# ============================================================
# HANDLER HARNESS
# ============================================================

def handler_api(session):
    """core.api.Api without its __init__: only what the browser_* handlers
    use (the browser, speak, the result text and the once-per-phase notes)."""
    from core.api import Api

    api = Api.__new__(Api)
    api.browser = session
    api.speak = lambda _t: None
    api.last_result_text = ""
    api.__dict__["_app_notes_given"] = set()
    return api


LINE_RX = re.compile(r'^\[(\d+)\] (\w+) "(.*?)"(.*)$')


def list_lines(text):
    """[(section, num, role, name, rest)] of an element list text."""
    out, section = [], ""
    for line in str(text or "").splitlines():
        m = LINE_RX.match(line)
        if m:
            out.append((section, int(m.group(1)), m.group(2), m.group(3), m.group(4)))
        elif line.endswith(":"):
            section = line
    return out


def first_main_link(text, skip=(), external_of=None, session=None, skip_prefixes=(),
                    sections=("Main content",)):
    """The first link in ``sections`` (default Main content) not marked (ad)
    whose name is not in skip and does not start with skip_prefixes: what the
    browser note tells the model "the first result" is. With external_of, only
    links to another host (a search result, not the engine's own pages; read
    from the session's last list)."""
    for section, num, role, name, rest in list_lines(text):
        low = name.strip().lower()
        if not (section.startswith(sections) and role == "link" and "(ad)" not in rest
                and low not in skip and len(name) > 3 and not low.startswith(skip_prefixes)
                and not low.startswith("skip to")):
            continue
        if external_of:
            entry = (session._refs.get(num) or (None, {}))[1]
            if not entry.get("h") or entry["h"].endswith(external_of):
                continue
        return num, name
    return None, None


def timed_list(session):
    t0 = time.perf_counter()
    text = session.elements_text()
    ms = (time.perf_counter() - t0) * 1000
    lines = list_lines(text)
    below = re.search(r"(\d+) more below the fold", text)
    return text, {"listed": len(lines), "chars": len(text), "tokens~": len(text) // 4,
                  "below_fold": int(below.group(1)) if below else 0, "ms_total": round(ms),
                  "dialog": "A dialog is open" in text}


# ============================================================
# CHECKS
# ============================================================

YOUTUBE_UI = {"all", "shorts", "videos", "unwatched", "watched", "recently uploaded", "live",
              "home", "subscriptions", "you", "filters", "search filters", "now playing"}


def check_page(api, session, name, open_step, skip=(), expect_path=None, title_has_name=False,
               external_of=None, skip_prefixes=(), sections=("Main content",), scrolls=0):
    """Open a page (navigate or search), list it, click the first main
    content link by number, and verify the click's report against the real
    URL and title. Returns a result dict."""
    from core.api import Api

    res = {"site": name}
    t0 = time.perf_counter()
    ok = open_step(api)
    res["open_s"] = round(time.perf_counter() - t0, 2)
    res["open_ok"] = ok
    if not ok:
        res["error"] = api.last_result_text[:200]
        return res
    text, stats = timed_list(session)
    res.update(stats)
    res["first_lines"] = [l for l in text.splitlines()[1:16]]
    pick = lambda t: first_main_link(t, skip, external_of, session, skip_prefixes, sections)
    num, link = pick(text)
    for _ in range(scrolls):            # not on screen yet: scroll, as the model would
        if num is not None:
            break
        Api._act_browser_scroll(api, "down")
        res["scrolled"] = res.get("scrolled", 0) + 1
        num, link = pick(api.last_result_text)
    if num is None:
        res["error"] = "no main content link listed"
        return res
    before = session.current_url()
    t0 = time.perf_counter()
    clicked = Api._act_browser_click(api, str(num))
    res["click_s"] = round(time.perf_counter() - t0, 2)
    after = session.current_url()
    res["clicked"] = f"[{num}] {link}"
    res["click_ok"] = clicked
    res["click_report"] = api.last_result_text.split("\n", 1)[0][:220]
    res["url_changed"] = after != before
    res["url_after"] = after[:120]
    if expect_path:
        res["expected_path_ok"] = expect_path in after
    if title_has_name:
        try:
            title = session._call(lambda: session._page.title())
        except Exception:
            title = ""
        res["title_after"] = title[:120]
        # The list's name may end with " · 3:26" (a duration) that the title lacks.
        res["title_matches_name"] = link.split(" · ")[0].lower()[:30] in title.lower()
    return res


def run_checks(session):
    from core.api import Api

    api = handler_api(session)
    return [
        check_page(api, session, "Wikipedia",
                   lambda a: Api._act_browser_navigate(
                       a, "en.wikipedia.org/wiki/Python_(programming_language)")),
        check_page(api, session, "DuckDuckGo",
                   lambda a: Api._act_browser_search(a, "python requests library documentation"),
                   external_of="duckduckgo.com"),
        check_page(api, session, "YouTube",
                   lambda a: Api._act_browser_navigate(
                       a, "youtube.com/results?search_query=lofi+hip+hop"),
                   skip=YOUTUBE_UI, expect_path="/watch", title_has_name=True,
                   skip_prefixes=("mix",), scrolls=2),
        check_page(api, session, "python.org",
                   lambda a: Api._act_browser_navigate(a, "python.org"),
                   sections=("Main content", "Elsewhere"), skip={"python™", "python"}),
    ]


# ============================================================
# MAIN
# ============================================================

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="really open a browser and the network")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", help="an existing EMPTY folder to use (default: a new one)")
    ap.add_argument("--keep-profile", action="store_true")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not args.yes:
        print("This opens MistAI's Playwright Firefox and visits public sites. Pass --yes.")
        return 2
    profile = args.profile or make_empty_profile()
    require_empty_profile(profile)
    from core import browser_session as bs

    session = bs.BrowserSession(profile_dir=profile, firefox_signins=None, headless=args.headless)
    results = []
    try:
        results = run_checks(session)
    finally:
        session.close()
        if not args.keep_profile and not args.profile and os.path.basename(
                profile).startswith(PROFILE_PREFIX):
            import shutil
            shutil.rmtree(profile, ignore_errors=True)     # only the folder made above
    for r in results:
        print("\n== " + r["site"])
        for k, v in r.items():
            if k == "first_lines":
                print("  first lines:")
                for line in v:
                    print("    " + line)
            elif k != "site":
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
