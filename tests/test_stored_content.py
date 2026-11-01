"""A3: what MistAI stores in memory.db.

- actions: (action, success, timestamp) only; the parameter column is empty.
- task_outcomes.note: a fixed phrase per outcome kind, never result text.
- app_usage: no longer written.
- conversations: rows older than MISTAI_HISTORY_HOURS (default 24) are
  deleted at startup and on insert; nothing newer is touched.
- the verbose-logs banner prints when the Api starts.
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import core.api as api_mod
import core.browser_session as browser_session
import core.config as config
from core.agent import AgentLoop
from core.executor import Executor
from core.memory import Memory
from core.task_memory import OUTCOME_NOTES, TaskMemory
from core.tools import ToolRegistry, ToolSpec

TYPED = "my confidential diary entry"
CONTACT = "Joanne Quillfeather"
MESSAGE = "meet me at the lighthouse pier"
COMMAND = "Get-Content marblewood-secrets.txt"
TITLE = "Quillfeather diary - Notepad"
CONTENT_WORDS = ("confidential", "diary", "joanne", "quillfeather", "lighthouse",
                 "pier", "marblewood")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "memory.db")
    monkeypatch.setenv("MISTAI_DB_PATH", path)
    monkeypatch.delenv("MISTAI_HISTORY_HOURS", raising=False)
    return path


def _all_rows(path):
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    finally:
        conn.close()


class _Registry:
    def __init__(self, specs):
        self.specs = specs

    def get(self, action):
        return self.specs[action]

    def settle(self, action):
        return 0

    def is_destructive(self, *_a, **_k):
        return False


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


def test_typed_text_a_message_and_a_command_never_reach_a_stored_row(db, monkeypatch):
    monkeypatch.setattr("core.executor.time.sleep", lambda _s: None)
    ex, memory, task_memory = _executor(db, {
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

    rows = _all_rows(db)
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


def test_hints_still_work_from_counts_and_fixed_notes(db):
    tm = TaskMemory(db)
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


def test_reliability_still_learns_from_the_actions_table(db):
    memory = Memory()
    for ok in (False, False, False, False, True):
        memory.add_action("click_on_text", ok)
    reg = ToolRegistry(db)
    assert reg.observed("click_on_text") == (5, 0.2)
    base = reg.get("click_on_text").base_reliability
    assert reg.reliability("click_on_text") == round(0.35 * base + 0.65 * 0.2, 3)
    memory.conn.close()
    reg._conn.close()


def test_app_usage_has_no_writer():
    import inspect
    assert "log_app_usage" not in inspect.getsource(api_mod)


# -- conversation expiry ------------------------------------------------------------------

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


def _messages(path):
    conn = sqlite3.connect(path)
    try:
        return [m for (m,) in conn.execute("SELECT message FROM conversations ORDER BY id")]
    finally:
        conn.close()


def test_startup_deletes_only_rows_older_than_24_hours(db):
    _seed(db, [72, 25, 23.9, 0.4, 0])
    Memory().conn.close()
    assert _messages(db) == ["turn 23.9", "turn 0.4", "turn 0"]


def test_an_insert_prunes_too_and_the_window_is_read_at_call_time(db, monkeypatch):
    _seed(db, [5, 1.5])
    memory = Memory()
    assert len(_messages(db)) == 2                       # both within 24h
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "2")
    memory.add_conversation("assistant", "fresh")
    assert _messages(db) == ["turn 1.5", "fresh"]
    memory.conn.close()


@pytest.mark.parametrize("raw, hours", [
    ("48", 48.0), ("0.25", 1.0), ("0", 1.0), ("-5", 1.0), ("soon", 24.0), ("nan", 24.0)])
def test_history_hours_parsing(monkeypatch, raw, hours):
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", raw)
    assert config.history_hours() == hours


def test_infinite_history_keeps_everything(db, monkeypatch):
    _seed(db, [24 * 400])
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "inf")
    Memory().conn.close()
    assert _messages(db) == [f"turn {24 * 400}"]


def test_the_agents_30_minute_block_is_unaffected(db, monkeypatch):
    monkeypatch.setenv("MISTAI_HISTORY_HOURS", "0.1")    # clamped to 1h
    _seed(db, [0.9, 0.3, 0.1])
    memory = Memory()
    block = AgentLoop(SimpleNamespace(memory=memory), None)._recent_conversation_block("x")
    memory.conn.close()
    assert "turn 0.3" in block and "turn 0.1" in block and "turn 0.9" not in block
    assert len(_messages(db)) == 3


# -- the verbose-logs banner prints when the Api starts --------------------------------

class _StopInit(Exception):
    pass


def _start_api(monkeypatch):
    """Run Api.__init__ up to its first subsystem, then stop."""
    def boom(*_a, **_k):
        raise _StopInit()
    monkeypatch.setattr(browser_session, "BrowserSession", boom)
    with pytest.raises(_StopInit):
        api_mod.Api()


@pytest.fixture
def fresh_banner(monkeypatch):
    monkeypatch.setattr(config, "_verbose_banner_shown", False)
    monkeypatch.delattr(sys, "frozen", raising=False)


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
def test_a_click_on_a_contact_name_stores_no_part_of_the_name(db, action):
    tm = TaskMemory(db)
    tm.record(action, CONTACT, True)
    tm.record(action, "Discord|" + CONTACT, False)
    tm.record(action, "", False)
    tm.conn.close()
    rows = _all_rows(db)["task_outcomes"]
    assert [(sig, cat, sc, fc) for sig, cat, _a, sc, fc, _ls, _n, _u in rows] == [
        (f"click:{action}", "clicking", 1, 2)]
    assert "joanne" not in repr(rows).lower() and "quillfeather" not in repr(rows).lower()


def test_click_counts_add_up_per_method(db):
    tm = TaskMemory(db)
    for target, ok in (("Send", True), ("Cancel", False), (CONTACT, True)):
        tm.record("click_on_text", target, ok)
    tm.record("uia_click", "Settings", False)
    counts = {sig: (sc, fc) for sig, sc, fc in tm.conn.execute(
        "SELECT signature, success_count, fail_count FROM task_outcomes")}
    tm.conn.close()
    assert counts == {"click:click_on_text": (2, 1), "click:uia_click": (0, 1)}


def test_old_click_rows_are_kept_and_still_hint_new_ones_never_do(db):
    tm = TaskMemory(db)
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
