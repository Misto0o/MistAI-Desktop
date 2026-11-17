"""Clipboard reliability for type_text: retries, key-event fallback, guards,
content-free logging, watcher sequence gating and clipboard restore."""
import sys
from types import SimpleNamespace

import pytest

from core import clipboard as clip
from core.api import Api
from tests.test_windows import _input_api


@pytest.fixture
def keys(monkeypatch):
    """Record SendInput fallback calls instead of sending real keystrokes."""
    sent = []
    monkeypatch.setattr(clip, "send_unicode", lambda t: sent.append(t) or True)
    return sent


def _busy(api, results):
    results = list(results)
    api.sets = []

    def set_clipboard(text):
        api.sets.append(text)
        return results.pop(0) if results else False

    api.set_clipboard = set_clipboard


def test_busy_then_available_retries_and_pastes(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [False, False, True, True])
    assert Api._act_type_text(api, "hello") is True
    assert ("ctrl", "v") in events and keys == []
    assert api.sets[-1] == "old"                 # user's clipboard restored


def test_permanently_busy_short_text_uses_keys_and_says_so(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    assert Api._act_type_text(api, "37*23") is True
    assert keys == ["37*23"]
    assert ("ctrl", "v") not in events and "enter" not in [str(e).lower() for e in events]
    assert "keyboard events" in api.last_result_text
    assert set(api.sets) == {"37*23"}            # nothing staged -> nothing to restore


@pytest.mark.parametrize("text", ["x" * 201, "line one\nline two"])
def test_long_or_multiline_text_fails_safely(monkeypatch, keys, text):
    api, events = _input_api(monkeypatch, "notepad", 20)
    _busy(api, [])
    assert Api._act_type_text(api, text) is False
    assert keys == [] and events == []           # field untouched
    assert "left unchanged" in api.last_result_text


def test_keyboard_fallback_respects_the_input_target_guard(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 21)   # VS Code is in front
    _busy(api, [])
    assert Api._act_type_text(api, "hi") is False
    assert keys == [] and events == []
    assert "NOT SENT" in api.last_result_text


def test_retry_delays_are_short_jittered_and_capped():
    for _ in range(20):
        d = clip.retry_delays()
        assert d and sum(d) <= 1.5 and max(d) < 0.5
    assert len({tuple(clip.retry_delays()) for _ in range(5)}) > 1   # jitter


def _fake_win32(monkeypatch, fail_open=False):
    calls = []

    def open_():
        calls.append("open")
        if fail_open:
            raise RuntimeError("SECRET-HOLDER-NAME")
    fake = SimpleNamespace(OpenClipboard=open_, CloseClipboard=lambda: calls.append("close"),
                           EmptyClipboard=lambda: None, SetClipboardData=lambda *a: None,
                           IsClipboardFormatAvailable=lambda f: True,
                           GetClipboardData=lambda f: "clip text", CF_UNICODETEXT=13)
    monkeypatch.setitem(sys.modules, "win32clipboard", fake)
    return calls


@pytest.mark.parametrize("holder", ["self", "other", "unknown"])
def test_busy_log_has_only_the_holder_label(monkeypatch, capsys, holder):
    _fake_win32(monkeypatch, fail_open=True)
    monkeypatch.setattr(clip, "holder", lambda: holder)
    assert clip.set_text("my private text") is False
    out = capsys.readouterr().out
    assert out.strip() == f"[TypeText] clipboard busy holder={holder}"
    assert "private" not in out and "SECRET" not in out


def test_holder_is_self_while_we_have_it_open(monkeypatch):
    seen = []
    fake = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_text_of", lambda w: seen.append(clip.holder()) or "")
    clip.read_text()
    assert seen == ["self"] and fake == ["open", "close"]


def test_watcher_does_not_open_the_clipboard_when_the_sequence_is_unchanged(monkeypatch):
    calls = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_seq", lambda: 41)
    clip._cache_seq = None
    assert clip.peek_text() == "clip text" and calls == ["open", "close"]
    assert clip.peek_text() == "clip text" and calls == ["open", "close"]   # no 2nd open
    monkeypatch.setattr(clip, "_seq", lambda: 42)
    clip.peek_text()
    assert calls == ["open", "close"] * 2


def test_watcher_keeps_the_old_value_when_the_clipboard_is_busy(monkeypatch):
    clip._cache, clip._cache_seq = "kept", 1
    _fake_win32(monkeypatch, fail_open=True)
    monkeypatch.setattr(clip, "_seq", lambda: 2)
    assert clip.peek_text() == "kept"


def test_own_write_does_not_trigger_a_watcher_reread(monkeypatch):
    calls = _fake_win32(monkeypatch)
    monkeypatch.setattr(clip, "_seq", lambda: 7)
    assert clip.set_text("staged") is True
    calls.clear()
    assert clip.peek_text() == "staged" and calls == []


def test_previous_clipboard_is_restored_after_the_paste(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20, clipboard="user's plain text")
    assert Api._act_type_text(api, "hello") is True
    assert api.clip_sets == ["hello", "user's plain text"]


def test_previous_clipboard_is_restored_even_if_the_paste_fails(monkeypatch, keys):
    api, events = _input_api(monkeypatch, "notepad", 20, clipboard="keep me")
    monkeypatch.setattr("core.api.pyautogui.hotkey",
                        lambda *k: (_ for _ in ()).throw(RuntimeError("x")))
    assert Api._act_type_text(api, "hello") is False
    assert api.clip_sets[-1] == "keep me"
