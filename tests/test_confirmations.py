"""Confirmation answers are "yes", "no" or "unclear" (config.confirmation_answer).

- yes runs the action; an explicit no keeps today's decline path and wording.
- unclear (empty, unrecognized, or neither a yes nor a no word): ask once
  more ("Sorry, yes or no?"), voice and typed alike; still unclear -> the
  phase fails closed: the user hears "I didn't hear a yes, so I didn't
  <verb> that.", the result is success False / "not confirmed", and no model
  call, executor action or other route follows. No decline signature.
- logs carry "[Confirm] answer=yes|no|unclear (button|voice|typed)" and
  "(retry)", never the words.
- planner._confirm uses the same helper.

Merged by area (pure moves, no test changed) from: test_confirm_answers.py, test_confirm_listen_profiles.py, test_enter_confirmation.py."""

import json
import threading
from types import SimpleNamespace
import pytest
from confirm_helpers import bind_confirmation
from core.agent import (CONFIRM_RETRY_QUESTION, NOT_CONFIRMED_ERROR, AgentLoop,
                        unclear_answer_speech)
from core.config import confirmation_answer
from core.planner import PlanRunner
from core.tools import SPECS
import core.api as api_mod
from core.api import Api
from core.config import confirm_listen_profile
import sys
from core.agent import AgentLoop
from core.tools import SPECS, ToolRegistry



# ==========================================================================
# moved from tests/test_confirm_answers.py
# ==========================================================================

CONTACT = "Joanne Quillfeather"


MESSAGE = "meet me at the lighthouse pier"


SEND = {"action": "quick_message", "parameter": f"Discord|{CONTACT}|{MESSAGE}"}


DONE = {"action": "done", "speech": "ok"}


MUMBLE = "marblewood cobblestone"        # an answer with neither a yes nor a no


CONTENT_WORDS = ("joanne", "quillfeather", "lighthouse", "pier", "marblewood",
                 "cobblestone", "maybe")


def _no_content(text):
    low = text.lower()
    assert [w for w in CONTENT_WORDS if w in low] == []


@pytest.mark.parametrize("answer, verdict", [
    ("yes", "yes"), ("Yes please.", "yes"), ("go ahead", "yes"), ("do it", "yes"),
    ("sure", "yes"), ("no", "no"), ("nope", "no"), ("not sure", "no"),
    ("No, sure", "no"), ("cancel", "no"), ("never mind", "no"),
    ("", "unclear"), (None, "unclear"), ("   ", "unclear"), ("maybe", "unclear"),
    (MUMBLE, "unclear"), ("yesterday", "unclear"), ("continue", "unclear"),
])
def test_the_helper(answer, verdict):
    assert confirmation_answer(answer) == verdict


def test_extra_yes_words_are_whole_words():
    assert confirmation_answer("continue", extra_yes=("continue",)) == "yes"
    assert confirmation_answer("discontinued", extra_yes=("continue",)) == "unclear"


@pytest.mark.parametrize("answer", ["", None, "maybe", MUMBLE, "no", "not sure"])
def test_is_confirmation_stays_the_strict_yes_test(answer):
    assert AgentLoop._is_confirmation(answer) is False
    assert AgentLoop._is_confirmation("yes") is True


# -- the agent loop ---------------------------------------------------------------------

def _run(monkeypatch, replies, answers, mode="voice", confirm=("quick_message", "window_close")):
    """One phase through the real AgentLoop. Returns a namespace with the
    executed calls, spoken lines, asked questions, stored rows, the requests
    sent to the model and the result."""
    replies = iter(json.dumps(r) for r in replies)
    answers = list(answers)
    rec = SimpleNamespace(calls=[], spoken=[], asked=[], rows=[], requests=[])

    def answer(question):
        rec.asked.append(question)
        return answers.pop(0) if answers else None

    def run(a, p):
        rec.calls.append((a, p))
        return SimpleNamespace(action=a, parameter="", success=True, detail="", verified=None,
                               escalate_to_user=False, reliability_note=lambda: "",
                               escalated_from="")

    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False,
        _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda a, *_x, **_k: a in confirm),
        executor=SimpleNamespace(run=run),
        memory=SimpleNamespace(add_action=lambda *a: rec.rows.append(a)),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda t, **_k: rec.spoken.append(t),
        tts=SimpleNamespace(speaking=False),
        _listen_once=lambda timeout=10: answer(rec.spoken[-1] if rec.spoken else ""),
        _get_text_answer=lambda q, timeout=60: answer(q),
        _normalize_utterance=lambda t: " ".join(str(t or "").lower().split()),
        _VOICE_STOP_WORDS={"stop"},
        stop_current_task=lambda: api._stop_requested.set(),
        _last_opened_app="",
    )

    def fake_post(*_a, json=None, **_k):
        rec.requests.append(json)
        return SimpleNamespace(ok=True, status_code=200,
                               json=lambda: {"response": next(replies)})

    monkeypatch.setattr("core.agent.requests.post", fake_post)
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "Discord"))
    rec.result = loop._loop("send it", "m", "none", mode, None)
    return rec


def _observations(rec):
    return [m["content"] for r in rec.requests for m in r.get("messages", [])
            if m.get("role") == "user" and "[OBSERVATION]" in str(m.get("content"))]


def test_unclear_then_yes_runs(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "yes"])
    assert rec.calls == [("quick_message", SEND["parameter"])]
    assert rec.asked[1] == CONFIRM_RETRY_QUESTION and len(rec.asked) == 2
    log = capsys.readouterr().out
    assert "[Confirm] answer=unclear (voice)\n" in log
    assert "[Confirm] answer=yes (voice) (retry)" in log
    _no_content(log)


UNCLEAR_SEND = {"success": False, "error": "not confirmed",
                "speech": "I didn't hear a yes, so I didn't send that.", "steps": 1}


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_unclear_twice_fails_closed(monkeypatch, capsys, mode):
    rec = _run(monkeypatch, [SEND, DONE], [MUMBLE, ""], mode=mode)
    assert rec.calls == []                                # the executor never ran
    assert rec.result == UNCLEAR_SEND and NOT_CONFIRMED_ERROR == "not confirmed"
    assert rec.spoken[-1] == "I didn't hear a yes, so I didn't send that."
    assert len(rec.asked) == 2 and rec.asked[1] == CONFIRM_RETRY_QUESTION   # one retry
    assert len(rec.requests) == 1                         # no model call after the answer
    assert rec.rows == []                                 # no confirm_declined row
    log = capsys.readouterr().out
    src = "voice" if mode == "voice" else "typed"
    assert f"[Confirm] answer=unclear ({src})\n" in log
    assert f"[Confirm] answer=unclear ({src}) (retry)" in log
    assert "[Confirm] not confirmed; the phase ends" in log
    _no_content(log)
    _no_content(repr(rec.rows))


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_unclear_cannot_lead_to_another_model_call_or_route(monkeypatch, mode):
    # The model has a manual route queued; none of it may run.
    manual = [{"action": "click_on_text", "parameter": CONTACT},
              {"action": "type_text", "parameter": MESSAGE},
              {"action": "press_key", "parameter": "enter"}]
    rec = _run(monkeypatch, [SEND, *manual, DONE], [MUMBLE, MUMBLE], mode=mode)
    assert rec.calls == [] and len(rec.requests) == 1
    assert rec.result["error"] == "not confirmed"
    assert not any("REFUSED" in o or "declined" in o for o in _observations(rec))


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_enter_in_a_messaging_app_fails_closed_too(monkeypatch, mode):
    enter = {"action": "press_key", "parameter": "enter"}
    rec = _run(monkeypatch, [enter, DONE], ["maybe", "hmm"], mode=mode,
               confirm=("press_key",))
    assert rec.calls == [] and len(rec.requests) == 1
    assert rec.result == {"success": False, "error": "not confirmed",
                          "speech": "I didn't hear a yes, so I didn't do that.", "steps": 1}


def test_other_actions_name_a_generic_verb(monkeypatch):
    rec = _run(monkeypatch, [{"action": "window_close", "parameter": "Notepad"}, DONE],
               ["maybe", "maybe"])
    assert rec.calls == []
    assert "I didn't hear a yes, so I didn't do that." in rec.spoken
    assert unclear_answer_speech("quick_message") == "I didn't hear a yes, so I didn't send that."


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_no_is_unchanged(monkeypatch, capsys, mode):
    rec = _run(monkeypatch, [SEND, {**SEND, "parameter": "Discord|Ada|hi"}], ["no", "nope"],
               mode=mode)
    assert rec.calls == []
    assert len(rec.asked) == 2                            # no retry after a no
    assert any("User explicitly declined this action" in o for o in _observations(rec))
    assert rec.rows == [("confirm_declined", False)]
    assert rec.result == {"success": False, "error": "User declined the requested action.",
                          "speech": "Okay, I won't do that."}
    assert "I didn't hear a yes" not in " ".join(rec.spoken)
    assert capsys.readouterr().out.count("[Confirm] answer=no") == 2


def test_typed_maybe_gets_the_one_retry_then_yes_runs(monkeypatch, capsys):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "yes"], mode="text")
    assert rec.calls == [("quick_message", SEND["parameter"])]
    assert rec.asked[1] == CONFIRM_RETRY_QUESTION and len(rec.asked) == 2
    assert rec.result["success"] is True
    assert "[Confirm] answer=yes (typed) (retry)" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_an_explicit_no_on_the_retry_is_still_the_decline_path(monkeypatch, mode):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "no"], mode=mode)
    assert rec.calls == []
    assert rec.rows == [("confirm_declined", False)]
    assert any("User explicitly declined this action" in o for o in _observations(rec))
    assert rec.result["success"] is True                  # the model's done ends it


def test_typed_yes_runs(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["Yes, go ahead"], mode="text")
    assert rec.calls == [("quick_message", SEND["parameter"])]


def test_a_voice_stop_on_the_retry_is_still_a_stop(monkeypatch):
    rec = _run(monkeypatch, [SEND, DONE], ["maybe", "stop"])
    assert rec.calls == []
    assert rec.result == {"success": False, "error": "Stopped by user"}


# -- the quick_message question ---------------------------------------------------------

def _question(action, param):
    return AgentLoop(SimpleNamespace(_resolve_path_placeholders=lambda p: p),
                     None)._confirmation_question(action, param)


def test_the_quick_message_question_names_message_contact_and_app():
    assert _question("quick_message", f"Discord|{CONTACT}|{MESSAGE}") == (
        f"Send '{MESSAGE}' to {CONTACT} on Discord?")


def test_a_long_message_is_capped_with_an_ellipsis():
    long = "word " * 30
    q = _question("quick_message", f"Discord|Ada|{long}")
    quoted = q[len("Send '"):q.index("' to Ada")]
    assert len(quoted) <= 60 and quoted.endswith("…")
    assert long.startswith(quoted[:-1])
    exact = "x" * 60
    assert _question("quick_message", f"Discord|Ada|{exact}") == f"Send '{exact}' to Ada on Discord?"


@pytest.mark.parametrize("action, param, expected", [
    ("quick_message", "Discord|Ada", "This will run: quick_message(Discord|Ada) -- "
                                     "this can't easily be undone. Proceed?"),
    ("window_close", "Notepad", "This will run: window_close(Notepad) -- "
                                "this can't easily be undone. Proceed?"),
])
def test_other_questions_are_unchanged(action, param, expected):
    assert _question(action, param) == expected


@pytest.mark.parametrize("mode", ["voice", "text"])
def test_the_question_is_asked_but_never_logged(monkeypatch, capsys, mode):
    rec = _run(monkeypatch, [SEND, DONE], ["yes"], mode=mode)
    assert rec.asked == [f"Send '{MESSAGE}' to {CONTACT} on Discord?"]
    _no_content(capsys.readouterr().out)


# -- planner._confirm --------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["voice", "text"])
@pytest.mark.parametrize("answer, expected, verdict", [
    ("continue", True, "yes"), ("yes please", True, "yes"), ("no", False, "no"),
    (f"maybe {MUMBLE}", False, "unclear"), (None, False, "unclear")])
def test_planner_confirm_uses_the_helper(capsys, mode, answer, expected, verdict):
    api = SimpleNamespace(speak=lambda _t: None, tts=SimpleNamespace(speaking=False),
                          _listen_once=lambda timeout=10: answer,
                          _get_text_answer=lambda _q, timeout=60: answer,
                          _stop_requested=threading.Event())
    bind_confirmation(api)
    runner = PlanRunner.__new__(PlanRunner)
    runner.api = api
    assert runner._confirm("Continue with the independent phases?", mode) is expected
    log = capsys.readouterr().out
    assert f"[Confirm] answer={verdict} ({'voice' if mode == 'voice' else 'typed'})" in log
    assert api.notified[-2:] == ["showConfirmPrompt", "closeConfirmPrompt"]
    _no_content(log)



# ==========================================================================
# moved from tests/test_confirm_listen_profiles.py
# MISTAI_CONFIRM_LISTEN = shared (default) | ptt | lead: an off-by-default A/B
# for voice confirmation and ask_user listens. The default is unchanged; the
# other profiles use their own recognizer and only READ push-to-talk's stored
# noise threshold.
# ==========================================================================

class _Mic:
    SAMPLE_RATE, SAMPLE_WIDTH = 16000, 2
    stream = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rec(calls, name, threshold=300):
    class Rec:
        energy_threshold = threshold
        pause_threshold = 0.8
        non_speaking_duration = 0.5

        def adjust_for_ambient_noise(self, src, duration=1):
            calls.append((name, "adjust", duration))
            self.energy_threshold = 999

        def listen(self, src, timeout=None, phrase_time_limit=None):
            calls.append((name, "listen", timeout, phrase_time_limit, self.energy_threshold,
                          self.pause_threshold))
            return object()

        def recognize_google(self, audio):
            calls.append((name, "recognize"))
            return "yes"
    return Rec()


@pytest.fixture
def api(monkeypatch):
    now = [5000.0]
    sleeps = []
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(api_mod.time, "sleep", lambda s: sleeps.append(round(s, 2)))
    a = Api.__new__(Api)
    a._mic_lock, a._listen_waiting = threading.Lock(), threading.Event()
    a.microphone = _Mic()
    a.calls, a.sleeps, a.now = [], sleeps, now
    a.recognizer = _rec(a.calls, "shared")
    a.ptt_recognizer = _rec(a.calls, "pttrec", threshold=420)
    a.confirm_recognizer = _rec(a.calls, "confirm")
    a._ptt_calibrated_at = now[0] - 60          # measured a minute ago: fresh
    a.tts = SimpleNamespace(speaking=False, finished_at=now[0] - 0.1)
    monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    monkeypatch.delenv("MISTAI_VOICE_DIAG", raising=False)
    return a


def test_profile_setting_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    assert confirm_listen_profile() == "shared"
    for value, expected in (("ptt", "ptt"), (" LEAD ", "lead"), ("shared", "shared"),
                            ("fast", "shared"), ("", "shared")):
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
        assert confirm_listen_profile() == expected       # read at call time


def test_default_listen_is_pinned_and_identical(api, capsys):
    assert Api._listen_once(api, timeout=10) == "yes"
    assert api.calls == [("shared", "adjust", 0.3), ("shared", "listen", 10, 15, 999, 1.0),
                         ("shared", "recognize")]
    assert api.recognizer.non_speaking_duration == 0.5
    assert api.sleeps == [] and "[Confirm] listen profile" not in capsys.readouterr().out


def test_ptt_profile_reuses_the_fresh_threshold_with_its_own_settings(api, capsys):
    assert Api._listen_once(api, timeout=6, profile="ptt") == "yes"
    assert api.calls == [("confirm", "listen", 10, 8, 420, 1.0), ("confirm", "recognize")]
    assert "[Confirm] listen profile=ptt" in capsys.readouterr().out
    # Nothing else changed: the shared and push-to-talk recognizers.
    assert api.recognizer.energy_threshold == 300 and api.recognizer.pause_threshold == 0.8
    assert api.ptt_recognizer.energy_threshold == 420 and api.ptt_recognizer.pause_threshold == 0.8


def test_ptt_profile_adjusts_when_the_threshold_is_stale_or_missing(api):
    api._ptt_calibrated_at = api.now[0] - 600         # 10 minutes: not fresh
    Api._listen_once(api, timeout=10, profile="ptt")
    assert api.calls[0] == ("confirm", "adjust", 0.3)
    api.calls.clear()
    api._ptt_calibrated_at = None
    Api._listen_once(api, timeout=10, profile="ptt")
    assert api.calls[0] == ("confirm", "adjust", 0.3)


def test_lead_profile_waits_after_tts_and_reuses_a_fresh_threshold(api, capsys):
    assert Api._listen_once(api, timeout=10, profile="lead") == "yes"
    assert api.sleeps == [0.3]                        # 0.4 s after TTS ended 0.1 s ago
    assert api.calls == [("confirm", "listen", 10, 15, 420, 1.0), ("confirm", "recognize")]
    assert "[Confirm] listen profile=lead" in capsys.readouterr().out


def test_lead_profile_waits_for_speech_to_end_first(api):
    api.tts = SimpleNamespace(speaking=True, finished_at=None)
    seen = []

    def sleep(s):
        seen.append(round(s, 2))
        api.tts.speaking, api.tts.finished_at = False, api.now[0]
    api_mod.time.sleep = sleep
    Api._listen_once(api, timeout=10, profile="lead")
    assert seen == [0.05, 0.4]


def test_lead_profile_does_not_wait_when_tts_ended_long_ago(api):
    api.tts.finished_at = api.now[0] - 3
    Api._listen_once(api, timeout=10, profile="lead")
    assert api.sleeps == []


def test_lead_without_a_fresh_threshold_adjusts_like_shared(api):
    api._ptt_calibrated_at = None
    Api._listen_once(api, timeout=10, profile="lead")
    assert api.calls[:2] == [("confirm", "adjust", 0.3), ("confirm", "listen", 10, 15, 999, 1.0)]


def test_push_to_talk_listens_are_unchanged_by_the_setting(api, monkeypatch):
    monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", "ptt")
    api._voice_mark = lambda *a, **k: None
    Api._listen_once(api, timeout=6, ptt=True)
    from core.config import PTT_PHRASE_TIME_LIMIT
    assert api.calls[0] == ("pttrec", "listen", 6, PTT_PHRASE_TIME_LIMIT, 420, 2.0)


@pytest.mark.parametrize("profile", ["ptt", "lead"])
def test_voice_diag_names_the_profile(api, monkeypatch, capsys, profile):
    monkeypatch.setenv("MISTAI_VOICE_DIAG", "1")
    Api._listen_once(api, timeout=10, profile=profile)
    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[VoiceDiag]")][0]
    assert f"rec={profile} " in line


# -- the callers -----------------------------------------------------------------------------

def _confirm_api(listens):
    a = Api.__new__(Api)
    a._answer_lock, a._answer_event = threading.Lock(), threading.Event()
    a._awaiting_text_answer = threading.Event()
    a._pending_answer = a._pending_source = None
    a._notify_js = lambda js: None
    a._window = None
    a.speak = lambda t: None
    a.tts = SimpleNamespace(speaking=False)
    a._voice_start = a._voice_mark = lambda *x, **k: None
    a._listen_once = lambda **kw: listens.append(kw) or "yes"
    return a


@pytest.mark.parametrize("value, extra", [(None, {}), ("shared", {}), ("bogus", {}),
                                          ("ptt", {"profile": "ptt"}),
                                          ("lead", {"profile": "lead"})])
def test_confirmations_pass_a_profile_only_when_one_is_chosen(monkeypatch, value, extra):
    if value is None:
        monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
    listens = []
    Api.ask_confirmation(_confirm_api(listens), "q?", "voice")
    assert len(listens) == 1
    kw = listens[0]
    assert {k: v for k, v in kw.items() if k == "profile"} == extra
    assert kw["timeout"] == 10


@pytest.mark.parametrize("value, extra", [(None, {}), ("ptt", {"profile": "ptt"})])
def test_ask_user_passes_a_profile_only_when_one_is_chosen(monkeypatch, value, extra):
    from core.agent import AgentLoop
    from core.tools import SPECS
    if value is None:
        monkeypatch.delenv("MISTAI_CONFIRM_LISTEN", raising=False)
    else:
        monkeypatch.setenv("MISTAI_CONFIRM_LISTEN", value)
    listens = []
    replies = iter(json.dumps(r) for r in [{"action": "ask_user", "parameter": "Which one?"},
                                           {"action": "done", "speech": "ok"}])
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": []}),
        api_manager=SimpleNamespace(get_headers=lambda: {}, get_api_url=lambda: "u",
                                    get_fallback_url=lambda: "f"),
        tools=SimpleNamespace(specs=SPECS, get=lambda n: SPECS[n],
                              requires_confirmation=lambda *a, **k: False),
        executor=SimpleNamespace(run=lambda a, p: None),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p, speak=lambda _t: None,
        tts=SimpleNamespace(speaking=False),
        _listen_once=lambda **kw: listens.append(kw) or "the first one",
        _normalize_utterance=lambda t: str(t).lower(), _VOICE_STOP_WORDS={"stop"},
    )
    monkeypatch.setattr("core.agent.requests.post", lambda *a, **k: SimpleNamespace(
        ok=True, status_code=200, json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    loop._loop("t", "m", "none", "voice", None)
    assert listens == [{"timeout": 10, **extra}]



# ==========================================================================
# moved from tests/test_enter_confirmation.py
# press_key('enter') confirmation: skipped only for a safe, known, in-front target.
# ==========================================================================

def _registry():
    reg = ToolRegistry.__new__(ToolRegistry)
    reg.specs = dict(SPECS)
    return reg


# -- ToolRegistry ---------------------------------------------------------------------


@pytest.mark.parametrize("key", ["enter", "Enter", "return"])
def test_enter_asks_unless_the_target_is_safe(key):
    reg = _registry()
    assert reg.requires_confirmation("press_key", key) is True
    assert reg.requires_confirmation("press_key", key, enter_safe=False) is True
    assert reg.requires_confirmation("press_key", key, enter_safe=True) is False


def test_delete_and_browser_enter_always_ask_and_plain_keys_never_do():
    reg = _registry()
    assert reg.requires_confirmation("press_key", "delete", enter_safe=True) is True
    assert reg.requires_confirmation("browser_press_key", "enter", enter_safe=True) is True
    for key in ("=", "3", "escape", "tab"):
        assert reg.requires_confirmation("press_key", key) is False


# -- Api.enter_target_is_safe -----------------------------------------------------------

PROCS = {1: "ApplicationFrameHost.exe", 2: "CalculatorApp.exe", 3: "firefox.exe",
         4: "Notepad.exe", 5: "Discord.exe", 6: "powershell.exe", 7: "msedgewebview2.exe"}


WINDOWS = {
    10: (1, "ApplicationFrameWindow", "Calculator", [1, 2]),
    11: (4, "Notepad", "Untitled - Notepad", []),
    12: (3, "MozillaWindowClass", "Docs - Mozilla Firefox", []),
    13: (5, "Chrome_WidgetWin_1", "#general - Discord", []),
    14: (6, "ConsoleWindowClass", "Windows PowerShell", []),
    15: (7, "Chrome_WidgetWin_1", "MistAI", []),
    16: (1, "ApplicationFrameWindow", "Mail", [1]),
    17: (4, "Notepad", "edge cases.txt - Notepad", []),
}


def _api(monkeypatch, target, fg):
    monkeypatch.setitem(sys.modules, "win32gui", SimpleNamespace(
        GetForegroundWindow=lambda: fg,
        GetWindowText=lambda h: WINDOWS[h][2],
        GetClassName=lambda h: WINDOWS[h][1],
        EnumChildWindows=lambda h, cb, extra: [cb(("child", p), extra) for p in WINDOWS[h][3]],
    ))
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda h: (0, h[1]) if isinstance(h, tuple) else (0, WINDOWS[h][0])))
    monkeypatch.setattr("core.api.psutil.Process", lambda pid: SimpleNamespace(name=lambda: PROCS[pid]))
    api = Api.__new__(Api)
    api._last_opened_app = target
    return api


@pytest.mark.parametrize("target, fg", [("calculator", 10), ("notepad", 11)])
def test_safe_target_in_front_skips_the_prompt(monkeypatch, target, fg):
    assert _api(monkeypatch, target, fg).enter_target_is_safe() is True


@pytest.mark.parametrize("target, fg, why", [
    (None, 10, "no target this phase"),
    ("calculator", 11, "target not in front"),
    ("firefox", 12, "browser"),
    ("discord", 13, "messaging"),
    ("powershell", 14, "terminal"),
    ("mistai", 15, "MistAI itself"),
    ("mail", 16, "mail (UWP frame)"),
    ("notepad", 17, "title looks like a browser"),
])
def test_unknown_or_risky_targets_keep_the_prompt(monkeypatch, target, fg, why):
    assert _api(monkeypatch, target, fg).enter_target_is_safe() is False, why


# -- AgentLoop ------------------------------------------------------------------------


def _loop(monkeypatch, safe, replies):
    replies = iter(json.dumps(r) for r in replies)
    calls, asked = [], []
    api = SimpleNamespace(
        task_memory=SimpleNamespace(get_hints_for_task=lambda _t: []),
        caption_window=None, captions_enabled=False, _stop_requested=threading.Event(),
        live=SimpleNamespace(snapshot={"open_apps": [], "screen_resolution": (1, 1)}),
        api_manager=SimpleNamespace(get_headers=lambda: {},
                                    get_api_url=lambda: "https://example.invalid/a",
                                    get_fallback_url=lambda: "https://example.invalid/b"),
        tools=_registry(),
        executor=SimpleNamespace(run=lambda a, p: calls.append((a, p)) or SimpleNamespace(
            action=a, parameter=p, success=True, detail="", verified=None,
            escalate_to_user=False, reliability_note=lambda: "", escalated_from="")),
        memory=SimpleNamespace(add_action=lambda *a: None),
        _resolve_path_placeholders=lambda p: p,
        speak=lambda _s: None,
        _get_text_answer=lambda q, timeout=60: asked.append(q) or "no",
        enter_target_is_safe=lambda: safe,
    )
    monkeypatch.setattr("core.agent.requests.post",
                        lambda *a, **k: SimpleNamespace(ok=True, status_code=200,
                                                        json=lambda: {"response": next(replies)}))
    monkeypatch.setattr("core.agent.time.sleep", lambda _s: None)
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    monkeypatch.setattr(loop, "_get_screen_context", lambda *a, **k: "screen")
    monkeypatch.setattr(loop, "_current_foreground_key", lambda: (1, "W"))
    loop._loop("task", "m", "none", "text", None)
    return calls, asked


STEPS = [{"action": "press_key", "parameter": "enter"}, {"action": "done", "speech": "ok"}]


def test_loop_presses_enter_without_asking_in_a_safe_target(monkeypatch):
    calls, asked = _loop(monkeypatch, True, STEPS)
    assert calls == [("press_key", "enter")] and asked == []


def test_loop_asks_before_enter_otherwise(monkeypatch):
    calls, asked = _loop(monkeypatch, False, STEPS)
    assert calls == [] and len(asked) == 1  # harness-style "no" declines it


def test_loop_never_asks_for_plain_keys(monkeypatch):
    calls, asked = _loop(monkeypatch, False, [{"action": "press_key", "parameter": "="},
                                              {"action": "done", "speech": "ok"}])
    assert calls == [("press_key", "=")] and asked == []
