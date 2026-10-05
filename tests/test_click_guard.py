"""Clicks must land inside the target app's window when one is known and in front."""

from types import SimpleNamespace

import pytest

import core.click_handler as ch
from core.api import Api

CALC = ((180, 300, 600, 560), "Calculator")


@pytest.fixture
def handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda: None)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=True),
                                screen_buffer=None)
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    h.clicks = clicks
    return h


def _finders(h, monkeypatch, ocr=None, uia=None, vision=None):
    monkeypatch.setattr(h, "_try_ocr_click", lambda q: ocr)
    monkeypatch.setattr(h, "_try_uia_click", lambda q: uia)
    monkeypatch.setattr(h, "_try_vision_click", lambda q: vision)


def test_match_outside_the_target_window_is_refused_and_the_next_method_tried(
        handler, monkeypatch, capsys):
    handler.target_rect_fn = lambda: CALC
    # The real run: OCR read the taskbar's "=n" at (544,1042); UIA found the button.
    _finders(handler, monkeypatch, ocr=(544, 1042), uia=(520, 520))
    assert handler.click_on_text("=") is True
    assert handler.clicks == [(520, 520)] and handler.last_method == "UIA"
    out = capsys.readouterr().out
    assert "REFUSED OCR click on '=' at (544, 1042)" in out
    assert "'Calculator' rect (180, 300, 600, 560) +8px" in out


def test_every_match_outside_means_no_click_and_a_reason(handler, monkeypatch):
    handler.target_rect_fn = lambda: CALC
    # The real run: Vision put '*' at (1512,337), far right of Calculator.
    _finders(handler, monkeypatch, vision=(1512, 337))
    assert handler.click_on_text("*") is False
    assert handler.clicks == []
    assert "The click on '*' at (1512, 337) (found by Vision) was NOT made" in handler.last_refusal
    assert "outside the 'Calculator' window (180, 300, 600, 560)" in handler.last_refusal


@pytest.mark.parametrize("point", [(180, 300), (600, 560), (175, 295), (607, 567)])
def test_points_inside_the_rect_plus_margin_are_clicked(handler, monkeypatch, point):
    handler.target_rect_fn = lambda: CALC
    _finders(handler, monkeypatch, ocr=point)
    assert handler.click_on_text("7") is True
    assert handler.clicks == [point]


def test_no_known_target_allows_taskbar_clicks(handler, monkeypatch):
    handler.target_rect_fn = lambda: None
    _finders(handler, monkeypatch, ocr=(900, 1060))
    assert handler.click_on_text("Start") is True
    assert handler.clicks == [(900, 1060)]
    handler.target_rect_fn = None
    assert handler.click_on_text("Start") is True


def test_unavailable_rect_fails_open(handler, monkeypatch, capsys):
    def boom():
        raise OSError("GetWindowRect failed")

    handler.target_rect_fn = boom
    _finders(handler, monkeypatch, ocr=(1512, 337))
    assert handler.click_on_text("x") is True
    assert "rect unavailable" in capsys.readouterr().out


def test_click_nth_is_guarded_too(handler, monkeypatch):
    handler.target_rect_fn = lambda: CALC
    monkeypatch.setattr(handler, "_try_ocr_nth", lambda t, n: (1500, 900))
    monkeypatch.setattr(handler, "_try_uia_nth", lambda t, n: None)
    monkeypatch.setattr(handler, "_try_vision_nth", lambda t, n: None)
    assert handler.click_nth_element("result", 1) is False
    assert handler.clicks == [] and "NOT made" in handler.last_refusal


# -- Api wiring ----------------------------------------------------------------------


def _api(monkeypatch, target, belongs):
    import win32gui

    api = Api.__new__(Api)
    api._last_opened_app = target
    api._window_belongs_to = lambda _h, _t: belongs
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 7)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda _h: CALC[0])
    monkeypatch.setattr(win32gui, "GetWindowText", lambda _h: "Calculator")
    return api


def test_target_rect_only_for_a_known_target_in_front(monkeypatch, capsys):
    assert Api._click_target_rect(_api(monkeypatch, None, True)) is None
    assert Api._click_target_rect(_api(monkeypatch, "calculator", True)) == CALC
    assert Api._click_target_rect(_api(monkeypatch, "calculator", False)) is None
    assert "calculator is not in front; window guard not applied" in capsys.readouterr().out


def test_refusal_reaches_the_result_text(handler, monkeypatch):
    handler.target_rect_fn = lambda: CALC
    _finders(handler, monkeypatch, ocr=(544, 1042))
    monkeypatch.setattr(handler, "_try_vision_click", lambda q: None)
    api = Api.__new__(Api)
    api.click_handler = handler
    api.last_result_text = ""
    assert Api._exec_sync(api, "click_on_text", "=") is False
    assert api.last_result_text.startswith("The click on '=' at (544, 1042) (found by OCR)")


def test_click_coords_is_guarded(handler, monkeypatch):
    handler.target_rect_fn = lambda: CALC
    monkeypatch.setattr("core.api.pyautogui.size", lambda: (1920, 1080))
    moved = []
    monkeypatch.setattr("core.api.pyautogui.moveTo", lambda *a, **k: moved.append(a))
    monkeypatch.setattr("core.api.pyautogui.click", lambda: None)
    monkeypatch.setattr("core.api.time.sleep", lambda _s: None)
    api = Api.__new__(Api)
    api.click_handler = handler
    api.last_result_text = ""
    assert Api._act_click_coords(api, "1512,337") is False
    assert moved == [] and "NOT made" in api.last_result_text
