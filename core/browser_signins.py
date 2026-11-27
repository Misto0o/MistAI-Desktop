# ============================================================
# BROWSER SIGN-INS MODULE
# ============================================================
#
# Settings > BROWSER SIGN-INS: which browsers are installed on this PC, which
# one is the default, and from which of them Mist may copy sign-ins into its
# OWN browser profile (core/browser_session.py). The browser Mist drives is
# always its own Playwright Firefox; nothing here starts or drives another
# browser.
#
# Import support (see the per-browser findings in docs/ARCHITECTURE.md and
# CLAUDE.md "Browser"):
# - Firefox: cookies.sqlite is a plain SQLite file and storage/ holds site
#   data; both are Firefox's own format, so Mist's Firefox reads them as is.
#   Supported (the existing seeding).
# - Chromium family (Chrome, Edge, Brave, Opera, Vivaldi): cookie values are
#   encrypted (DPAPI-wrapped AES key, and app-bound encryption in Chrome /
#   Edge), in a different format from Firefox. Importing them would mean
#   decrypting another app's protected data: NOT supported, never attempted.
# - Firefox forks (LibreWolf, Waterfox, Floorp, Zen) and anything else:
#   listed, not supported (unverified profile versions; LibreWolf clears
#   cookies on exit by default).
#
# Registry reads only (HKCU/HKLM Clients\StartMenuInternet, the list Windows
# itself uses for "Default apps", and the https UserChoice ProgId). Readers
# are injectable so tests use fakes. No logging of paths or names.
#
# SECTION INDEX
#   1. BROWSER FAMILIES
#   2. FAMILY FROM AN EXECUTABLE
#   3. FAMILY FROM A DEFAULT-BROWSER PROGID
#   4. REGISTRY READERS
#   5. INSTALLED BROWSERS
#   6. DEFAULT BROWSER
#   7. CHOICE LIST FOR SETTINGS
#   8. SOURCE RESOLUTION
# ============================================================

import os


# ============================================================
# BROWSER FAMILIES
# ============================================================

# id -> (label, supported). Order = the dropdown order for known browsers.
FAMILIES = {
    "firefox": ("Firefox", True),
    "chrome": ("Google Chrome", False),
    "edge": ("Microsoft Edge", False),
    "brave": ("Brave", False),
    "opera": ("Opera", False),
    "opera-gx": ("Opera GX", False),
    "vivaldi": ("Vivaldi", False),
    "librewolf": ("LibreWolf", False),
    "waterfox": ("Waterfox", False),
    "floorp": ("Floorp", False),
    "zen": ("Zen Browser", False),
}
SUPPORTED_SOURCES = tuple(k for k, (_l, ok) in FAMILIES.items() if ok)
DEFAULT_SOURCE = "firefox"
UNSUPPORTED_NOTE = "Not supported - use Sign in inside Mist's browser instead"
SIGNINS_EXPLANATION = (
    "Mist copies only the cookies and site data that keep you signed in (never saved "
    "passwords) into its own browser; the browser Mist drives is always its own.")

_EXE_FAMILIES = {
    "firefox.exe": "firefox", "chrome.exe": "chrome", "msedge.exe": "edge",
    "brave.exe": "brave", "vivaldi.exe": "vivaldi", "librewolf.exe": "librewolf",
    "waterfox.exe": "waterfox", "floorp.exe": "floorp", "zen.exe": "zen",
}
_SKIP_EXES = ("iexplore.exe",)          # Internet Explorer: not a browser choice


# ============================================================
# FAMILY FROM AN EXECUTABLE
# ============================================================

def family_for(exe_path, display_name=""):
    """A FAMILIES id for a browser's executable (and its registry display
    name, for Opera's launcher.exe), "other" for an unknown browser, None
    for one to skip."""
    path = str(exe_path or "").strip().strip('"').lower()
    exe = os.path.basename(path.split('"')[0].split(" --")[0].strip())
    name = str(display_name or "").lower()
    if exe in _SKIP_EXES:
        return None
    if "opera" in exe or (exe == "launcher.exe" and ("opera" in path or "opera" in name)):
        return "opera-gx" if ("opera gx" in path or "opera gx" in name or "operagx" in path) \
            else "opera"
    if exe in _EXE_FAMILIES:
        return _EXE_FAMILIES[exe]
    return "other" if exe.endswith(".exe") else None


# ============================================================
# FAMILY FROM A DEFAULT-BROWSER PROGID
# ============================================================

_PROGID_PREFIXES = (
    ("firefoxurl", "firefox"), ("chromehtml", "chrome"), ("msedgehtm", "edge"),
    ("bravehtml", "brave"), ("operagxstable", "opera-gx"), ("opera", "opera"),
    ("vivaldihtm", "vivaldi"), ("librewolfhtm", "librewolf"), ("waterfoxurl", "waterfox"),
    ("floorphtml", "floorp"), ("zenhtml", "zen"),
)


def family_for_progid(progid):
    """The family of the https UserChoice ProgId (e.g. "ChromeHTML"), or None."""
    p = str(progid or "").strip().lower()
    for prefix, fam in _PROGID_PREFIXES:
        if p.startswith(prefix):
            return fam
    return None


# ============================================================
# REGISTRY READERS
# ============================================================

_CLIENT_KEYS = (
    ("HKCU", r"Software\Clients\StartMenuInternet"),
    ("HKLM", r"SOFTWARE\Clients\StartMenuInternet"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"),
)
_USERCHOICE_KEY = (r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                   r"\https\UserChoice")


def registry_browsers():
    """[(display_name, exe_command)] from the StartMenuInternet keys. Never
    raises: an unreadable key is skipped."""
    try:
        import winreg
    except ImportError:
        return []
    hives = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    out = []
    for hive, path in _CLIENT_KEYS:
        try:
            root = winreg.OpenKey(hives[hive], path)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                name, cmd = sub, ""
                try:
                    with winreg.OpenKey(root, sub) as k:
                        name = winreg.QueryValueEx(k, "")[0] or sub
                except OSError:
                    pass
                try:
                    with winreg.OpenKey(root, sub + r"\shell\open\command") as k:
                        cmd = winreg.QueryValueEx(k, "")[0] or ""
                except OSError:
                    pass
                out.append((str(name), str(cmd)))
        finally:
            winreg.CloseKey(root)
    return out


def registry_default_progid():
    """The https UserChoice ProgId, or None."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _USERCHOICE_KEY) as k:
            return str(winreg.QueryValueEx(k, "ProgId")[0] or "") or None
    except Exception:
        return None


def firefox_profile_exists():
    """Is there a Firefox profiles.ini (a portable or unregistered Firefox)?"""
    base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")
    return os.path.isfile(os.path.join(base, "profiles.ini"))


# ============================================================
# INSTALLED BROWSERS
# ============================================================

def installed_browsers(read_browsers=registry_browsers, firefox_profile=firefox_profile_exists):
    """[(id, label)] of the known browsers found, one per family in FAMILIES
    order. Unknown entries are left out: other programs register there too
    (seen: a mouse vendor's "AppEngine"), and none of them could be imported
    anyway. Firefox is listed when only its profile folder exists too.
    Never raises."""
    try:
        entries = list(read_browsers() or [])
    except Exception:
        entries = []
    seen = set()
    for name, cmd in entries:
        fam = family_for(cmd, name)
        if fam in FAMILIES:
            seen.add(fam)
    try:
        if "firefox" not in seen and firefox_profile():
            seen.add("firefox")
    except Exception:
        pass
    return [(fam, FAMILIES[fam][0]) for fam in FAMILIES if fam in seen]


# ============================================================
# DEFAULT BROWSER
# ============================================================

def default_browser(read_progid=registry_default_progid):
    """The default browser's family id, or None. Never raises."""
    try:
        return family_for_progid(read_progid())
    except Exception:
        return None


# ============================================================
# CHOICE LIST FOR SETTINGS
# ============================================================

def is_supported(source):
    return source in SUPPORTED_SOURCES


def browser_choices(installed, default=None):
    """The Settings dropdown: [{id, label, supported, note, default}] for
    ``installed`` [(id, label)]; Firefox is always offered (the existing
    behavior), even when it was not found, marked "not found"."""
    items = list(installed)
    if not any(i == "firefox" for i, _l in items):
        items.insert(0, ("firefox", FAMILIES["firefox"][0] + " (not found)"))
    out = []
    for bid, label in items:
        ok = is_supported(bid)
        out.append({"id": bid, "label": label, "supported": ok,
                    "note": "" if ok else UNSUPPORTED_NOTE, "default": bid == default})
    return out


# ============================================================
# SOURCE RESOLUTION
# ============================================================

def resolve_source(saved, choices, switch_on, default=None):
    """The browser the setting means. A saved id that is offered wins; with
    nothing saved, a switch that is already on means Firefox (the setting
    used to be Firefox-only, so its users keep their behavior); else the
    default browser when offered; else Firefox."""
    ids = [c["id"] for c in choices]
    s = str(saved or "").strip().lower()
    if s in ids:
        return s
    if switch_on:
        return DEFAULT_SOURCE
    if default in ids:
        return default
    return DEFAULT_SOURCE
