"""Bringing windows to the front: the shared _focus_hwnd helper, honest
switch_app / window_activate results, and title matching that survives a
cut-off title quoted back with '-' for its em dash."""

import os
from types import SimpleNamespace

import pytest

import core.api as api_mod
import core.enhanced_utils as eu
from core import verify
from core.api import Api
from core.config import normalize_window_title
from test_uwp_and_input_target import PROCS, _fake_desktop

ME = os.getpid()
FIREFOX_TITLE = "MistAI multi-laptop setup and workflow — Mozilla Firefox"
QUOTED = "MistAI multi-laptop setup and workflow -"   # 40 chars, as in the real log


@pytest.fixture
def desk(monkeypatch):
    """A Firefox window (10), MistAI's own browser window (11, its process a
    child of this one), MistAI's UI (12, this process) and Notepad (13)."""
    monkeypatch.setitem(PROCS, ME, "python.exe")
    monkeypatch.setitem(PROCS, 9, "firefox.exe")
    keys = []
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: keys.append(k))
    monkeypatch.setattr(api_mod.subprocess, "run", lambda *a, **k: None)  # AppActivate

    def make(foreground=12, **kw):
        windows = {
            10: (3, "MozillaWindowClass", FIREFOX_TITLE, []),
            11: (9, "MozillaWindowClass", "about:blank — Nightly", []),
            12: (ME, "WindowsForms10", "mistai v2.0", []),
            13: (4, "Notepad", "Untitled - Notepad", []),
        }
        state = _fake_desktop(monkeypatch, windows, foreground=foreground,
                              parents={9: [500, ME]}, **kw)
        state["keys"] = keys
        api = Api.__new__(Api)
        api.last_result_text = ""
        return api, state

    return make


def _calls(state, name):
    return [c for c in state["calls"] if c[0] == name]


# -- _focus_hwnd --------------------------------------------------------------

def test_focus_succeeds_first_try_without_attach_or_alt(desk):
    api, state = desk()
    assert api._focus_hwnd(13) == (True, "")
    assert _calls(state, "SetForegroundWindow") == [("SetForegroundWindow", 13)]
    assert not _calls(state, "AttachThreadInput") and state["keys"] == []
    assert not _calls(state, "ShowWindow")      # not minimized: never restored


def test_minimized_window_is_restored_first(desk):
    api, state = desk(iconic={13})
    assert api._focus_hwnd(13)[0] is True
    assert state["calls"][0] == ("ShowWindow", 13, 9)


def test_refused_focus_tries_every_tier_and_names_the_window_in_front(desk):
    api, state = desk(refuse_focus=True)
    ok, reason = api._focus_hwnd(10)
    assert ok is False
    assert len(_calls(state, "SetForegroundWindow")) == 3      # plain, attached, after ALT
    assert _calls(state, "AttachThreadInput") == [
        ("AttachThreadInput", 1, 1012, True), ("AttachThreadInput", 1, 1012, False)]
    assert state["keys"] == ["alt"]
    assert reason == ("Windows refused focus; the window in front belongs to python.exe "
                      "(MistAI's own window, not minimized)")


def test_detach_always_runs_even_when_bring_to_top_fails(desk):
    api, state = desk(refuse_focus=True, bring_to_top_raises=True, iconic={12})
    ok, reason = api._focus_hwnd(10)
    assert ok is False and "minimized)" in reason and "not minimized" not in reason
    assert _calls(state, "AttachThreadInput")[-1] == ("AttachThreadInput", 1, 1012, False)


# -- switch_to_app ------------------------------------------------------------

def test_switch_reports_the_match_count_and_mistais_own_browser(desk, capsys):
    api, _ = desk()
    assert api.switch_to_app("firefox") is True
    assert api.last_result_text == (
        "Switched to 'firefox' (window 'MistAI multi-laptop setup and workflow —') "
        "[1 of 2 matching windows; another matching window is MistAI's own browser]")


def test_switch_says_found_but_refused_not_no_window(desk, capsys):
    api, _ = desk(refuse_focus=True)
    assert api.switch_to_app("firefox") is False
    text = api.last_result_text
    assert text.startswith("'firefox' has a window ('MistAI multi-laptop")
    assert "could not be brought to the front: Windows refused focus" in text
    assert "MistAI's own window" in text
    out = capsys.readouterr().out
    assert "exists but refused focus" in out and "No window found" not in out


def test_switch_with_no_window_says_so(desk, capsys):
    api, _ = desk()
    assert api.switch_to_app("calculator") is False
    assert api.last_result_text == "No window found for 'calculator'"
    assert "No window found for process 'calculator'" in capsys.readouterr().out


def test_switch_flags_when_the_chosen_window_is_mistais_own_browser(desk, monkeypatch):
    api, _ = desk()
    monkeypatch.setattr(api, "_find_hwnds_by_process_name", lambda _a: [11])
    assert api.switch_to_app("firefox") is True
    assert "this window is MistAI's own browser" in api.last_result_text


# -- window_activate ----------------------------------------------------------

@pytest.fixture
def gw_windows(monkeypatch):
    """pygetwindow's real matching rule (case-insensitive substring) over the
    desk's titles."""
    def make(titles):
        wins = [SimpleNamespace(title=t, _hWnd=h) for h, t in titles.items()]
        monkeypatch.setattr(eu, "WINDOW_MGMT_AVAILABLE", True)
        monkeypatch.setattr(eu, "gw", SimpleNamespace(
            getWindowsWithTitle=lambda q: [w for w in wins if q.upper() in w.title.upper()],
            getAllWindows=lambda: wins,
        ), raising=False)
    return make


def _activate_api(api):
    api.enhanced_available = True
    api.window_mgr = eu.WindowManager
    api.spoken = []
    api.speak = api.spoken.append
    return api


def test_window_activate_finds_a_cut_title_with_a_hyphen_for_the_em_dash(desk, gw_windows):
    api, state = desk()
    gw_windows({10: FIREFOX_TITLE, 12: "mistai v2.0"})
    assert Api._exec_sync(_activate_api(api), "window_activate", QUOTED) is True
    assert state["fg"] == 10
    assert api.last_result_text.startswith("Activate Window: MistAI multi-laptop")


def test_window_activate_found_but_refused_is_not_reported_as_not_found(desk, gw_windows):
    api, _ = desk(refuse_focus=True)
    gw_windows({10: FIREFOX_TITLE})
    assert Api._exec_sync(_activate_api(api), "window_activate", QUOTED) is False
    assert api.last_result_text.startswith("Found window 'MistAI multi-laptop")
    assert "could not bring it to the front: Windows refused focus" in api.last_result_text
    assert "Could not find window" not in api.last_result_text


def test_window_activate_not_found(desk, gw_windows):
    api, state = desk()
    gw_windows({10: FIREFOX_TITLE})
    assert Api._exec_sync(_activate_api(api), "window_activate", "Spotify Premium") is False
    assert api.last_result_text == "Could not find window: Spotify Premium"
    assert not _calls(state, "SetForegroundWindow")


# -- title normalization ------------------------------------------------------

@pytest.mark.parametrize("raw, norm", [
    (QUOTED, "MistAI multi-laptop setup and workflow"),
    ("Page — ", "Page"),
    ("a – b  —  c", "a - b - c"),
    ("Inbox |", "Inbox"),
    ("", ""),
    (" - ", ""),
])
def test_normalize_window_title(raw, norm):
    assert normalize_window_title(raw) == norm


def test_focus_verifier_accepts_the_quoted_title_with_a_hyphen():
    api = SimpleNamespace(live=SimpleNamespace(snapshot={"foreground_window": FIREFOX_TITLE}))
    ok, _ = verify._window_focused(api, "window_activate", QUOTED, None)
    assert ok is True
    api.live.snapshot["foreground_window"] = "Untitled - Notepad"
    assert verify._window_focused(api, "window_activate", QUOTED, None)[0] is False
