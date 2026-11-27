"""
tools/repro_uia_tree.py -- REAL desktop, READ-ONLY look at what UI Automation
exposes for a few apps (no clicks, no typing, never a photo, nothing deleted).

    python tools/repro_uia_tree.py --yes [--list] [--json out.json] [--apps Camera,Photos]

For each app it opens the app (Start-apps AppID, exe or URI), waits for its
window, reads the accessibility tree twice -- (a) a plain recursive walk
through pywinauto (like today's screen read) and (b) one cached FindAll
(CacheRequest with the properties the element list needs, bounded by
element count and time) -- prints counts, timings and (Camera/Photos) the
control names and shortcut keys, then closes ONLY the window it opened
(WM_CLOSE). An app that was already open is read and left open.

Close anything private first: control names (file names in Photos) are
printed to the console.
"""

# ============================================================
# SETUP
# ============================================================

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

INTERACTIVE = ("Button", "MenuItem", "TabItem", "ListItem", "DataItem", "CheckBox",
               "RadioButton", "Edit", "ComboBox", "Hyperlink", "Slider", "SplitButton",
               "TreeItem", "Spinner")
PRINT_NAMES_FOR = ("Camera", "Photos")
LIST_MODE = False          # --list: print MistAI's numbered control list instead

# (label, how to open, how to recognise its window by title word)
APPS = [
    ("Calculator", ("appname", "Calculator"), "Calculator"),
    ("Notepad", ("exe", "notepad.exe"), "Notepad"),
    ("Settings", ("uri", "ms-settings:"), "Settings"),
    ("File Explorer", ("exe", "explorer.exe"), "File Explorer"),
    ("Camera", ("appname", "Camera"), "Camera"),
    ("Photos", ("appname", "Photos"), "Photos"),
]


# ============================================================
# WINDOWS
# ============================================================

def top_windows():
    import win32gui

    out = {}

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
            out[h] = (win32gui.GetWindowText(h), win32gui.GetClassName(h))
        return True

    win32gui.EnumWindows(cb, None)
    return out


def proc_name(hwnd):
    import psutil
    import win32process

    try:
        return psutil.Process(win32process.GetWindowThreadProcessId(hwnd)[1]).name()
    except Exception:
        return "?"


def find_window(word, among):
    for h, (title, cls) in among.items():
        if word.lower() in title.lower() and "MistAI" not in title:
            return h
    return None


def launch(how):
    kind, what = how
    if kind == "exe":
        subprocess.Popen([what])
    elif kind == "uri":
        os.startfile(what)
    else:
        from core.start_apps import StartAppsCache

        found = StartAppsCache().lookup(what)
        if not found:
            raise RuntimeError(f"no Start-apps entry for {what}")
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{found[0]['app_id']}"])
        return found[0]["app_id"]
    return what


# ============================================================
# (A) PLAIN RECURSIVE WALK (pywinauto, like today's read)
# ============================================================

def plain_walk(hwnd, budget=10.0, max_elements=4000):
    from pywinauto.uia_element_info import UIAElementInfo

    t0 = time.perf_counter()
    queue, n, types = [UIAElementInfo(hwnd)], 0, {}
    while queue and n < max_elements and time.perf_counter() - t0 < budget:
        info = queue.pop(0)
        n += 1
        try:
            types[info.control_type] = types.get(info.control_type, 0) + 1
        except Exception:
            pass
        try:
            queue.extend(info.children())
        except Exception:
            pass
    return n, time.perf_counter() - t0, bool(queue)


def todays_read(hwnd):
    """What the screen read returns today (screen._walk_text), as lengths."""
    from core.screen import TEXT_CAP, _walk_text

    t0 = time.perf_counter()
    text, timed_out = _walk_text(hwnd, time.monotonic() + 1.5, TEXT_CAP)
    title = top_windows().get(hwnd, ("", ""))[0]
    parts = [p for p in text.split(" | ") if p]
    return {"chars": len(text), "parts": len(parts), "timed_out": timed_out,
            "title_only": all(p.strip().lower() == title.strip().lower() for p in parts),
            "cut_at_cap": len(text) >= TEXT_CAP - 3, "s": time.perf_counter() - t0}


# ============================================================
# (B) CACHED FINDALL
# ============================================================

def cached_findall(hwnd, budget=3.0):
    from pywinauto.uia_defines import IUIA

    iuia = IUIA()
    u, dll = iuia.iuia, iuia.UIA_dll
    props = {
        "name": dll.UIA_NamePropertyId, "type": dll.UIA_ControlTypePropertyId,
        "enabled": dll.UIA_IsEnabledPropertyId, "offscreen": dll.UIA_IsOffscreenPropertyId,
        "accel": dll.UIA_AcceleratorKeyPropertyId, "access": dll.UIA_AccessKeyPropertyId,
        "cls": dll.UIA_ClassNamePropertyId, "pid": dll.UIA_ProcessIdPropertyId,
    }
    req = u.CreateCacheRequest()
    for pid in props.values():
        req.AddProperty(pid)
    t0 = time.perf_counter()
    root = u.ElementFromHandle(hwnd)
    found = root.FindAllBuildCache(dll.TreeScope_Descendants, u.CreateTrueCondition(), req)
    rows = []
    for i in range(found.Length):
        if time.perf_counter() - t0 > budget:
            break
        el = found.GetElement(i)
        row = {k: el.GetCachedPropertyValue(p) for k, p in props.items()}
        row["type"] = iuia.known_control_type_ids.get(row["type"], str(row["type"]))
        rows.append(row)
    root_pid = root.CurrentProcessId
    return rows, time.perf_counter() - t0, root_pid


# ============================================================
# ONE APP
# ============================================================

def read_app(label, how, word, show_names):
    before = top_windows()
    hwnd = find_window(word, before)
    opened = False
    if hwnd is None:
        launch(how)
        deadline = time.time() + 10
        while time.time() < deadline and hwnd is None:
            time.sleep(0.4)
            new = {h: v for h, v in top_windows().items() if h not in before}
            hwnd = find_window(word, new)
        opened = hwnd is not None
        time.sleep(2.5)                       # let the content populate
    if hwnd is None:
        return {"app": label, "error": "no window found"}
    title, cls = top_windows().get(hwnd, ("?", "?"))
    res = {"app": label, "class": cls, "process": proc_name(hwnd), "opened_by_check": opened}
    try:
        if LIST_MODE:
            # What MistAI's numbered control list says for this window.
            from core.click_handler import (ControlNumbers, format_control_list,
                                            order_controls, read_controls)

            got = read_controls(hwnd)
            listed, counts = order_controls(got["rows"], title=title)
            text = format_control_list(ControlNumbers().stamp(hwnd, listed), counts, title,
                                       got["timed_out"])
            res["list"] = {"ms": round(got["ms"]), "listed": len(listed), "chars": len(text),
                           **{k: counts[k] for k in ("unnamed", "offscreen", "more",
                                                     "named_content", "popup")}}
            if show_names:
                res["controls"] = text.splitlines()
            return res
        res["today"] = todays_read(hwnd)
        n, s, more = plain_walk(hwnd)
        res["plain_walk"] = {"elements": n, "s": round(s, 3), "unfinished": more}
        rows, s, root_pid = cached_findall(hwnd)
        res["cached_findall_s"] = round(s, 3)
        res["elements"] = len(rows)
        types = {}
        for r in rows:
            types[r["type"]] = types.get(r["type"], 0) + 1
        res["by_type"] = dict(sorted(types.items(), key=lambda kv: -kv[1]))
        res["content_hosted"] = any(r["cls"] == "Windows.UI.Core.CoreWindow" for r in rows)
        res["other_process_elements"] = sum(1 for r in rows if r["pid"] != root_pid)
        inter = [r for r in rows if r["type"] in INTERACTIVE]
        res["interactive"] = len(inter)
        res["interactive_named"] = sum(1 for r in inter if (r["name"] or "").strip())
        res["interactive_unnamed"] = res["interactive"] - res["interactive_named"]
        res["with_shortcut"] = sum(1 for r in inter if (r["accel"] or r["access"]))
        res["offscreen"] = sum(1 for r in inter if r["offscreen"])
        res["disabled"] = sum(1 for r in inter if not r["enabled"])
        if show_names:
            res["controls"] = [
                f'{r["type"]} "{(r["name"] or "")[:60]}"'
                + (f' (shortcut: {r["accel"] or r["access"]})' if r["accel"] or r["access"] else "")
                + (" (off-screen)" if r["offscreen"] else "")
                + ("" if r["enabled"] else " (disabled)")
                for r in inter][:120]
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        if opened:
            import win32con
            import win32gui

            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                res["closed"] = True
            except Exception:
                res["closed"] = False
            time.sleep(1.0)
    return res


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="really open the apps (read-only)")
    ap.add_argument("--json", default="")
    ap.add_argument("--apps", default="")
    ap.add_argument("--list", action="store_true", help="show MistAI's numbered control list")
    a = ap.parse_args()
    global LIST_MODE
    LIST_MODE = a.list
    if not a.yes:
        print(__doc__)
        return 2
    want = {x.strip().lower() for x in a.apps.split(",") if x.strip()}
    results = []
    for label, how, word in APPS:
        if want and label.lower() not in want:
            continue
        print(f"\n=== {label} ===", flush=True)
        r = read_app(label, how, word, label in PRINT_NAMES_FOR)
        results.append(r)
        for k, v in r.items():
            if k != "controls":
                print(f"  {k}: {v}")
        for line in r.get("controls", []):
            print(f"    {line}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
