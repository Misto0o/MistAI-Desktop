"""browser_search / browser_navigate: block and CAPTCHA pages (stubbed browser)."""

from types import SimpleNamespace

import pytest

from core import config
from core.api import Api
from core.config import SEARCH_ROUTES, blocked_page_reason

RESULTS = "Marblewood lighthouse opening hours 9am to 5pm daily " * 5
GOOGLE_SORRY = ("https://www.google.com/sorry/index?continue=x", "https://www.google.com/",
                "Our systems have detected unusual traffic from your computer network. "
                "Quillfeather ref 12345")
CF_BLOCK = ("https://html.duckduckgo.com/html/?q=x", "Just a moment...",
            "Please verify you are human by completing the CAPTCHA below. Ray ID abc")
ARTICLE = ("https://example.org/blog/captchas", "How CAPTCHA works",
           "A CAPTCHA is a challenge that tries to tell people and bots apart. " * 60)


class Browser:
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


def _api(pages):
    api = Api.__new__(Api)
    api.browser = Browser(pages)
    api.spoken = []
    api.speak = lambda text: api.spoken.append(text)
    api.last_result_text = ""
    return api


def _content_free(result):
    for word in ("Quillfeather", "12345", "Ray ID", "Just a moment", "Our systems"):
        assert word not in result


# -- the detector -------------------------------------------------------------------


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
    ("https://www.google.com/search?q=sorry", "sorry - Google Search", RESULTS),
    ("https://a.example/", "Weather", "Sunny, 18 degrees"),
    ("", None, None),
])
def test_normal_pages_are_not_blocked(url, title, text):
    assert blocked_page_reason(url, title, text) is None


def test_the_reason_never_quotes_the_page():
    reasons = {blocked_page_reason(*p) for p in (GOOGLE_SORRY, CF_BLOCK)}
    assert reasons == {"the site redirected to a /sorry/ block page",
                       "the page shows a CAPTCHA"}     # the first rule that matches wins


def test_routes_google_first_then_one_other_engine():
    assert [n for n, _ in SEARCH_ROUTES][0] == "Google" and len(SEARCH_ROUTES) == 2
    assert all(u.startswith("https://") and u.endswith("q=") for _, u in SEARCH_ROUTES)


# -- browser_search -------------------------------------------------------------------


def test_google_blocked_then_the_second_route_works(capsys):
    api = _api([GOOGLE_SORRY, ("https://html.duckduckgo.com/html/?q=x", "results", RESULTS)])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert api.browser.visited == [SEARCH_ROUTES[0][1] + "marblewood%20hours",
                                   SEARCH_ROUTES[1][1] + "marblewood%20hours"]
    result = api.last_result_text
    assert result.startswith("Searched DuckDuckGo for 'marblewood hours'. "
                             "(Google showed a block page.) RESULTS PAGE TEXT (already read")
    _content_free(result)
    log = capsys.readouterr().out
    assert "[Browser] browser_search: Google showed a block page (the site redirected" in log
    assert "marblewood" not in log and "Quillfeather" not in log


def test_both_routes_blocked_is_a_failure_with_no_page_content():
    api = _api([GOOGLE_SORRY, CF_BLOCK])
    assert Api._act_browser_search(api, "marblewood hours") is False
    assert len(api.browser.visited) == 2                       # each route once
    assert api.last_result_text == (
        "BLOCKED: Google: the site redirected to a /sorry/ block page; DuckDuckGo: the page "
        "shows a CAPTCHA. Every search route showed a block/CAPTCHA page; do "
        "not try to solve it. Use browser_navigate to a specific site, or tell the user.")
    assert "already read" not in api.last_result_text
    _content_free(api.last_result_text)
    assert api.spoken == []


def test_a_normal_results_page_is_untouched():
    api = _api([("https://www.google.com/search?q=x", "x - Google Search", RESULTS)])
    assert Api._act_browser_search(api, "marblewood hours") is True
    assert len(api.browser.visited) == 1
    excerpt = " ".join(RESULTS.split())
    assert api.last_result_text == (
        "Searched Google for 'marblewood hours'. RESULTS PAGE TEXT (already read -- do NOT "
        "search again; answer from this, or browser_click a result): " + excerpt)
    assert api.spoken == ["Searching for marblewood hours"]


def test_an_article_about_captchas_is_a_normal_result():
    api = _api([ARTICLE])
    assert Api._act_browser_search(api, "how do captchas work") is True
    assert len(api.browser.visited) == 1 and "already read" in api.last_result_text


def test_a_navigation_error_is_reported_as_before():
    api = _api([])
    api.browser.navigate = lambda url: (_ for _ in ()).throw(RuntimeError("net down"))
    assert Api._act_browser_search(api, "q") is False
    assert api.last_result_text == "browser_search failed: net down"


# -- browser_navigate -------------------------------------------------------------------


def test_navigate_marks_a_block_page_and_suggests_browser_search():
    api = _api([CF_BLOCK])
    assert Api._act_browser_navigate(api, "example.org/page") is False
    assert api.last_result_text == (
        "BLOCKED: the page shows a CAPTCHA. This is a block/CAPTCHA page, not "
        "the content; do not try to solve it. Use browser_search for the information instead.")
    _content_free(api.last_result_text)
    assert api.spoken == []


def test_navigate_to_a_normal_page_is_untouched():
    api = _api([("https://example.org/", "Example", "Hello world")])
    assert Api._act_browser_navigate(api, "example.org") is True
    assert api.last_result_text == (
        "Navigated to https://example.org/ (Example). PAGE TEXT (already read -- do NOT "
        "navigate again; use browser_get_text with a selector only if you need more): "
        "Hello world")
    assert api.spoken == ["Navigating to https://example.org/"]


def test_the_page_limit_is_a_config_constant():
    assert config.BLOCK_PAGE_MAX_CHARS == 1500
