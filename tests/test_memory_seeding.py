"""Seeding fills gaps only: existing values win and restarts add no duplicates."""

from core.memory import Memory


def test_seeding_fills_gaps_without_overwriting_or_duplicating(monkeypatch, tmp_path):
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "m.db"))
    mem = Memory()
    try:
        mem.set_profile("hotkey_slack", "ctrl+k")  # learned by the user
        facts = [("MistAI runs on Windows", "system", 3), ("Second fact here", "system", 1)]
        for _ in range(2):  # two "startups"
            mem.seed_defaults({"hotkey_slack": "ctrl+g", "hotkey_discord": "ctrl+k"})
            mem.seed_facts(facts)
        assert mem.get_profile("hotkey_slack") == "ctrl+k"
        assert mem.get_profile("hotkey_discord") == "ctrl+k"
        assert sorted(mem.recall_facts(category="system")) == sorted(f[0] for f in facts)
        assert mem.recall_facts(query="windows runs") == ["MistAI runs on Windows", "Second fact here"]
    finally:
        mem.conn.close()
