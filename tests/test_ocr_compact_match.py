"""OCR compact matcher: a spaced/misheard name against one OCR word or a run of
2-4 adjacent words on one Tesseract line (core/click_handler.py)."""

import difflib
from types import SimpleNamespace

import pytest

import core.click_handler as ch

FRAME = SimpleNamespace(shape=(1080, 1920, 3))


def _data(words):
    """image_to_data-style dict. ``words`` = (text, left, top, width, height,
    line, word_num[, conf])."""
    d = {k: [] for k in ("text", "conf", "left", "top", "width", "height",
                         "block_num", "par_num", "line_num", "word_num")}
    for w in words:
        text, left, top, width, height, line, num = w[:7]
        d["text"].append(text)
        d["conf"].append(w[7] if len(w) > 7 else 90)
        d["left"].append(left)
        d["top"].append(top)
        d["width"].append(width)
        d["height"].append(height)
        d["block_num"].append(1)
        d["par_num"].append(1)
        d["line_num"].append(line)
        d["word_num"].append(num)
    return d


@pytest.fixture
def handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda x, y, duration=0: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "click", lambda *a, **k: None)
    monkeypatch.setattr(ch, "OCR_AVAILABLE", True)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=False),
                                screen_buffer=None)
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    monkeypatch.setattr(h, "_get_frame_for_ocr", lambda: FRAME)
    monkeypatch.setattr(h, "_ocr_button_regions", lambda f, q: (None, "", 0, 0))
    monkeypatch.setattr(h, "_prepare_frames", lambda f: [(FRAME, 1.0)])
    monkeypatch.setattr(h, "_try_uia_click", lambda q: None)
    h.clicks = clicks
    return h


def _ocr_sees(monkeypatch, words):
    data = _data(words)
    monkeypatch.setattr(ch, "pytesseract", SimpleNamespace(
        image_to_data=lambda *a, **k: data, Output=SimpleNamespace(DICT="dict")),
        raising=False)


SPAN = [("Inbox", 100, 200, 60, 20, 1, 1),
        ("So", 300, 400, 30, 20, 2, 1), ("Can", 336, 400, 40, 20, 2, 2),
        ("Xan", 382, 400, 40, 20, 2, 3), ("online", 430, 400, 60, 20, 2, 4)]


def test_a_spaced_query_matches_a_multi_word_span_and_clicks_its_centre(handler, monkeypatch):
    _ocr_sees(monkeypatch, SPAN)
    assert handler.click_on_text("so can zan") is True
    # So(300) .. Xan(382+40=422): centre x 361; y 400..420: centre 410.
    assert handler.clicks == [(361, 410)] and handler.last_method == "OCR"
    assert handler.last_found_text == "So Can Xan"


def test_a_spaced_query_matches_one_joined_word(handler, monkeypatch):
    _ocr_sees(monkeypatch, [("SoCanXan", 500, 600, 100, 20, 1, 1)])
    assert handler.click_on_text("so can zan") is True
    assert handler.clicks == [(550, 610)]


@pytest.mark.parametrize("words", [
    [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 2, 1),
     ("Xan", 382, 400, 40, 20, 2, 2)],                       # "So" is on another line
    [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 1, 2),
     ("Xan", 382, 400, 40, 20, 1, 4)],                       # word 3 (low conf) between
])
def test_words_on_different_lines_or_not_adjacent_never_form_a_span(handler, monkeypatch,
                                                                    words):
    _ocr_sees(monkeypatch, words)
    assert handler.click_on_text("so can zan") is False
    assert handler.clicks == []


def test_sam_and_pam_scores():
    # The single-word path's 65 accepts this pair (66); the compact path never
    # sees a 3-letter query, and its 80 would refuse a 66 anyway.
    assert int(difflib.SequenceMatcher(None, "pam", "sam").ratio() * 100) == 66
    assert ch._compact_score("pam", "sam") == 0
    assert ch._match_score("pam", "sam") == 66          # unchanged single-word path
    assert ch._compact_score("quillfeathr", "quillfeather") >= ch.COMPACT_MATCH_SCORE
    assert ch._compact_score("socanxan", "socanzan") == 87


def test_short_queries_do_not_use_the_compact_path(handler, monkeypatch):
    # "ab cd" compacts to 4 chars: below COMPACT_MIN_LEN even though "Ab"+"Cd"
    # joins to an exact match.
    _ocr_sees(monkeypatch, [("Ab", 300, 400, 30, 20, 1, 1), ("Cd", 336, 400, 30, 20, 1, 2)])
    assert handler._compact_match(ch.pytesseract.image_to_data(), "ab cd") is None
    assert handler.click_on_text("ab cd") is False


@pytest.mark.parametrize("query, cand, length_ok", [
    ("abcdefgh", "abcdefghij", False),        # 8 chars: max(1, 1) -> 2 longer is out
    ("abcdefghijklmn", "abcdefghijklxyzw", True),   # 14 chars: 15% -> 2 allowed
])
def test_length_guard(query, cand, length_ok):
    assert (ch._compact_score(cand, query) > 0) is length_ok


def test_the_suffix_rules_hold_on_the_compact_path():
    assert ch._compact_score("subscribers", "subscribe") == 0
    assert ch._compact_score("subscriber", "subscribe") == 0      # 1 char longer, derived
    assert ch._compact_score("subscription", "subscribe") == 0
    assert ch._compact_score("subscribed", "subscribe") == 90     # toggle state
    # The single-word scorer is unchanged.
    assert ch._match_score("subscribers", "subscribe") == 0
    assert ch._match_score("subscribed", "subscribe") == 90


def test_a_compact_match_never_overrides_a_single_word_match(handler, monkeypatch):
    # "Subscribed" wins at 90 on the single-word path; the compact span
    # "Sub scribe" (100) elsewhere is never consulted.
    _ocr_sees(monkeypatch, [("Subscribed", 700, 100, 80, 20, 1, 1),
                            ("Sub", 300, 500, 30, 20, 2, 1), ("scribe", 334, 500, 50, 20, 2, 2)])
    match, word, score, _ = handler._ocr_pass(FRAME, "subscribe", "cfg")
    assert (match, word, score) == ((740, 110), "Subscribed", 90)


def test_the_target_window_guard_applies_to_the_span_centre(handler, monkeypatch):
    _ocr_sees(monkeypatch, SPAN)
    handler.target_rect_fn = lambda: ((0, 0, 340, 1080), "Chat")   # centre x 361 is outside
    assert handler.click_on_text("so can zan") is False
    assert handler.clicks == []
    assert "at (361, 410) (found by OCR) was NOT made" in handler.last_refusal
    handler.target_rect_fn = lambda: ((0, 0, 400, 1080), "Chat")
    assert handler.click_on_text("so can zan") is True
    assert handler.clicks == [(361, 410)]


def test_default_logs_carry_counts_scores_and_coordinates_only(handler, monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _ocr_sees(monkeypatch, SPAN)
    handler.click_on_text("so can zan")
    log = capsys.readouterr().out
    assert "[OCR] Compact match: 3 word(s), score=87 @ (361, 410)" in log
    for word in ("so can zan", "Xan", "Can", "online", "Inbox"):
        assert word not in log


def test_the_words_print_only_under_the_flag(handler, monkeypatch, capsys):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    _ocr_sees(monkeypatch, SPAN)
    handler.click_on_text("so can zan")
    assert "as 'So Can Xan' @ (361, 410)" in capsys.readouterr().out


def test_a_compact_score_between_65_and_80_is_not_a_match(handler, monkeypatch):
    # Two letters off an 8-letter name scores 75: enough for the single-word
    # path's 65, not for the compact path's 80.
    assert ch._compact_score("socanxam", "socanzan") == 75
    _ocr_sees(monkeypatch, [("So", 300, 400, 30, 20, 1, 1), ("Can", 336, 400, 40, 20, 1, 2),
                            ("Xam", 382, 400, 40, 20, 1, 3)])
    assert handler.click_on_text("so can zan") is False
