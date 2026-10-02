"""
core/caption.py — on-screen status/caption overlay, plus the shared Tk root
that the whole app uses for any Tk-based UI (caption window, splash screen).
"""

import threading
import tkinter as tk
from queue import Empty, Queue
from tkinter import font as tkfont
from typing import Optional

_tk_root: Optional[tk.Tk] = None
_tk_ready = threading.Event()


def _tk_thread_main():
    global _tk_root
    _tk_root = tk.Tk()
    _tk_root.withdraw()
    _tk_root.protocol("WM_DELETE_WINDOW", lambda: None)
    _tk_ready.set()
    _tk_root.mainloop()


def start_tk_root(timeout=5):
    """Call once at startup. Spins up the shared Tk mainloop thread."""
    t = threading.Thread(target=_tk_thread_main, daemon=True, name="TkMain")
    t.start()
    _tk_ready.wait(timeout=timeout)
    return _tk_root


def get_tk_root():
    return _tk_root


class CaptionWindow:
    """Smart status/caption overlay."""

    def __init__(self):
        self._window = None
        self._label_top = None
        self._label_bot = None
        self.ready = False
        self._hide_id = None
        self._queue = Queue()
        self._current_action = ""
        self._is_thinking = False

        if _tk_root is None:
            print("[Caption] No Tk root available")
            return

        done = threading.Event()

        def _create():
            try:
                sw = _tk_root.winfo_screenwidth()
                sh = _tk_root.winfo_screenheight()
                w = int(sw * 0.65)
                x = (sw - w) // 2
                y = sh - 150
                self._window = tk.Toplevel(_tk_root)
                self._window.overrideredirect(True)
                self._window.attributes("-topmost", True)
                self._window.attributes("-alpha", 0.0)
                self._window.geometry(f"{w}x90+{x}+{y}")
                BG = "#111827"
                self._window.configure(bg=BG)

                f_title = tkfont.Font(family="Segoe UI", size=12, weight="bold")
                f_sub = tkfont.Font(family="Segoe UI", size=10)

                self._label_top = tk.Label(
                    self._window, text="", font=f_title, fg="white", bg=BG,
                    wraplength=w - 40, padx=20, pady=6, justify="center",
                )
                self._label_top.pack(fill="x")

                self._label_bot = tk.Label(
                    self._window, text="", font=f_sub, fg="#94a3b8", bg=BG,
                    wraplength=w - 40, padx=20, pady=4, justify="center",
                )
                self._label_bot.pack(fill="x")

                self.ready = True
                done.set()
                self._tick()
            except Exception as e:
                print(f"[Caption] Create failed: {e}")
                done.set()

        _tk_root.after(0, _create)
        done.wait(timeout=3)

    def _tick(self):
        if not self._window:
            return
        try:
            while True:
                msg = self._queue.get_nowait()
                msg_type = msg.get("type", "status")
                text = msg.get("text", "")
                duration = msg.get("duration", 6)

                BG_MAP = {
                    "thinking": "#4c1d95",
                    "speech": "#065f46",
                    "action": "#78350f",
                    "status": "#111827",
                    "error": "#7f1d1d",
                }
                bg = BG_MAP.get(msg_type, "#111827")

                if msg_type == "thinking":
                    self._label_top.configure(text=f"\U0001F4AD {text}", fg="#c084fc", bg=bg)
                    self._label_bot.configure(text=self._current_action, fg="#a78bfa", bg=bg)
                    self._is_thinking = True
                elif msg_type == "speech":
                    self._label_top.configure(text=f"\U0001F916 {text}", fg="#10b981", bg=bg)
                    self._label_bot.configure(text="", bg=bg)
                    self._is_thinking = False
                elif msg_type == "action":
                    parts = text.split("|", 1) if "|" in text else [text, ""]
                    action, param = parts[0], parts[1] if len(parts) > 1 else ""
                    self._current_action = f"-> {action} {param[:50] if param else ''}"
                    self._label_top.configure(text="\u2699 Executing", fg="#fbbf24", bg=bg)
                    self._label_bot.configure(text=self._current_action, fg="#fcd34d", bg=bg)
                    self._is_thinking = False
                elif msg_type == "status":
                    self._label_top.configure(text=text, fg="#64748b", bg=bg)
                    self._label_bot.configure(text="", bg=bg)
                    self._is_thinking = False
                elif msg_type == "error":
                    self._label_top.configure(text=f"\u274C {text}", fg="#fca5a5", bg=bg)
                    self._label_bot.configure(text="", bg=bg)

                self._window.configure(bg=bg)
                self._window.attributes("-alpha", 0.93)

                if self._hide_id:
                    self._window.after_cancel(self._hide_id)
                    self._hide_id = None

                if duration > 0:
                    self._hide_id = self._window.after(int(duration * 1000), self._hide)
        except Empty:
            pass

        if self._window:
            self._window.after(50, self._tick)

    def _hide(self):
        self._hide_id = None
        try:
            if self._window:
                self._window.attributes("-alpha", 0.0)
        except Exception:
            pass

    def show_thinking(self, text, duration=8):
        if self.ready and _tk_root:
            self._queue.put({"type": "thinking", "text": text, "duration": duration})

    def show_action(self, action, param="", duration=4):
        if self.ready and _tk_root:
            self._queue.put({"type": "action", "text": f"{action}|{param}", "duration": duration})

    def show_speech(self, text, duration=8):
        if self.ready and _tk_root:
            self._queue.put({"type": "speech", "text": text, "duration": duration})

    def show_status(self, text, duration=3):
        if self.ready and _tk_root:
            self._queue.put({"type": "status", "text": text, "duration": duration})

    def show_error(self, text, duration=5):
        if self.ready and _tk_root:
            self._queue.put({"type": "error", "text": text, "duration": duration})

    def destroy(self):
        if self._window and _tk_root:
            _tk_root.after(0, self._window.destroy)
            self._window = None
            self.ready = False


def show_splash(get_resource_path):
    """Optional splash screen, shown if a splash.png resource is bundled."""
    import os
    if _tk_root is None:
        return
    try:
        splash_path = get_resource_path("splash.png")
        if not os.path.exists(splash_path):
            return
        from PIL import Image, ImageTk

        done = threading.Event()

        def _build():
            img = Image.open(splash_path)
            iw, ih = img.size
            photo = ImageTk.PhotoImage(img)
            sw = _tk_root.winfo_screenwidth()
            sh = _tk_root.winfo_screenheight()
            win = tk.Toplevel(_tk_root)
            win.overrideredirect(True)
            win.configure(bg="black")
            win.attributes("-topmost", True)
            win.geometry(f"{iw}x{ih}+{(sw - iw) // 2}+{(sh - ih) // 2}")
            tk.Label(win, image=photo, bg="black").pack()
            win._photo = photo

            def fi(a=0.0):
                a = min(a + 0.05, 1.0)
                win.attributes("-alpha", a)
                win.after(40, fi, a) if a < 1.0 else win.after(2800, fo)

            def fo(a=1.0):
                a = max(a - 0.06, 0.0)
                win.attributes("-alpha", a)
                if a > 0.0:
                    win.after(30, fo, a)
                else:
                    win.destroy()
                    done.set()

            win.attributes("-alpha", 0.0)
            fi()

        _tk_root.after(0, _build)
        done.wait(timeout=6)
    except Exception as e:
        print(f"[Splash] {e}")
