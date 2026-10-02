"""
main.py — MistAI entry point.

Run with:  python main.py
"""

import logging
import os
import sys
import warnings
import atexit

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.CRITICAL)
for logger_name in logging.root.manager.loggerDict:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)

os.environ["PYWEBVIEW_LOG"] = "none"


class FilteredStderr:
    """Suppresses noisy pywebview/CEF chatter on stderr without hiding real errors."""

    _BLOCKED = [
        "pywebview", "CoreWebView2", "__abstractmethods__",
        "AllowExternalDrop", "CanGoBack", "CanGoForward", "ZoomFactor",
    ]

    def write(self, message):
        if not any(b in message for b in self._BLOCKED):
            sys.__stderr__.write(message)

    def flush(self):
        pass


sys.stderr = FilteredStderr()

import webview  # noqa: E402  (must come after stderr filtering / env setup)

from core.api import Api  # noqa: E402
from core.caption import show_splash, start_tk_root  # noqa: E402
from ui.html import HTML  # noqa: E402
from core.config import VERSION, get_resource_path, setup_tesseract  # noqa: E402


def main():
    print(f"[MistAI v{VERSION}] Starting...")
    start_tk_root()
    show_splash(get_resource_path)
    setup_tesseract()

    api = Api()
    window = webview.create_window(
        f"MistAI v{VERSION}",
        html=HTML,
        js_api=api,
        width=900,
        height=700,
        resizable=True,
    )
    api.window = window
    def _cleanup():
        # Signal all background loops to stop before interpreter teardown
        try:
            api.shutdown_wake_word_detection()
        except Exception:
            pass
    atexit.register(_cleanup)
    webview.start(debug=False, gui="edgechromium", private_mode=False)

if __name__ == "__main__":
    main()
