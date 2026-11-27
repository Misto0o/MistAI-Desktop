"""
tools/repro_comtypes_cache.py -- REAL-module check of the Windows voice
(pyttsx3 over SAPI) with a non-writable comtypes gen folder.

    python tools/repro_comtypes_cache.py

Each case runs in a fresh Python subprocess against the real comtypes and
pyttsx3 (it generates the SAPI wrapper modules, it does not speak):

  before    comtypes' own choice: an empty gen folder that refuses writes
            (as site-packages does under an all-users Python). Expected to
            FAIL with PermissionError, the real-log symptom.
  fixed     the same, after config.configure_comtypes_cache(): the modules
            go to a temporary user cache and pyttsx3 starts.
  corrupt   the user cache holds a truncated SpeechLib.py: the first init
            fails, tts._repair_speech_cache() rebuilds it, the retry starts.
  memory    the user cache refuses writes too: generation in memory only.

Only temporary folders are written; site-packages is never touched (its
generated modules are hidden from the child process, not removed).
"""

# ============================================================
# CHILD PROCESS
# ============================================================

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import builtins, json, os, sys
sys.path.insert(0, ROOT)
case, ro_dir, cache = sys.argv[1], sys.argv[2], sys.argv[3]
deny = [os.path.normcase(ro_dir)]
if case == "memory":
    deny.append(os.path.normcase(cache))
_open = builtins.open
def guarded_open(file, mode="r", *a, **k):
    p = os.path.normcase(os.path.abspath(str(file)))
    if any(c in mode for c in "wax+") and any(p.startswith(d) for d in deny):
        raise PermissionError(13, "Permission denied", str(file))
    return _open(file, mode, *a, **k)
builtins.open = guarded_open
import tempfile
_mk = tempfile.mkstemp
def guarded_mkstemp(*a, dir=None, **k):
    if dir and any(os.path.normcase(os.path.abspath(dir)).startswith(d) for d in deny):
        raise PermissionError(13, "Permission denied", dir)
    return _mk(*a, dir=dir, **k)
tempfile.mkstemp = guarded_mkstemp

import comtypes.client, comtypes.gen
# What comtypes picks under an all-users Python: its gen folder "passes"
# os.access but refuses the write. Hide the already generated modules.
comtypes.gen.__path__ = [ro_dir]
comtypes.client.gen_dir = ro_dir
from core import config, tts
out = {"case": case}
if case != "before":
    os.makedirs(cache, exist_ok=True)
    if case == "corrupt":
        with _open(os.path.join(cache, "SpeechLib.py"), "w") as f:
            f.write("from comtypes import (\n")       # truncated file
    out["mode"] = config.configure_comtypes_cache(folder=cache)
tries = []
for attempt in (1, 2):
    try:
        import pyttsx3
        eng = pyttsx3.init()
        tries.append("ok")
        break
    except Exception as e:
        tries.append(type(e).__name__)
        if case == "before" or attempt == 2:
            break
        out["repair"] = tts._repair_speech_cache()
out["tries"] = tries
out["cache_files"] = sorted(n for n in os.listdir(cache) if n.endswith(".py")) if os.path.isdir(cache) else []
print("RESULT " + json.dumps(out))
'''.replace("ROOT", repr(ROOT))


# ============================================================
# RUNNER
# ============================================================

def run_case(case):
    with tempfile.TemporaryDirectory(prefix="mist_comcache_") as tmp:
        ro_dir = os.path.join(tmp, "site_gen")
        cache = os.path.join(tmp, "user_cache")
        os.makedirs(ro_dir)
        p = subprocess.run([sys.executable, "-c", CHILD, case, ro_dir, cache],
                           capture_output=True, text=True, timeout=180, cwd=ROOT)
        for line in p.stdout.splitlines():
            if line.startswith("RESULT "):
                return json.loads(line[7:])
        return {"case": case, "error": (p.stderr or p.stdout)[-600:]}


def main():
    want = {"before": False, "fixed": True, "corrupt": True, "memory": True}
    bad = 0
    for case, ok_wanted in want.items():
        r = run_case(case)
        ok = r.get("tries", [None])[-1] == "ok"
        verdict = "PASS" if ok == ok_wanted else "FAIL"
        bad += verdict == "FAIL"
        print(f"{verdict}  {case:8s} tries={r.get('tries')} mode={r.get('mode')} "
              f"repair={r.get('repair')} cache_files={len(r.get('cache_files', []))}"
              + (f" error={r['error']}" if "error" in r else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
