"""Restore MistAI's window after a task (Api._restore_after_task).

ask_mistai minimizes MistAI before the agent runs; when the run REALLY ends
(the agent thread's done/error callback, after the gate is released) the
window comes back WITHOUT focus: ShowWindowAsync(SW_SHOWNOACTIVATE), then an
async, no-activate HWND_TOPMOST / HWND_NOTOPMOST pair that lifts it to the top
of the normal band and always ends non-topmost. Never SetForegroundWindow,
never window.restore(). win32gui / win32process / ShowWindowAsync are stubbed.
"""

import inspect
import os
import sys
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core import config
from core.api import Api
from test_conversation_layer import CHAT, _routing_api

HWND = 0x1234
SW_SHOWNOACTIVATE = 4
SWP_FLAGS = 0x4000 | 0x0010 | 0x0002 | 0x0001   # ASYNC | NOACTIVATE | NOMOVE | NOSIZE
SWP_ASYNC, SWP_NOACTIVATE = 0x4000, 0x0010
SWP_FORBIDDEN = 0x0040 | 0x0004 | 0x0020          # SHOWWINDOW | NOZORDER | FRAMECHANGED
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
WS_EX_TOPMOST = 0x8


class _Win32:
    """win32gui + win32process stand-ins that record every call."""

    def __init__(self, windows=None, iconic=True):
        self.calls = []
        self.iconic = iconic
        self.exstyle = 0              # WS_EX_TOPMOST tracked like Windows does
        self.raise_on = set()         # insert-after values whose call raises
        # hwnd -> (pid, owner, title): EnumWindows' top-level windows.
        self.windows = windows or {}

    def __getattr__(self, name):            # anything unexpected is recorded
        def call(*a):
            self.calls.append((name,) + a)
        return call

    def IsWindow(self, h):
        return True

    def IsIconic(self, h):
        self.calls.append(("IsIconic", h))
        return self.iconic

    def GetWindowLong(self, h, index):
        self.calls.append(("GetWindowLong", h, index))
        return self.exstyle

    def SetWindowPos(self, h, after, *rest):
        self.calls.append(("SetWindowPos", h, after) + rest)
        if after in self.raise_on:
            raise OSError("SetWindowPos failed")
        if after == HWND_TOPMOST:
            self.exstyle |= WS_EX_TOPMOST
        elif after == HWND_NOTOPMOST:
            self.exstyle &= ~WS_EX_TOPMOST

    def SetForegroundWindow(self, h):
        self.calls.append(("SetForegroundWindow", h))

    def EnumWindows(self, cb, extra):
        for h in self.windows:
            cb(h, extra)

    def GetWindowThreadProcessId(self, h):
        return (1, self.windows[h][0])

    def GetWindow(self, h, kind):
        return self.windows[h][1]

    def GetWindowText(self, h):
        return self.windows[h][2]


class _Window:
    title = "MistAI v9"

    def __init__(self, native=True):
        self.calls = []
        self.native = (SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: HWND))
                       if native else None)

    def restore(self):
        self.calls.append("restore")

    def minimize(self):
        self.calls.append("minimize")


@pytest.fixture
def win(monkeypatch):
    w = _Win32()
    monkeypatch.setitem(sys.modules, "win32gui", w)
    monkeypatch.setitem(sys.modules, "win32process", w)
    monkeypatch.setattr(api_mod, "_show_window_async",
                        lambda h, cmd: w.calls.append(("ShowWindowAsync", h, cmd)))
    monkeypatch.delenv("MISTAI_RESTORE_AFTER_TASK", raising=False)
    return w


RESTORE = [("IsIconic", HWND), ("ShowWindowAsync", HWND, SW_SHOWNOACTIVATE),
           ("GetWindowLong", HWND, -20),
           ("SetWindowPos", HWND, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS),
           ("SetWindowPos", HWND, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)]


def _restores(w):
    return [c for c in w.calls if c[0] == "ShowWindowAsync"]


def _api(monkeypatch, run=None, llm_reply=""):
    """The conversation tests' bare Api with a window. `run(callbacks)` is
    what the agent thread does (default: nothing yet)."""
    api = _routing_api(monkeypatch, llm_reply)
    api._window = _Window()
    api._awaiting_text_answer = threading.Event()
    agent_run = api.agent.run_project

    def run_project(*a, **k):
        agent_run(*a, **k)
        if run:
            run(k)

    api.agent.run_project = run_project
    return api


def _positions(w):
    return [c for c in w.calls if c[0] == "SetWindowPos"]


def _no_focus_change(api, w):
    focus = {"SetForegroundWindow", "BringWindowToTop", "SetActiveWindow", "SetFocus"}
    assert not any(c[0] in focus for c in w.calls)
    assert api._window.calls == []                  # no window.restore()
    for c in _positions(w):                         # never activates, always async
        flags = c[-1]
        assert flags & SWP_NOACTIVATE and flags & SWP_ASYNC
        assert not flags & SWP_FORBIDDEN
    if _positions(w):                               # always ends non-topmost
        assert _positions(w)[-1][2] == HWND_NOTOPMOST
    assert not w.exstyle & WS_EX_TOPMOST


# -- one no-activate restore per finished task -----------------------------------------

@pytest.mark.parametrize("finish", [
    lambda cb: cb["on_done"]({"success": True, "steps": 2, "speech": "Done."}),
    lambda cb: cb["on_done"]({"success": False, "error": "no answer", "steps": 3}),
    lambda cb: cb["on_done"]({"success": False, "error": "Stopped by user"}),
    lambda cb: cb["on_error"]("boom"),
], ids=["success", "failure", "stop", "error"])
def test_a_finished_task_restores_once_without_focus(monkeypatch, win, finish):
    api = _api(monkeypatch, run=finish)
    Api.ask_mistai(api, "open notepad")
    assert api.rec["minimized"] == 1
    assert win.calls == RESTORE
    assert not api._agent_lock.locked()          # restored after the release
    _no_focus_change(api, win)


def test_a_window_the_user_already_restored_is_left_alone(monkeypatch, win):
    win.iconic = False
    api = _api(monkeypatch, run=lambda cb: cb["on_done"]({"success": True}))
    Api.ask_mistai(api, "open notepad")
    assert win.calls == [("IsIconic", HWND)] and _positions(win) == []


# -- never for chat, filler or busy replies ---------------------------------------------

def test_chat_filler_and_busy_replies_never_restore(monkeypatch, win):
    api = _api(monkeypatch, llm_reply=CHAT)
    assert Api.ask_mistai(api, "tell me a joke")["command"]["speech"] == "Ha."
    assert Api.ask_mistai(api, "thanks")["command"]["speech"] == "You're welcome."
    api._agent_lock.acquire()                    # another task holds the gate
    api._agent_task = "earlier task"
    assert Api.ask_mistai(api, "open notepad")["error"] == "busy"
    assert api.rec["minimized"] == 0 and win.calls == [] and api._window.calls == []


# -- skipped when the run is no longer the latest -----------------------------------------

def test_a_stale_callback_after_a_new_run_started_does_not_restore(monkeypatch, win):
    api = _api(monkeypatch)
    assert Api.ask_mistai(api, "open notepad")["error"] == "still_running"
    callbacks = api.agent.callbacks
    with api._agent_state_lock:
        api._agent_generation += 1               # what a newer run's gate does
    callbacks["on_done"]({"success": True})
    assert win.calls == []


def test_a_stale_callback_after_a_newer_run_finished_does_not_restore(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 5                    # run 4 is old; run 5 already ended
    api._restore_after_task(4)
    assert win.calls == []
    api._restore_after_task(5)
    assert win.calls == RESTORE


def test_no_restore_while_another_run_holds_the_gate(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 4
    api._agent_lock.acquire()
    api._restore_after_task(4)
    assert _restores(win) == []


def test_no_restore_while_a_text_prompt_is_open(monkeypatch, win):
    api = _api(monkeypatch)
    Api.ask_mistai(api, "open notepad")
    api._awaiting_text_answer.set()
    api.agent.callbacks["on_done"]({"success": True})
    assert win.calls == []


def test_the_setting_off_skips_it_and_is_read_at_call_time(monkeypatch, win):
    api = _api(monkeypatch)
    Api.ask_mistai(api, "open notepad")          # setting on when the task started
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "0")
    assert config.restore_after_task_enabled() is False
    api.agent.callbacks["on_done"]({"success": True})
    assert win.calls == []
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "off")
    assert config.restore_after_task_enabled() is False
    monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "")
    assert config.restore_after_task_enabled() is True       # default on


# -- a run that outlasts the timeout restores when it really ends ----------------------------

def test_a_run_past_the_timeout_restores_when_it_really_ends(monkeypatch, win):
    api = _api(monkeypatch)                      # AGENT_HARD_TIMEOUT is 0.01s
    assert Api.ask_mistai(api, "long task")["error"] == "still_running"
    assert win.calls == []                       # still running: stays minimized
    api.agent.callbacks["on_done"]({"success": True, "steps": 9})
    assert win.calls == RESTORE
    _no_focus_change(api, win)


# -- the except branch -------------------------------------------------------------------

def test_an_exception_after_minimizing_restores(monkeypatch, win):
    def boom(_cb):
        raise RuntimeError("thread could not start")
    api = _api(monkeypatch, run=boom)
    assert Api.ask_mistai(api, "open notepad") == {"success": False,
                                                    "error": "thread could not start"}
    assert api.rec["minimized"] == 1 and not api._agent_lock.locked()
    assert win.calls == RESTORE
    _no_focus_change(api, win)


def test_an_exception_before_minimizing_does_not_restore(monkeypatch, win):
    api = _api(monkeypatch)
    api._task_needs_project_workspace = lambda _m: (_ for _ in ()).throw(ValueError("x"))
    assert Api.ask_mistai(api, "open notepad")["success"] is False
    assert api.rec["minimized"] == 0 and win.calls == []


# -- the handle, and bare objects ---------------------------------------------------------

def test_a_bare_object_with_no_window_skips_it(win):
    api = Api.__new__(Api)
    api._restore_after_task(0)                   # no _window, no locks: no error
    assert win.calls == []


def test_the_handle_falls_back_to_this_process_top_level_window(win):
    pid = os.getpid()
    win.windows = {
        0x10: (pid + 1, 0, "MistAI v9"),          # another process
        0x20: (pid, 0x99, "MistAI v9"),           # owned (a dialog)
        0x30: (pid, 0, "Captions"),               # the caption window
        0x40: (pid, 0, "MistAI v9"),              # MistAI itself
    }
    api = Api.__new__(Api)
    api._window = _Window(native=False)
    assert api._own_window_handle() == 0x40


def test_the_restore_path_never_focuses_or_calls_window_restore():
    src = inspect.getsource(Api._restore_after_task) + inspect.getsource(Api._own_window_handle)
    for name in ("SetForegroundWindow", "BringWindowToTop", "SetActiveWindow", "SetFocus",
                 ".restore()"):
        assert name not in src, name
    # The existing ask-the-user restore is unchanged.
    assert "self._window.restore()" in inspect.getsource(Api._get_text_answer)


# -- the raise: topmost then not-topmost, always ending non-topmost ------------------------

def _bare(win):
    api = Api.__new__(Api)
    api._window = _Window()
    api._agent_state_lock, api._agent_lock = threading.Lock(), threading.Lock()
    api._agent_generation = 1
    return api


def test_the_raise_logs_its_line(win, capsys):
    api = _bare(win)
    api._restore_after_task(1)
    assert win.calls == RESTORE
    assert "[Window] Restored and raised after the task (not activated)" in capsys.readouterr().out
    _no_focus_change(api, win)


def test_a_failed_topmost_call_still_clears_topmost(win, capsys):
    win.raise_on = {HWND_TOPMOST}
    api = _bare(win)
    api._restore_after_task(1)
    assert [c[2] for c in _positions(win)] == [HWND_TOPMOST, HWND_NOTOPMOST]
    assert "Restore after the task skipped: OSError" in capsys.readouterr().out
    _no_focus_change(api, win)


def test_a_failed_notopmost_call_is_logged_by_type_only(win, capsys):
    win.raise_on = {HWND_NOTOPMOST}
    api = _bare(win)
    api._restore_after_task(1)                   # no crash
    out = capsys.readouterr().out
    assert "[Window] Could not clear topmost: OSError" in out
    assert "SetWindowPos failed" not in out      # the type, never the message
    assert [c[2] for c in _positions(win)] == [HWND_TOPMOST, HWND_NOTOPMOST]


def test_an_already_topmost_window_is_left_alone(win):
    win.exstyle = WS_EX_TOPMOST
    api = _bare(win)
    api._restore_after_task(1)
    assert _positions(win) == []
    assert win.exstyle & WS_EX_TOPMOST           # its topmost state is kept
    assert ("ShowWindowAsync", HWND, SW_SHOWNOACTIVATE) in win.calls


@pytest.mark.parametrize("skip", ["stale", "gate", "prompt", "off", "shown"])
def test_every_skip_makes_no_position_call(monkeypatch, win, skip):
    api = _bare(win)
    api._awaiting_text_answer = threading.Event()
    gen = 1
    if skip == "stale":
        gen = 0
    elif skip == "gate":
        api._agent_lock.acquire()
    elif skip == "prompt":
        api._awaiting_text_answer.set()
    elif skip == "off":
        monkeypatch.setenv("MISTAI_RESTORE_AFTER_TASK", "0")
    elif skip == "shown":
        win.iconic = False
    api._restore_after_task(gen)
    assert _positions(win) == [] and _restores(win) == []
