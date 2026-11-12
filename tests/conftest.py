"""Keep test imports stable for both ``pytest`` and ``python -m pytest``."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest


@pytest.fixture(autouse=True)
def _first_read_uses_vision(monkeypatch):
    """MISTAI_LAZY_VISION's default is 2 (a phase's first read is title + Open
    apps). The agent-loop tests were written for a vision first read; they keep
    it unless they set the variable. Tests that delenv it see the real default."""
    monkeypatch.setenv("MISTAI_LAZY_VISION", "0")
