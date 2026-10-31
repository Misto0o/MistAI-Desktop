"""Logs are content-free unless MISTAI_VERBOSE_LOGS=1 (stage 1b).

By default thinking, heard words, typed text, click queries, OCR/UIA/vision
words, window titles, the done-check reminder and failure details print as
lengths or fixed labels. The flag is read at call time, prints a one-time
banner, and is always off in a built app (sys.frozen); MISTAI_TRACE_READS is
off there too. The scenario harness turns the flag on and says so in each log.
"""

import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest
import speech_recognition as sr

import core.api as api_mod
import core.click_handler as ch
import core.config as config
import core.uia_text as uia_text
from core.agent import AgentLoop
from core.api import Api
from core.executor import Executor
from core.tools import SPECS, ToolSpec

# Invented content, each word distinctive enough to search for.
CONTACT = "Joanne Quillfeather"
TYPED = "my confidential diary entry"
TITLE = "Quillfeather diary - Notepad"
HEARD = "mist tell joanne about the lighthouse pier"
CONTENT_WORDS = ("joanne", "quillfeather", "lighthouse", "pier", "confidential", "diary",
                 "marblewood")


def _no_content(text):
    low = text.lower()
    leaked = [w for w in CONTENT_WORDS if w in low]
    assert leaked == [], f"content in the log: {leaked}"


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    """Flag unset, not frozen, banner already shown (tests that need it reset it)."""
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    monkeypatch.delenv("MISTAI_TRACE_READS", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(config, "_verbose_banner_shown", True)


@pytest.fixture
def verbose(monkeypatch):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")


# -- the flag itself -------------------------------------------------------------------

def test_the_flag_is_read_at_call_time(monkeypatch):
    assert config.verbose_logs() is False
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    assert config.verbose_logs() is True
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "0")
    assert config.verbose_logs() is False


def test_the_banner_prints_once(monkeypatch, capsys, verbose):
    monkeypatch.setattr(config, "_verbose_banner_shown", False)
    assert config.verbose_logs() and config.verbose_logs()
    assert capsys.readouterr().out.count("verbose logs: on, logs contain content") == 1


def test_a_built_app_ignores_the_flag(monkeypatch, capsys, verbose):
    monkeypatch.setattr(config, "_verbose_banner_shown", False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.verbose_logs() is False
    assert config.log_text(TYPED) == f"<{len(TYPED)} chars>"
    assert "verbose logs" not in capsys.readouterr().out


def test_a_built_app_ignores_trace_reads(monkeypatch):
    monkeypatch.setenv("MISTAI_TRACE_READS", "1")
    assert config.trace_reads_enabled() is True
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert config.trace_reads_enabled() is False


def test_log_text_views(verbose):
    assert config.log_text(TYPED, 5) == repr(TYPED[:5])
    assert config.log_text(TYPED) == repr(TYPED)


def test_log_text_default_is_a_length():
    assert config.log_text(TYPED) == f"<{len(TYPED)} chars>"
    assert config.log_text(HEARD, unit="words") == "<7 words>"
    assert config.log_text(None, unit="words") == "<0 words>"


# -- the agent loop -----------------------------------------------------------------

def _result(action, success=True):
    return SimpleNamespace(action=action, parameter="", success=success, detail="",
                           verified=None, escalate_to_user=False,
                           reliability_note=lambda: "", escalated_from="")


def _run(monkeypatch, replies, run=None, answers=(), snapshot=None):
    """One phase through the real AgentLoop with scripted model replies.
    Returns the actions-table rows."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    rows = []
    listen = lambda timeout=10: answers.pop(0) if answers else None   # noqa: E731
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot=snapshot or {"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        memory=SimpleNamespace(add_action=lambda *a: rows.append(a)),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _t, **_k: None,
        tts=SimpleNamespace(speaking=False),
        _listen_once=listen,
        _get_text_answer=lambda _q, timeout=60: listen(),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        _last_opened_app="",
    )
    api.executor = SimpleNamespace(run=(lambda a, p: run(api, a, p)) if run
                                   else (lambda a, p: _result(a)))

    def fake_post(*_a, json=None, **_k):
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Notepad"))
    loop._loop("task", "m", "none", "text", None)
    return rows


THINK = [{"action": "done", "speech": "ok", "thinking": f"I should message {CONTACT} now"}]


def test_think_is_a_word_count(monkeypatch, capsys):
    _run(monkeypatch, THINK)
    log = capsys.readouterr().out
    assert "[Think] <6 words>" in log
    _no_content(log)


def test_think_in_full_under_the_flag(monkeypatch, capsys, verbose):
    _run(monkeypatch, THINK)
    assert f"[Think] 'I should message {CONTACT} now'" in capsys.readouterr().out


def _fg_change(api, action, _param):
    api.live.snapshot = {"open_apps": [], "foreground_hwnd": 2, "foreground_window": TITLE}
    return _result(action)


FG = [{"action": "analyze_screen", "parameter": ""}, {"action": "done", "speech": "ok"}]
FG_SNAP = {"open_apps": [], "foreground_hwnd": 1, "foreground_window": "Calculator"}


def test_a_foreground_change_reason_has_no_title(monkeypatch, capsys):
    _run(monkeypatch, FG, run=_fg_change, snapshot=dict(FG_SNAP))
    log = capsys.readouterr().out
    assert "[Screen] re-read: foreground changed\n" in log
    _no_content(log)


def test_a_foreground_change_reason_names_the_title_under_the_flag(monkeypatch, capsys, verbose):
    _run(monkeypatch, FG, run=_fg_change, snapshot=dict(FG_SNAP))
    assert f"[Screen] re-read: foreground changed to {TITLE!r}" in capsys.readouterr().out


def _fail(_api, action, _param):
    return _result(action, success=False)


REPEAT = [{"action": "type_text", "parameter": TYPED}] * 4 + [{"action": "done", "speech": "ok"}]


def test_a_refusal_logs_the_parameter_as_the_step_log_does(monkeypatch, capsys):
    _run(monkeypatch, REPEAT, run=_fail)
    log = capsys.readouterr().out
    assert f"[Refused] REFUSED: 'type_text(<{len(TYPED)} chars>)'" in log
    _no_content(log)


def test_the_refusal_observation_keeps_the_parameter():
    refuse = f"REFUSED: 'type_text({TYPED})' has already FAILED 3 times in a row"
    assert AgentLoop._refusal_for_log(refuse, "type_text", TYPED) == (
        f"REFUSED: 'type_text(<{len(TYPED)} chars>)' has already FAILED 3 times in a row")
    assert AgentLoop._refusal_for_log(refuse, "open_app", "notepad") == refuse


def _read(api, action, _param):
    api.last_result_text = f"The note says {CONTACT}"
    return _result(action)


EMPTY_DONE = [{"action": "read_screen_text", "parameter": "what does it say"},
              {"action": "done", "speech": ""}, {"action": "done", "speech": ""}]


def test_the_done_check_names_what_fired_not_the_reminder(monkeypatch, capsys):
    _run(monkeypatch, EMPTY_DONE, run=_read)
    log = capsys.readouterr().out
    assert "[DoneCheck] reminder sent (empty answer)" in log
    _no_content(log)


def test_the_done_check_prints_the_reminder_under_the_flag(monkeypatch, capsys, verbose):
    _run(monkeypatch, EMPTY_DONE, run=_read)
    log = capsys.readouterr().out
    assert "[DoneCheck] reminder sent" not in log
    assert "[DoneCheck] " in log


def test_ask_user_stores_no_question(monkeypatch, capsys):
    rows = _run(monkeypatch, [{"action": "ask_user", "parameter": f"Which {CONTACT}?"},
                              {"action": "done", "speech": "ok"}], answers=["the cousin"])
    assert ("ask_user", True) in rows
    _no_content(repr(rows))


def _vision_loop(description):
    api = SimpleNamespace(
        vision=SimpleNamespace(ready=True, understand_screen=lambda f, use_cache=False: description),
        screen=SimpleNamespace(frame_for_read=lambda label: object()),
        live=SimpleNamespace(snapshot={"foreground_window": TITLE}),
    )
    return AgentLoop(api, None)


def test_the_vision_screen_preview_is_a_length(capsys):
    desc = f"Notepad shows a letter to {CONTACT} about the pier"
    _vision_loop(desc)._get_screen_context()
    log = capsys.readouterr().out
    assert f"[Vision] Screen understood: <{len(desc)} chars>" in log
    _no_content(log)


def test_the_vision_screen_preview_under_the_flag(capsys, verbose):
    desc = f"Notepad shows a letter to {CONTACT} about the pier" + "x" * 200
    _vision_loop(desc)._get_screen_context()
    assert f"[Vision] Screen understood: {desc[:100]!r}" in capsys.readouterr().out


# -- [Exec] -------------------------------------------------------------------------

def _exec_api(monkeypatch, handler):
    api = Api.__new__(Api)
    monkeypatch.setattr(api, "_act_type_text", handler, raising=False)
    return api


def test_exec_lines_carry_no_content(monkeypatch, capsys):
    def boom(_p):
        raise ValueError(f"could not type {TYPED}")
    _exec_api(monkeypatch, boom)._exec_sync("type_text", TYPED)
    log = capsys.readouterr().out
    assert f"[Exec] type_text(<{len(TYPED)} chars>)" in log
    assert "[Exec] Error in type_text: ValueError" in log
    _no_content(log)


def test_exec_keeps_named_keys_and_app_names(monkeypatch, capsys):
    api = Api.__new__(Api)
    monkeypatch.setattr(api, "_act_open_app", lambda _p: True, raising=False)
    api._exec_sync("open_app", "notepad")
    assert "[Exec] open_app('notepad')" in capsys.readouterr().out


def test_exec_lines_in_full_under_the_flag(monkeypatch, capsys, verbose):
    def boom(_p):
        raise ValueError(f"could not type {TYPED}")
    long = TYPED * 10
    _exec_api(monkeypatch, boom)._exec_sync("type_text", long)
    log = capsys.readouterr().out
    assert f"[Exec] type_text({long[:80]!r})" in log            # still capped at 80
    assert f"[Exec] Error in type_text: could not type {TYPED}" in log


# -- [Executor] failure reason ----------------------------------------------------------

class _Rec:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _failing_executor(monkeypatch):
    monkeypatch.setattr("core.executor.time.sleep", lambda _s: None)
    monkeypatch.setattr("core.executor.verify.run", lambda *a, **k: (None, ""))
    api = Api.__new__(Api)
    api.last_result_text = ""
    api.tools = SimpleNamespace(get=lambda n: ToolSpec(name=n), settle=lambda n: 0,
                                is_destructive=lambda *a, **k: False)
    api.memory = api.task_memory = api.workspace = api.bus = _Rec()
    api.uia_nav = SimpleNamespace(invalidate_cache=lambda: None)
    api.click_handler = SimpleNamespace(last_method=None)
    api.live = SimpleNamespace(snapshot={"foreground_window": "Notepad"})
    api.screen = SimpleNamespace(mark_action=lambda: None)

    def sync(_a, _p):
        api.last_result_text = f"Error: {CONTACT} not found"
        return False
    api._exec_sync = sync
    return Executor(api)


def test_executor_reason_is_a_length_and_the_harness_regex_matches(monkeypatch, capsys):
    import tools.run_scenarios as rs
    _failing_executor(monkeypatch).run("custom_action", "x")
    log = capsys.readouterr().out
    detail = f"Error: {CONTACT} not found"
    assert f"[Executor] custom_action -> FAIL" in log
    assert f"| reason: <{len(detail)} chars>" in log
    assert rs._REASON_RX.findall(log) == [("custom_action", f"<{len(detail)} chars>")]
    _no_content(log)


def test_executor_reason_in_full_under_the_flag(monkeypatch, capsys, verbose):
    _failing_executor(monkeypatch).run("custom_action", "x")
    assert f"| reason: Error: {CONTACT} not found" in capsys.readouterr().out


# -- [Click] / [OCR] / [UIA] / [Vision] ---------------------------------------------------

CALC = ((180, 300, 600, 560), TITLE)


@pytest.fixture
def handler(monkeypatch):
    clicks = []
    monkeypatch.setattr(ch.pyautogui, "click", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(ch.pyautogui, "moveTo", lambda *a, **k: None)
    monkeypatch.setattr(ch.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    h = ch.ImprovedClickHandler(live_state=None, vision_engine=SimpleNamespace(ready=True),
                                screen_buffer=None)
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    return h


def _click_outside_then_fail(h, monkeypatch):
    h.target_rect_fn = lambda: CALC
    monkeypatch.setattr(h, "_try_ocr_click", lambda q: (544, 1042))
    monkeypatch.setattr(h, "_try_uia_click", lambda q: None)
    monkeypatch.setattr(h, "_try_vision_click", lambda q: None)
    monkeypatch.setattr(h, "_try_ocr_nth", lambda t, n: None)
    monkeypatch.setattr(h, "_try_uia_nth", lambda t, n: None)
    monkeypatch.setattr(h, "_try_vision_nth", lambda t, n: None)
    h.click_on_text(CONTACT)
    h.target_rect_fn = None
    h.click_nth_element(f"message from {CONTACT}", 2)


def test_click_lines_carry_no_content(handler, monkeypatch, capsys):
    _click_outside_then_fail(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[Click] Searching for: <{len(CONTACT)} chars>" in log
    assert (f"[Click] REFUSED OCR click on <{len(CONTACT)} chars> at (544, 1042): outside "
            f"<{len(TITLE)} chars> rect (180, 300, 600, 560) +8px") in log
    assert "[Click] FAILED: every match for the target was outside the target window" in log
    assert "[Click] Looking for #2 <" in log
    assert "[Click] FAILED: Could not find the target" in log
    _no_content(log)


def test_click_lines_in_full_under_the_flag(handler, monkeypatch, capsys, verbose):
    _click_outside_then_fail(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[Click] Searching for: {CONTACT!r}" in log
    assert f"REFUSED OCR click on {CONTACT!r} at (544, 1042): outside {TITLE!r}" in log
    assert f"every match for '{CONTACT}' was outside" in log
    assert f"Could not find #2 message from {CONTACT}" in log


def _ocr_no_match(h, monkeypatch):
    frame = SimpleNamespace(shape=(1000, 1000, 3))
    monkeypatch.setattr(ch, "OCR_AVAILABLE", True)
    monkeypatch.setattr(h, "_get_frame_for_ocr", lambda: frame)
    monkeypatch.setattr(h, "_ocr_button_regions", lambda f, q: (None, "Quillfeather", 30, 2))
    monkeypatch.setattr(h, "_prepare_frames", lambda f: [(frame, 1.0)])
    monkeypatch.setattr(h, "_ocr_pass", lambda g, q, c: (
        None, "diary", 40, [("lighthouse", 10, 500), ("pier", 50, 600)]))
    assert h._try_ocr_click(CONTACT) is None


def test_ocr_no_match_lines_carry_no_content(handler, monkeypatch, capsys):
    _ocr_no_match(handler, monkeypatch)
    log = capsys.readouterr().out
    assert "best <12 chars> @ score 30" in log
    assert f"[OCR] No match for <{len(CONTACT)} chars> above threshold (closest: <5 chars>" in log
    assert "words in the rest of the page" in log               # counts stay
    assert "Words from the actual page content" not in log
    _no_content(log)


def test_ocr_no_match_lines_in_full_under_the_flag(handler, monkeypatch, capsys, verbose):
    _ocr_no_match(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[OCR] No match for {CONTACT!r} above threshold (closest: 'diary'" in log
    assert "[OCR] Words from the actual page content: 'lighthouse'@(10,500)" in log


def _ocr_found(h, monkeypatch):
    monkeypatch.setattr(ch, "OCR_AVAILABLE", True)
    monkeypatch.setattr(h, "_get_frame_for_ocr", lambda: SimpleNamespace(shape=(1000, 1000, 3)))
    monkeypatch.setattr(h, "_ocr_button_regions", lambda f, q: ((40, 50), "Quillfeather", 99, 3))
    assert h._try_ocr_click(CONTACT) == (40, 50)


def test_ocr_found_line_keeps_coordinates_not_words(handler, monkeypatch, capsys):
    _ocr_found(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[OCR] Found <{len(CONTACT)} chars> as <12 chars> @ (40, 50) via" in log
    _no_content(log)


def test_ocr_found_line_under_the_flag(handler, monkeypatch, capsys, verbose):
    _ocr_found(handler, monkeypatch)
    assert f"[OCR] Found {CONTACT!r} as 'Quillfeather' @ (40, 50)" in capsys.readouterr().out


def _vision_found(h, monkeypatch):
    h.screen = SimpleNamespace(frame_for_read=lambda label: object())
    h.vision = SimpleNamespace(
        ready=True,
        find_element_coords=lambda q, f: (30, 40),
        find_nth_element_coords=lambda t, n, f: (f"From {CONTACT}", (60, 70)))
    assert h._try_vision_click(CONTACT) == (30, 40)
    assert h._try_vision_nth("message", 1) == (60, 70)


def test_vision_found_lines_keep_coordinates_not_words(handler, monkeypatch, capsys):
    _vision_found(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[Vision] Found <{len(CONTACT)} chars> @ (30, 40)" in log
    assert "[Vision] Found #1 <7 chars> <" in log and "@ (60, 70)" in log
    _no_content(log)


def test_vision_found_lines_under_the_flag(handler, monkeypatch, capsys, verbose):
    _vision_found(handler, monkeypatch)
    log = capsys.readouterr().out
    assert f"[Vision] Found {CONTACT!r} @ (30, 40)" in log
    assert f"[Vision] Found #1 'message' 'From {CONTACT}' @ (60, 70)" in log


@pytest.fixture
def uia_window(monkeypatch):
    import win32gui
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 42)
    monkeypatch.setattr(win32gui, "GetWindowText", lambda h: TITLE)
    monkeypatch.setattr(uia_text, "_process_name", lambda h: "notepad.exe")
    monkeypatch.setattr(uia_text, "_walk_text", lambda *_a: ("password: hunter2hunter2", False))


def test_uia_withheld_line_has_no_title(uia_window, capsys):
    assert uia_text.foreground_uia_text() == ("", TITLE)
    log = capsys.readouterr().out
    assert "[UIA] withheld the text of the window:" in log
    _no_content(log)


def test_uia_withheld_line_names_the_title_under_the_flag(uia_window, capsys, verbose):
    uia_text.foreground_uia_text()
    assert f"[UIA] withheld the text of '{TITLE}':" in capsys.readouterr().out


# -- [Wake] ----------------------------------------------------------------------------

class _Mic:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wake(monkeypatch, transcripts):
    """The real wake loop with a fake recognizer; the conversation is stubbed."""
    api = Api.__new__(Api)
    api._wake_shutdown = threading.Event()
    api._wake_enabled = threading.Event()
    api._wake_enabled.set()
    api._mic_lock = threading.Lock()
    api._agent_lock = threading.Lock()
    api._conversation_lock = threading.Lock()
    api.microphone = _Mic()
    api.tts = SimpleNamespace(speaking=False, wait_until_done=lambda timeout=None: None)
    api._wait_for_tts = lambda timeout=None: True
    queue = list(transcripts)

    class FakeRec:
        def __init__(self):
            self.energy_threshold = 300
            self.dynamic_energy_threshold = True
            self.pause_threshold = 0.8
            self.non_speaking_duration = 0.5

        def adjust_for_ambient_noise(self, src, duration=1):
            pass

        def listen(self, src, timeout=None, phrase_time_limit=None):
            return sr.AudioData(b"\x00\x00" * 1600, 16000, 2)

        def recognize_google(self, audio):
            if not queue:
                api._wake_shutdown.set()
                raise sr.UnknownValueError()
            return queue.pop(0)

    monkeypatch.setattr(api_mod.sr, "Recognizer", FakeRec)
    monkeypatch.setattr(api_mod, "wake_clips_enabled", lambda: False)
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: None)
    # Stop at the hand-off: the conversation itself is not under test.
    monkeypatch.setattr(api, "_voice_start", lambda *a, **k: api._wake_shutdown.set()
                        or (_ for _ in ()).throw(SystemExit), raising=False)
    try:
        api._google_wake_loop()
    except SystemExit:
        pass


def test_wake_lines_carry_word_counts_only(monkeypatch, capsys):
    _wake(monkeypatch, [HEARD])
    log = capsys.readouterr().out
    assert "[Wake] Heard: <7 words> (threshold" in log
    assert "[Wake] Matched wake word 'mist', command: <" in log
    _no_content(log)


def test_wake_lines_in_full_under_the_flag(monkeypatch, capsys, verbose):
    _wake(monkeypatch, [HEARD])
    log = capsys.readouterr().out
    assert f"[Wake] Heard: {HEARD!r}" in log
    assert "command: 'tell joanne about the lighthouse pier'" in log


# -- the scenario harness ----------------------------------------------------------------

def test_the_harness_log_header_says_verbose(tmp_path, monkeypatch, verbose):
    import tools.run_scenarios as rs
    path = tmp_path / "run.log"
    tee = rs.Tee(str(path), SimpleNamespace(write=lambda s: None, flush=lambda: None))
    tee.write("[Exec] type_text('x')\n")
    tee.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "[Harness] verbose logs: on, this log contains content"
    assert tee.text == "[Exec] type_text('x')\n"          # the parsed text has no header


def test_the_harness_turns_the_flag_on_for_its_runs_and_restores_it(tmp_path, capsys):
    import tools.run_scenarios as rs
    seen = []

    class FakeApi:
        AGENT_HARD_TIMEOUT = 0

        def __init__(self):
            self.workspace = SimpleNamespace(reset_task_state=lambda: None)
            self.live = SimpleNamespace(refresh=lambda: None)

        def ask_mistai(self, prompt, input_mode="text"):
            seen.append(os.environ.get("MISTAI_VERBOSE_LOGS"))
            print(f"[Exec] type_text({TYPED!r})")
            return {"success": True, "command": {"speech": "ok"}}

        def is_agent_busy(self):
            return {"busy": False}

    scen = tmp_path / "s.json"
    scen.write_text(json.dumps({"scenarios": [
        {"name": "t", "prompt": "type it", "expect": {
            "executed_with_param": [{"action": "type_text", "contains": "diary"}]}}]}))
    code = rs.main(["--stub", "--yes", "--mode", "gating", "--scenarios", str(scen),
                    "--log-root", str(tmp_path / "logs")], api_factory=FakeApi)
    assert code == 0, capsys.readouterr().out
    assert seen == ["1"]
    assert "MISTAI_VERBOSE_LOGS" not in os.environ
    logs = list((tmp_path / "logs").rglob("*.log"))
    assert logs and all(p.read_text(encoding="utf-8").startswith(
        "[Harness] verbose logs: on") for p in logs)
