"""The "Import my facts" button in Settings (ui/html.py #btn-import-facts).

The page only shows what the REAL Api.import_my_facts() reports (computed
here against a temporary database and facts file). Node tests skip when Node
is missing.
"""

import json

import pytest

from core.api import Api
from core.memory import Memory
from test_ui_model_card import HTML, _run_page, _script, needs_node


@pytest.fixture
def real_result(monkeypatch, tmp_path):
    """real_result(content) -> what Api.import_my_facts() returns for a facts
    file with that content (None: no file)."""
    monkeypatch.setenv("MISTAI_DB_PATH", str(tmp_path / "memory.db"))
    path = tmp_path / "my_facts.json"
    monkeypatch.setenv("MISTAI_MY_FACTS_PATH", str(path))
    api = Api.__new__(Api)
    api.memory = Memory()

    def run(content):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        return api.import_my_facts()

    yield run
    api.memory.conn.close()


def test_the_button_sits_in_settings_and_shows_text_only():
    assert '<button class="btn-cancel" id="btn-import-facts"' in HTML
    assert HTML.index('id="btn-import-facts"') < HTML.index('onclick="saveAPIKey()"')
    js = _script()
    handler = js.split("document.getElementById('btn-import-facts').onclick=async()=>{", 1)[1] \
        .split("\n};\n", 1)[0]
    assert "const r=await pywebview.api.import_my_facts();" in handler
    assert "note.textContent=(r&&r.message)||'Import finished';" in handler
    assert "innerHTML" not in handler and "try{" in handler and "}catch(e){" in handler


def _click(tmp_path, result=None, **config):
    r = _run_page(tmp_path, actions=[{"click": "btn-import-facts"}], facts_result=result,
                  **config)
    return r["actions"][0], r


@needs_node
def test_a_missing_file_is_a_plain_note(tmp_path, real_result):
    result = real_result(None)
    snap, _ = _click(tmp_path, result)
    assert snap["facts"] == [result["message"], "card-note"]          # not an error
    assert snap["facts"][0].startswith("No facts file found.")
    assert snap["errors"] == []


@needs_node
def test_an_import_reports_what_it_added_and_refreshes_the_counters(tmp_path, real_result):
    result = real_result(json.dumps(["I park on level 3", "My dentist is Dr. Lee"]))
    before = _run_page(tmp_path, facts_result=result)["calls"].count("get_memory_stats")
    snap, _ = _click(tmp_path, result)
    assert snap["facts"] == ["Imported 2 facts (0 already saved, 0 restored).", "card-note"]
    assert snap["statsCalls"] == before + 1            # the Facts count updates


@needs_node
def test_an_unreadable_file_and_a_failed_call_are_errors(tmp_path, real_result):
    snap, _ = _click(tmp_path, real_result("{not json"))
    assert snap["facts"][0].startswith("Could not read ") and snap["facts"][1] == "card-note error"
    snap, r = _click(tmp_path, None, fail={"import_my_facts": "reject"})
    assert snap["facts"] == ["Import failed: import_my_facts failed", "card-note error"]
    assert any(e.startswith("[facts] import failed:") for e in snap["errors"])
