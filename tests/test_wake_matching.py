"""Text-layer wake-word harness: transcript -> wake / soft / none.

CURRENT pins what fuzzy_wake does today, including the command it hands on.
DESIRED lists real-log gaps (the wake loop heard "hey miss" when the user said
"hey mist") as strict xfails. When matching changes, they XPASS, which fails
the run, so whoever flips them has to move them into CURRENT on purpose.

"soft" (planned): the wake is accepted, but any inline command is dropped and
the user is asked "Yes?" first. fuzzy_wake has no soft tier yet, so
classify() returns only "wake" or "none" for now.
"""

import pytest

from core.config import fuzzy_wake


def classify(text):
    ww, _ = fuzzy_wake(text)
    return "wake" if ww else "none"


# (transcript, expected canonical wake word or None, expected command)
CURRENT = [
    # Exact wake phrases (tier 1).
    ("mist", "mist", ""),
    ("Mist, open calculator", "mist", "open calculator"),
    ("hey mist", "hey mist", ""),
    ("Hey Mist, open the calculator", "hey mist", "open the calculator"),
    ("mistai", "mistai", ""),
    ("mist ai open notepad", "mist ai", "open notepad"),
    ("hey mist stop", "hey mist", "stop"),
    # Listed STT alternatives (tier 1).
    ("myst open calculator", "mist", "open calculator"),
    ("mest", "mist", ""),
    ("hey myst", "hey mist", ""),
    ("hey mest", "hey mist", ""),
    ("misty ai what time is it", "mistai", "what time is it"),
    ("miss eye", "mistai", ""),
    # First-word near-miss of "mist" (tier 2, ratio >= 0.85).
    ("mst", "mist", ""),
    # Rejected today.
    ("mists", None, None),           # "mist" + letters is never tier 2
    ("miss", None, None),
    ("missed", None, None),
    ("miss open calculator", None, None),
    ("a mist", None, None),
    ("hello", None, None),
    ("stop", None, None),
    ("", None, None),
    ("hey must be nice", None, None),
    ("hey missed you", None, None),
    ("hey listen", None, None),
    ("hey mister", None, None),
    ("they missed it", None, None),
    # Real-log transcripts the matcher is not meant to rescue. A lone "hey"
    # is a segmentation problem (the quiet "mist" was trimmed from the clip),
    # and so is "amidst" (ratio 0.80 vs "mist", not hey-led).
    ("hey", None, None),
    ("amidst", None, None),
]


@pytest.mark.parametrize("text,wake,command", CURRENT)
def test_current_wake_matching(text, wake, command):
    assert fuzzy_wake(text) == (wake, command)


# Real-log gaps: a near-miss of "mist" right after "hey". Each should become a
# soft wake. "hey missed" / "hey must" are left out on purpose because they
# collide with the negatives "hey missed you" / "hey must be nice".
DESIRED_SOFT = [
    "hey miss",                    # real log
    "hey miss open calculator",    # soft: the inline command must be dropped
    "hey midst",
    "hey mists",
]


@pytest.mark.parametrize("text", DESIRED_SOFT)
@pytest.mark.xfail(strict=True, reason="documented gap: no soft wake after 'hey' yet")
def test_desired_soft_wake_after_hey(text):
    assert classify(text) == "soft"
