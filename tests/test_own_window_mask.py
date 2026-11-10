"""The OCR mask finds MistAI's own window by handle (Api._own_window_handle) or
this process's PID, never by title text."""

import inspect
import os
import sys
import types
from types import SimpleNamespace

import pytest

import core.click_handler as ch
from core.api import Api

OWN, OTHER = 11, 22
RECTS = {OWN: (100, 100, 500, 400), OTHER: (0, 0, 1900, 1050)}
TITLES = {OWN: "MistAI", OTHER: "api.py - MistAI Desktop - Visual Studio Code"}


def _fake(monkeypatch, pids=None, iconic=(), visible=(OWN, OTHER), z_order=(OTHER, OWN)):
    pids = pids or {OWN: os.getpid(), OTHER: 999999}
    win32gui = types.ModuleType("win32gui")
    win32gui.EnumWindows = lambda cb, extra: [cb(h, extra) for h in z_order]
    win32gui.GetWindowText = lambda h: TITLES[h]
    win32gui.IsWindowVisible = lambda h: h in visible
    win32gui.IsIconic = lambda h: h in iconic
    win32gui.GetWindow = lambda h, rel: 0
    win32gui.GetWindowRect = lambda h: RECTS[h]
    win32process = types.ModuleType("win32process")
    win32process.GetWindowThreadProcessId = lambda h: (0, pids[h])
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process", win32process)


def _handler(own=None):
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=False),
                                screen_buffer=None)
    if own is not None:
        h.own_window_fn = own
    return h


def test_a_vs_code_window_titled_after_the_project_is_never_masked(monkeypatch, capsys):
    _fake(monkeypatch, z_order=(OTHER,), visible=(OTHER,))
    assert _handler(own=lambda: None)._get_mistai_window_rect() is None
    assert _handler()._get_mistai_window_rect() is None        # PID fallback: not ours
    assert "Visual Studio Code" not in capsys.readouterr().out


def test_the_own_handle_is_masked_whatever_is_in_front(monkeypatch, capsys):
    _fake(monkeypatch)
    assert _handler(own=lambda: OWN)._get_mistai_window_rect() == RECTS[OWN]
    assert "[OCR] MistAI-window mask rect: (100, 100, 500, 400)" in capsys.readouterr().out


def test_without_a_handle_this_process_s_window_is_used_not_the_title(monkeypatch):
    TITLES_BACKUP = dict(TITLES)
    try:
        TITLES[OWN] = "something unrelated"            # MistAI's window, any title
        _fake(monkeypatch)
        assert _handler()._get_mistai_window_rect() == RECTS[OWN]
    finally:
        TITLES.update(TITLES_BACKUP)


@pytest.mark.parametrize("iconic, visible", [((OWN,), (OWN, OTHER)), ((), (OTHER,))])
def test_a_minimized_or_hidden_own_window_is_not_masked(monkeypatch, iconic, visible):
    _fake(monkeypatch, iconic=iconic, visible=visible)
    assert _handler(own=lambda: OWN)._get_mistai_window_rect() is None


def test_a_failing_handle_lookup_means_no_mask(monkeypatch):
    _fake(monkeypatch)

    def boom():
        raise RuntimeError("no window yet")
    assert _handler(own=boom)._get_mistai_window_rect() is None


def test_the_implausible_rectangle_check_is_kept(capsys):
    h = _handler()
    assert h._sane_mask_rect((0, 0, 1900, 1050), (1080, 1920, 3)) is None    # 97% of the frame
    assert h._sane_mask_rect((100, 100, 500, 400), (1080, 1920, 3)) == (100, 100, 500, 400)
    assert "implausible" in capsys.readouterr().out


def test_api_passes_its_own_handle_lookup_in():
    src = inspect.getsource(Api.__init__)
    assert "self.click_handler.own_window_fn = self._own_window_handle" in src


def test_the_mask_code_never_matches_on_title_text():
    src = inspect.getsource(ch.ImprovedClickHandler._get_mistai_window_rect)
    src += inspect.getsource(ch.ImprovedClickHandler._own_process_window)
    assert "GetWindowText" not in src and '"MistAI" in' not in src
