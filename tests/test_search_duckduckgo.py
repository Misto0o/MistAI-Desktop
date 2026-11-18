"""browser_search uses one engine, DuckDuckGo (MISTAI_SEARCH_ENGINE=google switches
back): its normal page, then its HTML endpoint on a block or empty page; a
blocked search is a clear failure that never escalates to a browser app, and the
agent prompt says to search with browser_search only. Stubbed browser."""

import inspect
import re
from types import SimpleNamespace

import pytest

from core import config
from core.agent import AgentLoop
from core.api import Api
from core.executor import Executor
from core.tools import SPECS

RESULTS = "Marblewood lighthouse opening hours 9am to 5pm daily " * 5
DDG_ANOMALY_URL = ("https://duckduckgo.com/anomaly?q=x", "DuckDuckGo", "Please try again.")
DDG_BOTS = ("https://duckduckgo.com/?q=x", "DuckDuckGo",
            "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge.")
DDG_HTML_OK = ("https://html.duckduckgo.com/html/?q=x", "x at DuckDuckGo", RESULTS)
EMPTY = ("https://duckduckgo.com/?q=x", "DuckDuckGo", "   ")


@pytest.fixture(autouse=True)
def _default_engine(monkeypatch):
    monkeypatch.delenv("MISTAI_SEARCH_ENGINE", raising=False)


class Browser:
    def __init__(self, pages):
        self.pages, self.visited, self.current = list(pages), [], None

    def navigate(self, url):
        self.visited.append(url)
        self.current = self.pages.pop(0)
        return self.current[0], self.current[1]

    def get_text(self, _selector=""):
        return self.current[2]


def _api(pages):
    api = Api.__new__(Api)
    api.browser = Browser(pages)
    api.spoken = []
    api.speak = lambda text: api.spoken.append(text)
    api.last_result_text = ""
    return api


# -- engine selection and URLs ------------------------------------------------------------

@pytest.mark.parametrize("raw, engine", [
    (None, "duckduckgo"), ("duckduckgo", "duckduckgo"), ("google", "google"),
    (" Google ", "google"), ("bing", "duckduckgo"), ("", "duckduckgo"),
])
def test_the_engine_is_duckduckgo_unless_set_to_google(monkeypatch, raw, engine):
    if raw is not None:
        monkeypatch.setenv("MISTAI_SEARCH_ENGINE", raw)
    assert config.search_engine() == engine


def test_the_default_routes_never_leave_duckduckgo():
    routes = config.search_routes()
    assert [n for n, _ in routes] == ["DuckDuckGo", "DuckDuckGo HTML"]
    assert all(u.startswith("https://") and "duckduckgo.com" in u for _, u in routes)


def test_the_env_switches_back_at_call_time(monkeypatch):
    assert config.search_routes()[0][0] == "DuckDuckGo"
    monkeypatch.setenv("MISTAI_SEARCH_ENGINE", "google")
    assert config.search_routes()[0][0] == "Google"


@pytest.mark.parametrize("query, encoded", [
    ("marblewood hours", "marblewood%20hours"),
    ("a&b=c/d ?#", "a%26b%3Dc%2Fd%20%3F%23"),
    ("café 5+5", "caf%C3%A9%205%2B5"),
])
def test_the_query_is_fully_percent_encoded(query, encoded):
    assert config.search_url("https://duckduckgo.com/?q=", query) == (
        "https://duckduckgo.com/?q=" + encoded)


# -- DuckDuckGo block pages ----------------------------------------------------------------

@pytest.mark.parametrize("page", [DDG_ANOMALY_URL, DDG_BOTS])
def test_duckduckgo_anomaly_pages_are_blocks(page):
    assert config.blocked_page_reason(*page) == (
        "the page reports an anomaly (DuckDuckGo's bot check)")


def test_a_normal_duckduckgo_results_page_is_not_a_block():
    assert config.blocked_page_reason("https://duckduckgo.com/?q=x", "x at DuckDuckGo",
                                      RESULTS) is None


# -- browser_search ------------------------------------------------------------------------

def test_the_normal_page_answers_first(capsys):
    api = _api([("https://duckduckgo.com/?q=x", "x at DuckDuckGo", RESULTS)])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert api.browser.visited == ["https://duckduckgo.com/?q=marblewood%20hours"]
    assert api.last_result_text.startswith("Searched DuckDuckGo for 'marblewood hours'.")


def test_a_blocked_normal_page_falls_back_to_the_html_endpoint(capsys):
    api = _api([DDG_BOTS, DDG_HTML_OK])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert api.browser.visited == ["https://duckduckgo.com/?q=marblewood%20hours",
                                   "https://html.duckduckgo.com/html/?q=marblewood%20hours"]
    assert api.last_result_text.startswith(
        "Searched DuckDuckGo HTML for 'marblewood hours'. (DuckDuckGo showed a block page.)")
    log = capsys.readouterr().out
    assert "browser_search: DuckDuckGo showed a block page (the page reports an anomaly" in log
    assert "marblewood" not in log and "bots use" not in log


def test_an_empty_results_page_counts_as_blocked():
    api = _api([EMPTY, DDG_HTML_OK])
    assert Api._act_browser_search(api, "q") is True
    assert len(api.browser.visited) == 2


def test_a_blocked_search_is_a_clear_failure_with_no_other_route():
    api = _api([DDG_BOTS, EMPTY])
    assert Api._act_browser_search(api, "marblewood hours") is False
    assert all("duckduckgo.com" in u for u in api.browser.visited)
    assert len(api.browser.visited) == 2                     # no third route
    text = api.last_result_text
    assert text.startswith("BLOCKED: the search was blocked (DuckDuckGo: the page reports an "
                           "anomaly (DuckDuckGo's bot check); DuckDuckGo HTML: the results "
                           "page was empty).")
    assert "no browser app, no address bar" in text and "tell the user" in text
    assert "bots use" not in text and api.spoken == []


def test_a_failed_search_never_escalates_to_a_browser_app():
    assert SPECS["browser_search"].fallback_tools == ()
    # The Executor escalates ONLY through a tool's fallback_tools.
    src = inspect.getsource(Executor.run)
    assert "for fb in spec.fallback_tools:" in src
    assert "open_app" not in src


# -- the prompt rule -----------------------------------------------------------------------

def _system():
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: ""),
                          _resolve_path_placeholders=lambda p: p)
    return AgentLoop(api, None)._build_system("none", {"open_apps": []}, "screen", "", [])


def test_the_prompt_says_to_search_with_browser_search_only():
    text = _system()
    rule = next(l for l in text.splitlines() if l.startswith("- BROWSER:"))
    assert "for any web search or lookup use browser_search" in rule
    assert "browser_get_text" in rule
    assert "never open the user's browser app or type into an address bar to search" in rule
    assert "only when the user asks to open or use it" in rule
    assert len(re.findall(r"^- BROWSER", text, re.M)) == 1          # merged, not a new section
    assert "- browser_search <search query>: Web search (DuckDuckGo)" in text
    assert "Google search" not in text
