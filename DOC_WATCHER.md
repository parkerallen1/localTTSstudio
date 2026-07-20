# Google Docs → TTS auto-import

Share a Google Doc with a dedicated service-account email, and the machine
running TTS Studio picks it up, parses it, and generates all the audio
automatically. Open the app later and the project is there, ready to edit and
export.

```
share doc with tts-ingest@<project>.iam.gserviceaccount.com
        │
        ▼
doc_watcher.py (polls Drive every 2 min on the server machine)
        │  exports doc as Markdown
        ▼
POST /api/projects/import   (parses paragraphs, generates audio in background)
        │
        ▼
project appears in the app, fully generated
```

## One-time Google Cloud setup (~10 minutes)

1. Go to <https://console.cloud.google.com/> (any Google account) and create a
   project, e.g. **tts-studio**.
2. Enable the Drive API: **APIs & Services → Library → Google Drive API → Enable**.
3. Create the service account: **IAM & Admin → Service Accounts → Create**.
   Name it e.g. `tts-ingest`. No roles needed. Note the email it gets —
   `tts-ingest@tts-studio-xxxxx.iam.gserviceaccount.com`.
4. Open the service account → **Keys → Add key → Create new key → JSON**.
   A key file downloads. Move it to the server machine, e.g.
   `~/.qwen_tts_studio/google-key.json`, and `chmod 600` it.
5. (Optional) Add the service-account email to your Google Contacts as
   "TTS Studio" so it autocompletes in the Docs share dialog.

## On the machine that runs the app

1. Install the watcher's extra dependency into the app's venv (it is not part
   of the app itself):

   ```bash
   ./venv/bin/pip install google-auth
   ```

2. Create `~/.qwen_tts_studio/doc_watcher.json`:

   ```json
   {
     "service_account_key": "~/.qwen_tts_studio/google-key.json",
     "app_url": "http://127.0.0.1:8001",
     "app_token": "",
     "poll_seconds": 120,
     "folder_id": "",
     "settings": {}
   }
   ```

   - `app_token` — only needed if the app runs in server mode
     (`QWEN_TTS_SERVER_TOKEN`, see SERVER.md); use the same token.
   - `folder_id` — set to a Drive folder id to watch just that folder
     (share the folder with the service account once, then drop docs in).
     Empty = import every doc shared with the service account.
   - `settings` — per-import voice settings (same shape as a project's
     settings, e.g. `{"modelType": "CustomVoice", "speaker": "Ryan"}`).
     Leave empty to use the app-wide default (next step).

3. Set the default voice for imported docs (once):

   ```bash
   curl -X PUT http://127.0.0.1:8001/api/settings \
     -H 'Content-Type: application/json' \
     -d '{"import_defaults": {"modelType": "CustomVoice", "modelSize": "1.7B", "speaker": "Ryan"}}'
   ```

   For a cloned voice use `{"modelType": "Base", "savedVoiceId": "<profile id>"}`
   (profile ids: `curl http://127.0.0.1:8001/api/profiles`).

4. Run the watcher:

   ```bash
   ./venv/bin/python doc_watcher.py            # foreground loop
   ./venv/bin/python doc_watcher.py --once     # single poll (for cron)
   ```

   To keep it running permanently, install it as a launchd agent —
   `~/Library/LaunchAgents/com.tts.docwatcher.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.tts.docwatcher</string>
     <key>ProgramArguments</key>
     <array>
       <string>/PATH/TO/REPO/venv/bin/python</string>
       <string>/PATH/TO/REPO/doc_watcher.py</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>StandardOutPath</key><string>/tmp/doc_watcher.log</string>
     <key>StandardErrorPath</key><string>/tmp/doc_watcher.log</string>
   </dict>
   </plist>
   ```

   ```bash
   launchctl load ~/Library/LaunchAgents/com.tts.docwatcher.plist
   tail -f /tmp/doc_watcher.log
   ```

## Day-to-day use

- **Share a doc** with the service-account email (Viewer is enough) → within
  `poll_seconds` it's imported and generating. Progress shows in the app's
  Activity Log; the project appears in the project list immediately.
- Each doc is imported **once**. Editing a doc after import is ignored (the
  watcher logs a warning) — share a fresh copy to regenerate.
- State lives in `~/.qwen_tts_studio/doc_watcher_state.json`; delete a doc's
  entry there to force a re-import.

## Notes

- The watcher must run on the same machine as the app instance you open in the
  browser (or point `app_url` at it over Tailscale) — projects live where the
  import happens.
- The import endpoint can be used directly by anything else, too:

  ```bash
  curl -X POST http://127.0.0.1:8001/api/projects/import \
    -H 'Content-Type: application/json' \
    -d '{"name": "My piece", "raw_text": "# Title\n\nBody text..."}'
  ```
