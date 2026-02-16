# ☁️ MistAI Desktop Assistant – Beta V1.5

MistAI Desktop is a Jarvis-style AI assistant that runs directly on your computer.  
It listens for wake words, automates your desktop, reads your screen, and provides live captions.

🚀 **Current Version:** Beta V1.5  
💻 **Platform:** Windows, macOS, Linux  
🎤 **Wake Word Support**  
👁️ **Screen Vision (OCR)**  
📺 **Live Caption Overlay**  
🎨 **Splash Screen Loading**  

---

## 🆕 What's New in V1.5

### ✨ Professional Splash Screen
- Elegant fade-in animation on startup
- Custom branded loading experience
- Smooth transition to main window

### 🌍 Multi-Platform Support
- **Windows**: Full feature support with bundled Tesseract OCR
- **macOS**: All features except OCR (install Tesseract separately)
- **Linux**: All features except OCR (install Tesseract separately)

### 🔧 Improved Stability
- Enhanced error handling for missing dependencies
- Better fallback mechanisms for OCR failures
- Optimized build process with PyInstaller

---

## 🆕 Core Features

### 🎤 Wake Word Voice Assistant
- Activate with "Mist", "Hey Mist", or "MistAI"
- Natural speech commands with fuzzy matching
- Always-on background listener
- Continuous conversation mode (45-second timeout)

### 👁️ Vision Mode (Screen Understanding)
- Reads on-screen text automatically
- UI button detection and smart clicking
- Multi-strategy OCR with fallback modes
- AI-powered recovery when actions fail
- **Note:** OCR bundled on Windows only

### 📺 Live Captions System
- Real-time caption overlay at screen bottom
- Color-coded messages:
  - 🟢 **Green** = Assistant responses
  - 🟡 **Yellow** = System notifications  
  - 🟣 **Purple** = Proactive suggestions
- Privacy-first (user speech never displayed)

### 💡 Proactive Mode
- Screen monitoring with smart suggestions
- Context-aware recommendations
- Learns from your workflow

### ⚙️ Desktop Automation
- **App Control:** Open, focus, maximize applications
- **Input:** Type text, press keys, click elements
- **Navigation:** Scroll, fullscreen, window management
- **Volume:** System audio control
- **Multi-step:** Chain complex workflows

### 🧠 Context Memory
- Tracks conversation history
- Remembers opened apps
- Monitors active windows
- Session-aware actions

---

## 💻 System Requirements

### Windows
✔ Windows 10 / 11 (64-bit)  
✔ Microphone  
✔ Internet connection  
✔ Vision OCR (bundled)  

### macOS
✔ macOS 10.15 or later  
✔ Microphone  
✔ Internet connection  
⚠️ Vision OCR (requires [Tesseract](https://github.com/tesseract-ocr/tesseract) install)

### Linux
✔ Ubuntu 20.04+ / Debian / Fedora  
✔ Microphone  
✔ Internet connection  
⚠️ Vision OCR (requires [Tesseract](https://github.com/tesseract-ocr/tesseract) install)

---

## 🚀 Installation

### Option 1: Download Pre-built Executable (Recommended)

1. Go to [Releases](https://github.com/Misto0o/MistAI-Desktop/releases)
2. Download for your platform:
   - `MistAI-Windows.zip` (Windows)
   - `MistAI-macOS.zip` (macOS)
   - `MistAI-Linux.tar.gz` (Linux)
3. Extract and run:
   - **Windows:** `MistAI.exe`
   - **macOS:** `MistAI.app`
   - **Linux:** `./MistAI`
4. Say **"Hey Mist"** to start!

### Option 2: Build from Source
```bash