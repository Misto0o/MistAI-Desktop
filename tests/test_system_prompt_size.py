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
# updated on purpose for the batched-steps LOOP rule (default on), then for the
# merged BROWSER search rule (DuckDuckGo; two nearby rules tightened to make room),
# then for the batch form moved from the LOOP rule into RESPONSE FORMAT (the model
# never batched while the last format it read showed one action only), then for
# the steps form naming its five input actions and that run_command, file, browser
# and read actions are one per reply (the LOOP rule tightened to make room).
PROMPT_HASHES = {
    "plain": "6135c7a5ebfb8eedf1d60523fec9d9538187a508c2699da2a8a6c338cae69e5d",
    "project": "5a37832d469e0521a8400027a95f84a2f79bbe7f2a6c9c1ad72df36d32f9a69e",
    "long-screen": "cae631008ee81c812e13c3c98f3a6d6b90ad54b3d1922c29a2bb101c89a80177",
    "hints-and-conversation": "e153afcd0bdac1a31ad93b1bb30b92b00f60068bfb6fb89dbbf8a19ef438578e",
}
PLAIN_LENGTH = 21056            # 21,049 before the steps form named its five actions


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
