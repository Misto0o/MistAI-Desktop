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


# ============================================================
# DESTRUCTIVE GUARD AND AUDIT ISOLATION
# ============================================================
# The guard (core/guard.py) reads the real foreground window and focus
# before input actions, and the executor appends to the audit trail. Tests
# never touch the desktop or ~/MistAI: the probe returns "nothing known",
# the Api's switch-before-probe is a no-op, and the audit file is per test.
# Guard tests pass their own contexts to guard.classify or patch these.

@pytest.fixture(autouse=True)
def _guard_and_audit_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("MISTAI_UNATTENDED", raising=False)
    try:
        import core.guard as guard

        monkeypatch.setattr(guard, "read_ui_context", lambda *a, **k: guard.UIContext())
    except Exception:
        pass
    api_mod = sys.modules.get("core.api")
    if api_mod is not None and hasattr(api_mod.Api, "_guard_prepare"):
        monkeypatch.setattr(api_mod.Api, "_guard_prepare", lambda self, action: None)
