"""AgentLoop: a vision-resolved click no screen read confirmed gets one done-time
reminder that names the action only."""

import pytest

from core.executor import Executor, ExecutionResult
from test_input_repeat_and_done_check import _result, _run

TARGET = "Zanzibar Quillfeather"     # a contact-like click target; must never leak
DONE = {"action": "done", "speech": "Opened the chat."}
CHECK = "was resolved by vision and never confirmed"


def _click(method, confidence, verified=None, action="click_on_text"):
    r = _result(action, "clicked")
    r.method, r.confidence, r.verified = method, confidence, verified
    return r


def test_a_vision_click_then_done_gets_exactly_one_reminder(monkeypatch, capsys):
    out, _, calls, obs, spoken = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE, DONE,
    ], results={"click_on_text": _click("Vision", 0.45)})
    assert out["success"] and spoken == ["Opened the chat."]
    assert calls == [("click_on_text", TARGET)]
    checks = [o for o in obs if "Before finishing" in o]
    assert len(checks) == 1
    assert ("click_on_text was resolved by vision and never confirmed: read the screen to "
            "confirm it landed on the right item, or say you clicked but couldn't confirm"
            ) in checks[0]
    assert "Zanzibar" not in checks[0] and "Quillfeather" not in checks[0]
    log = capsys.readouterr().out
    assert "[DoneCheck] reminder sent (route notes)" in log
    assert "Zanzibar" not in log and "Quillfeather" not in log


@pytest.mark.parametrize("read", ["read_screen_text", "analyze_screen"])
def test_a_read_after_the_vision_click_means_no_reminder(monkeypatch, read):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET},
        {"action": read, "parameter": "is the chat open?"},
        DONE,
    ], results={"click_on_text": _click("Vision", 0.45),
                read: _result(read, "chat header shown")})
    assert out["success"]
    assert not any("Before finishing" in o for o in obs)
    assert len(obs) == 2          # no extra model call


def test_a_vision_click_after_the_read_still_counts(monkeypatch):
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "read_screen_text", "parameter": "q"},
        {"action": "click_nth", "parameter": "2"},
        DONE, DONE,
    ], results={"click_nth": _click("Vision", 0.45, action="click_nth"),
                "read_screen_text": _result("read_screen_text", "list")})
    assert sum(CHECK in o for o in obs) == 1


@pytest.mark.parametrize("method, confidence, verified", [
    ("OCR", 0.74, None),
    ("UIA", 0.92, None),
    ("Vision", 0.93, True),      # verified: the Executor raises it to 0.93
    ("Vision", 0.45, True),
])
def test_ocr_uia_and_verified_clicks_get_no_reminder(monkeypatch, method, confidence,
                                                     verified):
    out, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE,
    ], results={"click_on_text": _click(method, confidence, verified)})
    assert out["success"] and not any("Before finishing" in o for o in obs)


def test_a_failed_vision_click_is_not_an_unconfirmed_click(monkeypatch):
    r = _click("Vision", 0.11)
    r.success = False
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "click_on_text", "parameter": TARGET}, DONE,
    ], results={"click_on_text": r})
    assert not any(CHECK in o for o in obs)


def test_the_real_executor_scores_a_vision_click_below_the_threshold():
    # The agent's rule keys on the Executor's own numbers; pin them together.
    ex = Executor.__new__(Executor)
    spec = type("S", (), {"base_confidence": 0.7})()
    conf = ex._confidence(spec, "click_on_text", "Vision", True, None)
    assert conf < Executor.CONFIDENCE_THRESHOLD
    assert ex._confidence(spec, "click_on_text", "Vision", True, True) >= \
        Executor.CONFIDENCE_THRESHOLD
    assert ExecutionResult("click_on_text", "", True, conf, method="Vision").verified is None


def test_a_vision_click_after_typing_still_counts_as_submitting_it(monkeypatch):
    # The click both submits the typing (no "never submitted" warning) and
    # stays unconfirmed until the read.
    _, _, _, obs, _ = _run(monkeypatch, [
        {"action": "type_text", "parameter": "hello"},
        {"action": "click_on_text", "parameter": "Send"},
        {"action": "read_screen_text", "parameter": "sent?"},
        DONE,
    ], results={"click_on_text": _click("Vision", 0.45),
                "read_screen_text": _result("read_screen_text", "sent")})
    assert not any("Before finishing" in o for o in obs)
