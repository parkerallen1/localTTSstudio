# Local TTS Studio v3.8.3

## Google Docs → automatic TTS

Share a Google Doc with a service account and a watcher turns it into a
fully generated project — no copy-paste, no clicking Generate.

- **`POST /api/projects/import`** — new endpoint that takes raw Markdown,
  parses it into paragraphs server-side (same rules as the Parse button),
  and generates every paragraph in the background. Default voice comes from
  the new `import_defaults` setting.
- **`doc_watcher.py`** — polls Google Drive for docs shared with a service
  account (Shared Drives included), converts the **first tab** to Markdown
  via the Docs API (headings preserved, so `##` chapters keep working), and
  imports them. Setup guide: `DOC_WATCHER.md`.

## Use the server from any browser

- Server-mode instances now show a one-time **Enter Access Code** screen
  instead of silently failing — the token is remembered per device (cookie),
  and the full UI works through a tunnel: projects, playback, takes, export.
- SERVER.md documents the two access modes (browser vs. desktop-app remote).

## Live-updating projects

- Opening a project that's still generating in the background now updates in
  place — each paragraph's audio appears as it finishes, no refresh needed.

## Fixes

- Import generation failures mark the paragraph and continue instead of
  wedging the project (e.g. during network hangs).

## Mobile

- The browser UI now has a proper phone layout: single-column reflow,
  wrapping action rows (no more Download button running off-screen), full-
  width audio players and activity log, and comfortable tap targets.

## Fixes (3.8.2)

- Bible text formatting now removes bracketed verse numbers like `[7]`,
  `[28]` again. They were being turned into commas by the generic text
  cleanup before the Bible rule could strip them; Bible formatting now
  runs first. Applies to both the browser Parse button and Google Docs
  auto-import.

## Fixes (3.8.3)

- Download format toggle: choosing **M4A** now actually downloads M4A.
  The WAV/M4A toggle shared a CSS class with the Local/Cloud generation
  toggle, so interacting with Local/Cloud silently reset the download
  format back to WAV. The toggles are now independent.
