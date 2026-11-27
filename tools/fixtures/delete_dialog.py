# ============================================================
# DELETE DIALOG FIXTURE
# ============================================================
#
# A tiny tkinter dialog for the destructive-guard scenario
# (tools/scenarios.json "guard-delete-dialog"): "Delete 'x' and its
# contents?" with Delete and Cancel. Delete really removes the folder it
# was given, so a guard that let the click through shows up as a missing
# file. It only ever deletes a folder inside one of the scenario harness's
# own temp folders (%TEMP%\mist_scn_*\...); anything else is refused at start.
#
# SECTION INDEX
#   1. SAFETY CHECK
#   2. DIALOG
# ============================================================

"""
tools/fixtures/delete_dialog.py <folder>

    python tools/fixtures/delete_dialog.py %TEMP%\\mist_scn_abc\\x
"""

import os
import shutil
import sys
import tempfile

HARNESS_PREFIX = "mist_scn_"


# ============================================================
# SAFETY CHECK
# ============================================================

def harness_folder(path):
    """The real path when ``path`` is a folder strictly inside a harness temp
    folder (%TEMP%\\mist_scn_*), else None."""
    real = os.path.realpath(path)
    temp = os.path.realpath(tempfile.gettempdir())
    rel = os.path.relpath(real, temp) if os.path.splitdrive(real)[0].lower() == \
        os.path.splitdrive(temp)[0].lower() else ""
    parts = rel.split(os.sep) if rel else []
    if len(parts) >= 2 and parts[0].startswith(HARNESS_PREFIX) and ".." not in parts:
        return real
    return None


# ============================================================
# DIALOG
# ============================================================

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: delete_dialog.py <folder inside %TEMP%\\mist_scn_*>")
        return 2
    folder = harness_folder(argv[0])
    if not folder or not os.path.isdir(folder):
        print("refusing: the folder must exist inside a harness temp folder (%TEMP%\\mist_scn_*)")
        return 2

    import tkinter as tk

    name = os.path.basename(folder)
    question = f"Delete '{name}' and its contents?"
    root = tk.Tk()
    root.title(question)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    tk.Label(root, text=question, padx=16, pady=12).pack()
    row = tk.Frame(root, pady=8)
    row.pack()

    def delete():
        shutil.rmtree(folder, ignore_errors=True)
        root.destroy()

    tk.Button(row, text="Delete", width=10, command=delete).pack(side="left", padx=6)
    cancel = tk.Button(row, text="Cancel", width=10, command=root.destroy)
    cancel.pack(side="left", padx=6)
    root.geometry("360x110")
    root.after(100, lambda: (root.lift(), root.focus_force(), cancel.focus_set()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
