import sys
import os

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def has_display():
    """Check if we have a display available"""
    if os.name == 'nt':  # Windows
        return True
    else:  # Linux/Mac
        return bool(os.environ.get('DISPLAY'))

def main():
    # If we have a display, show splash. Otherwise skip directly to assistant.
    if has_display():
        try:
            from splash import show_splash
            show_splash()
        except Exception as e:
            print(f"Splash failed: {e}, starting assistant directly")
            from assistant import main as assistant_main
            assistant_main()
    else:
        print("No display detected, starting in headless mode")
        from assistant import main as assistant_main
        assistant_main()

if __name__ == "__main__":
    main()