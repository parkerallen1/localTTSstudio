# Local TTS Studio — Release v3.2.0 🎬

This release adds two paragraph-editing quality-of-life features for building longer, well-structured devotionals.

## ✨ New Features

### ➕ Insert paragraphs anywhere
- A **+** affordance now appears between every paragraph card (and at the end of the list). Click it to drop a new, empty editable paragraph exactly where you want it — no more re-parsing or reordering from the end.

### 🎬 Chapter markers + shortcode export
- Each paragraph has a new **⚑ Chapter** toggle to mark where a new chapter begins (shown with a blue left-accent on the card). The setting is saved with the project.
- A new **Copy Chapters Shortcode** button (next to **Download Audio**) copies a JSON array of chapters to your clipboard:
  ```json
  [
    { "title": "Introduction", "start": 0 },
    { "title": "Meet Gideon, the underdog", "start": 55 },
    { "title": "Final thoughts", "start": 253 }
  ]
  ```
  - **title** is the paragraph's first sentence (everything up to the first period).
  - **start** is the chapter's start time in seconds within the downloaded audio — computed from each paragraph's actual audio duration plus the 1-second gaps between segments, so the timestamps line up with the merged download.
- The button is enabled once all paragraphs are generated and at least one chapter is marked.

## 📥 Installation

1. Download `Local.TTS.Studio.v3.2.0.zip` from the **Assets** section below.
2. Unzip the file.
3. Drag the extracted `Local TTS Studio.app` into your `/Applications` folder.
4. Open the application. *(On first launch you may need to right-click → "Open" to bypass macOS Gatekeeper.)*

Existing users will be offered this update automatically via the in-app updater.
