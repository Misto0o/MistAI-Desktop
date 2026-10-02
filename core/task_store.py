"""
core/task_store.py — persistent TASK memory (design doc #2), distinct from
both conversational memory (core/memory.py) and pattern-outcome memory
(core/task_memory.py). This is: "what multi-phase goals exist, how far did
each get, and can it be resumed."

A TaskRecord stores the goal, the full plan DAG (with per-node statuses and
results), outputs, and files created. It is saved after EVERY node
transition, so if MistAI crashes mid-goal the record is still on disk with
status 'running' -- "Mist, resume PortfolioSite" picks it back up.

Same SQLite file as the rest of MistAI's memory (established pattern:
core/task_memory.py already opens its own connection to it).
"""

import difflib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime

RESUMABLE_STATUSES = ("paused", "running", "failed", "partial")


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


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return s[:40] or "task"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


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
