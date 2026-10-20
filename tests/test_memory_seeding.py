"""Nothing is seeded, and the Facts count is true.

Startup writes no rows: built-in hotkeys and MistAI's facts about itself are
read from code (config.DEFAULT_HOTKEYS, config.MISTAI_FACTS). Facts count and
recall only what the user saved (remember) or imported (Import my facts);
rows an older version seeded stay in old databases but are ignored.
"""

import inspect
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

import core.api as api_mod
from core import config
from core.api import Api
from core.memory import Memory, recall_builtin
from core.task_memory import TaskMemory
from core.task_store import TaskStore
from core.tools import ToolRegistry

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row_counts(path):
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


def _close(*owners):
    for o in owners:
        conn = getattr(o, "conn", None) or getattr(o, "_conn", None)
        if conn is not None:
            conn.close()


@pytest.fixture
def db(monkeypatch, tmp_path):
    path = str(tmp_path / "memory.db")
    monkeypatch.setenv("MISTAI_DB_PATH", path)
    return path


@pytest.fixture
def api(db):
    a = Api.__new__(Api)
    a.memory = Memory()
    yield a
    _close(a.memory)


# -- startup writes nothing -----------------------------------------------------------

def test_a_fresh_database_has_no_rows_after_startup(db):
    """Every store the app opens at startup (Api.__init__ opens these four)."""
    owners = [Memory(), TaskMemory(), TaskStore(), ToolRegistry()]
    try:
        counts = _row_counts(db)
        assert set(counts) >= {"conversations", "actions", "user_profile", "facts",
                               "reminders", "task_outcomes", "tasks"}
        assert all(n == 0 for n in counts.values()), counts
    finally:
        _close(*owners)


def _repo_sources():
    """Every Python file of the app, its tools and tests, plus the build spec."""
    for top in ("core", "ui", "tools", "tests"):
        for root, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            yield from (os.path.join(root, f) for f in files if f.endswith(".py"))
    yield os.path.join(REPO, "assistant.py")
    yield os.path.join(REPO, "MistAI.spec")


def test_startup_and_the_harness_seed_nothing():
    init = inspect.getsource(Api.__init__)
    assert "seed" not in init.replace("Nothing is seeded", "")
    assert not hasattr(Memory, "seed_defaults") and not hasattr(Memory, "seed_facts")
    # core/seed_data.py was moved out of the repo; nothing may import it.
    assert not os.path.exists(os.path.join(REPO, "core", "seed_data.py"))
    here = os.path.abspath(__file__)
    for path in _repo_sources():
        if os.path.abspath(path) == here:
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for name in ("seed_data", "seed_defaults", "seed_facts", "SEED_FACTS"):
            assert name not in text, f"{os.path.relpath(path, REPO)} mentions {name}"


def test_the_scenario_harness_reset_leaves_an_empty_database(db, tmp_path):
    from tools.run_scenarios import reset_session

    fake = SimpleNamespace()
    reset_session(fake, str(tmp_path / "scenario.db"))
    try:
        assert all(n == 0 for n in _row_counts(str(tmp_path / "scenario.db")).values())
    finally:
        _close(fake.memory, fake.task_memory, fake.task_store, fake.tools)


# -- old seeded rows are kept but ignored -----------------------------------------------

def _old_seeded_rows(memory):
    with memory.conn:
        memory.conn.executemany(
            "INSERT INTO facts (category,content,importance,keywords,timestamp) "
            "VALUES (?,?,?,?,'2026-01-01T00:00:00')",
            [("system", "MistAI old system fact", 2, "mistai system fact"),
             ("personal", "The user's favorite color is green", 3, "user's favorite color green"),
             ("work", "The user works on robotics", 3, "user works robotics")])


def test_seeded_rows_are_not_counted_or_recalled(api):
    _old_seeded_rows(api.memory)
    assert api.memory.get_stats()["facts"] == 0
    assert api.memory.recall_facts(query="favorite color") == []
    assert api.memory.recall_facts(category="personal") == []
    assert api.memory.recall_facts() == []
    assert api.recall_facts(query="favorite color") == []      # no fallback match either
    assert api.memory.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3  # kept


def test_remembered_facts_count_and_are_recalled_first(api):
    _old_seeded_rows(api.memory)
    api.remember_fact("The garage code is in the blue notebook")
    assert api.memory.get_stats()["facts"] == 1
    assert api.recall_facts(query="where is the garage code") == [
        "The garage code is in the blue notebook"]


# -- MistAI's facts about itself come from code ---------------------------------------

def test_recall_answers_what_are_you_from_the_in_code_facts(api):
    api.remember_fact("The garage code is in the blue notebook")   # an unrelated fact
    first = config.MISTAI_FACTS[0][0]
    assert api.recall_facts(query="what are you") [0] == first
    assert api.recall_facts(query="what can you do")[0] == first
    assert "wake words" in api.recall_facts(query="what are your wake words")[0]
    assert api.memory.get_stats()["facts"] == 1           # only the saved one counts


def test_the_in_code_facts_answer_only_questions_about_them():
    assert recall_builtin("my favorite color") == []
    assert recall_builtin("") == []


# -- Import my facts --------------------------------------------------------------------

@pytest.fixture
def facts_file(monkeypatch, tmp_path):
    path = tmp_path / "my_facts.json"
    monkeypatch.setenv("MISTAI_MY_FACTS_PATH", str(path))
    return path


def test_import_adds_new_facts_once_and_skips_duplicates_across_categories(
        api, facts_file, capsys):
    _old_seeded_rows(api.memory)
    facts_file.write_text(json.dumps([
        "My dentist is Dr. Lee",
        {"content": "I park on level 3"},
        "my dentist is  DR. LEE",                 # the same fact twice in the file
        "the user's FAVORITE color is green",     # already an (old seeded) row
        "   ", 7, None,                           # not facts
    ]), encoding="utf-8")
    first = api.import_my_facts()
    # The seeded line differs in case from the old row, so it is not EXACTLY
    # that row: skipped, not restored.
    assert first == {"success": True, "added": 2, "skipped": 2, "restored": 0,
                     "message": "Imported 2 facts (2 already saved, 0 restored)."}
    assert api.memory.get_stats()["facts"] == 2
    assert api.recall_facts(query="where do I park") == ["I park on level 3"]

    again = api.import_my_facts()
    assert again["added"] == 0 and again["skipped"] == 4
    assert api.memory.get_stats()["facts"] == 2
    out = capsys.readouterr().out
    assert "Imported 2 facts (2 already saved, 0 restored)." in out
    assert "dentist" not in out.lower() and "park" not in out.lower()   # never logs facts


def _facts_table(memory):
    return memory.conn.execute(
        "SELECT id, category, content, importance, keywords, timestamp FROM facts ORDER BY id"
    ).fetchall()


def test_an_exact_seeded_duplicate_is_restored_then_counted_and_recalled(api, facts_file):
    _old_seeded_rows(api.memory)
    copy = "The user's favorite color is green"
    with api.memory.conn:                       # the old bug left extra copies
        api.memory.conn.executemany(
            "INSERT INTO facts (category,content,importance,keywords,timestamp) "
            "VALUES ('personal',?,3,'user''s favorite color green','2026-02-01T00:00:00')",
            [(copy,), (copy,)])
    before = _facts_table(api.memory)
    facts_file.write_text(json.dumps([copy, "I park on level 3"]), encoding="utf-8")

    r = api.import_my_facts()
    assert r["message"] == "Imported 1 fact (0 already saved, 1 restored)."
    assert (r["added"], r["skipped"], r["restored"]) == (1, 0, 1)
    after = _facts_table(api.memory)
    assert len(after) == len(before) + 1                       # nothing deleted
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    assert len(changed) == 1                                   # ONE row, the oldest copy
    old, new = changed[0]
    assert old[0] == min(row[0] for row in before if row[2] == copy)
    assert new == (old[0], "imported") + old[2:]               # only the category changed
    assert [row[1] for row in after if row[2] == copy] == ["imported", "personal", "personal"]

    assert api.memory.get_stats()["facts"] == 2                # restored + the new one
    assert api.recall_facts(query="what is my favorite color") == [copy]

    again = api.import_my_facts()                              # nothing changes twice
    assert (again["added"], again["skipped"], again["restored"]) == (0, 2, 0)
    assert _facts_table(api.memory) == after


def test_a_fresh_database_only_gets_inserts(api, facts_file):
    facts_file.write_text(json.dumps(["I park on level 3", "My dentist is Dr. Lee"]),
                          encoding="utf-8")
    r = api.import_my_facts()
    assert (r["added"], r["skipped"], r["restored"]) == (2, 0, 0)
    assert [row[1] for row in _facts_table(api.memory)] == ["imported", "imported"]


def test_a_missing_facts_file_is_a_plain_message(api, facts_file):
    r = api.import_my_facts()
    assert r["success"] is True and r["added"] == 0
    assert r["message"].startswith("No facts file found. To import facts, create ")
    assert str(facts_file) in r["message"]


@pytest.mark.parametrize("content, success, message_start", [
    ("{not json", False, "Could not read "),
    ('{"a": 1}', True, None),
    ("[]", True, None),
])
def test_unreadable_or_empty_files(api, facts_file, content, success, message_start):
    facts_file.write_text(content, encoding="utf-8")
    r = api.import_my_facts()
    assert r["success"] is success and r["added"] == 0
    if message_start:
        assert r["message"].startswith(message_start)
    else:
        assert r["message"].endswith("has no facts to import (expected a JSON list).")
    assert api.memory.get_stats()["facts"] == 0


def test_the_facts_file_lives_outside_the_repo_and_is_ignored(monkeypatch):
    monkeypatch.delenv("MISTAI_MY_FACTS_PATH", raising=False)
    path = os.path.abspath(config.my_facts_path())
    assert not path.startswith(REPO + os.sep)
    assert path.endswith(os.path.join("MistAI", "my_facts.json"))
    with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as f:
        assert "my_facts.json" in f.read().splitlines()


# -- the hotkey defaults are an in-code fallback ----------------------------------------

def test_hotkey_fallback_learned_value_wins_and_unknown_apps_ask_once(api, monkeypatch):
    asked = []
    api._ask_llm_for_hotkey = lambda app: (asked.append(app), "ctrl+shift+j")[1]
    assert api._get_app_search_hotkey("Discord") == "ctrl+k"          # from code
    assert asked == [] and api.memory.get_profile("hotkey_discord") is None   # no row
    api.memory.set_profile("hotkey_discord", "ctrl+t")               # learned wins
    assert api._get_app_search_hotkey("discord") == "ctrl+t"
    assert api._get_app_search_hotkey("SomeNewChat") == "ctrl+shift+j"
    assert asked == ["somenewchat"]
    assert api.memory.get_profile("hotkey_somenewchat") == "ctrl+shift+j"
