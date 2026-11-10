"""Clicks must land inside the target app's window when one is known and in front.

Merged by area (pure moves, no test changed) from: test_click_guard.py, test_own_window_mask.py, test_unconfirmed_click.py."""

from types import SimpleNamespace
import pytest
import core.click_handler as ch
from core.api import Api
import inspect
import os
import sys
import types
from core.executor import Executor, ExecutionResult
from test_input_repeat_and_done_check import _result, _run



# ==========================================================================
# moved from tests/test_click_guard.py
# ==========================================================================

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
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")   # the query and title print only with the flag
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



# ==========================================================================
# moved from tests/test_own_window_mask.py
# The OCR mask finds MistAI's own window by handle (Api._own_window_handle) or
# this process's PID, never by title text.
# ==========================================================================

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



# ==========================================================================
# moved from tests/test_unconfirmed_click.py
# AgentLoop: a vision-resolved click no screen read confirmed gets one done-time
# reminder that names the action only.
# ==========================================================================

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
