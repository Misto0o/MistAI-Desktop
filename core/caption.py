# ============================================================
# OVERLAYS MODULE
# ============================================================
#
# The Tk overlays: the status/caption window (and the shared Tk root) and
# the click indicator that flashes where a click landed.
#
# SECTION INDEX
#   1. SHARED TK ROOT STATE
#   2. TK THREAD
#   3. STARTING THE TK ROOT
#   4. TK ROOT ACCESS
#   5. INERT OVERLAY WINDOWS
#   6. CAPTION OVERLAY
#   7. SPLASH SCREEN
#   8. CLICK INDICATOR
#   9. MARKER COLORS AND TIMING
#   10. CLICK MARKER OVERLAY
#   11. MARKER SINGLETON STATE
#   12. MARKER SINGLETON
# ============================================================

"""
core/caption.py — on-screen status/caption overlay, plus the shared Tk root
that the whole app uses for any Tk-based UI (caption window, splash screen).
"""

import threading
import tkinter as tk
from queue import Empty, Queue
from tkinter import font as tkfont
from typing import Optional
import queue


# ============================================================
# SHARED TK ROOT STATE
# ============================================================

_tk_root: Optional[tk.Tk] = None
_tk_ready = threading.Event()


# ============================================================
# TK THREAD
# ============================================================

def _tk_thread_main():
    global _tk_root
    _tk_root = tk.Tk()
    _tk_root.withdraw()
    _tk_root.protocol("WM_DELETE_WINDOW", lambda: None)
    _tk_ready.set()
    _tk_root.mainloop()


# ============================================================
# STARTING THE TK ROOT
# ============================================================

def start_tk_root(timeout=5):
    """Call once at startup. Spins up the shared Tk mainloop thread."""
    t = threading.Thread(target=_tk_thread_main, daemon=True, name="TkMain")
    t.start()
    _tk_ready.wait(timeout=timeout)
    return _tk_root


# ============================================================
# TK ROOT ACCESS
# ============================================================

def get_tk_root():
    return _tk_root


# ============================================================
# INERT OVERLAY WINDOWS
# ============================================================

# The overlays only show things. A Tk toplevel is otherwise an ordinary
# window: a click on any painted pixel lands on it and activates MistAI (the
# click marker's centre dot sat exactly on the click point, so Calculator
# never got the click). These extended styles make it inert: never
# activated, never the foreground, no taskbar button, and every mouse event
# passes through all of its surface to the window below.
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
INERT_EX_STYLES = WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED
GWL_EXSTYLE = -20
SWP_INERT = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED


def tk_frame_hwnd(win):
    """The Win32 wrapper window (class TkTopLevel) of a Tk toplevel."""
    return int(win.wm_frame(), 16)


def make_inert(hwnd):
    """Add INERT_EX_STYLES to a window (keeping the rest, dropping
    WS_EX_APPWINDOW); the resulting extended style, or None on failure.
    Call it while the window is still hidden, so it is never shown active.
    WS_EX_LAYERED needs layered attributes: Tk's -alpha / -transparentcolor
    set them, so apply those first."""
    try:
        import win32gui

        ex = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
        win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, (ex | INERT_EX_STYLES) & ~WS_EX_APPWINDOW)
        win32gui.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_INERT)
        return win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
    except Exception as e:
        print(f"[Overlay] could not make the window inert ({type(e).__name__})")
        return None


# ============================================================
# CAPTION OVERLAY
# ============================================================

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
                self._window.withdraw()            # never shown before it is inert
                self._window.overrideredirect(True)
                self._window.attributes("-topmost", True)
                self._window.attributes("-alpha", 0.0)
                self._window.geometry(f"{w}x90+{x}+{y}")
                self._window.update_idletasks()
                make_inert(tk_frame_hwnd(self._window))
                self._window.deiconify()
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


# ============================================================
# SPLASH SCREEN
# ============================================================

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


# ============================================================
# CLICK INDICATOR
# ============================================================

# core/click_indicator.py — visual debug overlay: flashes a glowing marker at
# the exact pixel MistAI is about to click, color-coded by which detection
# method resolved it. Built because logs saying "[Vision] Found 'Subscribe' @
# (115, 873)" don't tell you whether that's actually ON the button -- seeing a
# dot land next to it instead of on it is immediate, unambiguous ground truth.
#
# Pure debug aid. Three hard guarantees:
#   1. NEVER blocks or slows down the real click -- rendering happens on its
#      own Tk thread; every other thread only ever pushes onto a Queue, and
#      callers request the marker only AFTER the click was sent.
#   2. NEVER intercepts the real click -- one marker window, made inert
#      (make_inert: no activation, every pixel click-through) while still
#      hidden. -transparentcolor alone left the painted dot clickable: real
#      runs showed the click landing on MistAI's Tk window, which then took
#      the foreground while Calculator's display stayed 0.
#   3. NEVER takes the foreground -- the window is shown once, off screen,
#      already inert; a flash only moves it (no new windows, no re-show).
# If Tk isn't available (headless box, remote session, whatever), every call
# silently becomes a no-op. Automation must never depend on this working.

# ============================================================
# MARKER COLORS AND TIMING
# ============================================================

METHOD_COLORS = {
    "OCR": "#34C759",       # green
    "UIA": "#0A84FF",       # blue
    "Vision": "#FF9F0A",    # orange
    "coords": "#FF3B30",    # red
    "direct": "#8E8E93",    # gray
}
TRANSPARENT_KEY = "#010101"   # near-black -- unlikely to collide with real UI
FLASH_MS = 700
RING_STEPS = 10
MARKER_SIZE = 70
OFFSCREEN = -32000                  # where the marker waits between flashes


# ============================================================
# CLICK MARKER OVERLAY
# ============================================================

class ClickIndicator:
    """Runs its own Tk mainloop on a dedicated daemon thread. Call .show()
    from any thread; it's just a queue push."""

    def __init__(self):
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self._available = True
        self._tk = None
        self.root = None
        self.marker = None          # the one marker toplevel
        self.hwnd = 0               # its Win32 wrapper window
        self.ex_style = None        # its extended style after make_inert
        self._flash_id = 0          # a newer flash stops an older pulse
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(timeout=2.0)

    def show(self, x, y, method="direct", label=""):
        """Enqueue a flash at (x, y). Safe to call from the automation
        thread mid-click-sequence -- never raises, never blocks."""
        if not self._available:
            return
        try:
            self._queue.put_nowait((int(x), int(y), method or "direct", label or ""))
        except Exception:
            pass

    # -- Tk thread (only code in this class allowed to touch tk objects) ----

    def _run(self):
        try:
            import tkinter as tk
            self._tk = tk
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.attributes("-topmost", True)
            self._build_marker()
        except Exception as e:
            print(f"[ClickIndicator] Overlay unavailable ({e}) -- debug dots disabled")
            self._available = False
            self._ready.set()
            return

        self._ready.set()
        self._poll()
        try:
            self.root.mainloop()
        except Exception:
            pass

    def _poll(self):
        try:
            while True:
                x, y, method, label = self._queue.get_nowait()
                self._flash(x, y, method, label)
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.root.after(30, self._poll)
        except Exception:
            pass

    def _build_marker(self):
        """The one marker window: withdrawn, made inert, then shown off screen
        (so its first show is already non-activating and click-through)."""
        tk = self._tk
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        try:
            win.attributes("-transparentcolor", TRANSPARENT_KEY)
        except Exception:
            pass  # non-Windows Tk build
        win.geometry(f"{MARKER_SIZE}x{MARKER_SIZE}+{OFFSCREEN}+{OFFSCREEN}")
        self.canvas = tk.Canvas(win, width=MARKER_SIZE, height=MARKER_SIZE,
                                bg=TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack()
        win.update_idletasks()
        self.hwnd = tk_frame_hwnd(win)
        self.ex_style = make_inert(self.hwnd)
        win.deiconify()
        self.marker = win

    def _flash(self, x, y, method, label):
        color = METHOD_COLORS.get(method, METHOD_COLORS["direct"])
        size = MARKER_SIZE
        canvas, win = self.canvas, self.marker
        self._flash_id += 1
        flash_id = self._flash_id
        try:
            canvas.delete("all")
            cx = cy = size / 2
            tag_text = method + (f" '{label[:14]}'" if label else "")
            canvas.create_text(cx, size - 8, text=tag_text, fill=color,
                               font=("Segoe UI", 7))
            win.geometry(f"{size}x{size}+{int(x - size / 2)}+{int(y - size / 2)}")
        except Exception as e:
            print(f"[ClickIndicator] Flash render failed: {type(e).__name__}")
            return

        def park():
            if flash_id == self._flash_id:
                try:
                    canvas.delete("all")
                    win.geometry(f"{size}x{size}+{OFFSCREEN}+{OFFSCREEN}")
                except Exception:
                    pass

        def pulse(step=0):
            if flash_id != self._flash_id:
                return                       # a newer flash took the marker
            try:
                canvas.delete("ring")
                r = 6 + step * 3
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                   outline=color, width=max(1, 3 - step // 4),
                                   tags="ring")
                canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4,
                                   fill=color, outline="", tags="ring")
                if step < RING_STEPS:
                    win.after(FLASH_MS // RING_STEPS, pulse, step + 1)
                else:
                    win.after(150, park)
            except Exception:
                park()

        pulse()


# ============================================================
# MARKER SINGLETON STATE
# ============================================================

_instance = None
_lock = threading.Lock()


# ============================================================
# MARKER SINGLETON
# ============================================================

def get_indicator():
    """Process-wide singleton -- one Tk thread total, not one per call."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = ClickIndicator()
        return _instance
