# ============================================================
# MEMORY MODULE
# ============================================================
#
# SQLite storage: conversation, facts, reminders and usage (Memory),
# per-task operational hints (TaskMemory) and resumable tasks (TaskStore).
#
# SECTION INDEX
#   1. FACT KEYWORDS AND NORMALIZATION
#   2. BUILT-IN RECALL
#   3. MEMORY DATABASE
#   4. TASK MEMORY
#   5. HINT SETTINGS
#   6. OPERATIONAL HINTS
#   7. TASK STORE
#   8. RESUMABLE STATUSES
#   9. TASK RECORD
#   10. TASK NAME SLUGS
#   11. RESUMABLE TASKS
# ============================================================

"""
core/memory.py — SQLite-backed conversation, fact, reminder, and usage memory.
"""

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
import difflib
import json

from .config import MISTAI_FACTS, USER_FACT_CATEGORIES


# ============================================================
# FACT KEYWORDS AND NORMALIZATION
# ============================================================

# SQL placeholder list for the user's own fact categories.
_USER_CATS = ",".join("?" * len(USER_FACT_CATEGORIES))


def _keywords(content):
    return " ".join(w.lower() for w in content.split() if len(w) > 3 and w.isalpha())


def _normalize_fact(text):
    return " ".join(str(text or "").casefold().split())


# ============================================================
# BUILT-IN RECALL
# ============================================================

def recall_builtin(query, limit=5):
    """MistAI's facts about itself (config.MISTAI_FACTS) that a question is
    about, best first; [] when none is. From code, never the database."""
    words = set(re.findall(r"[a-z]+", str(query or "").lower()))
    scored = []
    for text, tags in MISTAI_FACTS:
        score = len(words & set(tags)) + len(
            {w for w in words if len(w) > 3} & set(_keywords(text).split()))
        if score:
            scored.append((score, text))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:limit]]


# ============================================================
# MEMORY DATABASE
# ============================================================

class Memory:
    def __init__(self):
        from .config import memory_db_path

        db = memory_db_path()
        os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db, check_same_thread=False)
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, message TEXT, timestamp TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, parameter TEXT, success INTEGER DEFAULT 1, timestamp TEXT);
                CREATE TABLE IF NOT EXISTS user_profile (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
                CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, content TEXT, importance INTEGER DEFAULT 1, keywords TEXT, timestamp TEXT);
                CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, due_time TEXT, done INTEGER DEFAULT 0, created TEXT);
                CREATE TABLE IF NOT EXISTS app_usage (app TEXT PRIMARY KEY, open_count INTEGER DEFAULT 1, last_used TEXT);
                CREATE TABLE IF NOT EXISTS session_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT, message_range TEXT, created TEXT);
            """)
            self.conn.commit()
        for sql in [
            "ALTER TABLE facts ADD COLUMN keywords TEXT",
            "ALTER TABLE conversations ADD COLUMN summary TEXT",
        ]:
            try:
                self.conn.execute(sql)
                self.conn.commit()
            except Exception:
                pass
        self.prune_conversations()

    def prune_conversations(self):
        """Delete conversation turns older than config.history_hours(); newer
        rows are untouched. Runs at startup and on every insert."""
        from .config import history_hours

        try:
            cutoff = (datetime.now() - timedelta(hours=history_hours())).isoformat()
        except OverflowError:
            return                                  # "keep forever"
        with self._lock:
            # isoformat() strings sort in time order (see get_recent_conversations)
            self.conn.execute("DELETE FROM conversations WHERE timestamp < ?", (cutoff,))
            self.conn.commit()

    def add_conversation(self, role, message):
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations (role,message,timestamp) VALUES (?,?,?)",
                (role, message, datetime.now().isoformat()),
            )
            self.conn.commit()
        self.prune_conversations()

    def get_recent_conversations(self, limit=12, since=None):
        # since: ISO timestamp; rows are stored via isoformat(), so string order is time order.
        with self._lock:
            rows = self.conn.execute(
                "SELECT role,message FROM conversations WHERE timestamp >= ? "
                "ORDER BY id DESC LIMIT ?",
                (since or "", limit),
            ).fetchall()
            summaries = self.conn.execute(
                "SELECT summary FROM session_summaries ORDER BY id DESC LIMIT 3"
            ).fetchall()
        result = []
        if summaries:
            for s in reversed(summaries):
                result.append(("summary", s[0]))
        result.extend(reversed(rows))
        return result

    def add_action(self, action, success=True):
        """One row per executed action: its name, success and time. The
        parameter column stays empty -- a parameter can be typed text, a
        message, a contact or a command (ToolRegistry.observed needs none)."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO actions (action,parameter,success,timestamp) VALUES (?,'',?,?)",
                (action, 1 if success else 0, datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_recent_actions(self, limit=5):
        with self._lock:
            rows = self.conn.execute(
                "SELECT action,parameter FROM actions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [f"{r[0]}({r[1]})" for r in rows]

    def set_profile(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO user_profile (key,value,updated) VALUES (?,?,?)",
                (key, value, datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_profile(self, key, default=None):
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM user_profile WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else default

    def count_profile_prefix(self, prefix):
        """How many user_profile keys start with prefix (exact, no LIKE)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM user_profile WHERE substr(key,1,?)=?",
                (len(prefix), prefix),
            ).fetchone()
        return int(row[0]) if row else 0

    def delete_profile_prefix(self, prefix):
        """Delete only the user_profile rows whose key starts with prefix."""
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM user_profile WHERE substr(key,1,?)=?", (len(prefix), prefix)
            )
            self.conn.commit()
        return cur.rowcount

    def remember_fact(self, content, category="general", importance=1):
        kw = _keywords(content)
        with self._lock:
            self.conn.execute(
                "INSERT INTO facts (category,content,importance,keywords,timestamp) VALUES (?,?,?,?,?)",
                (category, content, importance, kw, datetime.now().isoformat()),
            )
            self.conn.commit()

    def recall_facts(self, category=None, limit=5, query=None):
        """The user's own facts only (USER_FACT_CATEGORIES): rows an older
        version seeded are kept but never returned."""
        with self._lock:
            if query:
                rows = self.conn.execute(
                    "SELECT content,keywords,importance FROM facts "
                    f"WHERE category IN ({_USER_CATS}) "
                    "ORDER BY importance DESC,id DESC LIMIT 50",
                    USER_FACT_CATEGORIES,
                ).fetchall()
                qw = set(w.lower() for w in query.split() if len(w) > 3)
                # Only facts that share a word with the question: an unrelated
                # fact is not an answer (and would hide MistAI's own facts).
                scored = sorted(
                    [
                        (hits * 10 + imp, c)
                        for c, kw, imp in rows
                        if (hits := len(qw & set((kw or "").split())))
                    ],
                    key=lambda x: -x[0],
                )
                return [c for _, c in scored[:limit]]
            elif category:
                if category not in USER_FACT_CATEGORIES:
                    return []
                rows = self.conn.execute(
                    "SELECT content FROM facts WHERE category=? ORDER BY importance DESC,id DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT content FROM facts WHERE category IN ({_USER_CATS}) "
                    "ORDER BY importance DESC,id DESC LIMIT ?",
                    (*USER_FACT_CATEGORIES, limit),
                ).fetchall()
        return [r[0] for r in rows]

    def log_app_usage(self, app):
        with self._lock:
            self.conn.execute(
                "INSERT INTO app_usage (app,open_count,last_used) VALUES (?,1,?) "
                "ON CONFLICT(app) DO UPDATE SET open_count=open_count+1,last_used=excluded.last_used",
                (app.lower(), datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_frequent_apps(self, limit=5):
        with self._lock:
            rows = self.conn.execute(
                "SELECT app FROM app_usage ORDER BY open_count DESC LIMIT ?", (limit,)
            ).fetchall()
        return [r[0] for r in rows]

    def add_reminder(self, content, due_time=None):
        with self._lock:
            self.conn.execute(
                "INSERT INTO reminders (content,due_time,created) VALUES (?,?,?)",
                (content, due_time, datetime.now().isoformat()),
            )
            self.conn.commit()

    def get_pending_reminders(self):
        with self._lock:
            return self.conn.execute(
                "SELECT id,content,due_time FROM reminders WHERE done=0"
            ).fetchall()

    def complete_reminder(self, rid):
        with self._lock:
            self.conn.execute("UPDATE reminders SET done=1 WHERE id=?", (rid,))
            self.conn.commit()

    def get_stats(self):
        with self._lock:
            return {
                "conversations": self.conn.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()[0],
                "actions": self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[
                    0
                ],
                "facts": self.conn.execute(
                    f"SELECT COUNT(*) FROM facts WHERE category IN ({_USER_CATS})",
                    USER_FACT_CATEGORIES,
                ).fetchone()[0],
                "pending_reminders": self.conn.execute(
                    "SELECT COUNT(*) FROM reminders WHERE done=0"
                ).fetchone()[0],
            }

    def import_facts(self, contents):
        """Add the user's own facts (category "imported") in one transaction.

        - already one of the user's facts (any spelling/case), or twice in the
          list: skipped;
        - EXACTLY the text of a row an older version seeded ("system",
          "personal", "work"): that row is restored -- its category becomes
          "imported", nothing else changes. One row per fact (the oldest), so
          duplicate seeded copies stay ignored;
        - otherwise: inserted.

        Nothing is ever deleted. Returns (added, skipped, restored)."""
        now = datetime.now().isoformat()
        seeded_cats = ("system", "personal", "work")
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT id, category, content FROM facts ORDER BY id").fetchall()
            mine = {_normalize_fact(c) for _i, cat, c in rows if cat in USER_FACT_CATEGORIES}
            anywhere = {_normalize_fact(c) for _i, _cat, c in rows}
            seeded = {}
            for rid, cat, content in rows:
                if cat in seeded_cats:
                    seeded.setdefault(content, rid)            # the oldest copy
            new, restore, skipped = [], [], 0
            for text in contents:
                line, key = str(text).strip(), _normalize_fact(text)
                if not key or key in mine:
                    skipped += 1
                elif line in seeded:
                    restore.append(seeded[line])
                    mine.add(key)
                elif key in anywhere:
                    skipped += 1        # a seeded row, but not the exact text
                else:
                    new.append(" ".join(line.split()))
                    mine.add(key)
                    anywhere.add(key)
            self.conn.executemany(
                "UPDATE facts SET category='imported' WHERE id=?", [(i,) for i in restore])
            self.conn.executemany(
                "INSERT INTO facts (category,content,importance,keywords,timestamp) "
                "VALUES ('imported',?,1,?,?)",
                [(t, _keywords(t), now) for t in new],
            )
        return len(new), skipped, len(restore)


# ============================================================
# TASK MEMORY
# ============================================================

# core/task_memory.py — operational memory: what actions worked or failed
# before, at a pattern level (not tied to specific literal parameters like a
# recipient's name). Separate from core/memory.py's conversational/fact
# memory on purpose -- this is "MistAI's learned experience," not "things
# the user told MistAI about themselves."
#
# Goal A: don't repeat known failures (e.g. re-investigating a nonexistent
# app every single time it's asked to open).
# Goal B: reuse known-good strategies (e.g. prefer quick_message for
# Discord messaging once it's proven to work, instead of re-deriving via
# UIA/OCR every time).

# ============================================================
# HINT SETTINGS
# ============================================================

# A signature key shorter than this is too generic to match against task
# text: a click on "a new tab" produced the key "a", which then injected a
# "clicking 'a' has failed" hint into EVERY task containing the letter a.
MIN_KEY_LEN = 3

# task_outcomes.note is one of these fixed phrases, never an action's result
# text (that can quote a window title, a message or on-screen words).
OUTCOME_NOTES = {
    ("open_app", True): "opened",
    ("open_app", False): "did not open",
    ("messaging", True): "sent",
    ("messaging", False): "did not send",
    ("clicking", True): "clicked",
    ("clicking", False): "click failed",
}

# Click rows are keyed "click:<action>" (counts per method); an action name
# never matches a task's words, so they give no hint. Rows from before that
# kept the target's first word ("click:settings"); they are never rewritten.
CLICK_SIGNATURE_ACTIONS = ("uia_click", "click_on_text", "click_nth")


# ============================================================
# OPERATIONAL HINTS
# ============================================================

class TaskMemory:
    def __init__(self, db_path=None):
        if db_path is None:
            from .config import memory_db_path

            db_path = memory_db_path()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=3000")
        self._lock = threading.Lock()
        self._init()

    def _init(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS task_outcomes (
                    signature TEXT PRIMARY KEY,
                    category TEXT,
                    preferred_action TEXT,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    last_success INTEGER,
                    note TEXT,
                    updated TEXT
                )
            """)
            self.conn.commit()

    def _normalize(self, name):
        """Same normalization approach as Api._normalize_app_name -- strip
        punctuation/spaces/version suffixes so 'OBS Studio' and 'obs64'
        (or 'Cheese' vs any future recipient) collapse to comparable keys."""
        n = (name or "").lower().strip()
        n = re.sub(r"[^a-z0-9\s]", "", n)
        n = re.sub(r"\d+$", "", n).strip()
        n = re.sub(r"\s+", "", n)
        return n

    def build_signature(self, action, parameter):
        """Turn a specific action call into a generalized pattern key.
        Deliberately generic where the SPECIFIC value doesn't matter for
        the strategy (e.g. WHO you message on Discord doesn't change
        whether quick_message works), but specific where it does (e.g.
        WHICH app you're trying to open matters a lot)."""
        param = parameter or ""

        if action in ("open_app", "switch_app"):
            target = self._normalize(param)
            if len(target) < MIN_KEY_LEN:
                return None
            return f"open:{target}", "open_app"

        if action == "quick_message":
            parts = param.split("|", 2)
            app = self._normalize(parts[0]) if parts else ""
            if len(app) < MIN_KEY_LEN:
                return None
            return f"message:{app}", "messaging"

        if action in CLICK_SIGNATURE_ACTIONS:
            # The click method only: a word of the target can be a
            # contact's name or other on-screen content.
            return f"click:{action}", "clicking"

        if action == "browser_search" or action == "browser_navigate":
            return None  # too variable to be worth pattern-caching

        return None  # most actions (run_command, write_file, etc) are
        # inherently task-specific -- not worth caching

    def record(self, action, parameter, success):
        result = self.build_signature(action, parameter)
        if not result:
            return
        signature, category = result
        note = OUTCOME_NOTES[(category, bool(success))]

        with self._lock:
            row = self.conn.execute(
                "SELECT success_count, fail_count FROM task_outcomes WHERE signature=?",
                (signature,),
            ).fetchone()

            if row:
                sc, fc = row
                sc += 1 if success else 0
                fc += 0 if success else 1
                self.conn.execute(
                    """UPDATE task_outcomes
                       SET success_count=?, fail_count=?, last_success=?,
                           preferred_action=?, note=?, updated=?
                       WHERE signature=?""",
                    (
                        sc,
                        fc,
                        int(success),
                        action,
                        note,
                        datetime.now().isoformat(),
                        signature,
                    ),
                )
            else:
                self.conn.execute(
                    """INSERT INTO task_outcomes
                       (signature, category, preferred_action, success_count,
                        fail_count, last_success, note, updated)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        signature,
                        category,
                        action,
                        1 if success else 0,
                        0 if success else 1,
                        int(success),
                        note,
                        datetime.now().isoformat(),
                    ),
                )
            self.conn.commit()

    @staticmethod
    def _key_in_task(key, task_lower):
        """Does this signature key actually appear in the task, as a WORD?

        The old test stripped all whitespace from the task and asked
        `key in task` -- so 'code' matched "de*code* this base64 string' and
        injected a bogus "you've tried to open 'code' before and it FAILED"
        hint. Hints are presented to the model as authoritative learned
        experience, so a false match doesn't just add noise, it actively
        misleads."""
        if len(key) < MIN_KEY_LEN:
            return False
        # Compare against the task's words with punctuation stripped, so
        # "discord." and "discord," still match, but "decode" does not.
        words = re.findall(r"[a-z0-9]+", task_lower)
        if key in words:
            return True
        # Multi-word app names collapse to one key ("applemusic"); match them
        # against adjacent word pairs/triples rather than the whole string.
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                if "".join(words[i : i + n]) == key:
                    return True
        return False

    def get_hints_for_task(self, task_text, limit=5):
        """Scan stored signatures against the current task's text and
        return short, directive hint lines for the system prompt -- known
        failures get flagged so they're not re-investigated; known
        successful strategies get suggested so they're reused."""
        task_lower = (task_text or "").lower()
        with self._lock:
            rows = self.conn.execute(
                "SELECT signature, category, preferred_action, success_count, "
                "fail_count, last_success, note FROM task_outcomes"
            ).fetchall()

        hints = []
        for signature, category, action, sc, fc, last_success, note in rows:
            key = signature.split(":", 1)[-1]
            if not self._key_in_task(key, task_lower):
                continue

            if category == "open_app" and fc > 0 and sc == 0:
                hints.append(
                    f"- You've tried to open something matching '{key}' before and it "
                    f"FAILED every time ({note or 'not found on this system'}). "
                    f"Don't re-investigate from scratch -- report this directly unless "
                    f"the user gives new information."
                )
            elif category == "messaging" and sc > 0:
                hints.append(
                    f"- For messaging on '{key}', {action} has worked successfully "
                    f"before ({sc} time{'s' if sc != 1 else ''}). Prefer it over "
                    f"manual UIA/OCR hunting."
                )
            elif category == "clicking" and fc > sc and fc >= 2:
                hints.append(
                    f"- Clicking '{key}' via OCR/UIA has failed more than it's "
                    f"succeeded. Consider verifying with read_screen_text first."
                )

            if len(hints) >= limit:
                break

        return hints


# ============================================================
# TASK STORE
# ============================================================

# core/task_store.py — persistent TASK memory (design doc #2), distinct from
# both conversational memory (core/memory.py) and pattern-outcome memory
# (core/task_memory.py). This is: "what multi-phase goals exist, how far did
# each get, and can it be resumed."
#
# A TaskRecord stores the goal, the full plan DAG (with per-node statuses and
# results), outputs, and files created. It is saved after EVERY node
# transition, so if MistAI crashes mid-goal the record is still on disk with
# status 'running' -- "Mist, resume PortfolioSite" picks it back up.
#
# Same SQLite file as the rest of MistAI's memory (established pattern:
# core/task_memory.py already opens its own connection to it).

# ============================================================
# RESUMABLE STATUSES
# ============================================================

RESUMABLE_STATUSES = ("paused", "running", "failed", "partial")


# ============================================================
# TASK RECORD
# ============================================================

class TaskRecord:
    def __init__(
        self,
        task_id,
        name,
        goal,
        plan,
        status="running",
        outputs=None,
        files_created=None,
        created=None,
        updated=None,
        last_error="",
    ):
        self.task_id = task_id
        self.name = name
        self.goal = goal
        self.plan = plan or {}  # serialized Plan DAG
        self.status = status  # running|paused|completed|failed|partial
        self.outputs = outputs or {}  # node_id -> result summary
        self.files_created = files_created or []
        self.created = created or datetime.now().isoformat()
        self.updated = updated or self.created
        self.last_error = last_error

    def __repr__(self):
        return f"TaskRecord({self.task_id}, {self.status})"


# ============================================================
# TASK NAME SLUGS
# ============================================================

def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return s[:40] or "task"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# ============================================================
# RESUMABLE TASKS
# ============================================================

class TaskStore:
    def __init__(self, db_path=None):
        if db_path is None:
            from .config import memory_db_path

            db_path = memory_db_path()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self.conn.execute("PRAGMA busy_timeout=3000")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    name TEXT,
                    goal TEXT,
                    plan_json TEXT,
                    status TEXT,
                    outputs_json TEXT,
                    files_json TEXT,
                    created TEXT,
                    updated TEXT,
                    last_error TEXT
                )
            """)
            self.conn.commit()

    # -- CRUD ----------------------------------------------------------------

    def create(self, name, goal, plan_dict):
        base = _slug(name)
        task_id = base
        with self._lock:
            i = 2
            while self.conn.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone():
                task_id = f"{base}{i}"
                i += 1
        rec = TaskRecord(task_id, name, goal, plan_dict)
        self.save(rec)
        return rec

    def save(self, rec):
        rec.updated = datetime.now().isoformat()
        with self._lock:
            self.conn.execute(
                """INSERT INTO tasks
                   (task_id,name,goal,plan_json,status,outputs_json,files_json,
                    created,updated,last_error)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                     name=excluded.name, goal=excluded.goal,
                     plan_json=excluded.plan_json, status=excluded.status,
                     outputs_json=excluded.outputs_json,
                     files_json=excluded.files_json,
                     updated=excluded.updated, last_error=excluded.last_error""",
                (
                    rec.task_id,
                    rec.name,
                    rec.goal,
                    json.dumps(rec.plan),
                    rec.status,
                    json.dumps(rec.outputs),
                    json.dumps(rec.files_created),
                    rec.created,
                    rec.updated,
                    rec.last_error,
                ),
            )
            self.conn.commit()

    def get(self, task_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT task_id,name,goal,plan_json,status,outputs_json,"
                "files_json,created,updated,last_error FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list(self, status=None, limit=10):
        q = (
            "SELECT task_id,name,goal,plan_json,status,outputs_json,files_json,"
            "created,updated,last_error FROM tasks"
        )
        args = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY updated DESC LIMIT ?"
        args = args + (limit,)
        with self._lock:
            rows = self.conn.execute(q, args).fetchall()
        return [self._row_to_record(r) for r in rows]

    # -- Resume lookup -------------------------------------------------------

    def find_resumable(self, query):
        """Fuzzy-match a spoken/typed name ("resume PortfolioSite",
        "resume portfolio") against saved tasks. Prefers paused > running >
        failed/partial, then most recently updated."""
        qn = _norm(query)
        if not qn:
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT task_id,name,goal,plan_json,status,outputs_json,"
                "files_json,created,updated,last_error FROM tasks "
                f"WHERE status IN ({','.join('?' * len(RESUMABLE_STATUSES))}) "
                "ORDER BY updated DESC LIMIT 25",
                RESUMABLE_STATUSES,
            ).fetchall()

        pref = {"paused": 0, "running": 1, "failed": 2, "partial": 2}
        best, best_key = None, None
        for row in rows:
            rec = self._row_to_record(row)
            idn, namen = _norm(rec.task_id), _norm(rec.name)
            if qn == idn or qn == namen:
                score = 100
            elif qn in idn or qn in namen or idn in qn:
                score = 90
            else:
                score = int(
                    max(
                        difflib.SequenceMatcher(None, qn, idn).ratio(),
                        difflib.SequenceMatcher(None, qn, namen).ratio(),
                    )
                    * 100
                )
            if score < 60:
                continue
            key = (-score, pref.get(rec.status, 3))  # updated DESC already ordered
            if best_key is None or key < best_key:
                best, best_key = rec, key
        return best

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _row_to_record(row):
        (
            task_id,
            name,
            goal,
            plan_json,
            status,
            outputs_json,
            files_json,
            created,
            updated,
            last_error,
        ) = row

        def _load(s, default):
            try:
                return json.loads(s) if s else default
            except Exception:
                return default

        return TaskRecord(
            task_id,
            name,
            goal,
            _load(plan_json, {}),
            status,
            _load(outputs_json, {}),
            _load(files_json, []),
            created,
            updated,
            last_error or "",
        )
