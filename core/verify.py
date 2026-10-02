"""
core/verify.py — post-action verification (design doc #8): the agent
verifies instead of assuming.

Each verifier answers "did that action ACTUALLY take effect?" against
ground truth (disk, process list, foreground window) -- never against a
screenshot guess. Return contract:

    (True,  note)  -> independently confirmed
    (False, note)  -> independently CONTRADICTED (executor downgrades the
                      action to a failure even if the handler returned ok)
    (None,  note)  -> cannot verify (unknown != success; confidence stays
                      at the tool's base level)

Verifiers are looked up by ToolSpec.verifier; ToolSpec.verifier_arg can pin
a static target (e.g. browser_* always verifies the 'firefox' process),
otherwise the target derives from the action parameter.
"""

import os


def _resolve(api, path):
    try:
        return api._resolve_path_placeholders(path)
    except Exception:
        return path


def _file_written(api, action, param, arg):
    if "|||" not in (param or ""):
        return None, "no path to verify"
    path = _resolve(api, param.split("|||", 1)[0].strip())
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            api.workspace.note_file(path)
            return True, f"file exists ({os.path.getsize(path)} bytes)"
        return False, f"file missing or empty at {path}"
    except Exception as e:
        return None, f"verify error: {e}"


def _file_exists(api, action, param, arg):
    path = _resolve(api, (arg or param or "").strip())
    if not path:
        return None, "no path"
    return (os.path.isfile(path), f"checked {path}")


def _folder_exists(api, action, param, arg):
    path = _resolve(api, (arg or param or "").strip())
    if not path:
        return None, "no path"
    return (os.path.isdir(path), f"checked {path}")


def _process_running(api, action, param, arg):
    target = (arg or param or "").strip()
    if not target:
        return None, "no target process"
    try:
        running = api.is_app_running(target)
        return (running, f"process '{target}' {'found' if running else 'NOT found'}")
    except Exception as e:
        return None, f"verify error: {e}"


def _window_focused(api, action, param, arg):
    target = (arg or param or "").strip().lower()
    if not target:
        return None, "no target window"
    try:
        fw = api.live.snapshot.get("foreground_window", "").lower()
        ok = bool(fw) and (target in fw or fw in target)
        return (ok, f"foreground is '{fw[:40]}'")
    except Exception as e:
        return None, f"verify error: {e}"


def _app_visible(api, action, param, arg):
    """open_app's real intent: the app is on screen and usable -- not merely
    that a process by that name exists. Discord minimised to the tray has a
    process and no window; that used to verify as a success (conf 0.93)
    while VS Code stayed in front of the user.

        foreground  -> True   (it's in front of the user)
        visible     -> True   (has a top-level window; user can reach it)
        hidden      -> False  (running, but nothing to interact with)
        not running -> False
        can't tell  -> None   (win32 unavailable; falls back to base confidence)

    Uses Api.app_window_state(), the same helper _act_open_app decides with,
    so handler and verifier can never disagree about the same fact.
    """
    target = (arg or param or "").strip()
    if not target:
        return None, "no target app"
    try:
        state, title = api.app_window_state(target)
    except Exception as e:
        return None, f"verify error: {e}"
    if state == "foreground":
        return True, f"'{target}' is the foreground window ('{title[:40]}')"
    if state == "visible":
        return (
            True,
            f"'{target}' has a visible window ('{title[:40]}') but is not in front",
        )
    if state == "hidden":
        return (
            False,
            f"'{target}' is running but has NO visible window (tray/background only)",
        )
    if state == "not_running":
        return False, f"process '{target}' NOT found"
    return None, "could not determine window state"


# Words whose own label is expected to CHANGE the instant a click lands
# (Subscribe -> Subscribed, Follow -> Following, ...). Outside this list we
# stay silent (None) rather than guess -- most clicked text (links, menu
# items) is SUPPOSED to still be readable somewhere after a correct click,
# so "the word is still there" isn't evidence of failure in general.
_TOGGLE_TARGETS = {
    "subscribe": "subscribed",
    "follow": "following",
    "like": "liked",
    "save": "saved",
    "mute": "muted",
    "block": "blocked",
    "join": "joined",
    "unsubscribe": "subscribe",
    "unfollow": "follow",
    "unlike": "like",
    "unsave": "save",
    "unmute": "mute",
    "unblock": "block",
    "leave": "join",
}


def _click_landed(api, action, param, arg):
    """Sanity check for click_on_text/click_nth on known toggle labels.

    Requires the EXPECTED post-toggle word (Subscribe -> Subscribed), not
    just "any different text at that spot" -- a click that misses the real
    button and lands on some unrelated element will usually find DIFFERENT
    text there too (it's a different element!), which a naive before/after
    diff would wrongly read as a successful toggle. Only a match against
    the actual expected target word counts as confirmed success; anything
    else stays unverified (None) rather than a false positive."""
    try:
        handler = api.click_handler
    except Exception:
        return None, "no click handler"
    query = (handler.last_query or "").strip().lower()
    target = _TOGGLE_TARGETS.get(query)
    if not target:
        return None, "not a recognized toggle target"
    after = handler.recheck_text_near_last_click()
    if not after:
        return None, "OCR recheck found nothing"
    after_words = {w.strip(".,!?:;").lower() for w in after.split()}
    if target in after_words:
        return (
            True,
            f"label now reads '{after.strip()[:40]}' -- matches expected '{target}'",
        )
    if query in after_words:
        return (
            False,
            f"label still reads '{after.strip()[:40]}' -- click did not toggle it",
        )
    # Different text, but not the expected target word -- most likely the
    # click landed on the WRONG element entirely (a different label exists
    # there for unrelated reasons). Don't count this as confirmation.
    return None, (
        f"label reads '{after.strip()[:40]}', neither the original "
        f"nor the expected '{target}' -- click may have missed "
        f"the actual button"
    )


VERIFIERS = {
    "file_written": _file_written,
    "file_exists": _file_exists,
    "folder_exists": _folder_exists,
    "process_running": _process_running,
    "window_focused": _window_focused,
    "app_visible": _app_visible,
    "click_landed": _click_landed,
}


def run(name, api, action, param, arg=""):
    """Dispatch a verifier by name. Unknown/missing names -> (None, '')."""
    fn = VERIFIERS.get(name)
    if not fn:
        return None, ""
    try:
        return fn(api, action, param, arg)
    except Exception as e:
        return None, f"verifier crashed: {e}"
