# Local TTS Studio — Release v3.3.0 📝

This release makes pasting formatted Google Docs effortless and shrinks project storage.

## ✨ New Features

### 📝 Markdown-aware parsing
- Paste **Markdown** (not plain text) and the parser now understands your document's structure:
  - Each `##` heading (H2) starts a **new paragraph** with the **⚑ Chapter** toggle automatically turned on.
  - The **first paragraph** (your title) is always marked as a chapter start.
  - Markdown markers — `#`/`##`/`###`, `**bold**`, `*italic*`, `-`/`*` bullets, `[links](url)`, and backslash escapes — are stripped so the TTS never reads syntax aloud.
  - Leftover emoji modifiers (variation selectors, ZWJ, keycaps) are removed too.
- The **QQT metadata block** (lines labeled `Time:`, `Focus:`, `Scriptures:`) is dropped automatically — those headers aren't read aloud.
- The Input Text panel now reminds you to paste Markdown, with the exact Google Docs steps (*Tools → Preferences → Automatically detect Markdown*).

### 💾 Smaller projects (FLAC audio)
- Project audio is now stored as **FLAC** instead of WAV — lossless, but roughly half the size on disk. Existing projects are migrated automatically on first launch.

## 📥 Installation

1. Download `Local.TTS.Studio.v3.3.0.zip` from the **Assets** section below.
2. Unzip the file.
3. Drag the extracted `Local TTS Studio.app` into your `/Applications` folder.
4. Open the application. *(On first launch you may need to right-click → "Open" to bypass macOS Gatekeeper.)*

Existing users will be offered this update automatically via the in-app updater.
