"""Regression coverage for wake-word recognition and task cleanup."""

import pytest

from core.config import fuzzy_wake, strip_wake_prefix


@pytest.mark.parametrize(
    "utterance",
    [
        "list",
        "must",
        "most",
        "mint",
        "mistake",
        "dismiss",
        "optimistic",
    ],
)
def test_wake_detection_does_not_match_inside_or_near_other_words(utterance):
    assert fuzzy_wake(utterance) == (None, None)


@pytest.mark.parametrize(
    "utterance",
    [
        "I miss you",
        "missed the bus",
        "the midst of nowhere",
        "I walked through the mist today",
        "misty morning",
    ],
)
def test_wake_alternatives_do_not_turn_ordinary_speech_into_commands(utterance):
    assert fuzzy_wake(utterance) == (None, None)


def test_wake_is_detected_and_prefix_is_removed_before_planning():
    assert fuzzy_wake("Mist, open calculator") == ("mist", "open calculator")
    assert strip_wake_prefix("Hey Mist, open calculator") == "open calculator"


def test_prefix_stripping_only_removes_an_actual_leading_wake_phrase():
    command = "Please tell Mist hello when you see her"

    assert strip_wake_prefix(command) == command
