"""
core/uia_nav.py — JARVIS-style UIA window/element navigation.

Expanded version: exposes the full per-element property set (AutomationId,
control patterns, toggle/selection/expand states, etc.) and maintains a
lightweight background cache of the foreground window's element tree so
repeated lookups (e.g. retry-after-failure) don't require a full re-walk
every time.
"""

import threading
import time

import pyautogui

try:
    from pywinauto import Desktop
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False


class UIAElementInfo:
    """Structured snapshot of a single UIA element's properties. Built as a
    plain dict-like wrapper so it's easy to serialize into the agent's
    observation text without touching pywinauto internals downstream."""

    def __init__(self, element):
        self.element = element
        self.name = ""
        self.automation_id = ""
        self.control_type = "Unknown"
        self.rect = None
        self.enabled = None
        self.visible = None
        self.focused = None
        self.offscreen = None
        self.toggle_state = None
        self.selected = None
        self.expanded = None
        self.value = ""
        self.patterns = []
        self._extract()

    def _extract(self):
        el = self.element
        try:
            self.name = el.window_text().strip()
        except Exception:
            pass
        try:
            info = el.element_info
            self.automation_id = getattr(info, "automation_id", "") or ""
            self.control_type = getattr(info, "control_type", "Unknown") or "Unknown"
        except Exception:
            pass
        try:
            r = el.rectangle()
            self.rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        try:
            self.enabled = el.is_enabled()
        except Exception:
            pass
        try:
            self.visible = el.is_visible()
        except Exception:
            pass
        try:
            self.offscreen = not self.visible if self.visible is not None else None
        except Exception:
            pass
        try:
            self.value = el.get_value() if hasattr(el, "get_value") else ""
        except Exception:
            pass
        try:
            # Not all elements expose toggle/selection/expand patterns --
            # probe defensively rather than assuming support.
            if hasattr(el, "get_toggle_state"):
                self.toggle_state = el.get_toggle_state()
        except Exception:
            pass
        try:
            if hasattr(el, "is_selected"):
                self.selected = el.is_selected()
        except Exception:
            pass
        try:
            if hasattr(el, "is_expanded"):
                self.expanded = el.is_expanded()
        except Exception:
            pass
        try:
            self.patterns = list(el.iface_support.keys()) if hasattr(el, "iface_support") else []
        except Exception:
            pass

    @property
    def center(self):
        if not self.rect:
            return None
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def to_dict(self):
        return {
            "name": self.name,
            "automation_id": self.automation_id,
            "control_type": self.control_type,
            "rect": self.rect,
            "enabled": self.enabled,
            "visible": self.visible,
            "offscreen": self.offscreen,
            "toggle_state": self.toggle_state,
            "selected": self.selected,
            "expanded": self.expanded,
            "value": self.value[:80] if self.value else "",
            "patterns": self.patterns,
        }

    def __repr__(self):
        return f"UIAElementInfo(name={self.name!r}, type={self.control_type}, id={self.automation_id!r})"


class EnhancedUIANavigator:
    """JARVIS-style UIA navigator for intelligent multi-step navigation.

    Maintains a short-lived cache of the foreground window's element tree
    so repeated lookups within the same task (e.g. verify-then-click,
    or retry-after-failure) don't force a full UIA tree walk each time.
    Cache is keyed by window title and invalidated after CACHE_TTL seconds
    or on explicit refresh.
    """

    CACHE_TTL = 3.0

    def __init__(self):
        self.available = PYWINAUTO_AVAILABLE
        if not self.available:
            print("[UIANav] pywinauto not available, UIA navigation disabled")
        self._cache_lock = threading.Lock()
        self._cache = {}  # window_title -> (timestamp, [UIAElementInfo, ...])

    # -- Cache management -----------------------------------------------

    def _get_cached_elements(self, window_title, depth=8, force_refresh=False):
        """Return cached element list for a window if fresh, else rebuild."""
        with self._cache_lock:
            entry = self._cache.get(window_title)
            if not force_refresh and entry and (time.time() - entry[0]) < self.CACHE_TTL:
                return entry[1]

        elements = self._walk_elements(window_title, depth=depth)
        with self._cache_lock:
            self._cache[window_title] = (time.time(), elements)
        return elements

    def invalidate_cache(self, window_title=None):
        """Force the next lookup to re-walk the tree. Call this after any
        action that likely changed the UI (click, type, navigate)."""
        with self._cache_lock:
            if window_title:
                self._cache.pop(window_title, None)
            else:
                self._cache.clear()

    def _walk_elements(self, window_title, depth=8):
        if not self.available:
            return []
        try:
            desktop = Desktop(backend="uia")
            target = None
            for w in desktop.windows():
                try:
                    if window_title.lower() in w.window_text().lower():
                        target = w
                        break
                except Exception:
                    continue
            if target is None:
                return []

            try:
                descendants = target.descendants(depth=depth)
            except Exception:
                return []

            results = []
            for el in descendants:
                try:
                    results.append(UIAElementInfo(el))
                except Exception:
                    pass
            return results
        except Exception as e:
            print(f"[UIANav] Walk error: {e}")
            return []

    # -- Public lookup API -------------------------------------------------

    def get_all_windows(self):
        if not self.available:
            return []
        try:
            desktop = Desktop(backend="uia")
            windows = []
            for w in desktop.windows()[:50]:
                try:
                    windows.append({
                        "title": w.window_text(),
                        "class": getattr(w.element_info, "class_name", "Unknown"),
                        "control_type": getattr(w.element_info, "control_type", "Unknown"),
                        "rect": w.rectangle(),
                    })
                except Exception:
                    pass
            return windows
        except Exception:
            return []

    def find_element_by_name(self, window_title, element_name, partial=True):
        """Returns the raw pywinauto element (for backward compat with
        click_element). Uses the cache."""
        elements = self._get_cached_elements(window_title)
        en_lower = element_name.lower()
        for info in elements:
            nl = info.name.lower()
            if (partial and en_lower in nl) or (not partial and en_lower == nl):
                return info.element
        return None
    
    def find_text_input(self, window_title):
        """Find the most likely text INPUT field in a window -- prefers
        control type (Edit) over name matching, since input boxes are often
        unlabeled or share generic names with unrelated content (e.g. a
        message bubble's caption also containing the word 'message')."""
        elements = self._get_cached_elements(window_title)
        candidates = [
            info for info in elements
            if info.control_type in ("Edit")
            and info.enabled is not False
        ]
        if not candidates:
            return None
        # Prefer the one closest to the bottom of the screen -- chat input
        # boxes are conventionally docked at the bottom of the window.
        candidates.sort(key=lambda info: info.rect[3] if info.rect else 0, reverse=True)
        return candidates[0].element
    

    def find_element_info(self, window_title, element_name, partial=True):
        """Returns the full UIAElementInfo (name, AutomationId, control type,
        states, patterns, etc.) instead of just the raw element."""
        elements = self._get_cached_elements(window_title)
        en_lower = element_name.lower()
        for info in elements:
            nl = info.name.lower()
            if (partial and en_lower in nl) or (not partial and en_lower == nl):
                return info
        return None

    def find_by_automation_id(self, window_title, automation_id):
        """Find an element by its AutomationId -- more stable than name
        matching when an app exposes it, since AutomationIds rarely change
        across app versions/languages the way visible text does."""
        elements = self._get_cached_elements(window_title)
        for info in elements:
            if info.automation_id == automation_id:
                return info
        return None

    def find_elements_by_type(self, window_title, control_type, limit=10):
        """Returns full UIAElementInfo objects (not just coords) for every
        element of a given control type."""
        elements = self._get_cached_elements(window_title)
        ct_lower = control_type.lower()
        results = [info for info in elements if ct_lower in info.control_type.lower()]
        return results[:limit]

    def describe_window(self, window_title, max_elements=25):
        """Human-readable summary of a window's interactive elements --
        useful for feeding into the agent's observation text without a
        full raw dump."""
        elements = self._get_cached_elements(window_title)
        interesting_types = (
            "Button", "Edit", "Text", "Link", "MenuItem", "ListItem",
            "TabItem", "CheckBox", "RadioButton", "ComboBox",
        )
        lines = []
        for info in elements:
            if info.control_type in interesting_types and info.name:
                extra = []
                if info.toggle_state is not None:
                    extra.append(f"toggle={info.toggle_state}")
                if info.selected:
                    extra.append("selected")
                if info.enabled is False:
                    extra.append("disabled")
                suffix = f" ({', '.join(extra)})" if extra else ""
                lines.append(f"[{info.control_type}] {info.name}{suffix}")
            if len(lines) >= max_elements:
                break
        return lines

    # -- Interaction --------------------------------------------------------

    def click_element(self, element):
        if element is None:
            return False
        try:
            rect = element.rectangle()
            x = (rect.left + rect.right) // 2
            y = (rect.top + rect.bottom) // 2
            pyautogui.moveTo(x, y, duration=0.15)
            time.sleep(0.05)
            pyautogui.click()
            return True
        except Exception:
            return False

    def get_element_text(self, element):
        if element is None:
            return ""
        try:
            return element.window_text()
        except Exception:
            return ""