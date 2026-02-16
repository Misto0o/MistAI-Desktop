# -*- mode: python ; coding: utf-8 -*-
# MistAI Desktop Assistant - Production Build with Bundled Tesseract
# This spec file bundles Tesseract OCR for true one-click installation

import os
import glob
from pathlib import Path

block_cipher = None

# ==========================================
# TESSERACT BUNDLING CONFIGURATION
# ==========================================

# Get the base directory where this spec file is located
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# Path to your bundled Tesseract folder
TESSERACT_SOURCE = os.path.join(SPEC_DIR, 'MistAI_OCREngine', 'Tesseract-OCR')

# Verify Tesseract folder exists
if not os.path.exists(TESSERACT_SOURCE):
    raise FileNotFoundError(f"Tesseract folder not found at: {TESSERACT_SOURCE}")

# Collect ALL Tesseract files recursively
tesseract_files = []
for root, dirs, files in os.walk(TESSERACT_SOURCE):
    for file in files:
        source_path = os.path.join(root, file)
        # Calculate relative path from Tesseract-OCR folder
        rel_path = os.path.relpath(source_path, TESSERACT_SOURCE)
        # Destination in bundle: Tesseract-OCR/...
        dest_path = os.path.join('Tesseract-OCR', rel_path)
        # Add as tuple (source, destination_folder)
        dest_folder = os.path.dirname(dest_path)
        tesseract_files.append((source_path, dest_folder))

# Verify critical files exist
critical_files = [
    os.path.join(TESSERACT_SOURCE, 'tesseract.exe'),
    os.path.join(TESSERACT_SOURCE, 'tessdata', 'eng.traineddata'),
]

for critical_file in critical_files:
    if not os.path.exists(critical_file):
        print(f"WARNING: Critical file missing: {critical_file}")
    else:
        print(f"Found critical file: {os.path.basename(critical_file)}")

splash_image = os.path.join(SPEC_DIR, 'splash.png')
if os.path.exists(splash_image):
    print(f"Found splash.png at {splash_image}")
else:
    print(f"WARNING: splash.png not found at {splash_image}")

# Add icon check
icon_path = os.path.join(SPEC_DIR, 'mistaifaviocn', 'favicon.ico')
if not os.path.exists(icon_path):
    print(f"WARNING: icon not found at {icon_path}")
else:
    print(f"Found favicon.ico")

# Now create data_files list with splash image
data_files = tesseract_files.copy()
if os.path.exists(splash_image):
    data_files.append((splash_image, '.'))

# ==========================================
# PYINSTALLER ANALYSIS
# ==========================================

a = Analysis(
    ['launcher.py'],  # ← Changed from assistant.py to splash.py
    pathex=[],
    binaries=[],
    datas=data_files,
    hiddenimports=[
        'assistant',  # ← Add this so assistant.py is included
        'webview',
        'pyautogui',
        'requests',
        'speech_recognition',
        'pyttsx3',
        'psutil',
        'pytesseract',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'cv2',
        'numpy',
        'win32gui',
        'win32con',
        'win32com',
        'win32com.client',
        'pythoncom',
        'pywintypes',
        'tkinter',
        'tkinter.font',
        'queue',
        'threading',
        'datetime',
        'json',
        'difflib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'pandas',
        'scipy',
        'sklearn',
        'tensorflow',
        'torch',
        'keras',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='MistAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # No console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
)