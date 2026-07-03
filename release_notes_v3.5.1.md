# Local TTS Studio — Release v3.5.1 🩹

Hotfix for audio download/merge in v3.5.0.

## 🛠️ Fixes

### 📦 "Download Audio" works again for saved takes
- Fixed `Merge FAILED: [Errno 2] No such file or directory: 'ffprobe'` when downloading merged audio. Merging paragraphs whose audio was loaded from storage (reopened projects, or takes you switched to) required an `ffprobe` binary that isn't shipped with the app. Audio segments are now decoded natively, with no external tools involved.

## 📥 Installation

1. Download `Local.TTS.Studio.v3.5.1.zip` from the **Assets** section below.
2. Unzip the file.
3. Drag the extracted `Local TTS Studio.app` into your `/Applications` folder.
4. Open the application. *(On first launch you may need to right-click → "Open" to bypass macOS Gatekeeper.)*

Existing users will be offered this update automatically via the in-app updater.
