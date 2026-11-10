"""Canaries: contact, message, window title, URL, file name and typed text must
never reach the console in default mode. The REAL AgentLoop, Executor,
ToolRegistry, Api._exec_sync dispatch, Api handlers and ImprovedClickHandler
run; only OS-level calls (pyautogui, win32gui, Tesseract, pywinauto, vision,
the browser) are stubbed. Under MISTAI_VERBOSE_LOGS=1 the prints that are
meant to show content do show it."""

import json
import sys
import threading
import types
from types import SimpleNamespace

import pytest

import core.api as api_mod
import core.click_handler as ch
from core.agent import AgentLoop
from core.api import Api
from core.executor import Executor
from core.tools import ToolRegistry

CONTACT = "Zorblax Quindlewhip"
MESSAGE = "canary pelican rendezvous"
TITLE = "Zorblax Quindlewhip - Direct Messages - Discord"
URL = "https://canary-url.example/zq-secret-path"
FILE = "canary_zq_diary.txt"
TYPED = "canary walrus typed text"
ANSWER = "the second Zorblax please"
CANARIES = {"contact": "Quindlewhip", "message": "pelican", "title": "Direct Messages",
            "url": "canary-url", "file": "canary_zq_diary", "typed": "walrus",
            "answer": "second Zorblax"}


class _Permissive(types.ModuleType):
    """A stand-in OS module: any function returns 0 unless overridden."""

    def __getattr__(self, name):
        return lambda *a, **k: 0


def _fake_os_modules(monkeypatch):
    win32gui = _Permissive("win32gui")
    win32gui.GetForegroundWindow = lambda: 1
    win32gui.GetWindowText = lambda _h: TITLE
    win32gui.IsWindow = lambda _h: True
    win32gui.EnumWindows = lambda cb, extra: cb(1, extra)
    win32process = _Permissive("win32process")
    win32process.GetWindowThreadProcessId = lambda _h: (0, 4242)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    monkeypatch.setitem(sys.modules, "win32process", win32process)
    for mod in (api_mod, ch):
        for name in ("hotkey", "press", "click", "moveTo", "write"):
            monkeypatch.setattr(mod.pyautogui, name, lambda *a, **k: None)
        monkeypatch.setattr(mod.pyautogui, "size", lambda: (1920, 1080))
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(ch.time, "sleep", lambda _s: None)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    monkeypatch.setattr("core.executor.time.sleep", lambda _s: None)


def _ocr_words(monkeypatch, words):
    data = {k: [] for k in ("text", "conf", "left", "top", "width", "height",
                            "block_num", "par_num", "line_num", "word_num")}
    for i, w in enumerate(words):
        for k, v in (("text", w), ("conf", 90), ("left", 300 + 60 * i), ("top", 400),
                     ("width", 55), ("height", 20), ("block_num", 1), ("par_num", 1),
                     ("line_num", 1), ("word_num", i + 1)):
            data[k].append(v)
    monkeypatch.setattr(ch, "OCR_AVAILABLE", True)
    monkeypatch.setattr(ch, "pytesseract", SimpleNamespace(
        image_to_data=lambda *a, **k: data, Output=SimpleNamespace(DICT="dict")), raising=False)


class _UIAElement:
    def window_text(self):
        return CONTACT

    def rectangle(self):
        return SimpleNamespace(left=500, right=600, top=300, bottom=330, width=100, height=30)


class _UIAWindow:
    def window_text(self):
        return TITLE

    def descendants(self, depth=8):
        return [_UIAElement()]


def _api(monkeypatch):
    _fake_os_modules(monkeypatch)
    api = Api.__new__(Api)
    frame = SimpleNamespace(shape=(1080, 1920, 3))
    api.live = SimpleNamespace(snapshot={"foreground_window": TITLE, "foreground_hwnd": 1,
                                         "open_apps": ["discord"], "screen_resolution": (1920, 1080)})
    api.vision = SimpleNamespace(
        ready=True, find_element_coords=lambda q, f: (700, 500),
        find_nth_element_coords=lambda t, n, f: (TITLE, (700, 500)))
    api.screen = SimpleNamespace(frame_for_read=lambda label: object(), mark_action=lambda: None,
                                 frame=object())
    api.click_handler = ch.ImprovedClickHandler(api.live, api.vision, api.screen)
    h = api.click_handler
    monkeypatch.setattr(h, "_show_debug_marker", lambda x, y: None)
    monkeypatch.setattr(h, "_get_frame_for_ocr", lambda: frame)
    monkeypatch.setattr(h, "_ocr_button_regions", lambda f, q: (None, "", 0, 0))
    monkeypatch.setattr(h, "_prepare_frames", lambda f: [(frame, 1.0)])
    api.tools = ToolRegistry(":memory:")
    api.executor = Executor(api)
    api.memory = SimpleNamespace(add_action=lambda *a: None)
    api.task_memory = SimpleNamespace(record=lambda *a: None, get_hints_for_task=lambda _t: [])
    api.workspace = SimpleNamespace(record_execution=lambda *a: None)
    api.bus = SimpleNamespace(publish=lambda *a, **k: None)
    api.uia_nav = SimpleNamespace(invalidate_cache=lambda: None)
    api._stop_requested = threading.Event()
    api._agent_lock, api._agent_state_lock = threading.Lock(), threading.Lock()
    api.caption_window, api.captions_enabled = None, False
    api.last_result_text = ""
    api._last_opened_app = None
    api._window = None
    api.spoken = []
    api.speak = lambda text, **_k: api.spoken.append(text)
    api.tts = SimpleNamespace(speaking=False)
    api.api_manager = SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: URL,
                                      get_fallback_url=lambda: URL)
    api._resolve_path_placeholders = lambda p: p
    # Answers typed into the page: "yes" to a confirmation, ANSWER to ask_user.
    api._answer_lock, api._answer_event = threading.Lock(), threading.Event()
    api._awaiting_text_answer = threading.Event()
    api._pending_answer = api._pending_source = None

    def page(js):
        if js.startswith("showConfirmPrompt("):
            Api.submit_answer(api, "yes")
        elif js.startswith("showAskUserPrompt("):
            Api.submit_answer(api, ANSWER)
    api._notify_js = page
    # OS-level helpers of the handlers.
    api.app_window_state = lambda _app: ("foreground", TITLE)
    api.is_app_running = lambda _app: True
    api._find_hwnds_by_process_name = lambda _app: [1]
    api._focus_hwnd = lambda _h: (True, "")
    api._is_own_browser_window = lambda _h: False
    api._foreground_terminal = lambda: None
    api._foreground_process_matches = lambda _app: True
    api.set_clipboard = lambda _t: True
    api._clipboard_text_full = lambda: ""
    api._get_app_search_hotkey = lambda _app: "ctrl+k"
    api._vision_read = lambda q: (CONTACT, True)
    api._exact_ui_text = lambda q, include_browser=False: (
        f"{CONTACT}: {MESSAGE} ({FILE})", TITLE, True, "exact", "discord")
    api.enhanced_available = True
    api.window_mgr = SimpleNamespace(find_window=lambda t: SimpleNamespace(_hWnd=1, title=TITLE),
                                     find_window_normalized=lambda t: None)
    api.browser = SimpleNamespace(navigate=lambda u: (URL, TITLE),
                                  get_text=lambda _s="": f"{CONTACT} {MESSAGE} " * 80)
    monkeypatch.setattr(ch, "Desktop", lambda backend="uia": SimpleNamespace(
        windows=lambda: [_UIAWindow()]), raising=False)
    monkeypatch.setattr(ch, "PYWINAUTO_AVAILABLE", True)
    return api


STEPS = [
    {"action": "open_app", "parameter": "discord"},
    {"action": "switch_app", "parameter": "discord"},
    {"action": "window_activate", "parameter": TITLE},
    {"action": "click_on_text", "parameter": CONTACT},
    {"action": "type_text", "parameter": TYPED},
    {"action": "quick_message", "parameter": f"Discord|{CONTACT}|{MESSAGE}"},
    {"action": "read_screen_text", "parameter": f"what did {CONTACT} say?"},
    {"action": "browser_search", "parameter": f"{CONTACT} {MESSAGE}"},
    {"action": "browser_navigate", "parameter": URL},
    {"action": "write_file", "parameter": f"{FILE}|{TYPED}"},
    {"action": "ask_user", "parameter": f"Which {CONTACT} did you mean?"},
    {"action": "done", "speech": f"Sent {MESSAGE} to {CONTACT}."},
    {"action": "done", "speech": f"Sent {MESSAGE} to {CONTACT}."},   # after a done check
]


def _run_everything(monkeypatch, tmp_path):
    api = _api(monkeypatch)
    monkeypatch.setattr(api, "_resolve_path_placeholders", lambda p: str(tmp_path / p))
    _ocr_words(monkeypatch, CONTACT.split())
    replies = iter(json.dumps(s) for s in STEPS)
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: f"Window: {TITLE}")
    result = loop._loop(f"message {CONTACT} saying {MESSAGE} and save {FILE}", "m", "none",
                        "text", None)
    # The other two click routes, through the real Executor: UIA, then vision.
    monkeypatch.setattr(ch, "OCR_AVAILABLE", False)
    uia = api.executor.run("click_on_text", CONTACT)
    monkeypatch.setattr(ch, "Desktop", lambda backend="uia": SimpleNamespace(windows=lambda: []))
    vision = api.executor.run("click_on_text", CONTACT)
    # And a refused input: the report names the window in front.
    api._last_opened_app = "notepad"
    api._foreground_process_matches = lambda _app: False
    api.switch_to_app = lambda _app: False
    refused = api.executor.run("type_text", TYPED)
    return api, result, (uia, vision, refused)


def test_no_canary_reaches_the_console_in_default_mode(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    api, result, (uia, vision, refused) = _run_everything(monkeypatch, tmp_path)
    assert result["success"], result
    assert (uia.method, vision.method) == ("UIA", "Vision")
    assert uia.success and vision.success and not refused.success
    out = capsys.readouterr()
    leaked = {k: v for k, v in CANARIES.items() if v in out.out or v in out.err}
    assert leaked == {}, [l for l in (out.out + out.err).splitlines()
                          if any(v in l for v in leaked.values())][:10]
    # The paths really ran.
    for line in ("[TypeText] typed into the input target",
                 "[TypeText] NOT SENT (the input target is not in front)",
                 "[OCR] Compact match: 2 word(s), score=100", "[UIA] Found <", "[Vision] Found <",
                 "[Confirm] answer=yes (typed)", "[AskUser] answered", "[Exec] quick_message(",
                 "[AgentLoop] Task: <"):
        assert line in out.out, line


def test_the_model_still_sees_the_window_title(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    api = _api(monkeypatch)
    api._last_opened_app = None
    assert Api._act_type_text(api, TYPED) is True
    assert TITLE[:40] in api.last_result_text                  # the observation keeps it
    assert TITLE[:10] not in capsys.readouterr().out


def test_under_verbose_logs_the_content_prints_appear(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    _run_everything(monkeypatch, tmp_path)
    out = capsys.readouterr().out
    assert f"[TypeText] The text went to '{TITLE}'" in out        # the window title
    assert f"[AgentLoop] Task: 'message {CONTACT}" in out        # the request
    assert f"[Exec] type_text('{TYPED}')" in out                  # typed text
    assert f"[OCR] Found '{CONTACT}'" in out                      # the click target
    assert f"[Exec] browser_navigate('{URL}')" in out             # the URL


@pytest.mark.parametrize("call, line", [
    (lambda: __import__("core.planner", fromlist=["x"])._llm(
        SimpleNamespace(api_manager=SimpleNamespace(
            get_headers=lambda: {}, get_api_url=lambda: URL, get_fallback_url=lambda: URL)),
        "p", "m", timeout=1, fallback=False), "[Planner] LLM call failed: ConnectionError"),
])
def test_planner_llm_failure_prints_the_type_only(monkeypatch, capsys, call, line):
    import requests
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    monkeypatch.setattr("core.planner.requests.post", lambda u, **k: (_ for _ in ()).throw(
        requests.exceptions.ConnectionError(f"Max retries exceeded with url: {u}")))
    call()
    out = capsys.readouterr().out
    assert line in out and "canary-url" not in out


def test_plan_names_and_watched_paths_print_as_lengths(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    from core import watchers
    w = watchers.FileWatcher.__new__(watchers.FileWatcher)
    w._observer = SimpleNamespace(schedule=lambda *a, **k: None)
    w._handler, w._watched, w.available = None, set(), True
    monkeypatch.setattr(watchers.os.path, "isdir", lambda p: True)
    try:
        watchers.FileWatcher.watch(w, f"C:/Users/{CONTACT}/{FILE}")
    except Exception:
        pass
    out = capsys.readouterr().out
    assert "Quindlewhip" not in out and "canary_zq" not in out
