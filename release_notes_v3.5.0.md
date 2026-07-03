# Local TTS Studio — Release v3.5.0 🎬

Regeneration history ("takes") and major stability fixes for long generation sessions.

## ✨ New

### 🎞️ Every generation is kept as a take
- Regenerating a paragraph — even after editing its text — no longer throws away the previous audio. Each generation is saved as a numbered **take**.
- Take chips appear under the audio player once a paragraph has more than one take. Click a chip to switch which take plays (and which one is used in the exported audio); click the **×** on a chip to delete that take.
- Takes are saved with the project, so they're all still there after closing and reopening the app.
- While a regeneration is running, the previous takes stay listed and playable — and if the regeneration fails, the paragraph simply falls back to them instead of losing its audio.

## 🛠️ Fixes

### 🚀 Generation no longer gets stuck after many retries
- Fixed a stall where one runaway generation (the model failing to stop speaking) could grind at high CPU/memory for 10+ minutes while every subsequent generation queued behind it. Generation length is now capped relative to the paragraph's text, so a runaway stops within seconds.
- Refreshing the page or retrying no longer piles up abandoned work: queued generations whose browser request was already abandoned are skipped instead of executed.
- GPU memory is released after every generation, preventing the gradual memory creep (and eventual slowdown) over long sessions.
- Switching model size/type can no longer unload a model while it's mid-generation.

## 📥 Installation

1. Download `Local.TTS.Studio.v3.5.0.zip` from the **Assets** section below.
2. Unzip the file.
3. Drag the extracted `Local TTS Studio.app` into your `/Applications` folder.
4. Open the application. *(On first launch you may need to right-click → "Open" to bypass macOS Gatekeeper.)*

Existing users will be offered this update automatically via the in-app updater.
