"""The agent system prompt's template carries no indentation or trailing
spaces (the backend rejects a system prompt over 32,000 chars), and the
whitespace strip changed no word: for several fixed states the prompt,
with every whitespace run collapsed to one space, hashes to the value
recorded from the indented template before the strip.

When the prompt is changed ON PURPOSE, update PROMPT_HASHES and
PLAIN_LENGTH (print them with: python -m pytest tests/test_system_prompt_size.py -q -s)."""

import hashlib
import inspect
import re
from types import SimpleNamespace

import pytest

from core.agent import BACKEND_SYSTEM_LIMIT, AgentLoop

LONG_SCREEN = "\n".join(
    f"    [{i}] Button '{name} {i}' at ({10 * i}, {20 + i})  enabled"
    for i, name in enumerate(["Save", "Open", "Close", "Print", "Share", "Undo"] * 7)
)
PROJECT_BLOCK = ("WORKSPACE:\n  Current project: C:\\work\\demo-site (trusted)\n"
                 "  Files: index.html, style.css, app.js\n  Last command: npm run build -> exit 0")

STATES = {
    "plain": dict(gender="none", workspace="", screen="Window: Desktop", last="", hints=[], convo=""),
    "project": dict(gender="female", workspace=PROJECT_BLOCK, screen="Window: Visual Studio Code",
                    last="run_command", hints=[], convo=""),
    "long-screen": dict(gender="none", workspace="", screen="Window: Settings\n" + LONG_SCREEN,
                        last="click_on_text", hints=[], convo=""),
    "hints-and-conversation": dict(
        gender="male", workspace="", screen="Window: Discord", last="",
        hints=["- discord: the quick switcher is ctrl+k", "- notepad: Windows 11 restores tabs"],
        convo="\nRECENT CONVERSATION:\n  user: what's the weather\n  you: Sunny, 21 C.\n"),
}

# Recorded from the indented template (before the strip) for each state;
# updated on purpose for the batched-steps LOOP rule (default on).
PROMPT_HASHES = {
    "plain": "7fdac63552afdd3cd7923e7a136463fb2ee4b2690aa7c912ac227cd37a3a363a",
    "project": "8189cbf282aac2b5e0352441eea70b706395ee1b5dbbe81f1b95f4f239dc3be5",
    "long-screen": "3e1c4d84c25ee91546b4f1038e4071b6a69167b7a64e80dcc889ec32f5400f07",
    "hints-and-conversation": "95447f8efd581d1af97b6b1ed83b55f535e4d82e69c717ffeabc2c300898c469",
}
PLAIN_LENGTH = 20945            # 20,749 before the batched-steps rule (MISTAI_BATCH_STEPS)


def build(state):
    s = STATES[state]
    api = SimpleNamespace(workspace=SimpleNamespace(context_block=lambda: s["workspace"]),
                          _resolve_path_placeholders=lambda p: p)
    snap = {"open_apps": [], "foreground_window": "Desktop", "screen_resolution": (1920, 1080),
            "cpu_percent": 12, "ram_percent": 48}
    text = AgentLoop(api, None)._build_system(s["gender"], snap, s["screen"], s["last"],
                                              s["hints"], s["convo"])
    # The clock is the only moving part.
    return re.sub(r"CURRENT DATE/TIME: [^\n]*", "CURRENT DATE/TIME: <now>", text)


def collapsed(text):
    return " ".join(text.split())


def digest(text):
    return hashlib.sha256(collapsed(text).encode("utf-8")).hexdigest()


@pytest.mark.parametrize("state", sorted(STATES))
def test_the_strip_changed_no_word(state):
    print(f"\n{state}: {digest(build(state))} ({len(build(state))} chars)")
    assert digest(build(state)) == PROMPT_HASHES[state]


def _template_lines():
    src = inspect.getsource(AgentLoop._build_system).split("\n")
    start = next(i for i, l in enumerate(src) if 'return f"""You are MistAI' in l)
    end = next(i for i in range(start + 1, len(src)) if src[i].endswith('"""'))
    return src[start + 1:end + 1]


def test_the_template_has_no_indentation_or_trailing_spaces():
    # One line keeps its 4 spaces: tests/test_grounding_rules.py finds the end of
    # the quick_message menu entry by it ("\n    - ").
    kept = ["    - open_project_in_vscode : open an EXISTING project folder in a NEW VS Code"]
    bad = [l for l in _template_lines() if l != l.strip() and l not in kept]
    assert not bad, bad[:3]
    text = build("plain")
    # Line breaks and list markers are kept.
    assert "\n- run_command       : Execute a PowerShell command" in text   # inner spacing stays
    assert "\n1. run_command (PowerShell)" in text
    assert "\n- done              : Task is complete." in text


def test_interpolated_blocks_keep_their_own_layout():
    text = build("long-screen")
    assert LONG_SCREEN in text                       # the screen description is not re-indented
    assert PROJECT_BLOCK in build("project")


def test_the_plain_prompt_length_is_pinned_with_headroom():
    n = len(build("plain"))
    assert n == PLAIN_LENGTH
    assert n < BACKEND_SYSTEM_LIMIT * 0.8
