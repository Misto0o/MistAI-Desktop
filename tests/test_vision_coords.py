"""Vision coordinates on screens wider than 1920 px: the frame is scaled to
1920 wide before it is sent, so the model is told the size of the image it
gets and its answer is scaled back by (screen / image) before any click. At
1920x1080 the prompt and the coordinates are unchanged."""

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import core.click_handler as ch
import core.vision as vision_mod
from core.vision import CohereVision


def _vision(monkeypatch, screen, answer):
    v = CohereVision.__new__(CohereVision)
    v._client = object()                       # ready, never called
    v.prompts = []

    def call(prompt, frame=None):
        v.prompts.append(prompt)
        return answer

    v._call = call
    monkeypatch.setattr(vision_mod.pyautogui, "size", lambda: screen)
    return v


WIDE = (2560, 1440)


def test_sent_size():
    v = CohereVision.__new__(CohereVision)
    assert v._sent_size(Image.new("RGB", WIDE)) == (1920, 1080)
    assert v._sent_size(Image.new("RGB", (3840, 1600))) == (1920, 800)
    assert v._sent_size(Image.new("RGB", (1920, 1080))) == (1920, 1080)
    assert v._sent_size(Image.new("RGB", (1366, 768))) == (1366, 768)
    assert v._sent_size(object()) is None


def test_the_image_sent_is_the_size_the_prompt_states():
    v = CohereVision.__new__(CohereVision)
    png = base64.b64decode(v._b64(Image.new("RGB", WIDE)))
    assert Image.open(io.BytesIO(png)).size == (1920, 1080)


def test_wide_screen_answer_is_scaled_back(monkeypatch):
    v = _vision(monkeypatch, WIDE, "960,540")
    assert v.find_element_coords("Send button", Image.new("RGB", WIDE)) == (1280, 720)
    assert "Image size: 1920x1080 pixels" in v.prompts[0]
    assert "2560" not in v.prompts[0]


def test_wide_screen_corners(monkeypatch):
    frame = Image.new("RGB", WIDE)
    for answer, expected in (("0,0", (0, 0)), ("1920,1080", (2560, 1440)),
                             ("1919, 1079", (2559, 1439))):
        v = _vision(monkeypatch, WIDE, answer)
        assert v.find_element_coords("x", frame) == expected


@pytest.mark.parametrize("answer", ["1921,500", "500,1081", "-1,5", "2000,1200", "NOT_FOUND", "abc"])
def test_out_of_range_answers_are_rejected(monkeypatch, answer):
    # In image pixels: 2000,1200 would be on a 2560x1440 screen, but not in the image sent.
    v = _vision(monkeypatch, WIDE, answer)
    assert v.find_element_coords("x", Image.new("RGB", WIDE)) is None


def test_nth_element_is_scaled_back(monkeypatch):
    v = _vision(monkeypatch, WIDE, "TITLE: A video\nCOORDS: 960,540")
    assert v.find_nth_element_coords("video", 2, Image.new("RGB", WIDE)) == ("A video", (1280, 720))
    assert "Image size: 1920x1080 pixels" in v.prompts[0]
    v = _vision(monkeypatch, WIDE, "TITLE: A video\nCOORDS: 1930,540")
    assert v.find_nth_element_coords("video", 2, Image.new("RGB", WIDE)) == ("A video", None)


def test_1920x1080_is_unchanged(monkeypatch):
    frame = Image.new("RGB", (1920, 1080))
    v = _vision(monkeypatch, (1920, 1080), "960,540")
    assert v.find_element_coords("Send button", frame) == (960, 540)
    assert v.prompts[0] == (
        "Find 'Send button' on this screen.\n"
        "Screen resolution: 1920x1080.\n"
        "Reply with ONLY: x,y (pixel coordinates of the center of that element).\n"
        "If not found, reply: NOT_FOUND\n"
        "No explanation. Just coordinates or NOT_FOUND.")
    v = _vision(monkeypatch, (1920, 1080), "1921,5")
    assert v.find_element_coords("x", frame) is None
    v = _vision(monkeypatch, (1920, 1080), "TITLE: t\nCOORDS: 100,200")
    assert v.find_nth_element_coords("video", 1, frame) == ("t", (100, 200))
    assert "Screen resolution: 1920x1080.\n" in v.prompts[0]


def test_a_frame_without_a_size_uses_the_screen(monkeypatch):
    v = _vision(monkeypatch, WIDE, "960,540")
    assert v.find_element_coords("x", object()) == (960, 540)


def test_the_click_handler_clicks_the_scaled_point(monkeypatch):
    frame = Image.new("RGB", WIDE)
    v = _vision(monkeypatch, WIDE, "960,540")
    monkeypatch.setattr(ch.pyautogui, "size", lambda: WIDE)
    h = ch.ImprovedClickHandler(SimpleNamespace(snapshot={}), v,
                                SimpleNamespace(frame_for_read=lambda _l: frame, frame=frame))
    assert h._try_vision_click("Send button") == (1280, 720)
