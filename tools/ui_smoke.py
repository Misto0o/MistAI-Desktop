"""
tools/ui_smoke.py — opt-in check that the real page starts in real WebView2.

    python tools/ui_smoke.py                 # both modes: dev, then build
    python tools/ui_smoke.py --flags build   # one mode (dev | build)

Not part of pytest or CI. Windows only; needs pywebview and the Edge WebView2
runtime (what the app itself needs). No microphone, no Node, no network:

- memory/tasks/tool stats go to a throwaway SQLite file (MISTAI_DB_PATH),
  seeded with user_gender=female and model=mistral;
- Api.check_api_status is stubbed at class level (so pywebview still exposes
  it); no LLM request is made;
- the page is a copy of ui/html.py's HTML with an error-capture snippet added
  (the repo file is not touched), in a HIDDEN window in private mode, so
  nothing shows on screen and no WebView2 storage folder is left behind;
- at the end the tool closes the Api's database connections and removes only
  the mist_ui_smoke_* temp folder it created (if that fails it prints the path).

Each mode also checks the under-orb push-to-talk button: Api.push_to_talk
is stubbed on the class (no mic) to send the real notifications of a press
that heard nothing, and the page must show "Listening...", then the note,
then its normal status; the wake word switch must be off at startup.

Modes. "dev" is this source run. "build" acts as a release build: the real
Api.get_ui_flags runs with sys.frozen set for that call only (pywebview reads
sys.frozen to find its WebView2 DLL, so it is not set process-wide) and with
MISTAI_DEV unset. Each mode presses Save in the API modal with an empty key:
a dev run switches the route (set_api_mode), a build never does.

Building the real Api starts its usual local background threads (TTS worker,
screen buffer, file watcher), so each mode runs in its own process and ends
with os._exit.

Prints PASS/FAIL per check; exit code 0 when all pass, 1 otherwise.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

WAIT_SECONDS = 10
TMP_PREFIX = "mist_ui_smoke_"
MODES = ("dev", "build")
SEED = {"user_gender": "female", "model": "mistral"}

_CAPTURE = """<script>window.__smokeErrors=[];
window.addEventListener('error',e=>__smokeErrors.push('error: '+e.message));
window.addEventListener('unhandledrejection',e=>__smokeErrors.push(
  'unhandled rejection: '+((e.reason&&e.reason.message)||e.reason)));
const __consoleError=console.error;
console.error=(...a)=>{__smokeErrors.push('console.error: '+a.map(String).join(' '));
  __consoleError.apply(console,a);};
</script>"""

_STATE = """JSON.stringify({
  online: document.getElementById('online-text').textContent,
  options: Array.from(document.getElementById('sel-model').options)
    .map(o=>[o.value,o.textContent]),
  model: document.getElementById('sel-model').value,
  gender: document.getElementById('sel-gender').value,
  note: document.getElementById('model-note').textContent,
  backendRow: getComputedStyle(document.getElementById('backend-row')).display,
  errors: window.__smokeErrors || ['capture snippet missing']})"""

_PTT_STATE = """JSON.stringify({
  text: document.getElementById('mist-status-text').textContent,
  active: document.getElementById('btn-ptt').classList.contains('active'),
  label: document.getElementById('btn-ptt').nextElementSibling.textContent,
  wakeLabel: document.getElementById('sw-wake').previousElementSibling.textContent,
  wakeOn: document.getElementById('sw-wake').classList.contains('on'),
  oldWakeButton: !!document.getElementById('ctrl-wake')})"""

# Press Save with an empty key and the box ticked (the page's default).
_PRESS_SAVE = """window.__saveDone=false;
document.getElementById('apiKeyInput').value='';
document.getElementById('useBackend').checked=true;
saveAPIKey().then(()=>{window.__saveDone=true;});"""


def _run_both():
    """Each mode in its own process: pywebview starts once per process."""
    codes = []
    for mode in MODES:
        print(f"== {mode} ==", flush=True)
        child = subprocess.run([sys.executable, os.path.abspath(__file__), "--flags", mode])
        codes.append(child.returncode)
    print("ui_smoke:", "all modes passed" if not any(codes) else
          "FAILED in " + ", ".join(m for m, c in zip(MODES, codes) if c))
    return 0 if not any(codes) else 1


def _close_databases(*owners):
    for owner in owners:
        for attr in ("conn", "_conn"):
            conn = getattr(owner, attr, None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _remove_own_folder(path):
    """Remove only the folder this run created under the temp directory."""
    real = os.path.realpath(path)
    if (os.path.dirname(real) != os.path.realpath(tempfile.gettempdir())
            or not os.path.basename(real).startswith(TMP_PREFIX)):
        print(f"NOTE  not removing unexpected folder: {path}")
        return
    try:
        shutil.rmtree(real)
    except OSError as e:
        print(f"NOTE  could not remove the temp folder {path} ({type(e).__name__})")


def main_mode(mode):
    import webview

    from core.api import Api
    from core.config import MODEL_CHOICES, MODEL_LABELS, esc
    from core.memory import Memory
    from ui.html import HTML

    tmp = tempfile.mkdtemp(prefix=TMP_PREFIX)
    os.environ["MISTAI_DB_PATH"] = os.path.join(tmp, "memory.db")  # before any DB opens
    os.environ.pop("MISTAI_DEV", None)
    expect_dev = mode == "dev"
    seed = api = None
    result = {"code": 1}
    route_calls = []
    ptt_calls = []

    try:
        seed = Memory()
        for key, value in SEED.items():
            seed.set_profile(key, value)

        Api.check_api_status = lambda self: {"online": True, "reason": "ui_smoke stub"}
        real_flags, real_set_mode = Api.get_ui_flags, Api.set_api_mode

        def get_ui_flags(self):
            if expect_dev:
                return real_flags(self)
            sys.frozen = True                       # this call only (see docstring)
            try:
                return real_flags(self)
            finally:
                del sys.frozen

        def set_api_mode(self, use_backend):
            route_calls.append(use_backend)
            return real_set_mode(self, use_backend)

        Api.get_ui_flags, Api.set_api_mode = get_ui_flags, set_api_mode

        def push_to_talk(self):
            """No mic: replay the real notifications of a press that heard nothing."""
            ptt_calls.append(1)

            def turn():
                self._notify_js('handleListenState("waiting","")')
                time.sleep(0.4)
                self._notify_js('handleListenState("listening","")')
                time.sleep(1.2)
                self._notify_js(f'handleListenState("idle","{esc(Api._PTT_NOTHING)}")')

            threading.Thread(target=turn, daemon=True).start()
            return {"success": True}

        Api.push_to_talk = push_to_talk

        api = Api()
        page = HTML.replace("<head>", "<head>" + _CAPTURE, 1)
        window = webview.create_window("MistAI UI smoke", html=page, js_api=api, hidden=True)
        api._window = window
        expected_options = [[m, MODEL_LABELS[m]] for m in MODEL_CHOICES]

        def checks(state, saved, ptt):
            before, listening, note, after = (ptt.get(k) or {} for k in
                                              ("before", "listening", "note", "after"))
            return [
                ("header leaves 'Checking...'", state["online"] not in ("", "Checking..."),
                 state["online"]),
                ("Model dropdown has the 3 labelled options",
                 state["options"] == expected_options, state["options"]),
                ("saved model (mistral) is selected", state["model"] == "mistral",
                 state["model"]),
                ("Gender select shows the saved value (female)", state["gender"] == "female",
                 state["gender"]),
                ("model note is empty", state["note"] == "", state["note"]),
                (f"backend switch {'shown' if expect_dev else 'hidden'}",
                 (state["backendRow"] != "none") == expect_dev, state["backendRow"]),
                (f"Save {'switches' if expect_dev else 'never switches'} the route",
                 saved and route_calls == ([True] if expect_dev else []),
                 {"save finished": saved, "set_api_mode calls": route_calls}),
                ("under-orb button is push-to-talk ('Talk'), old wake button gone",
                 before.get("label") == "Talk" and before.get("oldWakeButton") is False,
                 before),
                ("'Wake word (beta)' switch is off at startup",
                 before.get("wakeLabel") == "Wake word (beta)"
                 and before.get("wakeOn") is False and api.wake_word_active is False,
                 {**before, "python wake_word_active": api.wake_word_active}),
                ("push-to-talk click reaches Python", ptt_calls == [1], ptt_calls),
                ("status shows 'Listening...' with the button highlighted",
                 listening.get("text") == "Listening..." and listening.get("active") is True,
                 listening),
                ("'Didn't catch that' shows, then the status returns",
                 note.get("text") == "Didn't catch that" and note.get("active") is False
                 and after.get("text") == "Ready" and after.get("active") is False,
                 {"note": note, "after": after}),
                ("no page errors captured", state["errors"] == [], state["errors"]),
            ]

        def poll(js, done):
            deadline = time.monotonic() + WAIT_SECONDS
            while True:
                value = window.evaluate_js(js)
                if done(value) or time.monotonic() > deadline:
                    return value
                time.sleep(0.25)

        def probe():
            try:
                if not window.events.loaded.wait(30):
                    print("FAIL  page never loaded")
                    return
                # Startup has settled once the flags step has applied (the row
                # is shown in dev) and the other cards have loaded.
                def started(s):
                    return (s["options"] and s["online"] != "Checking..."
                            and s["gender"] == "female"
                            and (s["backendRow"] != "none") == expect_dev)
                state = json.loads(poll(_STATE, lambda v: started(json.loads(v))))
                if not expect_dev:
                    time.sleep(1)      # give a wrongly-shown row time to appear
                    state = json.loads(window.evaluate_js(_STATE))
                window.evaluate_js(_PRESS_SAVE)
                saved = bool(poll("window.__saveDone===true", bool))
                # Push-to-talk: click, then follow the states the stub reports.
                ptt = {"before": json.loads(window.evaluate_js(_PTT_STATE))}
                window.evaluate_js("document.getElementById('btn-ptt').click()")
                for key, want in (("listening", "Listening..."),
                                  ("note", "Didn't catch that"), ("after", "Ready")):
                    deadline = time.monotonic() + 6
                    while time.monotonic() < deadline:
                        snap = json.loads(window.evaluate_js(_PTT_STATE))
                        if snap["text"] == want:
                            ptt[key] = snap
                            break
                        time.sleep(0.1)
                state = json.loads(window.evaluate_js(_STATE))   # errors after all of it
                results = checks(state, saved, ptt)
                for name, ok, got in results:
                    print(f"{'PASS' if ok else 'FAIL'}  {name}" +
                          ("" if ok else f"  (got {got!r})"))
                result["code"] = 0 if all(ok for _n, ok, _g in results) else 1
            except Exception as e:
                print(f"FAIL  smoke probe crashed: {type(e).__name__}: {e}")
            finally:
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(target=probe, daemon=True).start()
        try:
            webview.start(gui="edgechromium", private_mode=True)
        except Exception as e:
            print(f"FAIL  WebView2 did not start: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"FAIL  smoke setup crashed: {type(e).__name__}: {e}")
    finally:
        owners = [seed]
        if api is not None:
            owners += [getattr(api, n, None)
                       for n in ("memory", "task_memory", "task_store", "tools")]
        _close_databases(*owners)
        _remove_own_folder(tmp)
    print(f"ui_smoke [{mode}]:", "all checks passed" if result["code"] == 0 else "FAILED")
    return result["code"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--flags", choices=MODES, help="run one mode only")
    args = parser.parse_args()
    code = 1
    try:
        code = main_mode(args.flags) if args.flags else _run_both()
    finally:
        sys.stdout.flush()
        os._exit(code)
