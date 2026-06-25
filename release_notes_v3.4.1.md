# Local TTS Studio — Release v3.4.1 🔖

A small refinement to automatic chapter marking.

## ✨ Improvements

### 🚫 Smarter chapter detection
- Boilerplate section headings are no longer auto-marked as chapters, even though they're `##` headings. They still start their own paragraph — they just don't get a chapter marker:
  - **Settle in**
  - **Thought starter**
  - **Reflection Questions**
  - **Humor break**
  - **Bring the inspiration with you**
- Matching ignores emoji prefixes, `**bold**` wrapping, and capitalization (e.g. `## **🧘 Settle in**` is recognized as "Settle in").

## 📥 Installation

1. Download `Local.TTS.Studio.v3.4.1.zip` from the **Assets** section below.
2. Unzip the file.
3. Drag the extracted `Local TTS Studio.app` into your `/Applications` folder.
4. Open the application. *(On first launch you may need to right-click → "Open" to bypass macOS Gatekeeper.)*

Existing users will be offered this update automatically via the in-app updater.
