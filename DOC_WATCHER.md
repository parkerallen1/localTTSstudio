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
        │  (optional: "email" config block)
        ▼
emails the finished M4A back to whoever shared the doc, + a link to edit it
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
   - `email` — optional; email the finished audio back to the sharer once
     generation completes. See **Emailing finished audio back** below.

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

## Emailing finished audio back

When generation for an imported doc finishes, the watcher can email the
person who shared it — the merged **M4A** as an attachment, plus a reminder
of the app URL to open if they want to edit or re-export.

Sending uses the **Gmail API over OAuth** (Google's recommended path, not an
app password). You grant consent once with `gmail_auth.py`; the watcher then
sends headlessly using the stored refresh token.

### One-time: authorize sending (`gmail_auth.py`)

In the **same Google Cloud project** as the Drive service account:

1. **APIs & Services → Library →** enable **Gmail API**.
2. **APIs & Services → OAuth consent screen:**
   - User type **External**; add your Gmail under **Test users**.
   - Then click **PUBLISH APP** (Production). This matters — while an app is in
     *Testing*, Google **expires its refresh tokens after 7 days**, which would
     silently break the pipeline weekly. Published apps don't. You'll still get
     an "unverified app" screen at consent time; it's your own app, click through.
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID →**
   Application type **Desktop app**. Download the client-secret JSON.

Then, **on a machine with a browser** (e.g. your Mac):

```bash
./venv/bin/pip install google-auth-oauthlib
./venv/bin/python gmail_auth.py --client-secrets /path/to/client_secret.json
```

A browser opens — sign in as the account emails should come **from** and
approve. A token is written to `~/.qwen_tts_studio/gmail_token.json`. If the
watcher runs on a different machine (the mini), copy that token file over:

```bash
scp ~/.qwen_tts_studio/gmail_token.json mini:~/.qwen_tts_studio/
```

`google-auth-oauthlib` is only needed for this one-time step; the watcher
itself sends with `google-auth` (already installed).

### Config: the `email` block

```json
"email": {
  "enabled": true,
  "oauth_token": "~/.qwen_tts_studio/gmail_token.json",
  "from_address": "you@gmail.com",
  "from_name": "TTS Studio",
  "bcc": "you@gmail.com",
  "reply_to": "",
  "edit_url": "http://mini.your-tailnet.ts.net:8001",
  "treatment": "clear"
}
```

- `oauth_token` — path to the token from `gmail_auth.py`. Blank = the default
  `~/.qwen_tts_studio/gmail_token.json`.
- `from_address` — the Gmail you consented as. Gmail sends as that account.
- `bcc` — you're BCC'd on every send for oversight. Leave `""` to disable.
  (Delivered via a Bcc header, which Gmail strips from the recipient's copy.)
- `edit_url` — the app URL the recipient should open to edit. `127.0.0.1`
  isn't reachable from another machine, so use the host's Tailscale name/IP
  (e.g. `http://mini.your-tailnet.ts.net:8001`). The app has no per-project
  deep link yet, so the email tells them to open the project **by name**.
- `treatment` — audio treatment applied on export (same options as the app's
  export dropdown; `"clear"` is the default, `"none"` for raw).

**Who gets the email:** Drive tells us who shared the doc with the service
account (`sharingUser`, falling back to the document's owner). If neither is
available (e.g. some Shared Drive items), the watcher logs a warning and skips
the email for that doc — it never guesses a recipient.

**Reliability:** the email is only attempted after `import_status` is `done`.
A failed export or send is retried on the next poll, up to 5 times, then
marked `failed` in the state file. Each doc is emailed once.

**Test it** without waiting for a real doc (sends a link-only email to you):

```bash
./venv/bin/python - <<'PY'
import doc_watcher, json, os
cfg = json.load(open(os.path.expanduser("~/.qwen_tts_studio/doc_watcher.json")))
w = doc_watcher.Watcher(cfg)
addr = cfg["email"].get("from_address")
w.send_completion_email(addr, "You", "Test Doc", m4a_bytes=None, have_audio=False)
print("sent to", addr)
PY
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
