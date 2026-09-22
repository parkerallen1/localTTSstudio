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
emails the M4A + chapters shortcode to whoever shared the doc, + a link to edit
        │  (optional: "wordpress" config block)
        ▼
finds the post with the same title, uploads the M4A, sets its ACF fields
        │  (no single title match?)
        ▼
emails you the closest matches; reply with the post link and it publishes there
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
   `~/Library/LaunchAgents/com.localtts.docwatcher.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.localtts.docwatcher</string>
     <key>ProgramArguments</key>
     <array>
       <string>/PATH/TO/REPO/venv/bin/python</string>
       <string>/PATH/TO/REPO/doc_watcher.py</string>
     </array>
     <key>RunAtLoad</key><true/>
     <key>KeepAlive</key><true/>
     <key>StandardOutPath</key><string>/tmp/localtts-docwatcher.log</string>
     <key>StandardErrorPath</key><string>/tmp/localtts-docwatcher.log</string>
   </dict>
   </plist>
   ```

   ```bash
   launchctl load ~/Library/LaunchAgents/com.localtts.docwatcher.plist
   tail -f /tmp/localtts-docwatcher.log

   # after a git pull, restart it so the new code is actually running
   launchctl kickstart -k gui/$(id -u)/com.localtts.docwatcher
   ```

## Emailing finished audio back

When generation for an imported doc finishes, the watcher can email the
person who shared it — the merged **M4A** as an attachment, the **chapters
shortcode** for that audio, plus a reminder of the app URL to open if they
want to edit or re-export.

The shortcode is the same JSON array the app's **Copy Chapters Shortcode**
button produces (`[{"title": ..., "start": <seconds>}, ...]`), fetched from
`GET /api/projects/<id>/chapters` so the start times line up with the attached
M4A. Docs with no chapter-marked paragraphs simply omit that section.

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
If the chapters lookup fails, the email still goes out — just without the
shortcode.
A failed export or send is retried on the next poll, up to 5 times, then
marked `failed` in the state file. Each doc is emailed once.

**Test it** without waiting for a real doc (sends a link-only email to your
`bcc` address). Run it **from the repo directory** so `doc_watcher` imports:

```bash
cd /path/to/localTTSstudio    # the repo dir (where doc_watcher.py lives)
./venv/bin/python - <<'PY'
import doc_watcher, json, os
cfg = json.load(open(os.path.expanduser("~/.qwen_tts_studio/doc_watcher.json")))
w = doc_watcher.Watcher(cfg)
to = cfg["email"].get("bcc") or cfg["email"].get("from_address")
w.send_completion_email(to, "You", "Test Doc", m4a_bytes=None, have_audio=False)
print("sent to", to)
PY
```

## Attaching the audio to a WordPress post

With a `wordpress` block configured, the watcher goes one step further: once a
doc's audio is ready it finds the post with the same title, uploads the M4A to
the media library, and sets the ACF fields that point the post at it.

It works over SSH + wp-cli, which matters for three reasons:

- The M4A is streamed to the server and imported with `wp media import`, so it
  never passes through PHP's upload-size limit. A 45-minute devotional is fine.
- Fields are set with ACF's own `update_field()`. A plain `wp post meta update`
  writes the value but skips the `_fieldname` -> field-key row ACF needs, which
  leaves the field looking empty in the post editor if it had never been set.
- Each step that must share state runs in a single SSH session. **WP Engine
  gives every SSH connection its own container** — two connections a second
  apart report different hostnames and do not share `/tmp` — so the upload and
  the `wp media import` have to happen in one session, or the import is handed
  a path that no longer exists. Worth remembering before adding any step that
  writes a file in one command and reads it in the next.

Two other facts about this host, both measured: the gateway **re-parses the
command line and strips a level of quoting** (commands are base64-wrapped to
survive it), and **every wp-cli call costs ~30s**, nearly all of it WordPress
bootstrap — a direct `$wpdb` query came back in 28s against `wp post list`'s
30s for ~1,200 posts. Don't optimize the query; the only win available is
merging round trips. A publish takes roughly two minutes and blocks the
watcher's poll loop while it runs.

The watcher never changes a post's status. It only sets the fields you name,
on a post that already exists.

### One-time: SSH access

The watcher machine needs a key that can reach the install's SSH gateway:

```bash
ssh-copy-id -i ~/.ssh/wpengine_ed25519.pub install@install.ssh.wpengine.net
ssh install@install.ssh.wpengine.net 'wp --version'
```

On WP Engine you add the public key under **Users → SSH keys** in the portal
rather than with `ssh-copy-id`. Copy the private key to the watcher machine and
`chmod 600` it.

The watcher machine also needs the host's key in its `known_hosts`. The watcher
runs non-interactively, so there is no prompt to accept it at — a machine that
has never connected fails with `Host key verification failed` forever. Don't
reach for `StrictHostKeyChecking=no`; compare the fingerprint against a machine
that already trusts it:

```bash
ssh-keyscan -t rsa install.ssh.wpengine.net > /tmp/k && ssh-keygen -lf /tmp/k
ssh-keygen -F install.ssh.wpengine.net -l     # on the machine you already use
```

If the two fingerprints match, `cat /tmp/k >> ~/.ssh/known_hosts` on the
watcher machine.

Confirm ACF is really loaded there:

```bash
ssh install@install.ssh.wpengine.net 'cd sites/install && wp eval "var_dump(function_exists(\"update_field\"));"'
```

If that prints `bool(false)`, ACF is not active and there is nothing to write
into — the watcher will say so and stop rather than guess.

### Check it before you enable it

```bash
python doc_watcher.py --check-wordpress --title "A Doc Title You'd Share"
```

Read-only — it writes nothing. It confirms the SSH host, that `wp_path` is
really where WordPress lives, that ACF is loaded, and what each configured
field actually is. With `--title` it also dry-runs the title match, so you can
see whether a given doc would publish itself or come back to you as a question.

```
Host      install@install.ssh.wpengine.net
wp_path   /home/wpe-user/sites/install
wp-cli    WP-CLI 2.10.0
ACF       loaded

Fields, as they exist on post 4821:
  devotional_narration: file  key=field_a1  label='Devotional Narration'
  has_devotional_narration: select  key=field_b2  label='Has Devotional Narration'
      choices: {'Yes': 'Yes', 'No': 'No'}
  devotional_chapters: textarea  key=field_c3  label='Devotional Chapters'

Fix before enabling:
  • has_devotional_narration is set to 'yes' but its choices are ['Yes', 'No']
    — ACF stores the choice key, so that value won't display
```

That last line is the one worth caring about. **A Select stores its choice
key, not its label.** If the choices are `Yes`/`No` then the config has to say
`"Yes"`; writing `"yes"` stores something the field can't display, the post
editor shows an empty dropdown, and nothing anywhere complains. The check
compares what you configured against the real choice list so you find that out
now rather than after twenty devotionals.

`audio_field` and the keys of `extra_fields` take either an ACF field *name* or
a field *key* (`field_6f2a1b...`). The key is the more reliable of the two,
since names can repeat across field groups — the check prints both.

### Config: the `wordpress` block

```json
"wordpress": {
  "enabled": true,
  "ssh_host": "install@install.ssh.wpengine.net",
  "ssh_key": "~/.ssh/wpengine_ed25519",
  "wp_path": "/home/wpe-user/sites/install",
  "site_url": "https://example.com",
  "post_types": ["post"],
  "audio_field": "devotional_narration",
  "audio_field_format": "attachment_id",
  "extra_fields": {
    "has_devotional_narration": "Yes",
    "devotional_chapters": "{chapters}"
  },
  "notify": "you@gmail.com",
  "flush_cache": true,
  "dry_run": false
}
```

That example is the Deep Spirituality layout: an ACF File field for the M4A, a
Yes/No Select flipped on once the audio lands, and a textarea holding the
chapters shortcode. Run `--check-wordpress` to confirm the Select's real
choice keys before trusting `"Yes"`.

| Key | What it does |
|-----|--------------|
| `ssh_host` / `ssh_key` | How to reach the install. `ssh_key` is optional if your agent already has it. |
| `wp_path` | Directory wp-cli runs in — the WordPress root. |
| `site_url` | Only used to build view/edit links in the emails. |
| `post_types` | Which types to search for a title match. |
| `audio_field` | ACF field the audio goes into. Required. |
| `audio_field_format` | `attachment_id` (File/Audio fields) or `url` (text fields). |
| `extra_fields` | Other ACF fields to set. A value may be a literal (a Select's choice key) or use `{chapters}`, `{doc_name}`, `{doc_url}`. |
| `notify` | Who gets asked "which post?" and told when it lands. Defaults to whoever shared the doc. |
| `flush_cache` | Purge that one post from WP Engine's page cache after writing (`WpeCommon::purge_varnish_cache`), so the public page isn't stale. |
| `dry_run` | Match, report, and write nothing. Worth leaving on for the first few docs. |

`{chapters}` writes the same chapters as the app's **Copy Chapters Shortcode**
button, with curly quotes left as characters rather than `\u2019` escapes — the
way the hand-pasted ones on the site look. A value with no `{placeholder}` in
it is written literally, which is how the Yes/No flag gets flipped.

### How a publish writes

One SSH connection, one WordPress bootstrap (~45s): the M4A streams over the
connection's stdin, and a single `wp eval-file` imports it, writes the fields,
reads them back, and purges the post's cache. Why each part is the way it is:

- **Values are `wp_slash()`ed before `update_field()`.** It ends in core's
  `update_metadata()`, which *unslashes* — it expects `$_POST` data. Without
  the slash the chapters JSON silently loses every backslash (`\u2019` →
  `u2019`, `\"` ends the string). Every field is then read back and compared;
  a mismatch puts the previous values back and fails the attempt.
- **Fields are written by key.** A name only resolves through a post's
  `_fieldname` reference row, which a post never saved in the editor doesn't
  have; names are resolved through the post's field groups instead.
- **Retries don't duplicate the upload.** The attachment is tagged
  `_tts_source` = the project id, so an attempt that timed out after the import
  finished is picked up by the next one rather than uploaded again.
- **Existing values are overwritten.** New posts are made by duplicating the
  previous one, so they arrive carrying *its* narration and chapters — refusing
  to overwrite would block the normal case. The confirmation email lists every
  old value; the old audio stays in the media library.

### When no post matches

Doc titles and post titles drift — curly vs straight quotes, en dashes,
casing, `&` vs "and" — so titles are normalized on both sides before
comparing. A leading bracketed tag on the doc name is dropped:
`[QQT #96] Fully Known. Fully Loved.` matches the post `Fully Known. Fully
Loved.`. That absorbs the differences in form, but not a genuinely different
title.

When exactly one post matches, the watcher publishes on its own. Otherwise it
emails `notify` with the closest matches as tappable links and parks the doc.
Reply to that email with the post's URL (or its numeric ID) and the next poll
attaches the audio there and confirms.

Reading that reply needs the `gmail.readonly` scope, which the send-only token
doesn't have:

```bash
python gmail_auth.py --client-secrets /path/to/client_secret.json --with-replies
```

Google classes `gmail.readonly` as a **restricted** scope, so its consent
screen is sterner than `gmail.send`'s and an unverified app is capped at 100
users. That's fine for one person, but it's the part of this setup most likely
to give you trouble. If you'd rather not grant it, leave the token as-is:
everything else still works, and the watcher just emails you about the
stragglers and waits — it reads nothing and those docs stay unpublished until
you attach them by hand.

The watcher only ever reads the threads it started, and only accepts an answer
from the address it asked or the one it sends from (compared as parsed
addresses, not substrings).

### Notes

- Docs imported before you enabled the `wordpress` block have no `wp_status`
  in the state file and are deliberately left alone — enabling this doesn't
  retro-publish your back catalogue.
- A publish failure (including an SSH timeout) retries once per poll, five
  times, then emails you and gives up. The audio itself is unaffected; export it from the app by hand.
- Nothing here changes `post_status`. A draft stays a draft, and the
  confirmation email says so.

## Day-to-day use

- **Share a doc** with the service-account email (Viewer is enough) → within
  `poll_seconds` it's imported and generating. Progress shows in the app's
  Activity Log; the project appears in the project list immediately.
- Each doc is imported **once**. Editing a doc after import is ignored (the
  watcher logs a warning) — share a fresh copy to regenerate.
- Re-sharing the **same** doc does nothing: state is keyed by Drive file id,
  so the watcher sees it as already imported. Deleting the project in the app
  doesn't change that — the watcher keeps no link to the project.
- To force a re-import, remove the doc's entry from
  `~/.qwen_tts_studio/doc_watcher_state.json` **and restart the watcher**.
  The running process loads that file only at startup and writes its in-memory
  copy back on the next poll, so editing the file alone is silently undone:

  ```bash
  launchctl kickstart -k gui/$(id -u)/com.localtts.docwatcher
  ```

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
