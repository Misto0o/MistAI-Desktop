"""MISTAI_VISION_Y_NUDGE: the pixels _execute_click adds to y for a vision
click with y < 80. Default 30 = unchanged behaviour; read at call time."""

from types import SimpleNamespace

import pytest

import core.click_handler as ch
from core.config import vision_y_nudge


@pytest.fixture
def handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda *a, **k: None)
    monkeypatch.delenv("MISTAI_VISION_Y_NUDGE", raising=False)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=True),
                                screen_buffer=None)
    markers = []
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: markers.append((x, y)))
    h.clicks, h.markers = clicks, markers
    return h


def _click(h, method, x, y):
    h.last_method = method
    assert h._execute_click((x, y)) is True
    return h.clicks[-1]


def test_default_is_todays_behaviour(handler, capsys):
    assert _click(handler, "Vision", 700, 56) == (700, 86)
    assert handler.markers[-1] == (700, 86)                 # marker and click agree
    assert _click(handler, "Vision", 700, 80) == (700, 80)  # 80 and below: untouched
    assert _click(handler, "OCR", 700, 56) == (700, 56)     # other methods: never
    out = capsys.readouterr().out
    assert "[Click] Vision y-nudge applied: +30px (y 56 -> 86)" in out
    assert "[Click] Vision y-nudge not applied (y=80, nudge=30px)" in out
    assert out.count("y-nudge") == 2                         # OCR prints nothing


@pytest.mark.parametrize("value, expected_y", [("0", 56), ("12", 68), ("30", 86)])
def test_the_flag_sets_the_pixels(handler, monkeypatch, value, expected_y):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", value)
    assert _click(handler, "Vision", 700, 56) == (700, expected_y)


def test_zero_reports_not_applied(handler, monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", "0")
    _click(handler, "Vision", 700, 56)
    assert "[Click] Vision y-nudge not applied (y=56, nudge=0px)" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", "abc", "-5", "1.5", " 30 "])
def test_invalid_values_mean_30(monkeypatch, value):
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", value)
    assert vision_y_nudge() == 30


def test_read_at_call_time(handler, monkeypatch):
    assert _click(handler, "Vision", 700, 56) == (700, 86)
    monkeypatch.setenv("MISTAI_VISION_Y_NUDGE", "0")
    assert _click(handler, "Vision", 700, 56) == (700, 56)


def test_the_nudge_is_capped_near_the_bottom_as_before(handler, monkeypatch):
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 90))
    assert _click(handler, "Vision", 700, 60) == (700, 70)   # min(y + 30, sh - 20)
