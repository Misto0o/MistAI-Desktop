"""quick_message recipient checks: vision is asked what the switcher / chat
header shows (never told the expected name or the message), the answer is
compared locally (config.recipient_match), and the header is read once more
immediately before the final Enter, which is never pressed unless it matches."""

import threading
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core.api import QM_HEADER_QUESTION, QM_SWITCHER_QUESTION, QM_UNCONFIRMED_REASON, Api
from core.config import recipient_match

RECIPIENT = "Zorblax Quindlewhip"
MESSAGE = "canary pelican rendezvous"


def _api(monkeypatch, answers):
    """A stub Api whose vision reads return `answers` in order (a string, or
    None for a failed read). Records keys, clipboard contents and prompts."""
    api = Api.__new__(Api)
    api.vision = SimpleNamespace(ready=True)
    api.screen = SimpleNamespace(marks=0)
    api.screen.mark_action = lambda: setattr(api.screen, "marks", api.screen.marks + 1)
    api.is_app_running = lambda _a: True
    api.switch_to_app = lambda _a: True
    api._get_app_search_hotkey = lambda _a: "ctrl+k"
    api.last_result_text = ""
    api.keys, api.clips, api.prompts = [], [], []
    api.set_clipboard = lambda t: api.clips.append(t) or True
    queue = list(answers)

    def vision_read(q):
        api.prompts.append(q)
        a = queue.pop(0) if queue else None
        return (a, True) if a is not None else ("Could not read anything meaningful.", False)

    api._vision_read = vision_read
    monkeypatch.setattr(api_mod.pyautogui, "press", lambda k: api.keys.append(k))
    monkeypatch.setattr(api_mod.pyautogui, "hotkey", lambda *k: api.keys.append("+".join(k)))
    monkeypatch.setattr(api_mod.time, "sleep", lambda _s: None)
    return api


def _send(api):
    return Api._act_quick_message(api, f"discord|{RECIPIENT}|{MESSAGE}")


def test_happy_path_sends_with_three_checks_and_neutral_prompts(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, RECIPIENT, RECIPIENT])
    assert _send(api) is True
    assert api.last_result_text.startswith("Sent to")
    assert api.prompts == [QM_SWITCHER_QUESTION, QM_HEADER_QUESTION, QM_HEADER_QUESTION]
    assert api.clips == [RECIPIENT, MESSAGE]
    assert api.keys.count("enter") == 2          # open the chat, then send
    assert api.keys[-1] == "enter"
    assert api._last_send_exact is True
    assert api.screen.marks == 3                 # each check reads a frame after the keys


def test_no_vision_prompt_contains_the_recipient_or_the_message(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, RECIPIENT, RECIPIENT])
    _send(api)
    for prompt in api.prompts:
        low = prompt.lower()
        assert "expected" not in low
        for word in RECIPIENT.lower().split() + MESSAGE.lower().split():
            assert word not in low, prompt


def test_switcher_mismatch_types_and_presses_nothing(monkeypatch):
    api = _api(monkeypatch, ["Zorblax Quindlewhip Fan Club"])
    assert _send(api) is False
    assert "enter" not in api.keys
    assert MESSAGE not in api.clips
    assert api.keys[-1] == "escape"
    assert len(api.prompts) == 1


def test_header_mismatch_types_nothing_and_never_sends(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, "Someone Else"])
    assert _send(api) is False
    assert MESSAGE not in api.clips
    assert api.keys.count("enter") == 1          # only the switcher's Enter
    assert "NOTHING was typed or sent" in api.last_result_text


def test_final_check_mismatch_never_presses_enter(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, RECIPIENT, "Someone Else"])
    assert _send(api) is False
    assert api.keys.count("enter") == 1
    assert api.keys[-1] == "ctrl+v"              # the paste was the last key; no Enter after it
    assert api.last_result_text.startswith(QM_UNCONFIRMED_REASON)
    assert "Zorblax" not in api.last_result_text and "Someone" not in api.last_result_text
    assert len(api.prompts) == 3                 # a mismatch is not retried


def test_final_check_unreadable_twice_fails_closed(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, RECIPIENT, None, "NOT VISIBLE ON SCREEN"])
    assert _send(api) is False
    assert api.keys.count("enter") == 1
    assert len(api.prompts) == 4
    assert api.last_result_text.startswith(QM_UNCONFIRMED_REASON)


def test_final_check_unreadable_once_then_readable_sends(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, RECIPIENT, None, RECIPIENT])
    assert _send(api) is True
    assert api.keys.count("enter") == 2
    assert len(api.prompts) == 4


def test_unreadable_header_before_typing_is_not_retried(monkeypatch):
    api = _api(monkeypatch, [RECIPIENT, None, RECIPIENT])
    assert _send(api) is False
    assert MESSAGE not in api.clips
    assert len(api.prompts) == 2


def test_a_close_match_sends_but_is_not_exact(monkeypatch):
    api = _api(monkeypatch, ["Zorblax Quindlewhip.", "Zorblax Quindlewip", "Zorblax Quindlewip"])
    assert _send(api) is True
    assert api._last_send_exact is False


def test_quotes_and_case_still_exact(monkeypatch):
    api = _api(monkeypatch, ['"zorblax quindlewhip"', "ZORBLAX  QUINDLEWHIP", RECIPIENT])
    assert _send(api) is True
    assert api._last_send_exact is True


@pytest.mark.parametrize("seen, expected, matched, exact", [
    ("Anna Bell", "anna bell", True, True),
    ("anna-bell!", "Anna Bell", True, False),    # punctuation removed, then compact-equal
    ("AnnaBell", "Anna Bell", True, False),      # compact-equal, not exact
    ("Jonathon Smith", "Jonathan Smith", True, False),   # ratio 0.92, long enough
    ("Kirsten", "Kristen", True, False),         # ratio 0.857 >= 0.85
    ("Markus", "Marcus", False, False),          # ratio 0.833 < 0.85
    ("Michelle", "Michael", False, False),       # ratio 0.80
    ("Anne", "Anna", False, False),              # compact length 4 < 5: no fuzzy accept
    ("Ann", "Anna", False, False),
    ("", "Anna", False, False),
    ("Anna", "", False, False),
    ("José", "jose", False, False),             # accents are letters, not punctuation; short
    ("Zoë Kravitz", "zoë kravitz", True, True),
])
def test_recipient_match_thresholds(seen, expected, matched, exact):
    assert recipient_match(seen, expected) == (matched, exact)


def test_logs_name_no_person_and_no_message(monkeypatch, capsys):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    for answers in ([RECIPIENT, RECIPIENT, RECIPIENT], [RECIPIENT, RECIPIENT, "Zorblax Q"],
                    [RECIPIENT, "Quindlewhip Fan"], [RECIPIENT, RECIPIENT, None, None]):
        _send(_api(monkeypatch, answers))
    out = capsys.readouterr()
    text = out.out + out.err
    assert "[QuickMessage] final check: exact" in text
    assert "[QuickMessage] final check failed; Enter not pressed" in text
    for word in ("Zorblax", "Quindlewhip", "pelican", "rendezvous"):
        assert word not in text
