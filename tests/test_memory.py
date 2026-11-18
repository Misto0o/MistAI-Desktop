# ============================================================
# MEMORY AND WORKSPACE TESTS
# ============================================================
#
# Memory, stored content and project/workspace safety.
#
# SECTION INDEX
#   1. MEMORY
#   2. MEMORY HELPERS
#   3. MEMORY ROW COUNTS
#   4. MEMORY HELPERS (2)
#   5. MEMORY TESTS
#   6. MEMORY REPO SOURCES
#   7. MEMORY TESTS (2)
#   8. OLD SEEDED ROWS
#   9. MEMORY TESTS (3)
#   10. MEMORY HELPERS (3)
#   11. MEMORY TESTS (4)
#   12. MEMORY HELPERS (4)
#   13. MEMORY TESTS (5)
#   14. STORED CONTENT
#   15. STORED CONTENT HELPERS
#   16. STORED CONTENT ALL ROWS
#   17. STORED CONTENT REGISTRY
#   18. STORED CONTENT EXECUTOR
#   19. STORED CONTENT TESTS
#   20. STORED CONTENT SEED
#   21. STORED CONTENT MESSAGES
#   22. STORED CONTENT TESTS (2)
#   23. STORED CONTENT STOP INIT
#   24. STORED CONTENT START API
#   25. STORED CONTENT HELPERS (2)
#   26. STORED CONTENT TESTS (3)
#   27. PROJECT STATE SAFETY
#   28. PROJECT STATE SAFETY BUS
#   29. API WITH WORKSPACE
#   30. PROJECT STATE SAFETY TESTS
#   31. PROJECT STATE SAFETY HELPERS
#   32. PROJECT STATE SAFETY TESTS (2)
# ============================================================

"""Memory, stored content and project/workspace safety."""

import inspect
import json
import os
import sqlite3
from types import SimpleNamespace
import sys
from datetime import datetime, timedelta
import subprocess
import threading

import pytest

import core.api as api_mod
from core import config
from core.api import Api
from core.memory import Memory, recall_builtin, TaskMemory, TaskStore, OUTCOME_NOTES
from core.tools import ToolRegistry, ToolSpec
import core.browser_session as browser_session
import core.config as config
from core.agent import AgentLoop
from core.executor import Executor, ExecutionResult
from confirm_helpers import bind_confirmation
from core.planner import make_plan
from core.workspace import Workspace


# ============================================================
# MEMORY
# ============================================================

# Nothing is seeded, and the Facts count is true.
#
# Startup writes no rows: built-in hotkeys and MistAI's facts about itself are
# read from code (config.DEFAULT_HOTKEYS, config.MISTAI_FACTS). Facts count and
# recall only what the user saved (remember) or imported (Import my facts);
# rows an older version seeded stay in old databases but are ignored.
#
# Merged by area (pure moves, no test changed) from: test_memory_seeding.py, test_task_memory_regressions.py.


# ============================================================
# MEMORY HELPERS
# ============================================================

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ============================================================
# MEMORY ROW COUNTS
# ============================================================

def _row_counts(path):
    con = sqlite3.connect(path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        con.close()


# ============================================================
# MEMORY HELPERS (2)
# ============================================================

def _close(*owners):
    for o in owners:
        conn = getattr(o, "conn", None) or getattr(o, "_conn", None)
        if conn is not None:
            conn.close()


@pytest.fixture
def memory_db(monkeypatch, tmp_path):
    path = str(tmp_path / "memory.db")
    monkeypatch.setenv("MISTAI_DB_PATH", path)
    return path


@pytest.fixture
def api(memory_db):
    a = Api.__new__(Api)
    a.memory = Memory()
    yield a
    _close(a.memory)


# -- startup writes nothing -----------------------------------------------------------


# ============================================================
# MEMORY TESTS
# ============================================================

def test_a_fresh_database_has_no_rows_after_startup(memory_db):
    """Every store the app opens at startup (Api.__init__ opens these four)."""
    owners = [Memory(), TaskMemory(), TaskStore(), ToolRegistry()]
    try:
        counts = _row_counts(memory_db)
        assert set(counts) >= {"conversations", "actions", "user_profile", "facts",
                               "reminders", "task_outcomes", "tasks"}
        assert all(n == 0 for n in counts.values()), counts
    finally:
        _close(*owners)


# ============================================================
# MEMORY REPO SOURCES
# ============================================================

def _repo_sources():
    """Every Python file of the app, its tools and tests, plus the build spec."""
    for top in ("core", "ui", "tools", "tests"):
        for root, dirs, files in os.walk(os.path.join(REPO, top)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            yield from (os.path.join(root, f) for f in files if f.endswith(".py"))
    yield os.path.join(REPO, "assistant.py")
    yield os.path.join(REPO, "MistAI.spec")


# ============================================================
# MEMORY TESTS (2)
# ============================================================

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


def test_the_scenario_harness_reset_leaves_an_empty_database(memory_db, tmp_path):
    from tools.run_scenarios import reset_session

    fake = SimpleNamespace()
    reset_session(fake, str(tmp_path / "scenario.db"))
    try:
        assert all(n == 0 for n in _row_counts(str(tmp_path / "scenario.db")).values())
    finally:
        _close(fake.memory, fake.task_memory, fake.task_store, fake.tools)


# -- old seeded rows are kept but ignored -----------------------------------------------


# ============================================================
# OLD SEEDED ROWS
# ============================================================

def _old_seeded_rows(memory):
    with memory.conn:
        memory.conn.executemany(
            "INSERT INTO facts (category,content,importance,keywords,timestamp) "
            "VALUES (?,?,?,?,'2026-01-01T00:00:00')",
            [("system", "MistAI old system fact", 2, "mistai system fact"),
             ("personal", "The user's favorite color is green", 3, "user's favorite color green"),
             ("work", "The user works on robotics", 3, "user works robotics")])


# ============================================================
# MEMORY TESTS (3)
# ============================================================

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


# ============================================================
# MEMORY HELPERS (3)
# ============================================================

@pytest.fixture
def facts_file(monkeypatch, tmp_path):
    path = tmp_path / "my_facts.json"
    monkeypatch.setenv("MISTAI_MY_FACTS_PATH", str(path))
    return path


# ============================================================
# MEMORY TESTS (4)
# ============================================================

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


# ============================================================
# MEMORY HELPERS (4)
# ============================================================

def _facts_table(memory):
    return memory.conn.execute(
        "SELECT id, category, content, importance, keywords, timestamp FROM facts ORDER BY id"
    ).fetchall()


# ============================================================
# MEMORY TESTS (5)
# ============================================================

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


# ==========================================================================
# moved from tests/test_task_memory_regressions.py
# TaskMemory must be usable in tests and avoid substring-based hints.
# ==========================================================================

def test_memory_database_target_is_usable_without_a_parent_directory():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "Code", False)

        hints = memory.get_hints_for_task("Please open Code.")

        assert len(hints) == 1
        assert "code" in hints[0].lower()
    finally:
        memory.conn.close()


def test_task_memory_matches_signature_keys_as_words_not_substrings():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "code", False)

        assert memory.get_hints_for_task("Decode this base64 string.") == []
        assert memory.get_hints_for_task("Can you open code for me?")
    finally:
        memory.conn.close()


def test_task_memory_matches_normalized_multiword_app_names():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "Apple Music", False)

        hints = memory.get_hints_for_task("Open Apple Music, please.")

        assert len(hints) == 1
        assert "applemusic" in hints[0].lower()
    finally:
        memory.conn.close()


def test_task_store_in_memory_database_needs_no_parent_directory():
    store = TaskStore(":memory:")
    try:
        record = store.create("Test", "A test goal", {"nodes": []})

        assert store.get(record.task_id).goal == "A test goal"
    finally:
        store.conn.close()


# ============================================================
# STORED CONTENT
# ============================================================

# A3: what MistAI stores in memory.db.
#
# - actions: (action, success, timestamp) only; the parameter column is empty.
# - task_outcomes.note: a fixed phrase per outcome kind, never result text.
# - app_usage: no longer written.
# - conversations: rows older than MISTAI_HISTORY_HOURS (default 24) are
#   deleted at startup and on insert; nothing newer is touched.
# - the verbose-logs banner prints when the Api starts.


# ============================================================
# STORED CONTENT HELPERS
# ============================================================

TYPED = "my confidential diary entry"
CONTACT = "Joanne Quillfeather"
MESSAGE = "meet me at the lighthouse pier"
COMMAND = "Get-Content marblewood-secrets.txt"
TITLE = "Quillfeather diary - Notepad"
CONTENT_WORDS = ("confidential", "diary", "joanne", "quillfeather", "lighthouse",
                 "pier", "marblewood")


@pytest.fixture
def stored_content_db(tmp_path, monkeypatch):
    path = str(tmp_path / "memory.db")
    monkeypatch.setenv("MISTAI_DB_PATH", path)
    monkeypatch.delenv("MISTAI_HISTORY_HOURS", raising=False)
    return path


# ============================================================
# STORED CONTENT ALL ROWS
# ============================================================

def _all_rows(path):
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    finally:
        conn.close()


# ============================================================
# STORED CONTENT REGISTRY
# ============================================================

class _Registry:
    def __init__(self, specs):
        self.specs = specs

    def get(self, action):
        return self.specs[action]

    def settle(self, action):
        return 0

    def is_destructive(self, *_a, **_k):
        return False


# ============================================================
# STORED CONTENT EXECUTOR
# ============================================================

def _executor(db_path, outcomes):
    memory, task_memory = Memory(), TaskMemory(db_path)
    api = SimpleNamespace(
        tools=_Registry({a: ToolSpec(name=a, settle=0) for a in outcomes}),
        memory=memory, task_memory=task_memory,
        workspace=SimpleNamespace(record_execution=lambda *a: None),
        bus=SimpleNamespace(publish=lambda *a: None),
        uia_nav=SimpleNamespace(invalidate_cache=lambda: None),
        click_handler=SimpleNamespace(last_method=None),
        live=SimpleNamespace(snapshot={"foreground_window": ""}),
        last_result_text="",
    )

    def exec_sync(action, param):
        # Result text quoting the parameter and a window title, as handlers do.
        api.last_result_text = f"{action} with '{param}' in window '{TITLE}'"
        return outcomes[action]

    api._exec_sync = exec_sync
    return Executor(api), memory, task_memory


# ============================================================
# STORED CONTENT TESTS
# ============================================================

def test_typed_text_a_message_and_a_command_never_reach_a_stored_row(stored_content_db, monkeypatch):
    monkeypatch.setattr("core.executor.time.sleep", lambda _s: None)
    ex, memory, task_memory = _executor(stored_content_db, {
        "type_text": True, "quick_message": True, "run_command": True,
        "click_on_text": False, "open_app": False,
    })
    ex.run("type_text", TYPED)
    ex.run("quick_message", f"Discord|{CONTACT}|{MESSAGE}")
    ex.run("run_command", COMMAND)
    ex.run("click_on_text", "Send to the pier")
    ex.run("open_app", "Marblewood Diary")
    memory.conn.close()
    task_memory.conn.close()

    rows = _all_rows(stored_content_db)
    assert [(a, p, s) for _id, a, p, s, _t in rows["actions"]] == [
        ("type_text", "", 1), ("quick_message", "", 1), ("run_command", "", 1),
        ("click_on_text", "", 0), ("open_app", "", 0)]
    notes = {sig: note for sig, _c, _a, _sc, _fc, _ls, note, _u in rows["task_outcomes"]}
    assert notes == {"message:discord": "sent", "click:click_on_text": "click failed",
                     "open:marblewooddiary": "did not open"}
    assert rows["app_usage"] == []
    # Nothing typed, sent, run or read back is anywhere else either. (The
    # open_app signature keys on the app name, which is not content.)
    stored = repr({t: r for t, r in rows.items() if t != "task_outcomes"}).lower()
    assert [w for w in CONTENT_WORDS if w in stored] == []
    assert "confidential" not in repr(rows["task_outcomes"]).lower()
    assert TITLE.lower() not in repr(rows).lower()


def test_every_outcome_kind_has_a_fixed_note():
    assert {k[0] for k in OUTCOME_NOTES} == {"open_app", "messaging", "clicking"}
    assert all(len(v) < 20 for v in OUTCOME_NOTES.values())


def test_hints_still_work_from_counts_and_fixed_notes(stored_content_db):
    tm = TaskMemory(stored_content_db)
    tm.record("open_app", "Marblewood", False)
    tm.record("quick_message", f"Discord|{CONTACT}|{MESSAGE}", True)
    tm.record("quick_message", f"Discord|{CONTACT}|{MESSAGE}", True)
    for _ in range(2):
        tm.record("click_on_text", "Settings", False)
    hints = tm.get_hints_for_task("open marblewood, message on discord, click settings")
    tm.conn.close()
    assert any("'marblewood'" in h and "FAILED every time (did not open)" in h for h in hints)
    assert any("'discord'" in h and "quick_message" in h and "2 times" in h for h in hints)
    # A click row names the method, not the target: no per-target click hint.
    assert not any("Clicking" in h for h in hints)


def test_reliability_still_learns_from_the_actions_table(stored_content_db):
    memory = Memory()
    for ok in (False, False, False, False, True):
        memory.add_action("click_on_text", ok)
    reg = ToolRegistry(stored_content_db)
    assert reg.observed("click_on_text") == (5, 0.2)
    base = reg.get("click_on_text").base_reliability
    assert reg.reliability("click_on_text") == round(0.35 * base + 0.65 * 0.2, 3)
    memory.conn.close()
    reg._conn.close()


def test_app_usage_has_no_writer():
    import inspect
    assert "log_app_usage" not in inspect.getsource(api_mod)


# -- conversation expiry ------------------------------------------------------------------


# ============================================================
# STORED CONTENT SEED
# ============================================================

def _seed(path, ages_hours):
    """Rows aged as given (hours before now), oldest first."""
    Memory().conn.close()                                   # schema
    conn = sqlite3.connect(path)
    now = datetime.now()
    conn.executemany(
        "INSERT INTO conversations (role,message,timestamp) VALUES (?,?,?)",
        [("user", f"turn {h}", (now - timedelta(hours=h)).isoformat()) for h in ages_hours])
    conn.commit()
    conn.close()


# ============================================================
# STORED CONTENT MESSAGES
# ============================================================

def _messages(path):
    conn = sqlite3.connect(path)
    try:
        return [m for (m,) in conn.execute("SELECT message FROM conversations ORDER BY id")]
    finally:
        conn.close()


# ============================================================
# STORED CONTENT TESTS (2)
# ============================================================

def test_startup_deletes_only_rows_older_than_24_hours(stored_content_db):
    _seed(stored_content_db, [72, 25, 23.9, 0.4, 0])
    Memory().conn.close()
    assert _messages(stored_content_db) == ["turn 23.9", "turn 0.4", "turn 0"]


def test_an_insert_prunes_too_and_the_window_is_read_at_call_time(stored_content_db, monkeypatch):
    _seed(stored_content_db, [5, 1.5])
    memory = Memory()
    assert len(_messages(stored_content_db)) == 2                       # both within 24h
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "2")
    memory.add_conversation("assistant", "fresh")
    assert _messages(stored_content_db) == ["turn 1.5", "fresh"]
    memory.conn.close()


@pytest.mark.parametrize("raw, hours", [
    ("48", 48.0), ("0.25", 1.0), ("0", 1.0), ("-5", 1.0), ("soon", 24.0), ("nan", 24.0)])
def test_history_hours_parsing(monkeypatch, raw, hours):
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", raw)
    assert config.history_hours() == hours


def test_infinite_history_keeps_everything(stored_content_db, monkeypatch):
    _seed(stored_content_db, [24 * 400])
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "inf")
    Memory().conn.close()
    assert _messages(stored_content_db) == [f"turn {24 * 400}"]


def test_the_agents_30_minute_block_is_unaffected(stored_content_db, monkeypatch):
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "0.1")    # clamped to 1h
    _seed(stored_content_db, [0.9, 0.3, 0.1])
    memory = Memory()
    block = AgentLoop(SimpleNamespace(memory=memory), None)._recent_conversation_block("x")
    memory.conn.close()
    assert "turn 0.3" in block and "turn 0.1" in block and "turn 0.9" not in block
    assert len(_messages(stored_content_db)) == 3


# -- the verbose-logs banner prints when the Api starts --------------------------------


# ============================================================
# STORED CONTENT STOP INIT
# ============================================================

class _StopInit(Exception):
    pass


# ============================================================
# STORED CONTENT START API
# ============================================================

def _start_api(monkeypatch):
    """Run Api.__init__ up to its first subsystem, then stop."""
    def boom(*_a, **_k):
        raise _StopInit()
    monkeypatch.setattr(browser_session, "BrowserSession", boom)
    with pytest.raises(_StopInit):
        api_mod.Api()


# ============================================================
# STORED CONTENT HELPERS (2)
# ============================================================

@pytest.fixture
def fresh_banner(monkeypatch):
    monkeypatch.setattr(config, "_verbose_banner_shown", False)
    monkeypatch.delattr(sys, "frozen", raising=False)


# ============================================================
# STORED CONTENT TESTS (3)
# ============================================================

def test_the_banner_prints_once_at_api_start(monkeypatch, capsys, fresh_banner):
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    _start_api(monkeypatch)
    assert capsys.readouterr().out == "verbose logs: on, logs contain content\n"
    _start_api(monkeypatch)
    config.verbose_logs()
    assert "verbose logs" not in capsys.readouterr().out


def test_no_banner_when_off_or_frozen(monkeypatch, capsys, fresh_banner):
    monkeypatch.delenv("MISTAI_VERBOSE_LOGS", raising=False)
    _start_api(monkeypatch)
    monkeypatch.setenv("MISTAI_VERBOSE_LOGS", "1")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    _start_api(monkeypatch)
    assert "verbose logs" not in capsys.readouterr().out


# -- click signatures: the method only ------------------------------------------------


@pytest.mark.parametrize("action", ["click_on_text", "uia_click", "click_nth"])
def test_a_click_on_a_contact_name_stores_no_part_of_the_name(stored_content_db, action):
    tm = TaskMemory(stored_content_db)
    tm.record(action, CONTACT, True)
    tm.record(action, "Discord|" + CONTACT, False)
    tm.record(action, "", False)
    tm.conn.close()
    rows = _all_rows(stored_content_db)["task_outcomes"]
    assert [(sig, cat, sc, fc) for sig, cat, _a, sc, fc, _ls, _n, _u in rows] == [
        (f"click:{action}", "clicking", 1, 2)]
    assert "joanne" not in repr(rows).lower() and "quillfeather" not in repr(rows).lower()


def test_click_counts_add_up_per_method(stored_content_db):
    tm = TaskMemory(stored_content_db)
    for target, ok in (("Send", True), ("Cancel", False), (CONTACT, True)):
        tm.record("click_on_text", target, ok)
    tm.record("uia_click", "Settings", False)
    counts = {sig: (sc, fc) for sig, sc, fc in tm.conn.execute(
        "SELECT signature, success_count, fail_count FROM task_outcomes")}
    tm.conn.close()
    assert counts == {"click:click_on_text": (2, 1), "click:uia_click": (0, 1)}


def test_old_click_rows_are_kept_and_still_hint_new_ones_never_do(stored_content_db):
    tm = TaskMemory(stored_content_db)
    tm.conn.execute(
        "INSERT INTO task_outcomes (signature, category, preferred_action, success_count, "
        "fail_count, last_success, note, updated) VALUES "
        "('click:settings', 'clicking', 'click_on_text', 0, 3, 0, 'click failed', 'x')")
    tm.conn.commit()
    for _ in range(3):
        tm.record("click_on_text", "Settings", False)
    hints = tm.get_hints_for_task("click settings, then click_on_text")
    sigs = sorted(r[0] for r in tm.conn.execute("SELECT signature FROM task_outcomes"))
    tm.conn.close()
    assert sigs == ["click:click_on_text", "click:settings"]     # the old row is untouched
    assert hints == ["- Clicking 'settings' via OCR/UIA has failed more than it's "
                     "succeeded. Consider verifying with read_screen_text first."]


# ============================================================
# PROJECT STATE SAFETY
# ============================================================

# Regression coverage for existing-project state and safe mutation paths.


# ============================================================
# PROJECT STATE SAFETY BUS
# ============================================================

class _Bus:
    def __init__(self):
        self.events = []

    def subscribe(self, *_args):
        return lambda: None

    def publish(self, *args):
        self.events.append(args)


# ============================================================
# API WITH WORKSPACE
# ============================================================

def _api_with_workspace(tmp_path):
    api = Api.__new__(Api)
    api.workspace = Workspace(_Bus())
    api.last_result_text = ""
    api.last_command_result = None
    api._workspace_required_for_task = False
    api._trusted_project_roots = set()
    api._resolve_path_placeholders = lambda path: str(path)
    return api


# ============================================================
# PROJECT STATE SAFETY TESTS
# ============================================================

def test_explicit_existing_project_is_scanned_before_planning(tmp_path, monkeypatch):
    for name, content in {
        "index.html": "<main>portfolio</main>",
        "style.css": "main { color: red; }",
        "script.js": "console.log('portfolio')",
        "animate.js": "export const animate = true",
        "package.json": '{"scripts":{"dev":"vite"},"dependencies":{"animejs":"1"}}',
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")

    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'convert workspace at "{tmp_path}" to React')
    context = api.workspace.context_block()
    assert "Project files (already exist -- do NOT recreate)" in context
    assert "index.html" in context and "package.json" in context
    assert "dependencies: animejs" in context

    captured = {}

    def fake_llm(_api, prompt, _model):
        captured["prompt"] = prompt
        return '{"task_name":"PortfolioMigration","nodes":[{"id":"migrate","desc":"Migrate the existing portfolio","depends_on":[]}]}'

    monkeypatch.setattr("core.planner._llm", fake_llm)
    plan, _ = make_plan(api, "convert this existing website to React", "test")
    assert plan.nodes["migrate"].desc == "Migrate the existing portfolio"
    assert "index.html" in captured["prompt"]
    assert "package.json" in captured["prompt"]
    assert "dependencies: animejs" in captured["prompt"]


def test_existing_folder_is_not_inferred_without_explicit_or_vscode_evidence(tmp_path, monkeypatch):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(api, "_vscode_workspace_path", lambda: None)

    assert not Api._establish_workspace_for_task(api, "convert portfolio website to React")
    assert api.workspace.project_root is None


def test_explicit_project_path_persists_and_allows_mutation_after_inspection(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True

    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    assert api.workspace.project_root == str(project.resolve())
    assert not api._workspace_required_for_task
    assert Api._act_inspect_file(api, str(index))
    assert not AgentLoop(api, None)._workspace_mutation_is_blocked(
        "patch_file", f"{index}|||</main>|||<section>New</section></main>"
    )


def test_vscode_discovered_root_persists_for_later_actions(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    monkeypatch.setattr(api, "_vscode_workspace_path", lambda: str(project))

    assert Api._establish_workspace_for_task(api, "add a section to this website")
    assert Api._act_inspect_file(api, str(index))
    assert api.workspace.project_root == str(project.resolve())
    assert not AgentLoop(api, None)._workspace_mutation_is_blocked(
        "write_file", f"{index}|||<main>replacement</main>"
    )


def test_explicit_file_path_establishes_its_parent_but_arbitrary_inspection_does_not(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")

    explicit_api = _api_with_workspace(tmp_path)
    explicit_api._workspace_required_for_task = True
    assert Api._establish_workspace_for_task(explicit_api, f'Use "{index}"')
    assert explicit_api.workspace.project_root == str(project.resolve())
    assert Api._act_inspect_file(explicit_api, str(index))
    assert not explicit_api._workspace_required_for_task

    untrusted = tmp_path / "unrelated.txt"
    untrusted.write_text("not a project", encoding="utf-8")
    arbitrary_api = _api_with_workspace(tmp_path)
    assert Api._act_inspect_file(arbitrary_api, str(untrusted))
    assert arbitrary_api.workspace.project_root is None
    assert not Api._is_trusted_project_root(arbitrary_api, str(tmp_path))


def test_workspace_root_is_available_to_later_phase_framing(tmp_path):
    project = tmp_path / "Portfolio"
    project.mkdir()
    (project / "index.html").write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    assert Api._act_inspect_file(api, str(project / "index.html"))

    from core.planner import Plan, PlanNode, PlanRunner, DONE

    plan = Plan(
        [
            PlanNode("inspect", "Inspect the existing site"),
            PlanNode("edit", "Patch the inspected existing site", ["inspect"]),
        ]
    )
    plan.mark("inspect", DONE, "Verified phase actions: inspect_file(index.html)")
    frame = PlanRunner(api, None)._frame("Add a section", plan, plan.nodes["edit"])
    assert str(project) in frame
    assert "inspect_file(index.html)" in frame


def test_explicit_path_answer_clears_the_latched_workspace_gate(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    index.write_text("<main>portfolio</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    # This mirrors the first successful inspection in the reported failure:
    # it is useful evidence for editing, but is not enough by itself to trust
    # a model-provided project root.
    assert Api._act_inspect_file(api, str(index))
    assert api.workspace.project_root is None

    api._stop_requested = threading.Event()
    api.live = SimpleNamespace(
        snapshot={
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        }
    )
    api.vision = SimpleNamespace(ready=False)
    api.caption_window = None
    api.captions_enabled = False
    api.memory = SimpleNamespace(add_action=lambda *_args: None)
    api.task_memory = SimpleNamespace(get_hints_for_task=lambda _task: [])
    api.api_manager = SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://qa.invalid",
        get_fallback_url=lambda: "https://qa.invalid",
    )
    api._get_text_answer = lambda _question, timeout=60: str(project)
    api._normalize_utterance = lambda text: str(text or "").lower()
    api._VOICE_STOP_WORDS = set()
    api.speak = lambda *_args, **_kwargs: None
    api.tools = ToolRegistry(":memory:")

    replies = iter(
        [
            {"action": "ask_user", "parameter": "Which project folder should I use?", "speech": ""},
            {"action": "done", "parameter": "", "speech": ""},
        ]
    )

    class _Response:
        ok = True

        def __init__(self, reply):
            self.reply = reply

        def json(self):
            return {"response": json.dumps(self.reply)}

    monkeypatch.setattr(
        "core.agent.requests.post", lambda *_args, **_kwargs: _Response(next(replies))
    )
    try:
        bind_confirmation(api)
        loop = AgentLoop(api, None)
        loop._get_screen_context = lambda: "VS Code is open."
        result = loop._loop("Add a section to this website", "test", "none", "text", None)
    finally:
        api.tools._conn.close()

    assert result["success"]
    assert api.workspace.project_root == str(project.resolve())
    assert not api._workspace_required_for_task


def test_portfolio_path_handoff_inspects_patches_and_opens_same_project(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    index = project / "index.html"
    original = "<main><h1>Portfolio</h1></main>"
    index.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    api._stop_requested = threading.Event()
    api.live = SimpleNamespace(
        snapshot={
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        }
    )
    api.vision = SimpleNamespace(ready=False)
    api.caption_window = None
    api.captions_enabled = False
    api.memory = SimpleNamespace(add_action=lambda *_args: None)
    api.task_memory = SimpleNamespace(
        get_hints_for_task=lambda _task: [], record=lambda *_args: None
    )
    api.api_manager = SimpleNamespace(
        get_headers=lambda: {},
        get_api_url=lambda: "https://qa.invalid",
        get_fallback_url=lambda: "https://qa.invalid",
    )
    api._get_text_answer = lambda question, timeout=60: (
        str(project) if question.startswith("Which project") else "yes"
    )
    api._normalize_utterance = lambda text: str(text or "").lower()
    api._VOICE_STOP_WORDS = set()
    api.speak = lambda *_args, **_kwargs: None
    api.tools = ToolRegistry(":memory:")
    api.executor = Executor(api)
    api._resolve_app_exe = lambda _name: "code"

    opened = []
    process_lists = iter([[], [SimpleNamespace(info={"name": "Code.exe"})]])
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: opened.append(args))
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: next(process_lists))
    monkeypatch.setattr("core.api.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)

    replies = iter(
        [
            {"action": "inspect_file", "parameter": str(index), "speech": ""},
            {"action": "ask_user", "parameter": "Which project folder should I use?", "speech": ""},
            {
                "action": "patch_file",
                "parameter": (
                    f"{index}|||</main>|||<section id=\"about\">About</section></main>"
                ),
                "speech": "",
            },
            {"action": "open_project_in_vscode", "parameter": str(project), "speech": ""},
            {"action": "done", "parameter": "", "speech": ""},
        ]
    )

    class _Response:
        ok = True

        def __init__(self, reply):
            self.reply = reply

        def json(self):
            return {"response": json.dumps(self.reply)}

    monkeypatch.setattr(
        "core.agent.requests.post", lambda *_args, **_kwargs: _Response(next(replies))
    )
    try:
        bind_confirmation(api)
        loop = AgentLoop(api, None)
        loop._get_screen_context = lambda: "VS Code is open."
        result = loop._loop("Add a section to this website", "test", "none", "text", None)
    finally:
        api.tools._conn.close()

    assert result["success"]
    assert api.workspace.project_root == str(project.resolve())
    assert "<h1>Portfolio</h1>" in index.read_text(encoding="utf-8")
    assert "<section id=\"about\">About</section>" in index.read_text(encoding="utf-8")
    assert opened == [["code", "-n", str(project)]]


def test_vscode_status_discovers_one_direct_desktop_workspace(tmp_path, monkeypatch):
    desktop = tmp_path / "Desktop"
    documents = tmp_path / "Documents"
    project = desktop / "Portfolio"
    project.mkdir(parents=True)
    api = _api_with_workspace(tmp_path)
    api._get_desktop_path = lambda: str(desktop)
    api._resolve_path_placeholders = lambda _path: str(documents)

    captured = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(
            args, 0, "|   Folder (Portfolio): 5 files\n", ""
        )

    monkeypatch.setattr("core.api.shutil.which", lambda _name: r"C:\\VS Code\\bin\\code.cmd")
    monkeypatch.setattr("core.api.subprocess.run", fake_run)

    assert Api._vscode_workspace_path(api) == str(project.resolve())
    assert captured["args"][:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "--status" in captured["args"][-1]

    (documents / "Portfolio").mkdir(parents=True)
    assert Api._vscode_workspace_path(api) is None


def test_executor_status_text_is_safe_for_legacy_windows_console():
    status = ExecutionResult(
        "inspect_file", "package.json", True, confidence=1.0, verified=True
    ).short()
    assert status.encode("cp1252")
    assert "verify=OK" in status


def test_existing_file_requires_inspection_before_full_replacement(tmp_path):
    target = tmp_path / "package.json"
    original = '{"scripts":{"dev":"vite"},"dependencies":{"animejs":"1"}}'
    target.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)

    assert not Api._act_write_file(api, f"{target}|||{{\"license\":\"MIT\"}}")
    assert target.read_text(encoding="utf-8") == original
    assert "REFUSED" in api.last_result_text

    assert Api._act_inspect_file(api, str(target))
    assert Api._act_write_file(api, f"{target}|||{{\"license\":\"MIT\"}}")
    assert json.loads(target.read_text(encoding="utf-8")) == {"license": "MIT"}

    registry = ToolRegistry(db_path=str(tmp_path / "stats.db"))
    try:
        assert registry.requires_confirmation("write_file", f"{target}|||{{}}")
    finally:
        if registry._conn:
            registry._conn.close()
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    assert "REPLACE all existing content" in loop._confirmation_question(
        "write_file", f"{target}|||{{}}"
    )


def test_write_file_documentation_does_not_describe_an_append(tmp_path):
    target = tmp_path / "index.html"
    target.write_text("<main>existing</main>", encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    bind_confirmation(api)
    loop = AgentLoop(api, None)

    system = loop._build_system(
        "none",
        {
            "foreground_window": "VS Code",
            "open_apps": ["VS Code"],
            "screen_resolution": (1920, 1080),
            "cpu_percent": 0,
            "ram_percent": 0,
            "clipboard": "",
        },
        "test screen",
        "",
        [],
    )
    assert "REPLACES THE ENTIRE FILE" in system
    assert "NEVER appends" in system
    assert "patch_file" in system

    # This is the exact misunderstanding: even after inspection, a write with
    # only a new section is a complete replacement, never an append.
    assert Api._act_inspect_file(api, str(target))
    assert Api._act_write_file(api, f"{target}|||<section>new section</section>")
    assert target.read_text(encoding="utf-8") == "<section>new section</section>"


def test_patch_file_preserves_source_with_one_unique_inspected_anchor(tmp_path):
    target = tmp_path / "index.html"
    original = "<main><h1>Portfolio</h1></main>"
    target.write_text(original, encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    patch = f"{target}|||</main>|||<section id=\"about\">About</section></main>"

    assert not Api._act_patch_file(api, patch)
    assert target.read_text(encoding="utf-8") == original
    assert Api._act_inspect_file(api, str(target))
    assert Api._act_patch_file(api, patch)
    updated = target.read_text(encoding="utf-8")
    assert "<h1>Portfolio</h1>" in updated
    assert "<section id=\"about\">About</section>" in updated

    duplicate = tmp_path / "duplicate.html"
    duplicate.write_text("<main></main><main></main>", encoding="utf-8")
    assert Api._act_inspect_file(api, str(duplicate))
    before = duplicate.read_text(encoding="utf-8")
    assert not Api._act_patch_file(api, f"{duplicate}|||</main>|||changed")
    assert duplicate.read_text(encoding="utf-8") == before


def test_open_existing_trusted_project_is_allowed_without_mutation_gate(tmp_path, monkeypatch):
    project = tmp_path / "Portfolio"
    project.mkdir()
    api = _api_with_workspace(tmp_path)
    assert Api._establish_workspace_for_task(api, f'Project at "{project}"')
    api._workspace_required_for_task = True
    bind_confirmation(api)
    loop = AgentLoop(api, None)
    assert not loop._workspace_mutation_is_blocked("open_project_in_vscode", str(project))

    calls = []
    process_lists = iter(
        [
            [],
            [SimpleNamespace(info={"name": "Code.exe"})],
        ]
    )
    api._resolve_app_exe = lambda _name: "code"
    monkeypatch.setattr("core.api.subprocess.Popen", lambda args: calls.append(args))
    monkeypatch.setattr("core.api.psutil.process_iter", lambda _attrs: next(process_lists))
    monkeypatch.setattr("core.api.time.sleep", lambda _seconds: None)

    assert Api._act_open_project_in_vscode(api, str(project))
    assert calls == [["code", "-n", str(project)]]

    untrusted = tmp_path / "Untrusted"
    untrusted.mkdir()
    assert not Api._act_open_project_in_vscode(api, str(untrusted))
    assert "REFUSED" in api.last_result_text


def test_agent_blocks_project_write_until_workspace_and_file_are_inspected(tmp_path):
    target = tmp_path / "package.json"
    target.write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
    api = _api_with_workspace(tmp_path)
    api._workspace_required_for_task = True
    bind_confirmation(api)
    loop = AgentLoop(api, None)

    assert loop._workspace_mutation_is_blocked("write_file", f"{target}|||{{}}")
    api._workspace_required_for_task = False
    assert loop._existing_file_needs_inspection("write_file", f"{target}|||{{}}")
    assert Api._act_inspect_file(api, str(target))
    assert not loop._existing_file_needs_inspection("write_file", f"{target}|||{{}}")


def test_patch_json_preserves_package_scripts_and_dependencies(tmp_path):
    target = tmp_path / "package.json"
    target.write_text(
        json.dumps(
            {
                "scripts": {"dev": "vite"},
                "dependencies": {"animejs": "1.0.0"},
                "devDependencies": {"vite": "5.0.0"},
            }
        ),
        encoding="utf-8",
    )
    api = _api_with_workspace(tmp_path)

    assert Api._act_inspect_file(api, str(target))
    assert Api._act_patch_json(api, f'{target}|||{{"license":"MIT"}}')
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["license"] == "MIT"
    assert data["scripts"] == {"dev": "vite"}
    assert data["dependencies"] == {"animejs": "1.0.0"}
    assert data["devDependencies"] == {"vite": "5.0.0"}


def test_command_result_preserves_stdout_stderr_and_warning_status(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            _args[0], 0, "installed package\nwarning optional dependency", "stderr note"
        ),
    )

    assert Api._act_run_command(api, "yarn install animejs")
    result = api.last_command_result
    assert result["completed"] and result["success"] and result["has_warnings"]
    assert result["stdout"].startswith("installed package")
    assert result["stderr"] == "stderr note"
    assert "COMMAND COMPLETED WITH WARNINGS" in api.last_result_text
    assert "STDERR: stderr note" in api.last_result_text


def test_command_result_handles_nonzero_timeout_and_empty_output(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 7, "useful stdout", "failure"),
    )
    assert not Api._act_run_command(api, "failing-command")
    assert api.last_command_result["exit_code"] == 7
    assert api.last_command_result["stdout"] == "useful stdout"
    assert api.last_command_result["stderr"] == "failure"
    assert "COMMAND FAILED" in api.last_result_text

    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("slow-command", 15, output="partial", stderr="still running")
        ),
    )
    assert not Api._act_run_command(api, "slow-command")
    assert api.last_command_result["timed_out"]
    assert "COMMAND TIMED OUT" in api.last_result_text

    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], 0, "", ""),
    )
    assert Api._act_run_command(api, "quiet-command")
    assert api.last_command_result["success"]
    assert "OUTPUT: (no output)" in api.last_result_text


# ============================================================
# PROJECT STATE SAFETY HELPERS
# ============================================================

def _fake_run(monkeypatch, code, stdout="", stderr=""):
    monkeypatch.setattr(
        "core.api.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(_args[0], code, stdout, stderr),
    )


# ============================================================
# PROJECT STATE SAFETY TESTS (2)
# ============================================================

def test_suppressed_error_with_no_output_is_no_results_not_failure(monkeypatch, tmp_path):
    # Get-Process -Name msedge -ErrorAction SilentlyContinue: exit 1, nothing printed.
    api = _api_with_workspace(tmp_path)
    _fake_run(monkeypatch, 1)
    assert Api._act_run_command(api, "Get-Process -Name msedge -ErrorAction SilentlyContinue")
    result = api.last_command_result
    assert result["no_results"] and result["success"] and result["exit_code"] == 1
    assert "COMMAND RETURNED NO RESULTS (exit code 1, no stdout, no stderr)" in api.last_result_text
    assert "COMMAND FAILED" not in api.last_result_text


def test_real_errors_still_fail(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    for code, stdout, stderr in ((1, "", "Cannot find a process"), (1, "partial", ""),
                                 (7, "", ""), (3, "", "")):
        _fake_run(monkeypatch, code, stdout, stderr)
        assert not Api._act_run_command(api, "cmd"), (code, stdout, stderr)
        assert not api.last_command_result["no_results"]
        assert api.last_command_result["exit_code"] == code
        assert f"COMMAND FAILED (exit code {code})" in api.last_result_text


def test_executor_records_no_results_as_ok(monkeypatch, tmp_path):
    api = _api_with_workspace(tmp_path)
    api.tools = ToolRegistry(":memory:")
    api._exec_sync = lambda _action, param: Api._act_run_command(api, param)
    monkeypatch.setattr("core.executor.time.sleep", lambda _seconds: None)
    _fake_run(monkeypatch, 1)
    res = Executor(api).run("run_command", "Get-Process -Name msedge -ErrorAction SilentlyContinue")
    assert res.success and res.confidence > 0.2
    assert "NO RESULTS" in res.detail

    _fake_run(monkeypatch, 7)
    res = Executor(api).run("run_command", "failing-command")
    assert not res.success and res.confidence <= 0.2
