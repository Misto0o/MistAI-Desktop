"""
core/config.py — shared constants, paths, and small text utilities.
"""

import sys
import os
import re
import difflib

VERSION = "2.0"
STATUS_URL = "https://mist-ai.fly.dev/api/status"
MODEL = "cohere"

# ── API CONFIGURATION ─────────────────────────────────────────────────────────
PRIMARY_API = "https://api.mistai.org/api/v1/chat"
FALLBACK_API = "https://mist-ai.fly.dev/api/v1/chat"
# The desktop is configured around the Flask backend by default. Direct mode
# remains opt-in for deployments that have explicitly verified their API's
# structured agent-mode contract.
API_MODE = "backend"  # "direct_api" or "backend"

# The ONE place the desktop agent's model is chosen. agent.py, api.py and the
# UI all read this; nothing hardcodes "mistral"/"cohere" anymore. Mistral's
# free tier is currently disabled upstream (429s), so: cohere.
AGENT_MODEL = "cohere"
# Conversation layer (core/conversation.py): model and timeout of the
# chat-or-task routing call (primary URL only, no fallback). Any failure
# routes to the agent.
CHAT_MODEL = AGENT_MODEL
CHAT_TIMEOUT = 5

WAKE_WORDS = ["mist", "hey mist", "mistai", "mist ai"]

# Wake-loop energy threshold (core/wake_calibration.py). The threshold is
# fixed between calibrations. A calibration outside [MIN, MAX] is clamped and
# retried once. MIN keeps hum from opening clips; MAX only catches a
# calibration taken over speech or music. Real rooms have calibrated at 217
# and 668, so MAX leaves headroom above that.
WAKE_ENERGY_MIN = 60
WAKE_ENERGY_MAX = 1500
WAKE_RECAL_IDLE_S = 90         # re-calibrate after this long with no clip
WAKE_RECAL_AFTER_FAILURES = 3  # ... or after this many untranscribable clips in a row
WAKE_RECAL_RETRY_S = 30        # one retry this long after an out-of-band result

# Things Google STT actually returns when someone says "mist" / "mist ai".
# The common English words ``miss`` and ``missed`` are deliberately NOT in
# this list: even at the start of an utterance, "missed the bus" is not an
# assistant command.  The remaining spellings are uncommon enough to retain
# as practical STT fallbacks.
WAKE_WORD_ALTERNATIVES = {
    # Keep only plausible recognition variants. Everyday words such as
    # "midst", "misty", and "mystic" created more accidental activations
    # than useful wakes; alternatives are also accepted only as a prefix.
    "mist": ["myst", "mest"],
    "hey mist": ["hey mest", "hey myst"],
    "mistai": ["misty ai", "miss eye"],
    "mist ai": ["mess ai", "mist eye"],
}

_WAKE_CANDIDATES = None


def _wake_candidates():
    """(phrase, canonical, is_alternative, regex), longest phrase first."""
    global _WAKE_CANDIDATES
    if _WAKE_CANDIDATES is None:
        pairs = [(w, w, False) for w in WAKE_WORDS]
        for canon, alts in WAKE_WORD_ALTERNATIVES.items():
            pairs += [(a, canon, True) for a in alts]
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        _WAKE_CANDIDATES = [
            (
                p,
                c,
                alternative,
                re.compile(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])"),
            )
            for p, c, alternative in pairs
        ]
    return _WAKE_CANDIDATES


def fuzzy_wake(text, anchored=False):
    """Return (canonical_wake_word, command_after_it) or (None, None).

    anchored=False -- wake detection: the phrase must lead the utterance.
                      This favors an intentional invocation ("Mist, ...")
                      over ordinary speech about weather or a missed bus.
    anchored=True  -- prefix stripping for a command that was heard AFTER a
                      wake: only a leading wake phrase is removed, so a
                      command that merely mentions the word isn't mangled.
    """
    t = " ".join((text or "").lower().split())
    if not t:
        return None, None
    for phrase, canon, alternative, rx in _wake_candidates():
        m = rx.search(t)
        if not m:
            continue
        # A wake word is an invocation prefix. Requiring it at the start also
        # prevents normal phrases such as "I walked through the mist today"
        # from launching a desktop task in the background.
        if m.start() != 0:
            continue
        return canon, t[m.end() :].lstrip(" ,.!?").strip()
    # Last-resort tier: the FIRST word only (a wake word is said first), and
    # it has to be a near-miss of 'mist'. Do not accept a longer word merely
    # because it starts with "mist" -- that made "misty morning" wake Mist.
    first = t.split(" ", 1)
    if (
        3 <= len(first[0]) <= 6
        and not (first[0].startswith("mist") and len(first[0]) > 4)
        and difflib.SequenceMatcher(None, first[0], "mist").ratio() >= 0.85
    ):
        return "mist", first[1] if len(first) > 1 else ""
    return None, None


_TITLE_DASHES = str.maketrans({"—": "-", "–": "-"})


def normalize_window_title(text):
    """A window title for loose comparison: em/en dashes as '-', spaces
    collapsed, trailing separators trimmed. A title quoted back after being
    cut ('Page title —' -> 'Page title -') then still matches the real
    'Page title — Mozilla Firefox'."""
    t = " ".join(str(text or "").translate(_TITLE_DASHES).split())
    return t.rstrip(" -|:·•").strip()


def strip_wake_prefix(command):
    """A user who repeats 'hey mist ...' because the assistant was slow ends
    up with the wake phrase inside the COMMAND. Strip it (leading only) so
    the planner never sees 'hey Miss text rock your fm'."""
    ww, rest = fuzzy_wake(command, anchored=True)
    return rest if ww else (command or "").strip()


EDGE_TTS_VOICE = {
    "male": "en-US-GuyNeural",
    "female": "en-US-AriaNeural",
    # "none" used to be AriaNeural, i.e. "no preference" silently meant the
    # female voice. Neutral now maps to a distinct third voice.
    "none": "en-US-ChristopherNeural",
}

TRACKED_APPS = [
    "chrome",
    "firefox",
    "edge",
    "opera",
    "brave",
    "discord",
    "slack",
    "teams",
    "zoom",
    "spotify",
    "vlc",
    "vscode",
    "code",
    "pycharm",
    "sublime",
    "notepad++",
    "notepad",
    "excel",
    "word",
    "powerpoint",
    "outlook",
    "explorer",
    "steam",
    "epic",
    "obs",
    "audacity",
    "cmd",
    "powershell",
    "wt",
    "photoshop",
    "figma",
    "telegram",
    "calculator",
    "paint",
]

# Apps where Enter can send, post, submit or run something the user cannot
# take back: browsers, messaging, mail, terminals. press_key('enter') there
# always asks first, even in an app the phase opened (ToolRegistry).
ENTER_CONFIRM_APPS = (
    "chrome", "firefox", "edge", "msedge", "opera", "brave", "vivaldi", "iexplore", "browser",
    "discord", "slack", "teams", "telegram", "zoom", "whatsapp", "signal", "skype",
    "messenger", "phonelink",
    "outlook", "thunderbird", "mail", "hxoutlook", "mailspring",
    "windowsterminal", "terminal", "wt", "openconsole", "conhost", "cmd", "powershell", "pwsh",
)

# MistAI's own UI host (pywebview -> WebView2) and this process are never
# "user apps"; "edge" substring-matches msedgewebview2.
_SELF_PROCESS_NAMES = {"msedgewebview2"}


def env_flag(name, default):
    """Boolean environment switch: 1/true/yes/on or 0/false/no/off; anything
    else (including unset) means ``default``."""
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def vision_gating_enabled():
    """Gated screen-context reads in AgentLoop. On by default;
    MISTAI_VISION_GATING=0 turns it off (read per phase, so a harness can A/B)."""
    return env_flag("MISTAI_VISION_GATING", True)


def lazy_vision_mode():
    """MISTAI_LAZY_VISION: how a phase's first screen read is done. 0 (default)
    = normal (vision); 1 = window title + Open apps + a time-boxed UIA summary;
    2 = "no first read": title + Open apps only (no UIA walk, no vision)."""
    if os.environ.get("MISTAI_LAZY_VISION", "").strip() == "2":
        return 2
    return 1 if env_flag("MISTAI_LAZY_VISION", False) else 0


def wake_clips_enabled():
    """MISTAI_WAKE_SAVE_CLIPS=1: the wake loop saves every clip it hears (WAV
    + JSON sidecar) to wake_clip_dir() for offline tuning. Off by default."""
    return env_flag("MISTAI_WAKE_SAVE_CLIPS", False)


def wake_clip_dir():
    return os.path.join(os.path.expanduser("~"), "MistAI", "logs", "wake-clips")



def memory_db_path():
    """The SQLite file for memory/tasks/tool stats. MISTAI_DB_PATH overrides
    it (the scenario harness points this at a throwaway file)."""
    return os.environ.get("MISTAI_DB_PATH") or os.path.join(
        os.path.expanduser("~"), "MistAI", "memory.db"
    )


def is_self_process(name, pid=None):
    return pid == os.getpid() or (name or "").lower().replace(".exe", "") in _SELF_PROCESS_NAMES


# What a user calls an app -> its real process name(s), both normalized.
# Process matching is EXACT ("code" must not match "codex"), so names whose
# process differs from what people say need an entry here.
APP_PROCESS_ALIASES = {
    "edge": {"msedge"},
    "vscode": {"code"},
    "visualstudiocode": {"code"},
    "word": {"winword"},
    "powerpoint": {"powerpnt"},
    "outlook": {"outlook", "olk"},
    "teams": {"msteams", "teams"},
    "sublime": {"sublimetext"},
    "epic": {"epicgameslauncher"},
    "terminal": {"windowsterminal"},
    "wt": {"windowsterminal", "wt"},
    "calculator": {"calculatorapp", "calculator"},
    "paint": {"mspaint"},
    "powershell": {"powershell", "pwsh"},
}


def normalize_process_name(name):
    """'OBS64.exe' -> 'obs', 'ms-teams' -> 'msteams', 'Notepad++' -> 'notepad++'."""
    n = str(name or "").lower().strip()
    if n.endswith(".exe"):
        n = n[:-4]
    n = re.sub(r"[_\-\s]?(?:x64|x86|64|32)$", "", n)
    return re.sub(r"[^a-z0-9+]", "", n)


def app_process_names(app_name):
    """Exact (normalized) process names that count as ``app_name``: the name
    itself, each of its words ("visual studio code" -> code), and aliases."""
    raw = str(app_name or "").lower().strip()
    forms = {normalize_process_name(raw)}
    words = re.findall(r"[a-z0-9+]+", raw.replace(".exe", ""))
    if len(words) > 1:
        forms.update(normalize_process_name(w) for w in words)
    names = set(forms)
    for form in forms:
        names |= APP_PROCESS_ALIASES.get(form, set())
    return {n for n in names if len(n) >= 2}


_VENDOR_PREFIXES = ("microsoft", "windows", "system", "ms", "win")


def _process_cores(proc):
    """The process name plus forms with one vendor prefix and/or an "app"
    suffix removed: systemsettings -> settings, winstoreapp -> store. Still
    an exact comparison afterwards, so "codex" never becomes "code"."""
    cores = {proc}
    for p in _VENDOR_PREFIXES:
        if proc.startswith(p) and len(proc) - len(p) >= 3:
            cores.add(proc[len(p):])
    cores |= {c[:-3] for c in cores if c.endswith("app") and len(c) >= 6}
    return cores


def process_matches(app_name, process_name):
    proc = normalize_process_name(process_name)
    if not proc:
        return False
    names = app_process_names(app_name)
    if proc in names:
        return True
    # Known (tracked) apps match exactly or via an explicit alias only, so a
    # different vendor's "WinCode.exe" can never pass for VS Code. Prefix /
    # "app" stripping is for untracked built-ins (settings, store, camera).
    if names & TRACKED_PROCESS_NAMES:
        return False
    return bool(_process_cores(proc) & names)


# Exact process names tracked in LiveState's "Open apps" list.
TRACKED_PROCESS_NAMES = frozenset(
    n for app in TRACKED_APPS for n in app_process_names(app)
)


# Files whose content must never be handed to the model (inspect_file,
# run_command). Matched case-insensitively against the file name only.
SECRET_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.keystore",
    "credentials*",
)

_TOKEN_SPLIT_RX = re.compile(r"[\s'\"`;|,()=<>{}\[\]]+")
_SECRET_VAR_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
_ENV_DUMP_RX = re.compile(
    r"""(?ix)
    \b(?:get-childitem|gci|dir|ls|get-item|gi)\b[^;|&\n]*?(?<![\w$])env:[\\/]?\*?(?=$|[\s;|&)'"])
    | \bprintenv\b
    | (?:^|[;&|]|\bcmd(?:\.exe)?\s+/c)\s*set\s*(?=$|[;&|])
    | \[(?:system\.)?environment\]::getenvironmentvariables\s*\(
    """
)
_ENV_REF_RX = re.compile(
    r"""(?ix)
    (?:\$\{?env:|(?<![\w$])env:[\\/]?|%)([a-z_]\w*)
    | getenvironmentvariable\s*\(\s*['"]([^'"]+)['"]
    """
)
_PRIVATE_KEY_RX = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
# Well-known credential shapes, plus "<secret word> = <long value>".
_SECRET_TOKEN_RX = re.compile(
    r"""(?x)
    \b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}
    | \bgh[pousr]_[A-Za-z0-9]{30,}
    | \bgithub_pat_[A-Za-z0-9_]{30,}
    | \b(?:AKIA|ASIA)[A-Z0-9]{16}\b
    | \bxox[abposr]-[A-Za-z0-9-]{10,}
    | \bAIza[0-9A-Za-z_-]{35}
    | \beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}
    | (?i:\b[a-z_]*(?:api_?key|token|secret|passw(?:or)?d)[a-z_]*\b\s*[:=]\s*['"]?[^\s'"]{8,})
    """
)


def is_secret_file(name):
    import fnmatch

    base = str(name or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    return bool(base) and any(fnmatch.fnmatchcase(base, p) for p in SECRET_FILE_PATTERNS)


def secret_file_in(text):
    """First token in ``text`` whose file name matches SECRET_FILE_PATTERNS."""
    for token in _TOKEN_SPLIT_RX.split(str(text or "")):
        if token and is_secret_file(token):
            return token
    return None


def secret_output_reason(command, output=""):
    """Why run_command output must be withheld from the model, or None.

    Not airtight: wildcards (Get-Content *.en?), piped listings, and file or
    variable names built at runtime all get past these textual checks. It
    stops the obvious ways a model would read a secret, nothing more.
    """
    command = str(command or "")
    hit = secret_file_in(command)
    if hit:
        return f"command references secret-pattern file '{hit}'"
    if _ENV_DUMP_RX.search(command):
        return "command dumps environment variables"
    for m in _ENV_REF_RX.finditer(command):
        name = (m.group(1) or m.group(2) or "").upper()
        if any(w in name for w in _SECRET_VAR_WORDS):
            return f"command references secret environment variable '{name}'"
    if _PRIVATE_KEY_RX.search(str(output or "")):
        return "output contains a private key block"
    return None


def secret_text_reason(text):
    """Why on-screen text (e.g. a UIA snapshot) must be withheld, or None:
    a private key block or a credential-looking token. Textual, not airtight."""
    text = str(text or "")
    if _PRIVATE_KEY_RX.search(text):
        return "contains a private key block"
    if _SECRET_TOKEN_RX.search(text):
        return "contains a credential-looking token"
    return None


def get_resource_path(rel):
    try:
        base = sys._MEIPASS  # PyInstaller bundle
    except AttributeError:
        base = os.path.abspath(".")
    return os.path.join(base, rel)


def esc(s):
    """Escape a string for safe inline embedding inside a JS call."""
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("\r", " ")
        .replace("\n", " ")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
    )


def setup_tesseract():
    """Point pytesseract at MistAI's bundled OCR engine instead of relying
    on a system-wide Tesseract install. Checks the custom
    MistAI_OCREngine folder first (both frozen/PyInstaller and dev-run
    layouts), then falls back to common system install paths."""
    try:
        import pytesseract
    except ImportError:
        return False

    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
        exe = os.path.join(base, "Tesseract-OCR", "tesseract.exe")
        if os.path.exists(exe):
            pytesseract.pytesseract.tesseract_cmd = exe
            td = os.path.join(base, "Tesseract-OCR", "tessdata")
            os.environ["TESSDATA_PREFIX"] = (
                td if os.path.exists(td) else os.path.join(base, "Tesseract-OCR")
            )
            print(f"[Tesseract] {exe}")
            return True
        return False

    script_dir = os.path.dirname(os.path.abspath(__file__))  # core/
    project_root = os.path.dirname(script_dir)  # MistAI/
    candidates = [
        os.path.join(
            project_root, "MistAI_OCREngine", "Tesseract-OCR", "tesseract.exe"
        ),
        os.path.join(project_root, "MistAI_OCREngine", "tesseract.exe"),
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.join(
            os.getenv("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR", "tesseract.exe"
        ),
    ]
    for p in candidates:
        if os.path.exists(p):
            pytesseract.pytesseract.tesseract_cmd = p
            td = os.path.join(os.path.dirname(p), "tessdata")
            os.environ["TESSDATA_PREFIX"] = (
                td if os.path.exists(td) else os.path.dirname(p)
            )
            print(f"[Tesseract] {p}")
            return True

    print("[Tesseract] Not found -- OCR disabled")
    return False
