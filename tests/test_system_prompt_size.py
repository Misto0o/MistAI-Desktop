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
# and read actions are one per reply (the LOOP rule tightened to make room), then
# for the action menu generated from core/tools.py (per-action text moved there),
# then for the compact menu (one line per action; details via describe_action),
# then for the rules merged without dropping one (toggles, UIA inputs, browser),
# then for one short thinking sentence in RESPONSE FORMAT.
PROMPT_HASHES = {
    "plain": "653ddd2fe110da29b1abd10ad4c2c780efe4e1821f71f2b0078063a55a4bdf10",
    "project": "40e68057a35ccff2329fbf6acf91bad9cdf5d9b18ed523af8026bf80342ab87c",
    "long-screen": "b38042578c376094c431f08c0ecd1b78541e76defe9dee1a6fb314e2fdd54a2c",
    "hints-and-conversation": "9fe72f4cb61f99c157997d1ee0cd160c2fb57430262dfdd3f4a4db1d2a619f5d",
}
PLAIN_LENGTH = 13532            # 13,417 before the thinking cap; 21,056 originally


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
    bad = [l for l in _template_lines() if l != l.strip()]
    assert not bad, bad[:3]
    text = build("plain")
    # Line breaks and list markers are kept.
    assert "\n- run_command <the command>: Run PowerShell" in text
    assert "\n1. run_command (PowerShell)" in text
    assert "\n- done: End the task" in text


def test_interpolated_blocks_keep_their_own_layout():
    text = build("long-screen")
    assert LONG_SCREEN in text                       # the screen description is not re-indented
    assert PROJECT_BLOCK in build("project")


def test_the_plain_prompt_length_is_pinned_with_headroom():
    n = len(build("plain"))
    assert n == PLAIN_LENGTH
    assert n < BACKEND_SYSTEM_LIMIT * 0.8
