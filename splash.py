import time
import sys
import os
from tkinter import *
from PIL import Image, ImageTk

def get_resource_path(relative_path):
    """Get absolute path to resource, works for dev and PyInstaller"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def show_splash():
    """Display splash screen and launch assistant"""
    root = Tk()
    root.overrideredirect(True)
    root.configure(bg="black")
    
    # Load image
    try:
        img_path = get_resource_path("splash.png")
        img = Image.open(img_path)
        image_width, image_height = img.size
        photo = ImageTk.PhotoImage(img)
    except Exception as e:
        print(f"Failed to load splash image: {e}")
        root.destroy()
        from assistant import main
        main()
        return
    
    # Center window
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    x = (screen_width // 2) - (image_width // 2)
    y = (screen_height // 2) - (image_height // 2)
    root.geometry(f"{image_width}x{image_height}+{x}+{y}")
    
    # Label for image
    label = Label(root, image=photo, bg="black")
    label.pack()
    
    # Fade-in animation
    def fade_in(alpha=0):
        alpha += 0.05
        if alpha > 1:
            alpha = 1
        root.attributes("-alpha", alpha)
        if alpha < 1:
            root.after(50, fade_in, alpha)
        else:
            root.after(3000, close_splash)
    
    def close_splash():
        root.destroy()
        from assistant import main
        main()
    
    root.attributes("-alpha", 0)
    fade_in()
    root.mainloop()