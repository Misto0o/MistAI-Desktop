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

    def failing_neural(text, gen):
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
    assert neural_calls == ["one"]
    assert fallback_calls == ["one", "two", "three"]

    now[0] += 2  # cooldown over: the network is tried again
    engine._speak_one(0, "four")
    assert neural_calls == ["one", "four"]


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

def _gated_run(monkeypatch, script, gating, on_exec=None):
    """Run the loop over scripted actions; returns the number of screen reads."""
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