# ============================================================
# BROWSER AND SEARCH TESTS
# ============================================================
#
# browser_search engines and fallback, search/process matching and timing.
#
# SECTION INDEX
#   1. DUCKDUCKGO SEARCH
#   2. DUCKDUCKGO SEARCH HELPERS
#   3. DUCKDUCKGO SEARCH BROWSER
#   4. DUCKDUCKGO SEARCH API
#   5. DUCKDUCKGO SEARCH HELPERS (2)
#   6. DUCKDUCKGO SEARCH TESTS
#   7. BLOCK FALLBACK
#   8. BLOCK FALLBACK HELPERS
#   9. BLOCK FALLBACK BROWSER
#   10. BLOCK FALLBACK API
#   11. BLOCK FALLBACK HELPERS (2)
#   12. BLOCK FALLBACK TESTS
#   13. SEARCH, PROCESS AND TIMING
#   14. SEARCH, PROCESS AND TIMING HELPERS
#   15. SEARCH, PROCESS AND TIMING TESTS
#   16. SEARCH, PROCESS AND TIMING FIND API
#   17. SEARCH, PROCESS AND TIMING TESTS (2)
#   18. SEARCH, PROCESS AND TIMING HELPERS (2)
#   19. SEARCH, PROCESS AND TIMING TESTS (3)
#   20. SEARCH, PROCESS AND TIMING GATED RUN
#   21. SEARCH, PROCESS AND TIMING HELPERS (3)
#   22. SEARCH, PROCESS AND TIMING TESTS (4)
#   23. BROWSER START RETRY
#   24. BROWSER ELEMENT LIST
#   25. BROWSER ELEMENT LIST API
#   26. BROWSER PROFILE SETTING
#   27. BROWSER FIXTURE PAGES
#   28. BROWSER FIXTURE TESTS
#   29. PAGE READINESS, DOWNLOADS AND FILL
#   30. BROWSER LIST SPEED AND EMPTY PAGES
# ============================================================

"""browser_search engines and fallback, search/process matching and timing."""

import inspect
import re
from types import SimpleNamespace
import json
import os
import threading

import pytest

from core import config, planner, workspace as file_search
from core.agent import AgentLoop, Timings
from core.api import Api
import core.api as api_mod
from core import browser_signins as bsi
from core.executor import Executor
from core.tools import SPECS
from core.config import (
    SEARCH_ENGINES,
    blocked_page_reason,
    TRACKED_PROCESS_NAMES,
    normalize_process_name,
    process_matches,
)


# ============================================================
# DUCKDUCKGO SEARCH
# ============================================================

# browser_search uses one engine, DuckDuckGo (MISTAI_SEARCH_ENGINE=google switches
# back): its normal page, then its HTML endpoint on a block or empty page; a
# blocked search is a clear failure that never escalates to a browser app, and the
# agent prompt says to search with browser_search only. Stubbed browser.


# ============================================================
# DUCKDUCKGO SEARCH HELPERS
# ============================================================

DUCKDUCKGO_SEARCH_RESULTS = "Marblewood lighthouse opening hours 9am to 5pm daily " * 5
DDG_ANOMALY_URL = ("https://duckduckgo.com/anomaly?q=x", "DuckDuckGo", "Please try again.")
DDG_BOTS = ("https://duckduckgo.com/?q=x", "DuckDuckGo",
            "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.")
DDG_HTML_OK = ("https://html.duckduckgo.com/html/?q=x", "x at DuckDuckGo", DUCKDUCKGO_SEARCH_RESULTS)
EMPTY = ("https://duckduckgo.com/?q=x", "DuckDuckGo", "   ")


# ============================================================
# DUCKDUCKGO SEARCH BROWSER
# ============================================================

class DuckduckgoSearchBrowser:
    def __init__(self, pages):
        self.pages, self.visited, self.current = list(pages), [], None

    def navigate(self, url):
        self.visited.append(url)
        self.current = self.pages.pop(0)
        return self.current[0], self.current[1]

    def get_text(self, _selector=""):
        return self.current[2]


# ============================================================
# DUCKDUCKGO SEARCH API
# ============================================================

def _duckduckgo_search_api(pages):
    api = Api.__new__(Api)
    api.browser = DuckduckgoSearchBrowser(pages)
    api.spoken = []
    api.speak = lambda text: api.spoken.append(text)
    api.last_result_text = ""
    return api


# -- engine selection and URLs ------------------------------------------------------------


# -- DuckDuckGo block pages ----------------------------------------------------------------


# -- browser_search ------------------------------------------------------------------------


# -- the prompt rule -----------------------------------------------------------------------


# ============================================================
# DUCKDUCKGO SEARCH HELPERS (2)
# ============================================================

def _system():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


# ============================================================
# DUCKDUCKGO SEARCH TESTS
# ============================================================

class TestDuckduckgoSearch:
    @pytest.fixture(autouse=True)
    def _default_engine(self, monkeypatch):
        monkeypatch.delenv("MISTAI_SEARCH_ENGINE", raising=False)

    @pytest.mark.parametrize("raw, engine", [
        (None, "duckduckgo"), ("duckduckgo", "duckduckgo"), ("google", "google"),
        (" Google ", "google"), ("bing", "duckduckgo"), ("", "duckduckgo"),
    ])
    def test_the_engine_is_duckduckgo_unless_set_to_google(self, monkeypatch, raw, engine):
        if raw is not None:
            monkeypatch.setenv("MISTAI_SEARCH_ENGINE", raw)
        assert config.search_engine() == engine

    def test_the_default_routes_never_leave_duckduckgo(self):
        routes = config.search_routes()
        assert [n for n, _ in routes] == ["DuckDuckGo", "DuckDuckGo HTML"]
        assert all(u.startswith("https://") and "duckduckgo.com" in u for _, u in routes)

    def test_the_env_switches_back_at_call_time(self, monkeypatch):
        assert config.search_routes()[0][0] == "DuckDuckGo"
        monkeypatch.setenv("MISTAI_SEARCH_ENGINE", "google")
        assert config.search_routes()[0][0] == "Google"

    @pytest.mark.parametrize("query, encoded", [
        ("marblewood hours", "marblewood%20hours"),
        ("a&b=c/d ?#", "a%26b%3Dc%2Fd%20%3F%23"),
        ("café 5+5", "caf%C3%A9%205%2B5"),
    ])
    def test_the_query_is_fully_percent_encoded(self, query, encoded):
        assert config.search_url("https://duckduckgo.com/?q=", query) == (
            "https://duckduckgo.com/?q=" + encoded)

    @pytest.mark.parametrize("page", [DDG_ANOMALY_URL, DDG_BOTS])
    def test_duckduckgo_anomaly_pages_are_blocks(self, page):
        assert config.blocked_page_reason(*page) == (
            "the page reports an anomaly (DuckDuckGo's bot check)")

    def test_a_normal_duckduckgo_results_page_is_not_a_block(self):
        assert config.blocked_page_reason("https://duckduckgo.com/?q=x", "x at DuckDuckGo",
                                          DUCKDUCKGO_SEARCH_RESULTS) is None

    def test_the_normal_page_answers_first(self, capsys):
        api = _duckduckgo_search_api([("https://duckduckgo.com/?q=x", "x at DuckDuckGo", DUCKDUCKGO_SEARCH_RESULTS)])
        assert Api._act_browser_search(api, "marblewood hours") is True
        assert api.browser.visited == ["https://duckduckgo.com/?q=marblewood%20hours"]
        assert api.last_result_text.startswith("Searched DuckDuckGo for 'marblewood hours'.")

    def test_a_blocked_normal_page_falls_back_to_the_html_endpoint(self, capsys):
        api = _duckduckgo_search_api([DDG_BOTS, DDG_HTML_OK])
        assert Api._act_browser_search(api, "marblewood hours") is True
        assert api.browser.visited == ["https://duckduckgo.com/?q=marblewood%20hours",
                                       "https://html.duckduckgo.com/html/?q=marblewood%20hours"]
        assert api.last_result_text.startswith(
            "Searched DuckDuckGo HTML for 'marblewood hours'. (DuckDuckGo showed a block page.)")
        log = capsys.readouterr().out
        assert "browser_search: DuckDuckGo showed a block page (the page reports an anomaly" in log
        assert "marblewood" not in log and "bots use" not in log

    def test_an_empty_results_page_counts_as_blocked(self):
        api = _duckduckgo_search_api([EMPTY, DDG_HTML_OK])
        assert Api._act_browser_search(api, "q") is True
        assert len(api.browser.visited) == 2

    def test_a_blocked_search_is_a_clear_failure_with_no_other_route(self):
        api = _duckduckgo_search_api([DDG_BOTS, EMPTY])
        assert Api._act_browser_search(api, "marblewood hours") is False
        assert all("duckduckgo.com" in u for u in api.browser.visited)
        assert len(api.browser.visited) == 2                     # no third route
        text = api.last_result_text
        assert text.startswith("BLOCKED: the search was blocked (DuckDuckGo: the page reports an "
                               "anomaly (DuckDuckGo's bot check); DuckDuckGo HTML: the results "
                               "page was empty).")
        assert "no browser app, no address bar" in text and "tell the user" in text
        assert "bots use" not in text and api.spoken == []

    def test_a_failed_search_never_escalates_to_a_browser_app(self):
        assert SPECS["browser_search"].fallback_tools == ()
        # The Executor escalates ONLY through a tool's fallback_tools.
        src = inspect.getsource(Executor.run)
        assert "for fb in spec.fallback_tools:" in src
        assert "open_app" not in src

    def test_the_prompt_says_to_search_with_browser_search_only(self):
        text = _system()
        rule = next(l for l in text.splitlines() if l.startswith("- BROWSER:"))
        assert "for any web search or lookup use browser_search" in rule
        assert "browser_get_text" in rule
        assert "never open the user's browser app or type into an address bar to search" in rule
        assert "only when the user asks to open or use it" in rule
        assert len(re.findall(r"^- BROWSER", text, re.M)) == 1          # merged, not a new section
        assert "- browser_search <search query>: Web search (DuckDuckGo)" in text
        assert "Google search" not in text


# ============================================================
# BLOCK FALLBACK
# ============================================================

# browser_search / browser_navigate: block and CAPTCHA pages (stubbed browser).


# ============================================================
# BLOCK FALLBACK HELPERS
# ============================================================

GOOGLE_ROUTES = SEARCH_ENGINES["google"]


@pytest.fixture
def google(monkeypatch):
    """MISTAI_SEARCH_ENGINE=google: the old order (Google, then DuckDuckGo HTML)."""
    monkeypatch.setenv("MISTAI_SEARCH_ENGINE", "google")

BLOCK_FALLBACK_RESULTS = "Marblewood lighthouse opening hours 9am to 5pm daily " * 5
GOOGLE_SORRY = ("https://www.google.com/sorry/index?continue=x", "https://www.google.com/",
                "Our systems have detected unusual traffic from your computer network. "
                "Quillfeather ref 12345")
CF_BLOCK = ("https://html.duckduckgo.com/html/?q=x", "Just a moment...",
            "Please verify you are human by completing the CAPTCHA below. Ray ID abc")
ARTICLE = ("https://example.org/blog/captchas", "How CAPTCHA works",
           "A CAPTCHA is a challenge that tries to tell people and bots apart. " * 60)


# ============================================================
# BLOCK FALLBACK BROWSER
# ============================================================

class BlockFallbackBrowser:
    def __init__(self, pages):
        self.pages = list(pages)   # (url, title, text) returned per navigate, in order
        self.visited = []
        self.current = None

    def navigate(self, url):
        self.visited.append(url)
        self.current = self.pages.pop(0)
        return self.current[0], self.current[1]

    def get_text(self, _selector=""):
        return self.current[2]


# ============================================================
# BLOCK FALLBACK API
# ============================================================

def _block_fallback_api(pages):
    api = Api.__new__(Api)
    api.browser = BlockFallbackBrowser(pages)
    api.spoken = []
    api.speak = lambda text: api.spoken.append(text)
    api.last_result_text = ""
    return api


# ============================================================
# BLOCK FALLBACK HELPERS (2)
# ============================================================

def _content_free(result):
    for word in ("Quillfeather", "12345", "Ray ID", "Just a moment", "Our systems"):
        assert word not in result


# -- the detector -------------------------------------------------------------------


# ============================================================
# BLOCK FALLBACK TESTS
# ============================================================

@pytest.mark.parametrize("url, title, text, reason", [
    (*GOOGLE_SORRY, "the site redirected to a /sorry/ block page"),
    ("https://a.example/x", "Error", "Unusual traffic from your network.",
     "the page reports unusual traffic"),
    ("https://a.example/x", "Check", "Please confirm you're not a robot.",
     "the page asks to prove you're not a robot"),
    ("https://a.example/x", "Security check", "Solve the captcha to continue.",
     "the page shows a CAPTCHA"),
    ("https://a.example/x", "Verify you are human", "", "the page asks to verify you are human"),
    ("https://a.example/x", "403", "Access Denied. Reference #18.2f", "the page says access denied"),
])
def test_block_pages_get_a_fixed_reason(url, title, text, reason):
    assert blocked_page_reason(url, title, text) == reason


@pytest.mark.parametrize("url, title, text", [
    ARTICLE,                                                       # long article about captchas
    ("https://a.example/sorryville/menu", "Sorry Ville", "Our menu"),   # 'sorry' not a path step
    ("https://www.google.com/search?q=sorry", "sorry - Google Search", BLOCK_FALLBACK_RESULTS),
    ("https://a.example/", "Weather", "Sunny, 18 degrees"),
    ("", None, None),
])
def test_normal_pages_are_not_blocked(url, title, text):
    assert blocked_page_reason(url, title, text) is None


def test_the_reason_never_quotes_the_page():
    reasons = {blocked_page_reason(*p) for p in (GOOGLE_SORRY, CF_BLOCK)}
    assert reasons == {"the site redirected to a /sorry/ block page",
                       "the page shows a CAPTCHA"}     # the first rule that matches wins


def test_google_mode_routes_google_first_then_duckduckgo_html():
    assert [n for n, _ in GOOGLE_ROUTES] == ["Google", "DuckDuckGo HTML"]
    for routes in SEARCH_ENGINES.values():
        assert all(u.startswith("https://") and u.endswith("q=") for _, u in routes)


# -- browser_search -------------------------------------------------------------------


def test_google_blocked_then_the_second_route_works(capsys, google):
    api = _block_fallback_api([GOOGLE_SORRY, ("https://html.duckduckgo.com/html/?q=x", "results", BLOCK_FALLBACK_RESULTS)])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert api.browser.visited == [GOOGLE_ROUTES[0][1] + "marblewood%20hours",
                                   GOOGLE_ROUTES[1][1] + "marblewood%20hours"]
    result = api.last_result_text
    assert result.startswith("Searched DuckDuckGo HTML for 'marblewood hours'. "
                             "(Google showed a block page.) RESULTS PAGE TEXT (already read")
    _content_free(result)
    log = capsys.readouterr().out
    assert "[Browser] browser_search: Google showed a block page (the site redirected" in log
    assert "marblewood" not in log and "Quillfeather" not in log


def test_both_routes_blocked_is_a_failure_with_no_page_content(google):
    api = _block_fallback_api([GOOGLE_SORRY, CF_BLOCK])
    assert Api._act_browser_search(api, "marblewood hours") is False
    assert len(api.browser.visited) == 2                       # each route once
    assert api.last_result_text == (
        "BLOCKED: the search was blocked (Google: the site redirected to a /sorry/ block "
        "page; DuckDuckGo HTML: the page shows a CAPTCHA). Do not try to solve it or search "
        "another way (no browser app, no address bar); tell the user the web search was "
        "blocked.")
    assert "already read" not in api.last_result_text
    _content_free(api.last_result_text)
    assert api.spoken == []


def test_a_normal_results_page_is_untouched(google):
    api = _block_fallback_api([("https://www.google.com/search?q=x", "x - Google Search", BLOCK_FALLBACK_RESULTS)])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert len(api.browser.visited) == 1
    excerpt = " ".join(BLOCK_FALLBACK_RESULTS.split())
    assert api.last_result_text == (
        "Searched Google for 'marblewood hours'. RESULTS PAGE TEXT (already read -- do NOT "
        "search again; answer from this, or browser_click a result): " + excerpt)
    assert api.spoken == ["Searching for marblewood hours"]


def test_an_article_about_captchas_is_a_normal_result():
    api = _block_fallback_api([ARTICLE])
    assert Api._act_browser_search(api, "how do captchas work") is True
    assert len(api.browser.visited) == 1 and "already read" in api.last_result_text


def test_a_navigation_error_is_reported_as_before():
    api = _block_fallback_api([])
    api.browser.navigate = lambda url: (_ for _ in ()).throw(RuntimeError("net down"))
    assert Api._act_browser_search(api, "q") is False
    assert api.last_result_text == "browser_search failed: net down"


# -- browser_navigate -------------------------------------------------------------------


def test_navigate_marks_a_block_page_and_suggests_browser_search():
    api = _block_fallback_api([CF_BLOCK])
    assert Api._act_browser_navigate(api, "example.org/page") is False
    assert api.last_result_text == (
        "BLOCKED: the page shows a CAPTCHA. This is a block/CAPTCHA page, not "
        "the content; do not try to solve it. Use browser_search for the information instead.")
    _content_free(api.last_result_text)
    assert api.spoken == []


def test_navigate_to_a_normal_page_is_untouched():
    api = _block_fallback_api([("https://example.org/", "Example", "Hello world")])
    assert Api._act_browser_navigate(api, "example.org") is True
    assert api.last_result_text == (
        "Navigated to https://example.org/ (Example). PAGE TEXT (already read -- do NOT "
        "navigate again; use browser_get_text with a selector only if you need more): "
        "Hello world")
    assert api.spoken == ["Navigating to https://example.org/"]


def test_the_page_limit_is_a_config_constant():
    assert config.BLOCK_PAGE_MAX_CHARS == 1500


# ============================================================
# SEARCH, PROCESS AND TIMING
# ============================================================

# find_file search, result-limit audit, repeat-guard wording, exact process
# matching, timing output, and the TTS network cooldown.


# ============================================================
# SEARCH, PROCESS AND TIMING HELPERS
# ============================================================

def _tree(base, files):
    for rel in files:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")


# ============================================================
# SEARCH, PROCESS AND TIMING TESTS
# ============================================================

def test_search_skips_dependency_and_cache_trees_and_ranks_exact_first(tmp_path):
    _tree(
        tmp_path,
        [
            "proj/node_modules/jest-jasmine2/build/jest.environment.js",
            "proj/node_modules/pkg/.env",
            "proj/.git/.env",
            "proj/.venv/.env",
            "proj/__pycache__/.env",
            "proj/dist/.env",
            "proj/build/.env",
            "proj/.pytest_cache/.env",
            "proj/webpack-cache/.env",
            "proj/src/environment.ts",
            "proj/.env.example",
            "proj/deep/er/.env",
            "proj/.env",
        ],
    )
    matches, info = file_search.search(".env", [str(tmp_path / "proj")])
    paths = [p for _, p in matches]
    assert paths[0] == str(tmp_path / "proj" / ".env")
    assert matches[0][0] == file_search.RANK_EXACT
    assert str(tmp_path / "proj" / "deep" / "er" / ".env") in paths
    assert str(tmp_path / "proj" / ".env.example") in paths  # prefix, ranked after exact
    assert [r for r, _ in matches] == sorted(r for r, _ in matches)
    for bad in ("node_modules", ".git", ".venv", "__pycache__", "dist", "build", "cache"):
        assert not any(f"{os.sep}{bad}" in p for p in paths), bad
    # A dotfile query is not a substring search: no environment.ts noise.
    assert not any("environment" in p for p in paths)
    assert info["roots"] == [str(tmp_path / "proj")]


def test_search_caps_results_and_supports_wildcards(tmp_path):
    _tree(tmp_path, [f"docs/report{i}.pdf" for i in range(40)] + ["docs/notes.txt"])
    matches, info = file_search.search("*.pdf", [str(tmp_path)], max_results=25)
    assert len(matches) == 25 and info["total"] == 40
    assert all(p.endswith(".pdf") for _, p in matches)
    text = file_search.describe("*.pdf", matches, info)
    assert "40 match(es), showing best 25" in text


def test_search_respects_depth_limit_for_home(tmp_path):
    _tree(tmp_path, ["a/b/c/d/target.txt", "a/target.txt"])
    matches, _ = file_search.search(
        "target.txt", [str(tmp_path)], depth_limits={str(tmp_path): 2}
    )
    assert [p for _, p in matches] == [str(tmp_path / "a" / "target.txt")]


def test_ties_follow_root_order_and_home_sweep_skips_dot_folders(tmp_path):
    _tree(
        tmp_path,
        [
            "home/.vscode/extensions/x/package.json",
            "home/tools/package.json",
            "work/app/.github/package.json",
            "work/app/sub/dir/package.json",
        ],
    )
    roots = [str(tmp_path / "work" / "app"), str(tmp_path / "home")]
    matches, _ = file_search.search(
        "package.json", roots, depth_limits={str(tmp_path / "home"): 3}
    )
    paths = [p for _, p in matches]
    # Workspace root results come first even when a home path is shorter.
    assert paths[0].startswith(roots[0]) and paths[1].startswith(roots[0])
    assert str(tmp_path / "work" / "app" / ".github" / "package.json") in paths
    assert str(tmp_path / "home" / "tools" / "package.json") in paths
    assert not any(".vscode" in p for p in paths)


# ============================================================
# SEARCH, PROCESS AND TIMING FIND API
# ============================================================

def _find_api(tmp_path, project_root=None):
    api = Api.__new__(Api)
    folders = {
        "{desktop}": str(tmp_path / "Desktop"),
        "{documents}": str(tmp_path / "Documents"),
        "{downloads}": str(tmp_path / "Downloads"),
    }
    for path in folders.values():
        os.makedirs(path, exist_ok=True)

    def resolve(p):
        for ph, real in folders.items():
            p = p.replace(ph, real)
        return p

    api._resolve_path_placeholders = resolve
    api.workspace = SimpleNamespace(project_root=project_root)
    api.last_result_text = ""
    return api


# ============================================================
# SEARCH, PROCESS AND TIMING TESTS (2)
# ============================================================

def test_find_file_searches_workspace_root_first_and_reports_roots(tmp_path, monkeypatch):
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    _tree(tmp_path, ["work/app/.env", "Desktop/other/.env"])
    api = _find_api(tmp_path, project_root=str(tmp_path / "work" / "app"))

    roots, depths = Api.find_file_roots(api)
    assert roots[0] == str(tmp_path / "work" / "app")
    assert roots[1:4] == [str(tmp_path / n) for n in ("Desktop", "Documents", "Downloads")]
    assert depths == {str(tmp_path / "home"): file_search.HOME_DEPTH}

    assert Api._act_find_file(api, ".env")
    text = api.last_result_text
    assert "searched: " + str(tmp_path / "work" / "app") in text
    assert text.index(str(tmp_path / "work" / "app" / ".env")) < text.index(
        str(tmp_path / "Desktop" / "other" / ".env")
    )


def test_find_file_with_a_folder_searches_only_that_folder(tmp_path):
    _tree(tmp_path, ["Desktop/proj/.env", "Documents/.env"])
    api = _find_api(tmp_path)
    assert Api._act_find_file(api, "{desktop}\\proj\\*.env*")
    assert "searched: " + str(tmp_path / "Desktop" / "proj") + "." in api.last_result_text
    assert "Documents" not in api.last_result_text
    assert Api._act_find_file(api, str(tmp_path / "Desktop" / "proj") + os.sep + ".env")
    assert "exists (exact path)" in api.last_result_text
    assert not Api._act_find_file(api, str(tmp_path / "nope") + os.sep + ".env")
    assert "folder not found" in api.last_result_text
    assert not Api._act_find_file(api, "")
    assert not Api._act_find_file(api, "proj\.env")
    assert "not an absolute folder" in api.last_result_text


def test_bare_filename_is_searched_not_resolved_against_the_cwd(tmp_path, monkeypatch):
    (tmp_path / "cwd").mkdir()
    (tmp_path / "cwd" / ".env").write_text("SECRET=1", encoding="utf-8")
    monkeypatch.chdir(tmp_path / "cwd")
    monkeypatch.setattr("os.path.expanduser", lambda _p: str(tmp_path / "home"))
    os.makedirs(tmp_path / "home", exist_ok=True)
    api = _find_api(tmp_path)
    assert not Api._act_find_file(api, ".env")
    assert "exists (exact path)" not in api.last_result_text
    assert "NO MATCHES" in api.last_result_text


# -- result_chars audit ----------------------------------------------------------


def test_data_returning_tools_have_result_limits():
    assert SPECS["find_file"].result_chars == 4000
    assert SPECS["analyze_screen"].result_chars == 2000
    assert SPECS["recall"].result_chars == 1500
    assert SPECS["get_reminders"].result_chars == 1500
    assert SPECS["check_email"].result_chars == 1200
    # find_file no longer drives the UI, so it must not force a screen refresh.
    assert "info" in SPECS["find_file"].categories
    assert SPECS["find_file"].settle == 0.0


# -- Repeat-guard wording --------------------------------------------------------


# ============================================================
# SEARCH, PROCESS AND TIMING HELPERS (2)
# ============================================================

def _advice(action):
    return AgentLoop(SimpleNamespace(tools=SimpleNamespace(get=lambda n: SPECS[n])), None)._repeat_advice(
        action
    )


# ============================================================
# SEARCH, PROCESS AND TIMING TESTS (3)
# ============================================================

def test_repeat_advice_depends_on_the_tool_kind():
    for action in ("run_command", "find_file", "inspect_file"):
        text = _advice(action)
        assert "identical call already ran" in text
        assert "different parameters, a narrower query, or an absolute path" in text
        assert "browser_get_text" not in text
    assert "browser_get_text" in _advice("browser_search")
    assert "already ran without error" in _advice("open_app")
    assert "did take effect" not in _advice("open_app")


def test_repeated_run_command_is_refused_with_search_advice(monkeypatch):
    replies = iter(
        [json.dumps({"action": "run_command", "parameter": "Get-Process"})] * 2
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    sent, calls = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: calls.append(a)
            or SimpleNamespace(
                success=True, detail="out", verified=True, escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )

    def fake_post(*_a, json=None, **_k):
        sent.append(json["messages"])
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    assert calls == ["run_command"]
    refusal = sent[-1][-2]["content"]
    assert "REFUSED" in refusal and "a narrower query" in refusal
    assert "browser_get_text" not in refusal


def test_search_result_rule_is_merged_into_the_find_file_entry():
    api = SimpleNamespace(
        workspace=SimpleNamespace(context_block=lambda: ""),
        _resolve_path_placeholders=lambda p: p,
    )
    system = AgentLoop(api, None)._build_system("none", {"open_apps": []}, "s", "", [])
    line = next(l for l in system.splitlines() if "- find_file" in l)
    assert "several results" in line and "checking its path" in line


# -- Exact process matching ------------------------------------------------------


@pytest.mark.parametrize(
    "app, proc, expected",
    [
        ("code", "Code.exe", True),
        ("code", "codex.exe", False),
        ("code", "codex-windows-sandbox-service.exe", False),
        ("CODE", "code", True),
        ("vscode", "Code.exe", True),
        ("Visual Studio Code", "Code.exe", True),
        ("edge", "msedge.exe", True),
        ("edge", "msedgewebview2.exe", False),
        ("obs studio", "obs64.exe", True),
        ("word", "WINWORD.EXE", True),
        ("notepad", "notepad++.exe", False),
        ("notepad", "notepad.exe", True),
        ("teams", "ms-teams.exe", True),
        ("settings", "SystemSettings.exe", True),
        ("calculator", "CalculatorApp.exe", True),
        ("discord", "Discord.exe", True),
    ],
)
def test_process_matching_is_exact_with_aliases(app, proc, expected):
    assert process_matches(app, proc) is expected


def test_is_app_running_does_not_confuse_code_with_codex(monkeypatch):
    api = Api.__new__(Api)
    procs = [
        SimpleNamespace(info={"name": "codex.exe", "pid": 10}),
        SimpleNamespace(info={"name": "codex-windows-sandbox-service.exe", "pid": 11}),
    ]
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: list(procs))
    assert Api.is_app_running(api, "code") is False
    procs.append(SimpleNamespace(info={"name": "Code.exe", "pid": 12}))
    assert Api.is_app_running(api, "code") is True
    assert Api.is_app_running(api, "Visual Studio Code") is True


def test_open_apps_list_tracks_exact_process_names(monkeypatch):
    from core import live_state

    assert normalize_process_name("codex") not in TRACKED_PROCESS_NAMES
    assert normalize_process_name("Code.exe") in TRACKED_PROCESS_NAMES
    tracker = live_state.LiveStateTracker.__new__(live_state.LiveStateTracker)
    tracker._state, tracker._lock = {}, threading.Lock()
    monkeypatch.setattr(
        live_state.psutil,
        "process_iter",
        lambda _attrs: [
            SimpleNamespace(info={"name": n, "pid": i})
            for i, n in enumerate(
                ["codex.exe", "codex-windows-sandbox-service.exe", "Code.exe", "msedge.exe",
                 "msedgewebview2.exe", "obs64.exe", "notepad++.exe"],
                start=100,
            )
        ],
    )
    tracker._update()
    assert tracker._state["open_apps"] == ["code", "msedge", "obs64", "notepad++"]


# -- Timing ----------------------------------------------------------------------


def test_step_phase_and_planning_times_are_printed(monkeypatch, capsys):
    replies = iter(
        [json.dumps({"action": "open_app", "parameter": "notepad"}),
         json.dumps({"action": "done", "speech": "ok"})]
    )
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(
            run=lambda a, p: SimpleNamespace(
                success=True, detail="", verified=True, escalate_to_user=False,
                reliability_note=lambda: "",
            )
        ),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    loop.task_timing = Timings()
    monkeypatch.setattr(loop, "_get_screen_context", lambda: "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    loop._loop("t", "m", "none", "text", None)
    out = capsys.readouterr().out
    assert "[Timing] step 1: screen=" in out and "llm=" in out and "exec=" in out
    assert "[Timing] step 2: " in out
    assert "[Timing] Phase: " in out and "total" in out
    assert loop.task_timing.calls["llm"] == 2 and loop.task_timing.calls["exec"] == 1

    api.agent = loop
    monkeypatch.setattr(planner, "_llm", lambda *a, **k: "not json")
    api.workspace = SimpleNamespace(context_block=lambda: "")
    planner.make_plan(api, "research things then write them down", "m")
    assert loop.task_timing.calls["plan"] == 1
    assert "[Timing] Planning LLM: " in capsys.readouterr().out


# -- TTS cooldown ------------------------------------------------------------------


def test_tts_skips_edge_tts_for_a_while_after_a_failure(monkeypatch):
    from core import tts

    monkeypatch.setattr(tts, "EDGE_TTS_AVAILABLE", True)
    engine = tts.NeuralTTS.__new__(tts.NeuralTTS)
    engine._cv = threading.Condition()
    engine._generation = 0
    engine._neural_down_until = 0.0
    neural_calls, fallback_calls = [], []

    def failing_neural(text, gen, retry=False):
        neural_calls.append(text)
        raise OSError("self-signed certificate in certificate chain")

    engine._neural = failing_neural
    engine._fallback = lambda text, gen: fallback_calls.append(text)
    now = [1000.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])

    engine._speak_one(0, "one")
    engine._speak_one(0, "two")
    now[0] += tts.NeuralTTS.NEURAL_COOLDOWN - 1
    engine._speak_one(0, "three")
    assert neural_calls == ["one", "one"]          # the first try and its one retry
    assert fallback_calls == ["one", "two", "three"]

    now[0] += 2  # cooldown over: the network is tried again
    engine._speak_one(0, "four")
    assert neural_calls == ["one", "one", "four", "four"]


@pytest.mark.parametrize(
    "app, proc",
    [
        ("edge", "msedgewebview2.exe"),
        ("edge", "MicrosoftEdgeUpdate.exe"),
        ("edge", "msedge_proxy.exe"),
        ("word", "wordpad.exe"),
        ("settings", "SystemSettingsBroker.exe"),
        ("settings", "SettingSyncHost.exe"),
        ("settings", "SettingsHandlerHost.exe"),
        ("teams", "WinTeamsUpdate.exe"),
        # Vendor-prefix stripping never turns another vendor's binary into a
        # known app: "wincode" is not VS Code, "mschrome" is not Chrome.
        ("code", "WinCode.exe"),
        ("chrome", "MsChrome.exe"),
        ("discord", "WindowsDiscord.exe"),
        ("notepad", "WinNotepad.exe"),
    ],
)
def test_process_matching_negatives(app, proc):
    assert process_matches(app, proc) is False


def test_no_tracked_apps_process_matches_a_different_tracked_app():
    from core.config import TRACKED_APPS, app_process_names

    collisions = [
        (a, p, b)
        for a in TRACKED_APPS
        for p in app_process_names(a)
        for b in TRACKED_APPS
        if not (app_process_names(a) & app_process_names(b)) and process_matches(b, p)
    ]
    assert collisions == []


def test_built_in_apps_still_match_through_vendor_prefixes():
    assert process_matches("settings", "SystemSettings.exe")
    assert process_matches("store", "WinStore.App.exe")
    assert process_matches("photos", "Microsoft.Photos.exe")
    assert process_matches("camera", "WindowsCamera.exe")


# -- Vision gating -----------------------------------------------------------------


# ============================================================
# SEARCH, PROCESS AND TIMING GATED RUN
# ============================================================

def _gated_run(monkeypatch, script, gating, on_exec=None, screen=None):
    """Run the loop over scripted actions; returns the number of screen reads.
    ``screen``: a WindowWatch stand-in (change detection), else none."""
    monkeypatch.setenv("MISTAI_VISION_GATING", "1" if gating else "0")
    replies = iter(
        [json.dumps({"action": a, "parameter": p}) for a, p in script]
        + [json.dumps({"action": "done", "speech": "ok"})]
    )
    snap = {"open_apps": [], "foreground_hwnd": 1, "foreground_window": "Editor"}

    def run(action, param):
        if on_exec:
            on_exec(action, snap)
        return SimpleNamespace(
            success=True, detail="", verified=True, escalate_to_user=False,
            reliability_note=lambda: "",
        )

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None,
        captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snap),
        api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: "u", get_fallback_url=lambda: "f"
        ),
        tools=SimpleNamespace(
            specs=SPECS, get=lambda n: SPECS[n], requires_confirmation=lambda *a, **k: False
        ),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
    )
    if screen is not None:
        api.screen = screen
    monkeypatch.setattr(
        "core.agent.requests.post",
        lambda *a, **k: SimpleNamespace(ok=True, status_code=200, json=lambda: {"response": next(replies)}),
    )
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    reads = []
    monkeypatch.setattr(loop, "_get_screen_context", lambda: reads.append(1) or "screen")
    monkeypatch.setattr(loop, "_build_system", lambda *a, **k: "sys")
    assert loop._loop("t", "m", "none", "text", None)["success"]
    return len(reads), loop


# ============================================================
# SEARCH, PROCESS AND TIMING HELPERS (3)
# ============================================================

NON_UI = [("run_command", "Get-Process"), ("find_file", "a.txt"), ("write_file", "x|||y")]


# ============================================================
# SEARCH, PROCESS AND TIMING TESTS (4)
# ============================================================

def test_gating_off_keeps_the_old_refresh_after_system_and_file_actions(monkeypatch, capsys):
    reads, _ = _gated_run(monkeypatch, NON_UI, gating=False)
    # run_command and write_file (system/file) each force a re-read.
    assert reads == 3
    assert "[Agent] Vision gating: off" in capsys.readouterr().out


def test_gating_on_skips_reads_after_non_ui_and_browser_dom_actions(monkeypatch, capsys):
    script = NON_UI + [("browser_click", "Subscribe"), ("browser_get_text", "")]
    reads, _ = _gated_run(monkeypatch, script, gating=True)
    assert reads == 1
    assert "[Agent] Vision gating: on" in capsys.readouterr().out


def test_gating_on_always_rereads_after_ui_changing_actions(monkeypatch):
    # Same foreground window key throughout: UI actions still invalidate.
    script = [("open_app", "notepad"), ("type_text", "hi"), ("press_key", "enter"),
              ("window_maximize", "Notepad"), ("click_on_text", "File"), ("open_url", "x.com")]
    reads, _ = _gated_run(monkeypatch, script, gating=True)
    assert reads == 1 + len(script)


def test_gating_on_rereads_when_the_foreground_window_changes(monkeypatch):
    def switch_window(action, snap):
        if action == "run_command":
            snap["foreground_hwnd"], snap["foreground_window"] = 2, "Notepad"

    reads, _ = _gated_run(monkeypatch, NON_UI, gating=True, on_exec=switch_window)
    assert reads == 2


def test_gating_on_rereads_after_the_cache_expires(monkeypatch):
    monkeypatch.setattr(AgentLoop, "GATED_CACHE_SECONDS", -1.0)
    reads, _ = _gated_run(monkeypatch, NON_UI, gating=True)
    assert reads == 1 + len(NON_UI)


# ============================================================
# BROWSER START RETRY
# ============================================================

def _session(tmp_path, monkeypatch, alive, ready, page):
    """A BrowserSession left with a failed start: a worker thread that is
    (not) alive, a ready flag and maybe a page."""
    import core.browser_session as bs
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bs, "START_TIMEOUT", 0.01)
    s = bs.BrowserSession(profile_dir=str(tmp_path))
    s._thread = SimpleNamespace(is_alive=lambda: alive)
    s._start_error = "Browser did not start within 30s"
    if ready:
        s._ready.set()
    s._page = page
    s.started = []
    monkeypatch.setattr(bs.threading, "Thread", lambda **k: SimpleNamespace(
        start=lambda: s.started.append(1) or (s._ready.set(), setattr(s, "_page", "page")),
        is_alive=lambda: True))
    return s


def test_a_crashed_start_is_cleared_and_the_next_call_starts_again(tmp_path, monkeypatch, capsys):
    s = _session(tmp_path, monkeypatch, alive=False, ready=True, page=None)
    s._req_q.put = lambda item: item[1].put(("ok", 42))
    assert s._call(lambda: 0) == 42
    assert s.started == [1] and s._start_error == ""
    assert "[Browser] previous start failed -- starting again" in capsys.readouterr().out


def test_a_slow_start_that_finished_later_is_used(tmp_path, monkeypatch, capsys):
    s = _session(tmp_path, monkeypatch, alive=True, ready=True, page="page")
    s._req_q.put = lambda item: item[1].put(("ok", "done"))
    assert s._call(lambda: 0) == "done"
    assert s.started == [] and s._start_error == ""
    assert "[Browser] start finished after the timeout" in capsys.readouterr().out


def test_a_start_still_hanging_keeps_failing_without_a_second_browser(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, alive=True, ready=False, page=None)
    with pytest.raises(RuntimeError, match="did not start"):
        s._call(lambda: 0)
    assert s.started == []


def test_close_clears_a_start_error(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, alive=False, ready=True, page=None)
    s._thread = SimpleNamespace(join=lambda timeout=0: None)
    s.close()
    assert s._start_error == "" and s._thread is None


def test_missing_playwright_is_never_retried(tmp_path, monkeypatch):
    s = _session(tmp_path, monkeypatch, alive=False, ready=True, page=None)
    import core.browser_session as bs
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", False)
    with pytest.raises(RuntimeError):
        s._call(lambda: 0)
    assert s.started == []


def test_start_phase_line_is_numbers_only(tmp_path, monkeypatch, capsys):
    import contextlib
    import core.browser_session as bs
    ctx = SimpleNamespace(pages=["page"], close=lambda: None)
    fake = SimpleNamespace(firefox=SimpleNamespace(launch_persistent_context=lambda *a, **k: ctx))
    monkeypatch.setattr(bs, "sync_playwright", lambda: contextlib.nullcontext(fake), raising=False)
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    s = bs.BrowserSession(profile_dir=str(tmp_path / "secretprofile"))
    s._req_q.put(None)                                 # stop right after the start
    s._run()
    lines = [l for l in capsys.readouterr().out.splitlines() if "start phases" in l]
    assert len(lines) == 1 and re.fullmatch(
        r"\[Browser\] start phases: seed [\d.]+s driver [\d.]+s launch [\d.]+s page [\d.]+s", lines[0])


# ============================================================
# BROWSER ELEMENT LIST
# ============================================================

# core/browser_session.py: the numbered element list (order, format, dedupe),
# text matching, the click-effect sentence and short error labels. Pure
# functions, no browser.

import core.browser_session as bs


def _el(num, role="link", name="x", v=1, l="main", order=None, **kw):
    e = {"num": num, "r": role, "n": name, "v": v, "l": l, "order": (0, num if order is None else order),
         "t": 0 if v else 900, "h": "", "s": "", "c": "", "ad": 0, "frame": 0}
    e.update(kw)
    return e


def test_the_dialog_comes_first_then_main_then_the_rest_and_off_screen_is_counted():
    entries = [_el(1, name="Home", l="nav"), _el(2, name="Result one"), _el(3, name="Far", v=0),
               _el(4, "button", "Accept all", l="dialog"), _el(5, name="Up there", v=0, t=-500)]
    groups = bs.order_elements(entries, has_main=True)
    text, count = bs.format_element_list(groups, unreadable_frames=1)
    lines = text.splitlines()
    assert lines[0] == bs.ELEMENTS_HEADER and count == 3
    assert lines[1:] == [
        bs.DIALOG_HEADER, '[4] button "Accept all"',
        bs.MAIN_HEADER, '[2] link "Result one"',
        bs.OTHER_HEADER, '[1] link "Home"',
        "1 more below the fold (use browser_scroll, then browser_elements); 1 above "
        "(scrolled past); 1 iframe(s) not readable."]


def test_without_a_main_landmark_everything_but_menus_is_main_content():
    groups = bs.order_elements([_el(1, name="A", l="other"), _el(2, name="B", l="footer")],
                               has_main=False)
    assert [e["num"] for e in groups[1]] == [1] and [e["num"] for e in groups[2]] == [2]


def test_duplicate_names_get_their_host_and_exact_repeats_are_dropped():
    entries = bs.dedupe_elements([_el(1, name="Read more", h="a.example"),
                                  _el(2, name="Read more", h="b.example"),
                                  _el(3, name="Read more", h="b.example")])
    assert [e["num"] for e in entries] == [1, 2]
    text, _ = bs.format_element_list(bs.order_elements(entries))
    assert '[1] link "Read more" (a.example)' in text and '[2] link "Read more" (b.example)' in text


def test_lines_carry_state_cover_and_ad_and_names_are_cut():
    line = bs.element_line(_el(7, "checkbox", "N" * 80, s="checked", c="dialog 'Cookies'", ad=1))
    assert line == f'[7] checkbox "{"N" * 59}…" (checked) (covered by dialog \'Cookies\') (ad)'


def test_the_list_is_capped_by_items_and_characters():
    entries = [_el(i, name=f"Link number {i}") for i in range(1, 101)]
    text, count = bs.format_element_list(bs.order_elements(entries))
    assert count == bs.ELEMENT_LIST_MAX and "40 more on screen not listed." in text
    text, count = bs.format_element_list(bs.order_elements(entries), max_chars=300)
    assert len(text) <= 300 + 60 and count < 15


def test_a_page_with_nothing_clickable_says_so():
    text, count = bs.format_element_list(([], [], [], [], []))
    assert count == 0 and "(no clickable elements are visible)" in text


def test_matching_prefers_exact_then_visible_links_and_reports_ties():
    entries = [_el(1, "clickable", "python", v=1, order=1),
               _el(2, "link", "Python docs", v=0, order=2),
               _el(3, "link", "Python docs", v=1, order=3),
               _el(4, "link", "Learn Python docs fast", v=1, order=4)]
    assert [e["num"] for e in bs.match_elements(entries, "python docs")] == [3, 2]
    assert [e["num"] for e in bs.match_elements(entries, "PYTHON")] == [1]
    assert bs.match_elements(entries, "docs fast|x") == []
    assert [e["num"] for e in bs.match_elements(entries, "python docs", role="link")] == [3, 2]
    assert bs.match_elements(entries, "python", role="button") == []


def test_short_targets_match_only_whole_words():
    entries = [_el(1, name="52 results"), _el(2, name="Page 2")]
    assert [e["num"] for e in bs.match_elements(entries, "2")] == [2]


def test_covered_matches_rank_after_uncovered_ones():
    entries = [_el(1, name="Article", c="dialog 'x'"), _el(2, name="Article")]
    assert bs.match_elements(entries, "article")[0]["num"] == 2


def test_no_match_names_the_closest_elements():
    entries = [_el(3, name="Requests: HTTP for Humans"), _el(7, name="Quickstart"),
               _el(9, name="Zebra")]
    msg = bs.no_match_message("requests http humans guide", bs.closest_elements(entries, "requests http humans guide"))
    assert msg.startswith("no element matches 'requests http humans guide'. Closest: [3] link")
    assert "Zebra" not in msg
    assert "Nothing on the page is close" in bs.no_match_message("qq", [])


SIG = {"url": "http://h/a", "title": "A", "n": 10, "t": 100, "sy": 0, "dlg": [], "media": "",
       "ex": 0, "chk": 0, "act": "BODY#"}


@pytest.mark.parametrize("change, expected, changed", [
    ({"url": "http://h/b", "title": "B"}, 'the page changed to h/b (title "B")', True),
    ({"title": "Done"}, 'the title changed to "Done"', True),
    ({"dlg": ["Cookies"]}, "a dialog appeared ('Cookies')", True),
    ({"url": "http://h/a#s2"}, "the page jumped to 's2' on the same page", True),
    ({"media": "1"}, "media on the page started playing", True),
    ({"n": 12}, "the page changed in place (content or a menu updated)", True),
    ({"sy": 300}, "the page scrolled", True),
    ({}, "but the page did not change (no navigation, new tab, download or visible change)", False),
])
def test_click_effects_are_named(change, expected, changed):
    assert bs.describe_effect(SIG, dict(SIG, **change)) == (expected, changed)


def test_click_effects_for_tabs_downloads_dialogs_closing_and_state():
    assert bs.describe_effect(dict(SIG, dlg=["Cookies"]), SIG) == ("the dialog 'Cookies' closed", True)
    assert bs.describe_effect(SIG, SIG, download="report.pdf")[0].startswith(
        "a download started: report.pdf")
    assert bs.describe_effect(SIG, SIG, download="x")[1] is False
    assert bs.describe_effect(SIG, SIG, new_tab=("https://www.b.example/p?q=1", "B")) == (
        'it opened a new tab: b.example/p (title "B"); the browser now works in that tab', True)
    assert bs.describe_effect(SIG, None) == ("the page started navigating away", True)
    assert bs.describe_effect(SIG, SIG, "a", "b")[0].startswith("the clicked element changed state")
    assert bs.describe_effect(SIG, dict(SIG, act="INPUT#q"), target_role="searchbox") == (
        "the field is now focused", True)


def test_click_errors_are_one_short_line_never_the_call_log():
    log = ("Locator.click: Timeout 3000ms exceeded.\nCall log:\n  - waiting for get_by_text\n"
           "  - <div> intercepts pointer events")
    assert bs.short_click_error(Exception(log)) == "something on top of it intercepts the click"
    assert bs.short_click_error(bs.PlaywrightTimeout("Timeout 3000ms exceeded.")) == (
        "it did not become clickable within 3s")
    assert bs.is_closed_error(Exception("Target page, context or browser has been closed"))
    assert bs.CLICK_TIMEOUT_MS == 3000


def test_dismiss_candidates_are_the_dialogs_accept_reject_close_buttons():
    entries = [_el(1, "button", "Accept all cookies", l="dialog"), _el(2, "link", "Privacy policy", l="dialog"),
               _el(3, "button", "Reject all", l="dialog"), _el(4, "button", "Close", l="main")]
    assert [e["num"] for e in bs.dismiss_candidates(entries)] == [1, 3]


def test_links_to_one_target_merge_into_the_best_named_one_in_the_first_place():
    entries = bs.dedupe_elements([
        _el(1, name="Now playing", k=7, order=1), _el(2, name="https://a.example › x", k=8, order=2),
        _el(3, name="lofi hip hop radio - beats to relax", k=7, order=3),
        _el(4, name="Requests: HTTP for Humans", k=8, order=4)])
    assert [(e["num"], e["order"]) for e in entries] == [(3, (0, 1)), (4, (0, 2))]
    assert bs._better_link_name("A title", "https://x") and not bs._better_link_name("https://x", "T")


def test_main_content_off_screen_lets_the_other_on_screen_items_count_as_main():
    entries = [_el(1, name="Hero link", l="other"), _el(2, name="Docs", l="nav"),
               _el(3, name="Article", l="main", v=0)]
    groups = bs.order_elements(entries, has_main=True)
    assert [e["num"] for e in groups[1]] == [1] and [e["num"] for e in groups[2]] == [2]


def test_the_real_check_script_refuses_a_missing_or_used_profile(tmp_path, monkeypatch):
    import tools.browser_real_check as rc
    monkeypatch.delenv("MISTAI_BROWSER_SEED_PROFILE", raising=False)
    with pytest.raises(SystemExit, match="does not exist"):
        rc.require_empty_profile(str(tmp_path / "missing"))
    used = tmp_path / "used"
    used.mkdir()
    (used / "cookies.sqlite").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        rc.require_empty_profile(str(used))
    empty = tmp_path / "empty"
    empty.mkdir()
    rc.require_empty_profile(str(empty))
    monkeypatch.setenv("MISTAI_BROWSER_SEED_PROFILE", "1")
    with pytest.raises(SystemExit, match="MISTAI_BROWSER_SEED_PROFILE"):
        rc.require_empty_profile(str(empty))
    src = inspect.getsource(rc.main)
    assert "firefox_signins=None" in src and "require_empty_profile(profile)" in src


def _page_read_api(monkeypatch, in_front=True, text="Beta body text " * 10):
    import core.api as api_mod
    api = Api.__new__(Api)
    api.browser = SimpleNamespace(running=True, page_text=lambda timeout=2.0: ("Beta", text))
    monkeypatch.setattr(api_mod.clip, "foreground_for_reading", lambda *a: (5, "Beta"))
    monkeypatch.setattr(api_mod.clip, "playwright_window", lambda: 5 if in_front else 6)
    monkeypatch.delenv("MISTAI_UIA_FIRST", raising=False)
    return api


def test_a_screen_read_of_mistais_browser_page_uses_the_page_text_not_vision(monkeypatch, capsys):
    api = _page_read_api(monkeypatch)
    out = api._browser_page_answer("what does the page say about beta")
    assert out.startswith("PAGE TEXT of MistAI's browser page 'Beta' (DOM, exact; no screenshot")
    assert "browser_elements" in out
    assert "no vision call" in capsys.readouterr().out
    assert api._browser_page_answer("what colour is the header") == ""      # about looks: vision
    assert _page_read_api(monkeypatch, in_front=False)._browser_page_answer("q") == ""
    assert _page_read_api(monkeypatch, text="tiny")._browser_page_answer("q") == ""


# ============================================================
# BROWSER ELEMENT LIST API
# ============================================================

class _ListBrowser:
    """A stand-in session with element lists: click answers are scripted."""

    def __init__(self, click_result=None):
        self.click_result = click_result or {"ok": True, "changed": True, "message": "Clicked [3] x: done."}
        self.calls, self.lists, self.note = [], 0, ""
        self.running, self.last_url, self.last_nav_still_loading = True, "https://h/a", False

    def click(self, target="", role=None, ref=None):
        self.calls.append((target, role, ref))
        return self.click_result

    def elements_text(self, still_loading=None):
        self.lists += 1
        return "PAGE ELEMENTS (...):\n[3] link \"x\""

    def take_note(self):
        note, self.note = self.note, ""
        return note

    def navigate(self, url):
        return url, "T"

    def get_text(self, _s=""):
        return "Some page text"

    def scroll(self, d):
        return True, 40


def _list_api(browser):
    api = Api.__new__(Api)
    api.browser, api.spoken, api.last_result_text = browser, [], ""
    api.speak = api.spoken.append
    return api


@pytest.mark.parametrize("param, call", [
    ("12", ("", None, 12)), ("[12]", ("", None, 12)), ("#7", ("", None, 7)),
    ("Subscribe|button", ("Subscribe", "button", None)), ("lofi hip hop", ("lofi hip hop", None, None)),
])
def test_browser_click_takes_a_number_or_text(param, call):
    b = _ListBrowser()
    assert Api._act_browser_click(_list_api(b), param) is True
    assert b.calls == [call]


def test_a_changed_page_gets_the_fresh_list_once_with_the_tip():
    b = _ListBrowser()
    api = _list_api(b)
    Api._act_browser_click(api, "3")
    assert api.last_result_text.startswith("Clicked [3] x: done.\nPAGE ELEMENTS")
    assert api.last_result_text.endswith(config.BROWSER_NOTE)
    Api._act_browser_click(api, "3")
    assert config.BROWSER_NOTE not in api.last_result_text      # once per phase
    assert len(config.BROWSER_NOTE) <= 400


def test_no_change_and_downloads_get_no_list_and_failures_do():
    b = _ListBrowser({"ok": True, "changed": False, "message": "Clicked [3] x but the page did not change."})
    api = _list_api(b)
    assert Api._act_browser_click(api, "3") is True
    assert b.lists == 0 and "did not change" in api.last_result_text
    b.click_result = {"ok": False, "changed": False, "stale": True, "message": "element [3] is no longer on the page"}
    assert Api._act_browser_click(api, "3") is False
    assert b.lists == 1 and "PAGE ELEMENTS" in api.last_result_text


def test_a_reopened_browser_is_said_before_the_result():
    b = _ListBrowser()
    b.note = bs.BrowserSession.REOPENED_NOTE
    api = _list_api(b)
    Api._act_browser_click(api, "3")
    assert api.last_result_text.startswith("NOTE: the browser had been closed; reopened. Clicked")


def test_a_click_exception_is_one_line():
    b = _ListBrowser()
    b.click = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Locator.click: boom\nCall log:\n - x"))
    api = _list_api(b)
    assert Api._act_browser_click(api, "3") is False
    assert api.last_result_text == "browser_click failed: Locator.click: boom"


def test_navigate_and_scroll_append_the_list_and_a_stalled_load_says_so():
    b = _ListBrowser()
    api = _list_api(b)
    assert Api._act_browser_navigate(api, "h/a") is True
    assert "\nPAGE ELEMENTS" in api.last_result_text and "still loading" not in api.last_result_text
    b.last_nav_still_loading = True                 # listed at the cap, still going
    assert Api._act_browser_navigate(api, "h/a") is True
    assert "The page is still loading; here is what is on it so far." in api.last_result_text
    assert "load was stopped" not in api.last_result_text
    b.last_nav_stopped = True                       # nothing to list until 20 s: stopped
    assert Api._act_browser_navigate(api, "h/a") is True
    assert "The page was still loading after 20s; the load was stopped. Here is what is on it." in (
        api.last_result_text)
    b.get_text = lambda _s="": ""
    assert Api._act_browser_navigate(api, "h/a") is False
    assert "did not load within 20s" in api.last_result_text
    b.last_nav_stopped = False                      # partial with no text yet: still a page
    assert Api._act_browser_navigate(api, "h/a") is True
    assert Api._act_browser_scroll(api, "down") is True
    assert api.last_result_text.startswith("Scrolled down; the bottom of the screen is now at about 40%")
    assert Api._act_browser_scroll(api, "sideways") is False


def test_browser_elements_never_starts_the_browser():
    b = _ListBrowser()
    b.running = False
    api = _list_api(b)
    assert Api._act_browser_elements(api, "") is False
    assert "not open" in api.last_result_text and b.lists == 0
    b.running = True
    assert Api._act_browser_elements(api, "") is True and b.lists == 1


def test_the_new_browser_actions_are_registered_and_read_sized():
    from core.tools import action_entry, READ_RESULT_CHARS
    for name in ("browser_elements", "browser_scroll"):
        assert name in SPECS and SPECS[name].result_chars >= READ_RESULT_CHARS
    assert "never silently clicks the wrong thing" not in action_entry("browser_click")
    assert SPECS["browser_elements"].categories == ("browser", "perception")


# ============================================================
# BROWSER PROFILE SETTING
# ============================================================

# Settings "Use my Firefox sign-ins" (user_profile browser_seed_profile, env
# MISTAI_BROWSER_SEED_PROFILE, default off): off = a clean MistAI profile and
# nothing read from the user's Firefox profile; on = the old seeding.


class _Profile:
    def __init__(self):
        self.rows = {}

    def get_profile(self, key, default=None):
        return self.rows.get(key, default)

    def set_profile(self, key, value):
        self.rows[key] = value


def _signins_api():
    api = Api.__new__(Api)
    api.memory = _Profile()
    return api


def test_firefox_signins_are_off_by_default_saved_and_env_overridable(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_BROWSER_SEED_PROFILE", raising=False)
    api = _signins_api()
    assert api.get_firefox_signins() is False
    assert api.set_firefox_signins(True) == {"success": True, "enabled": True}
    assert api.memory.rows["browser_seed_profile"] == "on"
    assert "applies to the next browser start" in capsys.readouterr().out
    monkeypatch.setenv("MISTAI_BROWSER_SEED_PROFILE", "0")
    assert api.get_firefox_signins() is False
    monkeypatch.setenv("MISTAI_BROWSER_SEED_PROFILE", "1")
    api.set_firefox_signins(False)
    assert api.get_firefox_signins() is True
    assert config.browser_seed_profile_enabled("on") is True


def _start_without_launching(monkeypatch, session):
    monkeypatch.setattr(bs, "PLAYWRIGHT_AVAILABLE", True)
    monkeypatch.setattr(bs.threading, "Thread", lambda **k: SimpleNamespace(
        start=lambda: session._ready.set(), is_alive=lambda: True))
    session.ensure_started()


def test_off_uses_the_clean_profile_and_never_reads_the_firefox_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

    def never(*_a, **_k):
        raise AssertionError("the user's Firefox profile was read")

    monkeypatch.setattr(bs, "clone_daily_profile", never)
    monkeypatch.setattr(bs, "find_default_firefox_profile", never)
    for fn in (None, lambda: False, lambda: 1 / 0):        # no setting, off, a broken getter
        s = bs.BrowserSession(firefox_signins=fn)
        _start_without_launching(monkeypatch, s)
        assert s.profile_dir == os.path.join(str(tmp_path), "MistAI", "browser_profile_clean")
    out = capsys.readouterr().out
    assert out.count("[Browser] profile: clean") == 3 and str(tmp_path) not in out
    s = bs.BrowserSession(firefox_signins=lambda: False)
    for refused in (s.use_daily_profile_copy, lambda: s.import_cookies("a.example")):
        with pytest.raises(RuntimeError, match="Let Mist use sign-ins from my browser"):
            refused()


def test_on_seeds_a_missing_profile_once_like_before(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    copies = []

    def clone(dest):
        copies.append(dest)
        os.makedirs(dest)
        return "C:\\secret\\profile", ["cookies.sqlite", "storage"]

    monkeypatch.setattr(bs, "clone_daily_profile", clone)
    seeded = os.path.join(str(tmp_path), "MistAI", "browser_profile_from_daily")
    s = bs.BrowserSession(firefox_signins=lambda: True)
    _start_without_launching(monkeypatch, s)
    assert s.profile_dir == seeded and copies == [seeded]
    s2 = bs.BrowserSession(firefox_signins=lambda: True)
    _start_without_launching(monkeypatch, s2)             # the folder exists now: reused
    assert copies == [seeded]
    out = capsys.readouterr().out
    assert out.count("[Browser] profile: seeded from Firefox (opt-in)") == 2
    assert "secret" not in out and "[Browser] Auto-seeded profile (2 item(s))" in out


def test_an_explicit_profile_folder_is_never_seeded(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "clone_daily_profile", lambda d: (_ for _ in ()).throw(AssertionError))
    s = bs.BrowserSession(profile_dir=str(tmp_path / "empty"))
    _start_without_launching(monkeypatch, s)
    assert s.profile_dir == str(tmp_path / "empty") and not os.path.exists(s.profile_dir)


def test_the_api_hands_the_setting_to_the_browser_session():
    init = inspect.getsource(Api.__init__)
    assert "BrowserSession(firefox_signins=self.get_firefox_signins)" in init


def test_the_setting_moved_into_settings_under_browser_sign_ins():
    from ui.html import HTML
    assert 'id="sw-firefox_signins"' not in HTML and "Use my Firefox sign-ins" not in HTML
    sec = HTML.index('id="sec-signins"')
    assert sec < HTML.index('id="sw-signins"') < HTML.index('id="sel-signins-browser"') \
        < HTML.index('id="btn-signin-browser"') < HTML.index('onclick="saveAPIKey()"')
    assert ('<div class="toggle-row set-toggle"><span>Let Mist use sign-ins from my browser</span>'
            '<div class="sw" id="sw-signins"></div></div>') in HTML
    assert ">Sign in inside Mist's browser</button>" in HTML
    js = HTML.split("<script>")[-1]
    assert "['sw-signins','Let Mist use sign-ins from my browser']" in js
    assert "runStep('signins',loadSignins);" in _fn_text(js, "openAPIModal")
    assert "pywebview.api.set_browser_signins(enabled,source)" in js
    assert "pywebview.api.open_signin_browser()" in js
    # The sentences come from Python, not the page.
    assert bsi.SIGNINS_EXPLANATION not in HTML and bsi.UNSUPPORTED_NOTE not in HTML


def _fn_text(js, name):
    return re.search(r"function " + name + r"\(.*?\n}\n", js, re.S).group(0)


# -- detection (fake registry) ---------------------------------------------------------------

_FAKE_INSTALLS = [
    ("Mozilla Firefox", r'"C:\Program Files\Mozilla Firefox\firefox.exe"'),
    ("Google Chrome", r'"C:\Program Files\Google\Chrome\Application\chrome.exe"'),
    ("Microsoft Edge", r'"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"'),
    ("Brave", r'"C:\Users\u\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"'),
    ("Opera Stable", r'"C:\Users\u\AppData\Local\Programs\Opera\launcher.exe"'),
    ("Opera GX Stable", r'"C:\Users\u\AppData\Local\Programs\Opera GX\launcher.exe"'),
    ("Vivaldi", r'"C:\Users\u\AppData\Local\Vivaldi\Application\vivaldi.exe"'),
    ("Internet Explorer", r'"C:\Program Files\Internet Explorer\iexplore.exe"'),
    ("Razer AppEngine", r'"C:\Program Files\Razer\rzappengine.exe"'),   # not a browser
    ("Mozilla Firefox", r'"C:\Program Files\Mozilla Firefox\firefox.exe"'),   # HKLM twice
]


def test_installed_browsers_are_found_once_each_in_a_fixed_order():
    found = bsi.installed_browsers(lambda: _FAKE_INSTALLS, lambda: False)
    assert [i for i, _l in found] == ["firefox", "chrome", "edge", "brave", "opera",
                                      "opera-gx", "vivaldi"]
    assert dict(found)["edge"] == "Microsoft Edge"
    assert bsi.installed_browsers(lambda: [], lambda: True) == [("firefox", "Firefox")]
    assert bsi.installed_browsers(lambda: 1 / 0, lambda: 1 / 0) == []       # never raises
    assert bsi.installed_browsers(lambda: [("LibreWolf", "librewolf.exe")], lambda: False) == [
        ("librewolf", "LibreWolf")]


@pytest.mark.parametrize("progid, fam", [
    ("FirefoxURL-308046B0AF4A39CB", "firefox"), ("ChromeHTML", "chrome"), ("MSEdgeHTM", "edge"),
    ("BraveHTML", "brave"), ("OperaStable", "opera"), ("OperaGXStable", "opera-gx"),
    ("VivaldiHTM.ABC", "vivaldi"), ("IE.HTTPS", None), (None, None),
])
def test_the_default_browser_comes_from_the_https_progid(progid, fam):
    assert bsi.default_browser(lambda: progid) == fam
    assert bsi.default_browser(lambda: 1 / 0) is None


def test_only_firefox_is_supported_and_the_others_say_so():
    found = bsi.installed_browsers(lambda: _FAKE_INSTALLS, lambda: False)
    choices = bsi.browser_choices(found, default="chrome")
    by_id = {c["id"]: c for c in choices}
    assert by_id["firefox"]["supported"] is True and by_id["firefox"]["note"] == ""
    for fam in ("chrome", "edge", "brave", "opera", "opera-gx", "vivaldi"):
        assert by_id[fam]["supported"] is False
        assert by_id[fam]["note"] == "Not supported - use Sign in inside Mist's browser instead"
    assert [c["id"] for c in choices if c["default"]] == ["chrome"]
    # Firefox is always offered (the existing behavior), marked when not found.
    only_chrome = bsi.browser_choices([("chrome", "Google Chrome")])
    assert only_chrome[0] == {"id": "firefox", "label": "Firefox (not found)", "supported": True,
                              "note": "", "default": False}


def test_the_source_defaults_to_the_default_browser_but_keeps_old_firefox_users():
    choices = bsi.browser_choices([("firefox", "Firefox"), ("chrome", "Google Chrome")], "chrome")
    assert bsi.resolve_source("", choices, switch_on=False, default="chrome") == "chrome"
    assert bsi.resolve_source("", choices, switch_on=True, default="chrome") == "firefox"
    assert bsi.resolve_source("chrome", choices, switch_on=True, default="firefox") == "chrome"
    assert bsi.resolve_source("vivaldi", choices, switch_on=False, default=None) == "firefox"


# -- the Api ---------------------------------------------------------------------------------

def _fake_detection(monkeypatch, installs=_FAKE_INSTALLS, progid="ChromeHTML"):
    monkeypatch.setattr(bsi, "registry_browsers", lambda: installs)
    monkeypatch.setattr(bsi, "registry_default_progid", lambda: progid)
    monkeypatch.setattr(bsi, "firefox_profile_exists", lambda: False)
    monkeypatch.setattr(bsi.installed_browsers, "__defaults__", (lambda: installs, lambda: False))
    monkeypatch.setattr(bsi.default_browser, "__defaults__", (lambda: progid,))


def test_the_api_lists_browsers_preselects_the_default_and_saves_the_choice(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_BROWSER_SEED_PROFILE", raising=False)
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _fake_detection(monkeypatch)
    api = _signins_api()
    s = api.get_browser_signins()
    assert s["enabled"] is False and s["source"] == "chrome" and s["supported"] is False
    assert s["note"] == bsi.UNSUPPORTED_NOTE and s["explanation"] == bsi.SIGNINS_EXPLANATION
    assert "never saved passwords" in s["explanation"] and "always its own" in s["explanation"]
    r = api.set_browser_signins(True, "chrome")                     # on, but not importable
    assert r["success"] is True and r["enabled"] is True and r["supported"] is False
    assert api.get_firefox_signins() is False                        # no seeding from Chrome
    r = api.set_browser_signins(None, "firefox")
    assert r["source"] == "firefox" and api.get_firefox_signins() is True
    assert api.memory.rows == {"browser_seed_profile": "on", "browser_signins_source": "firefox"}
    bad = api.set_browser_signins(None, "netscape")
    assert bad["success"] is False and api.memory.rows["browser_signins_source"] == "firefox"
    out = capsys.readouterr().out
    assert "[Browser] browser sign-ins: on, chrome (not supported; applies to the next browser start)" in out
    assert "Program Files" not in out


def test_a_switch_that_was_on_before_keeps_seeding_from_firefox(monkeypatch):
    monkeypatch.delenv("MISTAI_BROWSER_SEED_PROFILE", raising=False)
    _fake_detection(monkeypatch)                                     # default browser: Chrome
    api = _signins_api()
    api.memory.rows["browser_seed_profile"] = "on"                   # saved by the old switch
    assert api.get_firefox_signins() is True
    assert api.get_browser_signins()["source"] == "firefox"
    monkeypatch.setenv("MISTAI_BROWSER_SEED_PROFILE", "0")           # the env override still wins
    assert api.get_firefox_signins() is False
    monkeypatch.setenv("MISTAI_BROWSER_SEED_PROFILE", "1")
    api.memory.rows.clear()
    assert api.get_firefox_signins() is True


def test_sign_in_inside_mists_browser_opens_its_own_browser(monkeypatch):
    import threading as _t
    api = _signins_api()
    started = []
    api.browser = SimpleNamespace(available=True, ensure_started=lambda: started.append(1))
    api._agent_lock = _t.Lock()
    api._focus_hwnd = lambda hwnd: started.append(("focus", hwnd))
    monkeypatch.setattr(api_mod.clip, "playwright_window", lambda: 42)
    monkeypatch.setattr(api_mod.threading, "Thread",
                        lambda target, **k: SimpleNamespace(start=target))
    r = api.open_signin_browser()
    assert r["success"] is True and "Mist's own browser" in r["message"]
    assert started == [1, ("focus", 42)]
    api._agent_lock.acquire()                                         # a task is running
    assert api.open_signin_browser()["success"] is False and started == [1, ("focus", 42)]
    api._agent_lock.release()
    api.browser = SimpleNamespace(available=False)
    assert api.open_signin_browser()["success"] is False


def test_firefox_seeding_copies_sign_ins_but_no_saved_passwords(tmp_path, monkeypatch):
    assert bs._PROFILE_ESSENTIAL_FILES == ("cookies.sqlite", "permissions.sqlite")
    assert bs._PROFILE_ESSENTIAL_DIRS == ("storage",)
    src = tmp_path / "daily"
    (src / "storage" / "default").mkdir(parents=True)
    for name in ("cookies.sqlite", "permissions.sqlite", "logins.json", "key4.db", "cert9.db",
                 "places.sqlite"):
        (src / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(bs, "find_default_firefox_profile", lambda: str(src))
    dest = tmp_path / "mist"
    _source, copied = bs.clone_daily_profile(str(dest))
    assert sorted(copied) == ["cookies.sqlite", "permissions.sqlite", "storage"]
    assert sorted(os.listdir(dest)) == ["cookies.sqlite", "permissions.sqlite", "storage"]


# ============================================================
# BROWSER FIXTURE PAGES
# ============================================================

# Real headless Playwright Firefox against local pages on 127.0.0.1 (no real
# sites), with a fresh EMPTY profile folder; skipped when Playwright or its
# Firefox is not installed. "{other}" is the same server under another host
# name (localhost), so an iframe from it is cross-origin.

FIXTURE_PAGES = {
    "/links.html": "<title>Links</title><main><a href='/a.html'>Alpha page</a> "
                   "<a href='/b.html'>Beta page</a></main>",
    "/a.html": "<title>Alpha</title><main><h1>Alpha</h1><a href='/links.html'>Back to links</a></main>",
    "/b.html": "<title>Beta</title><main><h1>Beta</h1><p>Beta body text for reading.</p></main>",
    "/dupes.html": "<title>Dupes</title><main><a href='/a.html'>Read more</a> "
                   "<a href='{other}/b.html'>Read more</a></main>",
    "/hidden.html": "<title>Hidden</title><main><div style='display:none'><a href='/b.html'>python docs"
                    "</a></div><a href='/a.html'>python docs</a></main>",
    "/fold.html": "<title>Fold</title><main><a href='/b.html'>Top link</a><div style='height:4000px'></div>"
                  "<a href='/a.html'>Far link</a></main>",
    "/modal.html": "<title>Modal</title><main><a href='/a.html'>Article</a></main>"
                   "<div id='m' role='dialog' aria-label='Cookie consent' style='position:fixed;inset:0;"
                   "background:#fff'><button onclick=\"document.getElementById('m').remove()\">Accept all"
                   "</button><button>Settings</button></div>",
    "/iframe.html": "<title>Frame</title><main><a href='/a.html'>Headline</a></main>"
                    "<iframe id='f' title='Privacy Center' src='{other}/consent.html' style='position:fixed;"
                    "top:0;left:0;width:100%;height:100%;border:0'></iframe><script>addEventListener("
                    "'message',()=>document.getElementById('f').remove())</script>",
    "/consent.html": "<title>Consent</title><body style='background:#fff'><button onclick=\"parent."
                     "postMessage('ok','*')\">Consent and continue</button></body>",
    "/shadow.html": "<title>Shadow</title><main><x-card></x-card></main><script>customElements.define("
                    "'x-card',class extends HTMLElement{constructor(){super();this.attachShadow({mode:'open'})"
                    ".innerHTML=\"<button onclick=\\\"document.title='Shadow clicked'\\\">Shadow button</button>\";}})"
                    "</script>",
    "/newtab.html": "<title>Tabs</title><main><a href='/b.html' target='_blank'>Open beta in a new tab</a></main>",
    "/download.html": "<title>Files</title><main><a href='/file.pdf'>Get the report</a></main>",
    "/toggle.html": "<title>Toggle</title><main><label><input type='checkbox' id='c'> Remember me</label>"
                    "<button>Does nothing</button></main>",
    # Results rendered by script after a slow fetch: the header link is there at once.
    "/late.html": "<title>Late</title><header><a href='/a.html'>Home</a></header><main id='m'></main>"
                  "<script>fetch('/late.json').then(r=>r.json()).then(d=>{const a=document."
                  "createElement('a');a.href='/b.html';a.textContent=d.name;document.getElementById"
                  "('m').append(a);});</script>",
    # Never settles: a node is added every 100 ms.
    "/busy.html": "<title>Busy</title><main><a href='/a.html'>Busy link</a><div id='t'></div></main>"
                  "<script>setInterval(()=>{const s=document.createElement('span');s.textContent='.';"
                  "document.getElementById('t').append(s);},100)</script>",
    "/form.html": "<title>Form</title><main><label for='q'>Search the site</label><input id='q'>"
                  "<input placeholder='Your email'><a href='/a.html'>Help</a></main>",
}


class _FixtureHandler(__import__("http.server").server.BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/file.pdf":
            body = b"%PDF-1.4 fixture"
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="report.pdf"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/late.json":
            __import__("time").sleep(0.8)
            body = b'{"name": "Late result"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/slowtext.html":     # text but nothing to click, then it stalls
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>Slow text</title><p>Plain text that arrived before the "
                             b"stall, long enough to be read as a page.</p>" + b" " * 2048)
            for _ in range(40):
                if self.server.stopping.wait(0.25):
                    break
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    break
            return
        if path == "/slow.html":         # never finishes: part of the page, then nothing
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>Slow</title><main><a href='/a.html'>Loaded part</a> some text "
                             b"that arrived before the stall, enough to read.</main>" + b" " * 2048)
            for _ in range(40):
                if self.server.stopping.wait(0.25):
                    break
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    break
            return
        page = FIXTURE_PAGES.get(path)
        if page is None:
            self.send_error(404)
            return
        body = page.replace("{other}", self.server.other).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def fixture_site():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    srv.daemon_threads = True
    srv.stopping = threading.Event()
    port = srv.server_address[1]
    srv.other = f"http://localhost:{port}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.stopping.set()
    srv.shutdown()


@pytest.fixture(scope="module")
def live(tmp_path_factory, fixture_site):
    if not bs.PLAYWRIGHT_AVAILABLE:
        pytest.skip("Playwright not installed")
    profile = tmp_path_factory.mktemp("pw_profile_empty")
    assert os.listdir(profile) == []                  # a fresh, EMPTY profile, never a copy
    s = bs.BrowserSession(profile_dir=str(profile), headless=True)
    s.ensure_started()
    if s._start_error:
        pytest.skip(f"Playwright Firefox could not start ({s._start_error[:80]})")
    s.base = fixture_site
    yield s
    s.close()


def _num(text, role, name):
    m = re.search(r"\[(\d+)\] " + role + ' "' + re.escape(name) + '"', text)
    assert m, text
    return int(m.group(1))


def _live_api(session, downloads=None):
    import tempfile
    api = _list_api(session)
    api.__dict__["_app_notes_given"] = set()
    # Never the user's real Downloads folder in a test.
    folder = str(downloads) if downloads else os.path.join(tempfile.gettempdir(),
                                                           "mist_test_downloads")
    api._downloads_dir = lambda: folder
    return api


# ============================================================
# BROWSER FIXTURE TESTS
# ============================================================

def test_live_normal_links_are_listed_and_clicked_by_number(live):
    live.navigate(live.base + "/links.html")
    text = live.elements_text()
    alpha = _num(text, "link", "Alpha page")
    res = live.click(ref=alpha)
    assert res["ok"] and res["changed"]
    assert res["message"].startswith(f'Clicked [{alpha}] link "Alpha page": the page changed to '
                                     '127.0.0.1')
    assert '/a.html (title "Alpha")' in res["message"] and live.last_url.endswith("/a.html")


def test_live_duplicate_names_are_told_apart_by_host(live):
    live.navigate(live.base + "/dupes.html")
    text = live.elements_text()
    assert '"Read more" (127.0.0.1' in text and '"Read more" (localhost' in text
    res = live.click("Read more")
    assert res["message"].startswith("2 matches; clicked [")


def test_live_a_hidden_first_match_is_skipped_for_the_visible_one(live):
    live.navigate(live.base + "/hidden.html")
    text = live.elements_text()
    assert text.count('"python docs"') == 1
    res = live.click("python docs")
    assert res["ok"] and "/a.html" in res["message"]


def test_live_below_the_fold_is_counted_and_still_clickable_by_text(live):
    live.navigate(live.base + "/fold.html")
    text = live.elements_text()
    assert "Far link" not in text and "1 more below the fold" in text
    res = live.click("Far link")
    assert res["ok"] and "/a.html" in res["message"]


def test_live_a_modal_comes_first_and_a_covered_target_is_not_clicked(live):
    live.navigate(live.base + "/modal.html")
    text = live.elements_text()
    lines = text.splitlines()
    assert lines[1] == bs.DIALOG_HEADER and "Accept all" in lines[2]
    article = _num(text, "link", "Article")
    assert "(covered by dialog 'Cookie consent')" in text
    res = live.click(ref=article)
    accept = _num(text, "button", "Accept all")
    assert not res["ok"] and res["message"].startswith(
        f'[{article}] link "Article" is covered by dialog \'Cookie consent\'; it was not clicked.')
    assert f'[{accept}] button "Accept all" probably dismisses it' in res["message"]
    assert live.last_url.endswith("/modal.html")
    res = live.click(ref=accept)
    assert res["ok"] and res["changed"] and "the dialog 'Cookie consent' closed" in res["message"]
    assert live.click(ref=article)["ok"]


def test_live_a_consent_dialog_in_a_cross_origin_iframe(live):
    live.navigate(live.base + "/iframe.html")
    text = live.elements_text()
    assert bs.DIALOG_HEADER in text and '"Consent and continue"' in text
    headline = _num(text, "link", "Headline")
    res = live.click(ref=headline)
    assert not res["ok"] and "is covered by iframe 'Privacy Center'" in res["message"]
    consent = _num(text, "button", "Consent and continue")
    assert f"[{consent}]" in res["message"]
    res = live.click(ref=consent)
    assert res["ok"] and res["changed"], res
    assert live.click(ref=headline)["ok"]


def test_live_open_shadow_dom_elements_are_listed_and_clickable(live):
    live.navigate(live.base + "/shadow.html")
    text = live.elements_text()
    res = live.click(ref=_num(text, "button", "Shadow button"))
    assert res["ok"] and 'the title changed to "Shadow clicked"' in res["message"]


def test_live_a_new_tab_is_adopted(live):
    live.navigate(live.base + "/newtab.html")
    res = live.click("Open beta in a new tab")
    assert res["ok"] and "it opened a new tab: 127.0.0.1" in res["message"]
    assert "/b.html" in res["message"] and live.current_url().endswith("/b.html")
    assert "Beta body text" in live.get_text("")


def test_live_a_download_is_named_and_not_called_a_plain_click(live):
    live.navigate(live.base + "/download.html")
    res = live.click("Get the report")
    assert res["ok"] and not res["changed"]
    assert "a download started: report.pdf" in res["message"]


def test_live_state_changes_and_no_change_are_reported(live):
    live.navigate(live.base + "/toggle.html")
    text = live.elements_text()
    assert 'checkbox "Remember me" (not checked)' in text
    res = live.click(ref=_num(text, "checkbox", "Remember me"))
    assert "changed state" in res["message"]
    res = live.click("Does nothing")
    assert res["ok"] and not res["changed"] and "but the page did not change" in res["message"]


def test_live_no_match_fails_fast_with_the_closest_names(live):
    import time as _time
    live.navigate(live.base + "/links.html")
    t0 = _time.monotonic()
    res = live.click("Alpha pages overview")
    assert _time.monotonic() - t0 < 2.0
    assert not res["ok"] and "no element matches 'Alpha pages overview'. Closest: [" in res["message"]
    assert '"Alpha page"' in res["message"]


def test_live_a_stale_number_after_a_page_change_is_refused_clearly(live):
    api = _live_api(live)
    assert Api._act_browser_navigate(api, live.base + "/links.html") is True
    alpha = _num(api.last_result_text, "link", "Alpha page")
    beta = _num(api.last_result_text, "link", "Beta page")
    assert Api._act_browser_click(api, str(beta)) is True
    assert "\nPAGE ELEMENTS" in api.last_result_text           # the page changed: fresh list
    assert Api._act_browser_click(api, str(alpha)) is False
    assert api.last_result_text.startswith(
        f"element [{alpha}] is no longer on the page (the page changed since that list)")
    assert live.last_url.endswith("/b.html")


def test_live_a_page_that_never_finishes_returns_at_the_cap_with_what_it_has(live):
    import time as _time
    api = _live_api(live)
    t0 = _time.monotonic()
    assert Api._act_browser_navigate(api, live.base + "/slow.html") is True
    assert _time.monotonic() - t0 < bs.READY_CAP_S + 1.5
    assert "The page is still loading; here is what is on it so far." in api.last_result_text
    assert '"Loaded part"' in api.last_result_text and "was still loading; this is what" in (
        api.last_result_text)
    t1 = _time.monotonic()
    live.navigate(live.base + "/b.html")                       # not queued behind the stall
    assert _time.monotonic() - t1 < 3 and not live.last_nav_still_loading


def test_live_a_page_with_nothing_to_list_is_stopped_at_the_nav_timeout(live, monkeypatch):
    import time as _time
    monkeypatch.setattr(bs, "NAV_TIMEOUT_MS", 4000)
    api = _live_api(live)
    t0 = _time.monotonic()
    assert Api._act_browser_navigate(api, live.base + "/slowtext.html") is True
    assert 3.5 < _time.monotonic() - t0 < 9
    assert "The page was still loading after" in api.last_result_text and "load was stopped" in (
        api.last_result_text)
    assert live.last_nav_stopped


def test_live_a_closed_page_and_a_closed_browser_are_reopened_once(live):
    live.navigate(live.base + "/links.html")
    live._call(lambda: live._page.close())
    assert live.navigate(live.base + "/a.html")[1] == "Alpha"
    assert live.take_note() == bs.BrowserSession.REOPENED_NOTE
    live._call(lambda: live._context.close())
    api = _live_api(live)
    assert Api._act_browser_navigate(api, live.base + "/b.html") is True
    assert api.last_result_text.startswith("NOTE: the browser had been closed; reopened. Navigated to")
    assert "has been closed" not in api.last_result_text


# ============================================================
# PAGE READINESS, DOWNLOADS AND FILL
# ============================================================

def _sample(n=5, rs="complete", q=1000, title="New", url="https://h/new"):
    return {"url": url, "title": title, "rs": rs, "n": n, "q": q}


def test_ready_verdict_needs_a_listable_settled_page():
    v = bs.ready_verdict
    assert v(None, None, 0) == "nothing"
    assert v(_sample(n=0, rs="interactive"), None, 0) == "nothing"       # nothing to list yet
    assert v(_sample(n=0), _sample(n=0), 0) == "ready"                  # a complete, plain page
    assert v(_sample(rs="loading"), _sample(rs="loading"), 0) == "partial"
    assert v(_sample(), None, 0) == "partial"                          # one sample: not stable
    assert v(_sample(n=6), _sample(n=5), 0) == "partial"              # still adding elements
    assert v(_sample(q=100), _sample(q=50), 0) == "usable"            # DOM changed 100 ms ago
    assert v(_sample(), _sample(), 1) == "usable"                     # a script/fetch pending
    assert v(_sample(), _sample(), 0) == "ready"
    assert v(_sample(q=-1), _sample(q=-1), 0) == "ready"              # no observer: not a reason


def test_ready_verdict_waits_for_the_new_url_and_a_settled_title():
    v = bs.ready_verdict
    old = "https://h/old"
    assert v(_sample(url=old), _sample(url=old), 0, need_url_change_from=old) == "nothing"
    assert v(_sample(), _sample(), 0, need_url_change_from=old) == "ready"
    # A single-page app's interim title (the site's bare name, part of the old title).
    assert v(_sample(title="YouTube"), _sample(title="YouTube"), 0,
             before_title="lofi - YouTube") == "usable"
    assert v(_sample(title="Old - Site"), _sample(title="Old - Site"), 0,
             before_title="Old - Site") == "usable"
    assert v(_sample(title="Old - Site"), _sample(title="Old - Site"), 0,
             before_title="Old - Site", same_title_ok=True) == "ready"
    assert v(_sample(url="about:blank"), _sample(url="about:blank"), 0, skip_blank=True) == "nothing"
    # Only a page still loading or still adding elements is called "still loading".
    assert [bs.still_loading(x) for x in ("ready", "usable", "partial", "nothing")] == [
        False, False, True, True]


def test_a_still_loading_page_change_says_so():
    before = {"url": "https://h/a", "title": "A"}
    after = {"url": "https://h/b", "title": "B"}
    text, changed = bs.describe_effect(before, after, still_loading=True)
    assert changed and text.endswith(bs.STILL_LOADING_NOTE)
    assert bs.STILL_LOADING_NOTE not in bs.describe_effect(before, after)[0]
    assert "still loading" in bs.describe_effect(before, before, new_tab=("https://h/c", "C"),
                                                 still_loading=True)[0]


@pytest.mark.parametrize("raw, want", [
    ("report.pdf", "report.pdf"), ("..\\..\\evil.exe", "evil.exe"), ("a/b/c.txt", "c.txt"),
    ('bad:<name>?.txt', "bad__name__.txt"), ("CON.txt", "_CON.txt"), ("", "download"),
    ("...", "download"), ("x" * 300 + ".zip", "x" * 146 + ".zip"),
])
def test_download_names_are_plain_safe_file_names(raw, want):
    assert bs.safe_download_name(raw) == want


def test_a_saved_download_never_overwrites(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"new")
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "report.pdf").write_bytes(b"old")
    (folder / "report (1).pdf").write_bytes(b"old1")
    path = bs.save_download_copy(str(src), str(folder), "report.pdf")
    assert os.path.basename(path) == "report (2).pdf" and open(path, "rb").read() == b"new"
    assert (folder / "report.pdf").read_bytes() == b"old"
    assert (folder / "report (1).pdf").read_bytes() == b"old1"


class _DlBrowser(_ListBrowser):
    """A stand-in whose clicks start a download; saves are recorded."""

    def __init__(self):
        super().__init__({"ok": True, "changed": False,
                          "message": "Clicked [3] x: a download started: r.pdf (the page did not change)."})
        self.last_download = None
        self.last_nav_download = None
        self.saved = []

    def click(self, target="", role=None, ref=None):
        self.last_download = object()
        return self.click_result

    def save_download(self, folder, timeout=None):
        self.saved.append(folder)
        self.last_download = None
        return os.path.join(folder, "r (1).pdf")


def test_only_clicks_and_navigations_that_start_a_download_save_it():
    b = _DlBrowser()
    api = _list_api(b)
    api._downloads_dir = lambda: "DL"
    assert Api._act_browser_click(api, "3") is True
    assert b.saved == ["DL"] and api.last_result_text.endswith(
        "Saved it to the Downloads folder as r (1).pdf.")
    assert b.lists == 0
    b.last_nav_download = "r.pdf"
    b.last_download = object()
    assert Api._act_browser_navigate(api, "h/r.pdf") is True
    assert api.last_result_text.startswith("browser_navigate: that address is a file download (r.pdf)")
    assert len(b.saved) == 2
    # Any other action never saves one.
    b.last_download = object()
    Api._act_browser_scroll(api, "down")
    assert len(b.saved) == 2


def test_a_failed_or_slow_download_save_is_said_plainly():
    b = _DlBrowser()
    api = _list_api(b)
    api._downloads_dir = lambda: "DL"

    def slow(folder, timeout=None):
        raise RuntimeError("Browser action timed out after 30s (page may be stuck loading)")
    b.save_download = slow
    Api._act_browser_click(api, "3")
    assert api.last_result_text.endswith(
        "It is still downloading; it will be saved to the Downloads folder when it finishes.")
    b.save_download = lambda folder, timeout=None: (_ for _ in ()).throw(OSError("disk full"))
    Api._act_browser_click(api, "3")
    assert "It could not be saved to the Downloads folder: disk full." in api.last_result_text


def test_a_pasted_list_line_is_read_as_its_number():
    b = _ListBrowser()
    assert Api._act_browser_click(_list_api(b), '[12] link "lofi hip hop radio"') is True
    assert b.calls == [("", None, 12)]
    b = _ListBrowser()
    Api._act_browser_click(_list_api(b), '12 Monkeys "film"')
    assert b.calls == [('12 Monkeys "film"', None, None)]


def test_live_a_page_changing_click_returns_soon_after_the_new_page_can_be_listed(live):
    import time as _time
    api = _live_api(live)
    Api._act_browser_navigate(api, live.base + "/links.html")
    num = _num(api.last_result_text, "link", "Alpha page")
    t0 = _time.monotonic()
    assert Api._act_browser_click(api, str(num)) is True
    assert _time.monotonic() - t0 < 2.0
    assert "the page changed to 127.0.0.1" in api.last_result_text and '(title "Alpha")' in (
        api.last_result_text)
    assert "still loading" not in api.last_result_text
    assert '"Back to links"' in api.last_result_text


def test_live_navigation_waits_for_script_rendered_results_but_not_forever(live):
    import time as _time
    api = _live_api(live)
    t0 = _time.monotonic()
    assert Api._act_browser_navigate(api, live.base + "/late.html") is True
    assert _time.monotonic() - t0 < bs.READY_CAP_S + 1.5
    assert '"Late result"' in api.last_result_text and "still loading" not in api.last_result_text
    t0 = _time.monotonic()
    assert Api._act_browser_navigate(api, live.base + "/busy.html") is True
    assert _time.monotonic() - t0 < bs.READY_CAP_S + 1.5
    assert '"Busy link"' in api.last_result_text
    assert "still loading" not in api.last_result_text      # parsed and listable: usable


def test_live_a_download_by_click_and_by_navigate_is_saved_numbered(live, tmp_path):
    api = _live_api(live, downloads=tmp_path)
    (tmp_path / "report.pdf").write_bytes(b"mine")
    Api._act_browser_navigate(api, live.base + "/download.html")
    num = _num(api.last_result_text, "link", "Get the report")
    assert Api._act_browser_click(api, str(num)) is True
    assert "a download started: report.pdf" in api.last_result_text
    assert api.last_result_text.endswith("Saved it to the Downloads folder as report (1).pdf.")
    assert Api._act_browser_navigate(api, live.base + "/file.pdf") is True
    assert "is a file download (report.pdf)" in api.last_result_text
    assert api.last_result_text.endswith("Saved it to the Downloads folder as report (2).pdf.")
    assert (tmp_path / "report.pdf").read_bytes() == b"mine"
    assert (tmp_path / "report (2).pdf").read_bytes() == b"%PDF-1.4 fixture"


def test_live_fill_by_number_or_name_and_a_missing_field_fails_fast(live):
    import time as _time
    api = _live_api(live)
    Api._act_browser_navigate(api, live.base + "/form.html")
    text = api.last_result_text
    q = _num(text, "textbox", "Search the site")
    assert Api._act_browser_fill(api, f"{q}|lofi") is True
    assert api.last_result_text.startswith(f'Filled [{q}] textbox "Search the site" (not submitted')
    assert Api._act_browser_fill(api, "Your email|a@b.c") is True
    assert live._call(lambda: live._page.input_value("input[placeholder='Your email']")) == "a@b.c"
    t0 = _time.monotonic()
    assert Api._act_browser_fill(api, "Password|x") is False
    assert _time.monotonic() - t0 < 2.0
    assert "no form field matches 'Password'. Fields on the page: [" in api.last_result_text
    assert '"Search the site"' in api.last_result_text
    link = _num(text, "link", "Help")
    assert Api._act_browser_fill(api, f"{link}|x") is False
    assert f'[{link}] link "Help" is not a form field.' in api.last_result_text


# ============================================================
# BROWSER LIST SPEED AND EMPTY PAGES
# ============================================================

# A very large page (shop-like: hidden menus, product cards, shadow roots,
# frames) lists within its budget with the same items; an empty list gets one
# short wait and one more read, then says what is likely (blocked, still
# loading, iframe, text only, blank). Local fixtures only.

def _big_dom(cards=400, menu=1500, shadow=20, frames=4, footer=200):
    h = ["<title>Big shop</title><style>.card{cursor:pointer;display:inline-block;width:220px;"
         "margin:4px;vertical-align:top}.menu{display:none}</style><header><nav>"
         "<input type='search' placeholder='Search the shop'><button>Go</button>"]
    h += [f"<a href='/c{i}'>Category {i}</a> " for i in range(30)]
    h.append("<div class='menu'>" + "".join(f"<a href='/m{i}'>Menu item {i}</a>"
                                           for i in range(menu)) + "</div></nav></header><main>")
    for i in range(cards):
        badge = "<span>Sponsored</span>" if i % 9 == 0 else ""
        h.append(f"<div class='card' onclick=''><div>{badge}<a href='/p{i}'><img alt='Product {i} "
                 f"photo' width='200' height='120'></a></div><h2><a href='/p{i}'><span>Product "
                 f"number {i}</span></a></h2><div><span>${i}.99</span></div><div><span "
                 f"style='cursor:pointer'>See options</span></div><button>Add to cart {i}</button>"
                 "<div><div><span>Ships soon</span></div></div></div>")
    h += [f"<x-row data-n='{i}'></x-row>" for i in range(shadow)]
    h += [f"<iframe src='/bigframe.html?{i}' width='600' height='300'></iframe>" for i in range(frames)]
    h.append("</main><footer>" + "".join(f"<a href='/f{i}'>Footer link {i}</a> " for i in range(footer))
             + "</footer><script>customElements.define('x-row',class extends HTMLElement{"
             "constructor(){super();const n=this.dataset.n;this.attachShadow({mode:'open'})."
             "innerHTML=Array.from({length:10},(_,j)=>`<button>Shadow ${n}-${j}</button>`)"
             ".join('');}})</script>")
    return "".join(h)


FIXTURE_PAGES.update({
    "/bigdom.html": _big_dom(),
    "/bigframe.html": "<title>F</title>" + "".join(f"<a href='/x{j}'>Frame link {j}</a> "
                                                   for j in range(200)),
    # Content added by a timer 2.5 s after load (no request to wait for).
    "/lateslow.html": "<title>Late timer</title><main id='m'><p>Loading results</p></main><script>"
                      "setTimeout(()=>{for(let i=0;i<3;i++){const a=document.createElement('a');"
                      "a.href='/a.html';a.textContent='Late link '+i;document.getElementById('m')"
                      ".append(a);}},2500)</script>",
    "/onlyframe.html": "<title>Framed</title><iframe src='{other}/framed_text.html' style='position:"
                       "fixed;inset:0;width:100%;height:100%;border:0'></iframe>",
    "/framed_text.html": "<title>Inner</title><p>Only text lives in this frame, nothing to click.</p>",
    "/challenge.html": "<title>Just a moment...</title><p>Checking your browser before accessing "
                       "the site. This takes a few seconds.</p>",
    "/blank.html": "<title>Empty</title><body></body>",
    "/textonly.html": "<title>Essay</title><main><p>" + "A long essay paragraph. " * 40
                      + "</p><a href='/a.html'>One link</a></main>",
})


def test_elements_results_are_json_strings_or_dicts():
    assert bs.decode_elements_read('{"items": [1]}') == {"items": [1]}
    assert bs.decode_elements_read({"items": []}) == {"items": []}
    assert bs.decode_elements_read("not json") == {} and bs.decode_elements_read(None) == {}


def test_the_list_timing_line_is_counts_and_milliseconds_only():
    main = {"tm": {"walk": 2, "overlays": 3, "scan": 30, "prune": 31, "ads": 33, "items": 50},
            "_ms": 60, "nodes": 9000, "cands": 750, "items": [{"n": "Secret name"}] * 3}
    line = bs._list_timing_line([(None, main, (0, 0), False), (None, {"_ms": 20}, (0, 0), False)],
                                0.0, 0.1, 0.105, 0.11)
    assert line.startswith("[Browser] list timing: read 100 ms (main 60: walk 2, overlays 1, "
                           "scan 27, prune 1, ads 2, items 17; 1 frame(s) 20)")
    assert "nodes 9000, candidates 750, kept 3" in line and "Secret" not in line


def test_counted_only_items_join_the_below_and_above_counts():
    e = {"r": "link", "n": "A", "v": 1, "l": "main", "num": 1, "order": (0, 0)}
    text, n = bs.format_element_list(([], [e], [], [], []), extra=(5, 2))
    assert n == 1 and "5 more below the fold" in text and "2 above (scrolled past)" in text
    assert "below the fold" not in bs.format_element_list(([], [e], [], [], []))[0]


def test_a_sparse_list_is_empty_or_one_item_with_nothing_off_screen():
    one = [{"v": 1}]
    assert bs.list_is_sparse(([], [], [], [], []))
    assert bs.list_is_sparse(([], one, [], [], []))
    assert not bs.list_is_sparse(([], one * 2, [], [], []))
    assert not bs.list_is_sparse(([], [], [], one, []))
    assert not bs.list_is_sparse(([], [], [], [], []), extra=(3, 0))


@pytest.mark.parametrize("url, diag, kw, want", [
    ("https://x/", {"title": "Just a moment...", "text": "Checking your browser before you go"}, {},
     "blocked"),
    ("https://x/", {"text": "Verify you are human"}, {"loading": True}, "blocked"),
    ("https://x/", {"rs": "loading", "text": ""}, {}, "loading"),
    ("https://x/", {"rs": "complete", "text": ""}, {"pending": 1}, "loading"),
    ("https://x/", {"rs": "complete", "text": "", "frames": [{"share": 0.9, "src": "https://y/f"}]},
     {}, "iframe"),
    ("https://x/", {"rs": "complete", "text": ""}, {"unreadable": 1}, "iframe"),
    ("https://x/", {"rs": "complete", "text": "word " * 100, "tl": 500}, {}, "text"),
    ("https://x/", {"rs": "complete", "text": "word " * 20, "tl": 100}, {"listed": 1}, "small"),
    ("https://x/", {"rs": "complete", "text": ""}, {}, "blank"),
])
def test_an_empty_list_names_its_likely_cause(url, diag, kw, want):
    assert bs.empty_list_kind(url, diag, **kw)[0] == want


def test_the_empty_list_message_has_the_mark_title_excerpt_and_next_step():
    msg = bs.empty_list_message("iframe", {"title": "Framed page", "text": "Inner   text " * 40},
                                "https://y.example/frame")
    lines = msg.splitlines()
    assert lines[0].startswith(f"{config.BROWSER_EMPTY_LIST_MARK} (iframe): ")
    assert "https://y.example/frame" in lines[0]
    assert lines[1] == 'Page title: "Framed page".'
    assert lines[2].startswith('Text on the page: "Inner text Inner text') and len(lines[2]) < 240
    assert lines[3].startswith("Next step: browser_navigate the frame's address")
    blank = bs.empty_list_message("blank", {"title": "", "text": ""})
    assert "Text on the page" not in blank and "Next step:" in blank


def test_interstitial_bot_checks_count_as_block_pages():
    for text in ("Checking if the site connection is secure", "Performing security verification",
                 "Please enable JavaScript and cookies to continue", "Press & Hold to confirm"):
        assert config.blocked_page_reason("https://x/", "", text), text
    assert config.blocked_page_reason("https://x/", "Recipes", "A normal page about bread.") is None


def test_live_a_large_page_lists_within_budget_and_counts_the_rest(live, capsys):
    import time as _time
    live.navigate(live.base + "/bigdom.html")
    live.elements_text()                                       # warm-up
    t0 = _time.perf_counter()
    text = live.elements_text()
    took = _time.perf_counter() - t0
    assert took < 1.5, took                                    # ~0.2-0.5 s here; was 0.45-0.8 s
    assert '"Product number 0"' in text and "more below the fold" in text
    log = capsys.readouterr().out
    assert "[Browser] list timing: read " in log and "candidates" in log
    timing = [ln for ln in log.splitlines() if "list timing" in ln]
    assert timing and not any("Product" in ln or "Category" in ln for ln in timing)


def test_live_the_bottom_of_a_large_page_is_listed(live):
    live.navigate(live.base + "/bigdom.html")
    live.scroll("bottom")
    text = live.elements_text()
    # Deep in the DOM, past the scan cap, but on screen: kept and numbered
    # (the 60 shown lines go to the frames' links in Main content first).
    footer = [e for e in live._entries if e["n"] == "Footer link 199"]
    assert footer and footer[0]["v"] and footer[0]["num"]
    assert "above (scrolled past)" in text


def test_live_a_late_rendered_page_is_listed_after_one_wait(live, capsys):
    live.navigate(live.base + "/lateslow.html")
    text = live.elements_text()
    assert '"Late link 0"' in text and config.BROWSER_EMPTY_LIST_MARK not in text
    assert "element list retried after" in capsys.readouterr().out


@pytest.mark.parametrize("path, kind", [("/onlyframe.html", "iframe"), ("/challenge.html", "blocked"),
                                        ("/blank.html", "blank"), ("/textonly.html", "text")])
def test_live_an_empty_list_says_what_is_likely(live, monkeypatch, capsys, path, kind):
    monkeypatch.setattr(bs, "EMPTY_WAIT_S", 0.8)
    live.navigate(live.base + path)
    text = live.elements_text()
    assert f"{config.BROWSER_EMPTY_LIST_MARK} ({kind}):" in text and "Next step:" in text
    assert "Page title:" in text
    if kind == "iframe":
        assert "framed_text.html" in text
    assert f"element list empty after a retry ({kind})" in capsys.readouterr().out


def test_live_a_still_loading_empty_page_says_so(live, monkeypatch):
    monkeypatch.setattr(bs, "NAV_TIMEOUT_MS", 3000)
    monkeypatch.setattr(bs, "EMPTY_WAIT_S", 0.5)
    live.navigate(live.base + "/slowtext.html")
    text = live.elements_text()
    assert f"{config.BROWSER_EMPTY_LIST_MARK} (loading):" in text
    assert "Plain text that arrived" in text                   # the excerpt


def test_live_a_small_page_with_one_link_is_not_called_empty(live):
    live.navigate(live.base + "/a.html")
    text = live.elements_text()
    assert '"Back to links"' in text and config.BROWSER_EMPTY_LIST_MARK not in text


def test_live_a_page_changing_click_lists_the_new_page_once(live):
    live.navigate(live.base + "/links.html")
    num = _num(live.elements_text(), "link", "Alpha page")
    api = _live_api(live)
    calls = []
    real = live._collect
    live._collect = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        assert Api._act_browser_click(api, str(num)) is True
    finally:
        del live._collect
    assert "the page changed" in api.last_result_text and '"Back to links"' in api.last_result_text
    assert calls == [1]


# -- Page text size: the main content first, with how much is left -----------

FIXTURE_PAGES["/article.html"] = (
    "<title>Article</title><header><nav>" + "".join(f"<a href='/n{i}'>Menu {i}</a> "
                                                    for i in range(80))
    + "</nav></header><main><h1>The story</h1><p>" + "Story sentence about rivers. " * 200
    + "</p></main><footer>Footer words</footer>")


def test_live_main_text_skips_menus_and_reports_the_full_length(live):
    live.navigate(live.base + "/article.html")
    got = live.main_text()
    assert got["src"] == "main" and got["text"].startswith("The story")
    assert "Menu 3" not in got["text"] and got["total"] > 5000 and got["body"] > got["total"]
    live.navigate(live.base + "/b.html")
    assert live.main_text()["src"] == "body"                  # no <main>: the whole page


def test_live_the_navigate_excerpt_is_the_main_content_with_what_is_left(live):
    api = _live_api(live)
    assert Api._act_browser_navigate(api, live.base + "/article.html") is True
    text = api.last_result_text
    excerpt = text.split("if you need more): ", 1)[1]
    assert excerpt.startswith("The story Story sentence") and "Menu 3 " not in excerpt[:900]
    assert "more characters; use browser_get_text to read more]" in excerpt


def test_the_step_context_prefers_the_main_content(monkeypatch):
    from core import live_state
    from core.agent import AgentLoop
    loop = AgentLoop(SimpleNamespace(live=SimpleNamespace(snapshot={"open_apps": []})), None)
    loop.api.browser = SimpleNamespace(running=True, main_text=lambda timeout=2.0: {
        "title": "Article", "src": "main", "text": "Story sentence. " * 200, "total": 9000,
        "body": 12000})
    monkeypatch.setattr(live_state, "foreground_for_reading", lambda: (77, "Article", False))
    monkeypatch.setattr(live_state, "playwright_window", lambda: 77)
    monkeypatch.delenv("MISTAI_UIA_FIRST", raising=False)
    loop._phase_task = "summarise the article"
    ctx, why = loop._browser_page_context()
    assert why == "" and "PAGE TEXT (the browser page's main content, DOM text): Story" in ctx
    sent = ctx.split("DOM text): ", 1)[1]
    left = int(re.search(r"\[cut, (\d+) more characters; use browser_get_text to read more\]",
                         sent).group(1))
    assert 9000 - 1500 - 2 <= left <= 9000 - 1500           # the page's whole main text counts
