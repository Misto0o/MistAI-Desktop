"""MISTAI_CONFIRM_LISTEN = shared (default) | ptt | lead: an off-by-default A/B
for voice confirmation and ask_user listens. The default is unchanged; the
other profiles use their own recognizer and only READ push-to-talk's stored
noise threshold."""

import json
import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core.api import Api
from core.config import confirm_listen_profile


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
