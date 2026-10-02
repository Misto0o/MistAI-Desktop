"""
core/click_indicator.py — visual debug overlay: flashes a glowing marker at
the exact pixel MistAI is about to click, color-coded by which detection
method resolved it. Built because logs saying "[Vision] Found 'Subscribe' @
(115, 873)" don't tell you whether that's actually ON the button -- seeing a
dot land next to it instead of on it is immediate, unambiguous ground truth.

Pure debug aid. Two hard guarantees:
  1. NEVER blocks or slows down the real click -- rendering happens on its
     own Tk thread; every other thread only ever pushes onto a Queue.
  2. NEVER intercepts the real click -- the overlay window is fully
     click-through (Windows -transparentcolor), so even if a marker is
     sitting exactly on top of the target pixel when pyautogui clicks, the
     click still reaches whatever's underneath.
If Tk isn't available (headless box, remote session, whatever), every call
silently becomes a no-op. Automation must never depend on this working.
"""

import queue
import threading

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


class ClickIndicator:
    """Runs its own Tk mainloop on a dedicated daemon thread. Call .show()
    from any thread; it's just a queue push."""

    def __init__(self):
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self._available = True
        self._tk = None
        self.root = None
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

    def _flash(self, x, y, method, label):
        tk = self._tk
        color = METHOD_COLORS.get(method, METHOD_COLORS["direct"])
        size = 70
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-transparentcolor", TRANSPARENT_KEY)
            except Exception:
                pass  # non-Windows Tk build -- still shows, just not click-through
            win.geometry(f"{size}x{size}+{int(x - size / 2)}+{int(y - size / 2)}")
            canvas = tk.Canvas(win, width=size, height=size,
                               bg=TRANSPARENT_KEY, highlightthickness=0)
            canvas.pack()
            cx = cy = size / 2
            tag_text = method + (f" '{label[:14]}'" if label else "")
            canvas.create_text(cx, size - 8, text=tag_text, fill=color,
                               font=("Segoe UI", 7))
        except Exception as e:
            print(f"[ClickIndicator] Flash render failed: {e}")
            return

        def pulse(step=0):
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
                    win.after(150, win.destroy)
            except Exception:
                try:
                    win.destroy()
                except Exception:
                    pass

        pulse()


_instance = None
_lock = threading.Lock()


def get_indicator():
    """Process-wide singleton -- one Tk thread total, not one per call."""
    global _instance
    with _lock:
        if _instance is None:
            _instance = ClickIndicator()
        return _instance