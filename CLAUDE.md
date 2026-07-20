# TTS Studio

A local, offline text-to-speech desktop app for macOS. It runs Qwen3-TTS models
on-device to turn long-form text (devotionals, scripts) into audio, paragraph by
paragraph, and packages as a native `.app`. No cloud calls for synthesis.

## Architecture

It's a **local web app wrapped as a desktop app**: a FastAPI backend serves a
browser UI, and a launcher boots that server and presents it natively.

```
app_launcher.py                  desktop entrypoint (what PyInstaller bundles)
  ├─ starts uvicorn ──► main.py  FastAPI backend on 127.0.0.1:8001
  │                       └─ serves static/ (the UI) + JSON API at /api/*
  ├─ opens the browser at the server URL
  └─ runs a macOS menu-bar app (rumps): Open in browser / Quit

Browser (static/index.html + script.js + style.css)
  └─ calls /api/* on the backend ──► models load on demand, audio saved to disk
```

**Request flow (typical):** user pastes Markdown → `script.js` parses it into
paragraph cards client-side → POSTs each paragraph to `/api/generate` → backend
loads/caches the Qwen3-TTS model and synthesizes → audio saved server-side as
FLAC under the project → user downloads a merged WAV/M4A via `/api/merge` +
`/api/convert`.

## File map

| Path | What it is |
|------|-----------|
| `main.py` | FastAPI backend. Model lifecycle (`get_tts_model`), synthesis, projects/profiles CRUD, audio merge/convert, activity-log SSE, auto-update. `APP_VERSION` lives here. |
| `app_launcher.py` | Desktop entrypoint. Env setup, log redirect, single-instance lock, uvicorn thread, menu bar, graceful shutdown. |
| `static/index.html` | The single UI page. Element IDs are the contract with `script.js`. |
| `static/script.js` | All frontend logic (parsing, generation, projects, export, settings). One big `DOMContentLoaded` closure. |
| `static/style.css` | Dark glassmorphism theme. Shared tokens in `:root`. |
| `LocalTTSStudio.spec` | PyInstaller build spec for the `.app`. |
| `text_parser.py` | Python port of the frontend's Markdown→paragraphs pipeline (keep in sync with `script.js`). Used by `/api/projects/import`. |
| `doc_watcher.py` | Standalone Google Docs watcher: polls Drive for docs shared with a service account, imports them via `/api/projects/import`. Not bundled into the .app. |
| `DOC_WATCHER.md` | Setup guide for the Google Docs auto-import pipeline (service account, config, launchd). |
| `SERVER.md` | How to run the app as a shared remote generation server (token auth + tunnel). |
| `migrate_audio_to_flac.py` | One-time WAV→FLAC migration for existing projects. |
| `download_model.py`, `test_*.py` | Dev helpers / ad-hoc smoke tests (not run by the app). |
| `ffmpeg` | Bundled binary used for FLAC decode and M4A conversion. |

Each source file also has a header comment explaining its role — start there.

## Data & runtime

- **Frozen app:** data in `~/.qwen_tts_studio/` (`projects/`, `profiles/`,
  `settings.json`); logs in `~/.qwen_tts_studio/app.log`. Bundled files resolve
  under `sys._MEIPASS`.
- **Dev:** data in `./data/` (gitignored). Models cache to the Hugging Face
  cache (`~/.cache/huggingface`), downloaded on demand.
- **Models:** modes `Base` (voice cloning), `CustomVoice`, `VoiceDesign`; sizes
  `0.6B` / `1.7B`. One model is held at a time; the previous is freed on switch.
- **Port:** the launcher serves on `127.0.0.1:8001`.
- **Remote generation:** if `settings.json` has `remote_server_url`,
  `/api/generate` is forwarded there (bearer-token auth) instead of loading a
  model locally. Setting `QWEN_TTS_SERVER_TOKEN` runs an instance in server
  mode: all `/api/*` routes require that token. See `SERVER.md`.

## Dev workflow

```bash
python -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/python app_launcher.py     # starts server + menu bar, opens browser
```

The frontend is static and browser-cached — **hard-refresh** after editing
`script.js`/`style.css`/`index.html`.

## Build & release (macOS)

The release process used in this repo:

1. Bump `APP_VERSION` in `main.py` and add `release_notes_vX.Y.Z.md`.
2. Commit and `git push origin main`.
3. Build: `./venv/bin/python -m PyInstaller LocalTTSStudio.spec --noconfirm`
   (output: `dist/TTS Studio.app`).
4. Zip: `cd dist && ditto -c -k --sequesterRsrc --keepParent "TTS Studio.app" "TTS.Studio.vX.Y.Z.zip"`.
5. Release: `gh release create vX.Y.Z --title "..." --notes-file release_notes_vX.Y.Z.md --target main "dist/TTS.Studio.vX.Y.Z.zip"`.

The build is **unsigned / not notarized**, so first launch needs right-click →
Open (Gatekeeper). The in-app updater (`/api/check_update`) compares
`APP_VERSION` to the latest GitHub release and offers the `.zip` asset, so the
asset must end in `.zip` and be attached to the release.

## Conventions

- Git: commit each logical change with a clear message; don't push or release
  unless asked.
- Keep version-bump + release-notes in one commit (see history).
- Reuse CSS `:root` tokens; reuse backend helpers (e.g. `_ffmpeg_bin`,
  `emit_log`) rather than re-implementing.
