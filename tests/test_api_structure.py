"""core/api.py is split into mixins by topic (methods moved verbatim). Pins
what the split may not break: Api still has every method, and the methods
that read names tests rebind on core.api (or that tests find by file) stay
in core/api.py, where those patches reach them."""

import ast
import os

import core.api as api_mod
from core.api import Api

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REBOUND = ("wake_clips_enabled", "recipient_match", "_show_window_async", "HEARTBEAT_SECONDS")
MIXIN_MODULES = ("core.api_browser", "core.api_system", "core.api_windows")


def test_api_inherits_the_mixins_and_keeps_their_methods():
    names = [c.__module__ for c in Api.__mro__[1:-1]]
    assert sorted(names) == sorted(MIXIN_MODULES)
    for name in ("_act_browser_search", "_act_browser_get_text", "get_battery_status",
                 "remember_fact", "_act_window_activate", "_act_window_list"):
        assert callable(getattr(Api, name)), name


def test_no_mixin_reads_a_name_tests_rebind_on_core_api():
    for mod in MIXIN_MODULES:
        path = os.path.join(REPO, *mod.split(".")) + ".py"
        tree = ast.parse(open(path, encoding="utf-8").read())
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not used & set(REBOUND), (mod, used & set(REBOUND))
    for name in REBOUND:
        assert hasattr(api_mod, name), name


def test_methods_reading_rebound_names_are_defined_in_api_py():
    for name in ("_google_wake_loop", "_qm_read_name", "_restore_after_task",
                 "_start_heartbeat", "_heartbeat_tick", "_ptt_turn"):
        assert name in Api.__dict__, name
