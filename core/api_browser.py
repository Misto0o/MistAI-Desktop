"""
core/api_browser.py -- MistAI's Playwright browser actions (browser_*)

Methods of core.api.Api, moved here verbatim (Api inherits BrowserActionsMixin).
"""

from .config import blocked_page_reason, search_routes, search_url


class BrowserActionsMixin:
    def _act_browser_import_cookies(self, param):
        if not param:
            self.last_result_text = "browser_import_cookies needs domain(s), e.g. 'youtube.com' or 'youtube.com|google.com'"
            return False
        try:
            counts = self.browser.import_cookies(param)
            self.last_result_text = f"Imported cookies: {counts}. Navigate/reload the page to see the logged-in state."
            return True
        except Exception as e:
            self.last_result_text = f"browser_import_cookies failed: {e}"
            return False

    def _act_browser_use_daily_profile(self, param):
        try:
            source, copied = self.browser.use_daily_profile_copy()
            self.last_result_text = f"Cloned logins from {source} ({', '.join(copied)}). Next browser action will use them."
            return True
        except Exception as e:
            self.last_result_text = f"browser_use_daily_profile failed: {e}"
            return False   

    def _act_browser_open(self, param):
            try:
                self.browser.ensure_started()
                if self.browser._start_error:
                    self.last_result_text = self.browser._start_error
                    return False
                if param:
                    url, title = self.browser.navigate(param)
                    self.last_result_text = f"Opened {url} ({title})"
                else:
                    self.last_result_text = "Browser ready"
                return True
            except Exception as e:
                self.last_result_text = f"browser_open failed: {e}"
                return False

    def _page_text(self):
        try:
            return " ".join((self.browser.get_text("") or "").split())
        except Exception:
            return ""

    def _page_excerpt(self, limit=900, text=None):
        """First chunk of the current page's real DOM text, for handing back
        to the model in the same observation as a navigate/search. Without
        this the model had to *know* to call browser_get_text as a second
        step -- and a small model instead re-ran the search five times."""
        text = self._page_text() if text is None else text
        return text[:limit] + ("..." if len(text) > limit else "")

    def _act_browser_navigate(self, param):
        if not param:
            self.last_result_text = "browser_navigate needs a URL"
            return False
        try:
            url, title = self.browser.navigate(param)
            text = self._page_text()
            reason = blocked_page_reason(url, title, text)
            if reason:
                print(f"[Browser] browser_navigate: block page ({reason})")
                self.last_result_text = (
                    f"BLOCKED: {reason}. This is a block/CAPTCHA page, not the content; "
                    f"do not try to solve it. Use browser_search for the information instead.")
                return False
            excerpt = self._page_excerpt(text=text)
            self.last_result_text = f"Navigated to {url} ({title})."
            if excerpt:
                self.last_result_text += (
                    f" PAGE TEXT (already read -- do NOT navigate again; use "
                    f"browser_get_text with a selector only if you need more): "
                    f"{excerpt}"
                )
            self.speak(f"Navigating to {url}")
            return True
        except Exception as e:
            self.last_result_text = f"browser_navigate failed: {e}"
            return False

    def _act_browser_search(self, param):
        if not param:
            return False
        blocked = []  # (route name, fixed reason) of each route that was blocked or empty
        try:
            for name, base in search_routes():
                url, title = self.browser.navigate(search_url(base, param))
                text = self._page_text()
                reason = blocked_page_reason(url, title, text) or (
                    "the results page was empty" if not text.strip() else None)
                if reason:
                    print(f"[Browser] browser_search: {name} showed a block page ({reason})")
                    blocked.append((name, reason))
                    continue
                excerpt = self._page_excerpt(text=text)
                self.last_result_text = f"Searched {name} for '{param}'."
                if blocked:
                    self.last_result_text += (
                        f" ({', '.join(n for n, _ in blocked)} showed a block page.)")
                if excerpt:
                    self.last_result_text += (
                        f" RESULTS PAGE TEXT (already read -- do NOT search again; "
                        f"answer from this, or browser_click a result): {excerpt}"
                    )
                self.speak(f"Searching for {param}")
                return True
        except Exception as e:
            self.last_result_text = f"browser_search failed: {e}"
            return False
        self.last_result_text = (
            "BLOCKED: the search was blocked ("
            + "; ".join(f"{n}: {r}" for n, r in blocked)
            + "). Do not try to solve it or search another way (no browser app, no "
            "address bar); tell the user the web search was blocked.")
        return False

    def _act_browser_close(self):
        try:
            self.browser.close()
            self.last_result_text = "Browser closed"
            self.speak("Browser closed")
            return True
        except Exception as e:
            self.last_result_text = f"browser_close failed: {e}"
            return False

    def _act_browser_click(self, param):
        if not param:
            self.last_result_text = "browser_click needs the visible text to click"
            return False
        if "|" in param:
            text, role = param.split("|", 1)
        else:
            text, role = param, None
        try:
            self.browser.click(text.strip(), role=(role.strip() if role else None))
            self.last_result_text = f"Clicked '{text.strip()}' in the browser (real DOM match)"
            return True
        except Exception as e:
            self.last_result_text = f"browser_click could not find/click '{text.strip()}': {e}"
            return False

    def _act_browser_fill(self, param):
        if "|" not in (param or ""):
            self.last_result_text = "browser_fill needs format: field label|text to type"
            return False
        field, text = param.split("|", 1)
        try:
            self.browser.fill(field.strip(), text)
            self.last_result_text = f"Filled '{field.strip()}'"
            return True
        except Exception as e:
            self.last_result_text = f"browser_fill could not find field '{field.strip()}': {e}"
            return False

    def _act_browser_press_key(self, param):
        if not param:
            return False
        try:
            self.browser.press_key(param.strip())
            self.last_result_text = f"Pressed '{param.strip()}' in the browser"
            return True
        except Exception as e:
            self.last_result_text = f"browser_press_key failed: {e}"
            return False
