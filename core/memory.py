"""
core/memory.py — SQLite-backed conversation, fact, reminder, and usage memory.
"""

import os
import sqlite3
import threading
from datetime import datetime


def _keywords(content):
    return " ".join(w.lower() for w in content.split() if len(w) > 3 and w.isalpha())


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
        with self._lock:
            if query:
                rows = self.conn.execute(
                    "SELECT content,keywords,importance FROM facts ORDER BY importance DESC,id DESC LIMIT 50"
                ).fetchall()
                qw = set(w.lower() for w in query.split() if len(w) > 3)
                scored = sorted(
                    [
                        (len(qw & set((kw or "").split())) * 10 + imp, c)
                        for c, kw, imp in rows
                    ],
                    key=lambda x: -x[0],
                )
                return [c for _, c in scored[:limit]]
            elif category:
                rows = self.conn.execute(
                    "SELECT content FROM facts WHERE category=? ORDER BY importance DESC,id DESC LIMIT ?",
                    (category, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT content FROM facts ORDER BY importance DESC,id DESC LIMIT ?",
                    (limit,),
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
                "facts": self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
                "pending_reminders": self.conn.execute(
                    "SELECT COUNT(*) FROM reminders WHERE done=0"
                ).fetchone()[0],
            }

    def seed_defaults(self, seed_dict):
        """Preload known facts/settings WITHOUT overwriting anything the
        user or the app has already learned -- only fills in gaps. Safe to
        call every startup; existing values always win."""
        # One transaction: a commit per row (fsync each) made seeding a
        # fresh database take ~0.3s.
        now = datetime.now().isoformat()
        with self._lock, self.conn:
            existing = dict(self.conn.execute("SELECT key,value FROM user_profile").fetchall())
            self.conn.executemany(
                "INSERT OR REPLACE INTO user_profile (key,value,updated) VALUES (?,?,?)",
                [(k, v, now) for k, v in seed_dict.items() if existing.get(k) is None],
            )

    def seed_facts(self, fact_list):
        """Preload starter facts WITHOUT duplicating them on every restart.
        Skips any fact whose content already exists verbatim."""
        now = datetime.now().isoformat()
        with self._lock, self.conn:
            existing = {
                row[0]
                for row in self.conn.execute(
                    "SELECT content FROM facts WHERE category='system'"
                ).fetchall()
            }
            self.conn.executemany(
                "INSERT INTO facts (category,content,importance,keywords,timestamp) VALUES (?,?,?,?,?)",
                [(category, content, importance, _keywords(content), now)
                 for content, category, importance in fact_list if content not in existing],
            )
