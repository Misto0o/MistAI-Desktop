"""TaskMemory must be usable in tests and avoid substring-based hints."""

from core.task_memory import TaskMemory
from core.task_store import TaskStore


def test_memory_database_target_is_usable_without_a_parent_directory():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "Code", False, "not installed")

        hints = memory.get_hints_for_task("Please open Code.")

        assert len(hints) == 1
        assert "code" in hints[0].lower()
    finally:
        memory.conn.close()


def test_task_memory_matches_signature_keys_as_words_not_substrings():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "code", False, "not installed")

        assert memory.get_hints_for_task("Decode this base64 string.") == []
        assert memory.get_hints_for_task("Can you open code for me?")
    finally:
        memory.conn.close()


def test_task_memory_matches_normalized_multiword_app_names():
    memory = TaskMemory(":memory:")
    try:
        memory.record("open_app", "Apple Music", False, "not installed")

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
