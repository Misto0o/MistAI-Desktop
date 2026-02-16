# -*- mode: python ; coding: utf-8 -*-
import os
import site
from pathlib import Path

block_cipher = None
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

# ==========================================
# TESSERACT BUNDLING
# ==========================================
TESSERACT_SOURCE = os.path.join(SPEC_DIR, 'MistAI_OCREngine', 'Tesseract-OCR')

if not os.path.exists(TESSERACT_SOURCE):
    raise FileNotFoundError(f"Tesseract folder not found at: {TESSERACT_SOURCE}")

tesseract_files = []
for root, dirs, files in os.walk(TESSERACT_SOURCE):
    for file in files:
        source_path = os.path.join(root, file)
        rel_path = os.path.relpath(source_path, TESSERACT_SOURCE)
        dest_path = os.path.join('Tesseract-OCR', rel_path)
        dest_folder = os.path.dirname(dest_path)
        tesseract_files.append((source_path, dest_folder))

# ==========================================
# PYAUDIO BUNDLING (Windows)
# ==========================================
pyaudio_binaries = []
if os.name == 'nt':
    try:
        import pyaudio
        site_packages = site.getsitepackages()
        for site_pkg in site_packages:
            pyaudio_path = os.path.join(site_pkg, 'pyaudio')
            if os.path.exists(pyaudio_path):
                for file in os.listdir(pyaudio_path):
                    if file.endswith('.pyd') or file.endswith('.dll'):
                        pyaudio_binaries.append((os.path.join(pyaudio_path, file), '.'))
                        print(f"Found PyAudio binary: {file}")
    except Exception as e:
        print(f"Warning: Could not bundle PyAudio: {e}")

# ==========================================
# SPLASH IMAGE & ICON
# ==========================================
splash_image = os.path.join(SPEC_DIR, 'splash.png')
icon_path = os.path.join(SPEC_DIR, 'mistaifaviocn', 'favicon.ico')

data_files = tesseract_files.copy()
if os.path.exists(splash_image):
    data_files.append((splash_image, '.'))
    print(f"Found splash.png")

# ==========================================
# PYINSTALLER ANALYSIS
# ==========================================
a = Analysis(
    ['assistant.py'],
    pathex=[],
    binaries=pyaudio_binaries,
    datas=data_files,
    hiddenimports=[
        'webview',
        'pyautogui',
        'requests',
        'speech_recognition',
        'pyttsx3',
        'pyaudio',
        '_portaudio',
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
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path if os.path.exists(icon_path) else None,
)