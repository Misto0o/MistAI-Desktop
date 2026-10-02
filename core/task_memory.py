"""
core/task_memory.py — operational memory: what actions worked or failed
before, at a pattern level (not tied to specific literal parameters like a
recipient's name). Separate from core/memory.py's conversational/fact
memory on purpose -- this is "MistAI's learned experience," not "things
the user told MistAI about themselves."

Goal A: don't repeat known failures (e.g. re-investigating a nonexistent
app every single time it's asked to open).
Goal B: reuse known-good strategies (e.g. prefer quick_message for
Discord messaging once it's proven to work, instead of re-deriving via
UIA/OCR every time).
"""

import os
import re
import sqlite3
import threading
from datetime import datetime

# A signature key shorter than this is too generic to match against task
# text: a click on "a new tab" produced the key "a", which then injected a
# "clicking 'a' has failed" hint into EVERY task containing the letter a.
MIN_KEY_LEN = 3


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

        if action in ("uia_click", "click_on_text", "click_nth"):
            # Generalize by the FIRST word of the target text, not the
            # whole literal string -- "Cheese" and "Wy" as Discord contacts
            # shouldn't create separate signatures, but distinct UI concepts
            # (e.g. "Send" button vs "Cancel" button) should.
            key = self._normalize(param.split("|")[-1].split()[0]) if param else ""
            if len(key) < MIN_KEY_LEN:
                return None  # "a", "ok", "x" match everything; not worth storing
            return f"click:{key}", "clicking"

        if action == "browser_search" or action == "browser_navigate":
            return None  # too variable to be worth pattern-caching

        return None  # most actions (run_command, write_file, etc) are
        # inherently task-specific -- not worth caching

    def record(self, action, parameter, success, note=""):
        result = self.build_signature(action, parameter)
        if not result:
            return
        signature, category = result

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
                        note[:300],
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
                        note[:300],
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
