"""
core/seed_data.py — default knowledge MistAI ships with, so common lookups
(app hotkeys, known sites, etc) don't need a fresh LLM call or extra agent
steps on first use. Anything NOT in here gets learned dynamically at
runtime (hotkeys via _ask_llm_for_hotkey) and cached the same way from
then on. Existing/learned values always take priority over these seeds --
see Memory.seed_defaults(), which never overwrites.
"""

# -- App quick-switch/search hotkeys (used by quick_message) ----------------
DEFAULT_HOTKEYS = {
    "hotkey_discord": "ctrl+k",
    "hotkey_slack": "ctrl+k",
    "hotkey_teams": "ctrl+e",
    "hotkey_whatsapp": "ctrl+f",
    "hotkey_telegram": "ctrl+f",
    "hotkey_signal": "ctrl+f",
    "hotkey_skype": "ctrl+f",
    "hotkey_messenger": "ctrl+k",
    "hotkey_zoom": "ctrl+f",
    "hotkey_outlook": "ctrl+e",
    "hotkey_gmail": "/",
    "hotkey_vscode": "ctrl+p",
    "hotkey_spotify": "ctrl+l",
    "hotkey_steam": "ctrl+f",
    "hotkey_notion": "ctrl+p",
}

# -- Well-known site shortcuts: lets browser_navigate skip a search step ----
# for common destinations instead of the model guessing/searching Google
# for something it should already know the URL of.
DEFAULT_SITE_SHORTCUTS = {
    "site_youtube": "youtube.com",
    "site_google": "google.com",
    "site_gmail": "mail.google.com",
    "site_github": "github.com",
    "site_reddit": "reddit.com",
    "site_twitter": "x.com",
    "site_x": "x.com",
    "site_discord": "discord.com/app",
    "site_netflix": "netflix.com",
    "site_amazon": "amazon.com",
    "site_wikipedia": "wikipedia.org",
    "site_twitch": "twitch.tv",
    "site_spotify": "open.spotify.com",
    "site_linkedin": "linkedin.com",
    "site_instagram": "instagram.com",
    "site_facebook": "facebook.com",
    "site_mistai": "mistai.org",
}

# -- General facts about the system itself, seeded into the facts table so
# `recall` can answer basic "what are you" / "what can you do" questions
# without needing a fresh LLM guess every time. category="system" keeps
# these separate from user-specific facts.
SEED_FACTS = [
    ("MistAI is a JARVIS-style desktop assistant that can see the screen, "
     "control apps, run system commands, and browse the web.", "system", 2),
    ("MistAI prefers real system tools (PowerShell, UIA, DOM access) over "
     "guessing at pixel coordinates whenever one is available.", "system", 1),
    ("MistAI will always ask for confirmation before destructive actions "
     "like deleting files, even if the instruction was unambiguous.", "system", 2),
    ("MistAI organizes desktop/folder files by type into category "
     "subfolders: Images, Documents, Archives, Installers, Shortcuts.", "system", 1),
    ("MistAI's wake words are 'Mist', 'Hey Mist', 'MistAI', and 'Mist AI'.", "system", 1),
    
    ("The user's name is Kristian.", "personal", 3),
    ("Kristians PC name is MistOnYTPC online; GitHub handle is Misto0o.", "personal", 2),
    ("Kristian is the lead programmer for FTC robotics Team 6183, 'Triple Paradox'.", "work", 3),
    ("Kristian is building YOU; MistAI Desktop, a JARVIS-style Windows desktop AI assistant.", "work", 3),
    ("Kristian is a Knicks fan; his dad is also a lifelong Knicks fan from Queens.", "personal", 2),
    ("Kristian works primarily on Windows 11 in VS Code with Python and Java.", "work", 2),
    ("Kristians email address is crystal.cook07@gmail.com.", "personal", 1),
    ("Kristians favorite color is blue.", "personal", 1),
    ("Kristians favorite food is pizza.", "personal", 1),
    ("Kristians favorite hobby is playing video games.", "personal", 1),
    ("Kristians favorite music genre is hip-hop.", "personal", 1),
]