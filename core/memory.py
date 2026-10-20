"""
core/memory.py — SQLite-backed conversation, fact, reminder, and usage memory.
"""

import os
import re
import sqlite3
import threading
from datetime import datetime

from .config import MISTAI_FACTS, USER_FACT_CATEGORIES

# SQL placeholder list for the user's own fact categories.
_USER_CATS = ",".join("?" * len(USER_FACT_CATEGORIES))


def _keywords(content):
    return " ".join(w.lower() for w in content.split() if len(w) > 3 and w.isalpha())


def _normalize_fact(text):
    return " ".join(str(text or "").casefold().split())


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

    def add_conversation(self, role, message):
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations (role,message,timestamp) VALUES (?,?,?)",
                (role, message, datetime.now().isoformat()),
            )
            self.conn.commit()

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

    def add_action(self, action, parameter="", success=True):
        with self._lock:
            self.conn.execute(
                "INSERT INTO actions (action,parameter,success,timestamp) VALUES (?,?,?,?)",
                (action, parameter, 1 if success else 0, datetime.now().isoformat()),
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
