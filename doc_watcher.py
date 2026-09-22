"""
Google Docs watcher — auto-import shared docs into TTS Studio.

Share a Google Doc with a service account's email address and this script
turns it into a generated TTS project automatically:

  1. Polls Google Drive for Google Docs the service account can see
     (everything shared with it, or one folder via "folder_id").
  2. Reads each new doc via the Docs API and converts its FIRST tab to
     Markdown (headings preserved — ## marks a chapter in the app). Docs with
     multiple tabs import only the first; if the Docs API is unavailable it
     falls back to Drive's whole-document Markdown export.
  3. POSTs it to the app's /api/projects/import endpoint, which parses it into
     paragraphs and generates audio for each one in the background.
  4. (Optional) Once generation finishes, emails the finished audio back to the
     person who shared the doc — an M4A attachment, the chapters shortcode for
     that audio, plus a reminder of the app URL to open if they want to edit
     it. Enable via the "email" config block.
  5. (Optional) Attaches that audio to the WordPress post with the same title,
     over SSH + wp-cli: uploads the M4A to the media library and sets the ACF
     fields that point the post at it. If no single post title matches, it
     emails asking which post, and publishes once you reply with the link.
     Enable via the "wordpress" config block.

Each doc is imported ONCE (tracked in a state file by doc id); edits to an
already-imported doc are logged but ignored — re-share a copy to regenerate.

Setup (one-time, see DOC_WATCHER.md for the full walkthrough):
  • Google Cloud project with the Drive API enabled
  • a service account + downloaded JSON key
  • pip install google-auth requests   (google-auth is NOT an app dependency —
    this script is run standalone, not bundled into the .app)
  • config file (default ~/.qwen_tts_studio/doc_watcher.json):
      {
        "service_account_key": "/path/to/key.json",
        "app_url": "http://127.0.0.1:8001",
        "app_token": "",              // only if the app runs in server mode
        "poll_seconds": 120,
        "folder_id": "",              // optional: watch one folder only
        "settings": {},               // optional per-import voice settings;
                                      // empty -> app's import_defaults
        "email": {                    // optional: email finished audio back
          "enabled": true,
          "oauth_token": "",          // path to gmail_auth.py's token file
                                      // (default ~/.qwen_tts_studio/gmail_token.json)
          "from_address": "you@gmail.com",  // the Gmail you consented as
          "from_name": "TTS Studio",
          "bcc": "you@gmail.com",     // get a copy of every send (oversight)
          "reply_to": "",             // optional
          "edit_url": "http://mini.tailnet:8001",  // reachable app URL
          "treatment": "clear"        // export treatment (see /api/export)
        },
        "wordpress": {                // optional: attach audio to a post
          "enabled": true,
          "ssh_host": "install@install.ssh.wpengine.net",
          "ssh_key": "~/.ssh/wpengine_ed25519",
          "wp_path": "/home/wpe-user/sites/install",  // where wp-cli runs
          "site_url": "https://example.com",          // for links in emails
          "post_types": ["post"],
          "audio_field": "audio_file",       // ACF field name or field key
          "audio_field_format": "attachment_id",  // or "url"
          "extra_fields": {                  // {chapters} {doc_name} {doc_url}
            "chapters": "{chapters}"
          },
          "notify": "you@gmail.com",   // who gets asked / told; defaults to
                                       // whoever shared the doc
          "flush_cache": true,         // purge this post from WP Engine's cache
          "dry_run": false             // true = match and report, write nothing
        }
      }

The WordPress step never changes a post's status — it only sets the fields
you name on a post that already exists. Set "dry_run": true for the first few
docs to watch what it would match before it writes anything.

Completion emails use the Gmail API over OAuth (Google's recommended path,
not an app password): run gmail_auth.py ONCE to grant consent and write the
token file, then point "email.oauth_token" at it. See DOC_WATCHER.md.

Run:  python doc_watcher.py [--once] [--config PATH]
Keep it running with launchd/cron on the machine that hosts the app.
"""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage

import requests

from wp_publisher import WordPressPublisher, WordPressError

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCredentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
    sys.exit("Missing dependency: pip install google-auth")

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_THREADS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/threads"
# gmail.readonly is only needed for the WordPress "which post?" reply loop; a
# token granted just gmail.send still sends fine and fails loudly on reads.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly"]
# drive.readonly also authorizes Docs API reads (documents.get accepts it).
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Google Docs named styles -> Markdown heading prefixes. H2 is what the app
# treats as a chapter start, so preserving these is what makes chapters work.
_HEADING_PREFIX = {
    "TITLE": "# ",
    "HEADING_1": "# ",
    "HEADING_2": "## ",
    "HEADING_3": "### ",
    "HEADING_4": "#### ",
    "HEADING_5": "##### ",
    "HEADING_6": "###### ",
}


def docs_json_to_markdown(document_tab):
    """Convert one Docs API tab body (documents.get JSON) to Markdown.

    Deliberately minimal: headings, bullets, and bold are what the app's
    parser cares about (## marks a chapter; everything else is stripped for
    TTS anyway). Tables and drawings are skipped."""
    lines = []
    for item in document_tab.get("body", {}).get("content", []):
        para = item.get("paragraph")
        if not para:
            continue
        parts = []
        for el in para.get("elements", []):
            run = el.get("textRun")
            if not run:
                continue
            text = run.get("content", "").replace("\n", "")
            if text and run.get("textStyle", {}).get("bold"):
                text = f"**{text}**"
            parts.append(text)
        text = "".join(parts).strip()
        if not text:
            continue
        prefix = _HEADING_PREFIX.get(
            para.get("paragraphStyle", {}).get("namedStyleType", ""), "")
        if not prefix and "bullet" in para:
            prefix = "- "
        lines.append(prefix + text)
    return "\n\n".join(lines)
DEFAULT_DIR = os.path.expanduser("~/.qwen_tts_studio")
DEFAULT_CONFIG = os.path.join(DEFAULT_DIR, "doc_watcher.json")
STATE_FILE = os.path.join(DEFAULT_DIR, "doc_watcher_state.json")
# OAuth token written by gmail_auth.py; used to send completion emails.
DEFAULT_GMAIL_TOKEN = os.path.join(DEFAULT_DIR, "gmail_token.json")

# Give up emailing a doc after this many failed export/send attempts (one per
# poll) so a permanently-broken recipient/SMTP config doesn't retry forever.
MAX_EMAIL_ATTEMPTS = 5
# Same idea for the WordPress hand-off: a bad SSH key or a missing ACF field
# shouldn't retry against the live site forever.
MAX_WP_ATTEMPTS = 5
# How many times we'll write back saying "that reply didn't have a post link"
# before leaving the doc alone.
MAX_REPLY_PROMPTS = 3
# Gmail rejects messages over 25 MB; stay under it and fall back to a link-only
# email when the audio is bigger.
MAX_ATTACH_BYTES = 24 * 1024 * 1024


def log(msg, level="info"):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


class Watcher:
    def __init__(self, config):
        key_path = os.path.expanduser(config.get("service_account_key", ""))
        if not key_path or not os.path.exists(key_path):
            sys.exit(f"service_account_key not found: {key_path!r} — check the config file.")
        self.creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)
        self.app_url = (config.get("app_url") or "http://127.0.0.1:8001").rstrip("/")
        self.app_token = (config.get("app_token") or "").strip()
        self.folder_id = (config.get("folder_id") or "").strip()
        self.settings = config.get("settings") or None
        self.email = config.get("email") or {}
        self.wordpress = config.get("wordpress") or {}
        self._wp_pub = None
        self.state = load_json(STATE_FILE, {})

    def _google_headers(self):
        if not self.creds.valid:
            self.creds.refresh(GoogleAuthRequest())
        return {"Authorization": f"Bearer {self.creds.token}"}

    def _app_headers(self):
        return {"Authorization": f"Bearer {self.app_token}"} if self.app_token else {}

    def list_docs(self):
        """All Google Docs visible to the service account (or one folder).

        No "sharedWithMe" clause: it misses items in Shared Drives. The
        service account owns nothing, so "everything it can see" and
        "everything shared with it" are the same set."""
        query = "mimeType='application/vnd.google-apps.document' and trashed=false"
        if self.folder_id:
            query += f" and '{self.folder_id}' in parents"
        docs, page_token = [], None
        while True:
            params = {
                "q": query,
                "fields": ("nextPageToken, files(id, name, modifiedTime, webViewLink, "
                           "owners(emailAddress,displayName), sharingUser(emailAddress,displayName))"),
                "pageSize": 100,
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "corpora": "allDrives",
            }
            if page_token:
                params["pageToken"] = page_token
            r = requests.get(f"{DRIVE_API}/files", params=params,
                             headers=self._google_headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
            docs.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return docs

    def export_markdown(self, doc_id):
        """Fallback: Drive's whole-document export (includes ALL tabs)."""
        for mime in ("text/markdown", "text/plain"):
            r = requests.get(f"{DRIVE_API}/files/{doc_id}/export",
                             params={"mimeType": mime},
                             headers=self._google_headers(), timeout=60)
            if r.status_code == 200:
                # Drive's export sends no charset, so requests would fall back
                # to ISO-8859-1 and mangle every emoji and smart quote.
                r.encoding = "utf-8"
                return r.text
        r.raise_for_status()

    def fetch_doc_markdown(self, doc):
        """Get a doc's content as Markdown, restricted to its FIRST tab.

        Drive's export endpoint can't target a tab, so read the document
        structure via the Docs API and build the Markdown ourselves. If that
        fails for any reason, fall back to the whole-doc Drive export."""
        try:
            r = requests.get(f"{DOCS_API}/documents/{doc['id']}",
                             params={"includeTabsContent": "true"},
                             headers=self._google_headers(), timeout=60)
            r.raise_for_status()
            data = r.json()
            tabs = data.get("tabs") or []
            if tabs:
                if len(tabs) > 1:
                    log(f"\"{doc['name']}\" has {len(tabs)} tabs — importing only the first.")
                body = tabs[0].get("documentTab", {})
            else:
                body = data  # old-style response: body at the top level
            markdown = docs_json_to_markdown(body)
            if markdown.strip():
                return markdown
            log(f"Docs API returned no text for \"{doc['name']}\" — falling back to Drive export.", "warn")
        except requests.RequestException as e:
            log(f"Docs API read failed for \"{doc['name']}\" ({e}) — falling back to Drive export (all tabs).", "warn")
        return self.export_markdown(doc["id"])

    def import_doc(self, doc, raw_text):
        payload = {
            "name": doc["name"],
            "raw_text": raw_text,
            "source": {
                "kind": "google_doc",
                "doc_id": doc["id"],
                "modified_time": doc.get("modifiedTime"),
                "url": doc.get("webViewLink"),
            },
        }
        if self.settings:
            payload["settings"] = self.settings
        r = requests.post(f"{self.app_url}/api/projects/import",
                          json=payload, headers=self._app_headers(), timeout=60)
        r.raise_for_status()
        return r.json()

    def poll_once(self):
        docs = self.list_docs()
        for doc in docs:
            known = self.state.get(doc["id"])
            if known:
                if doc.get("modifiedTime") != known.get("modified_time"):
                    log(f"\"{doc['name']}\" was edited after import — ignoring "
                        f"(share a copy to regenerate).", "warn")
                    known["modified_time"] = doc.get("modifiedTime")  # warn once
                    save_state(self.state)
                continue
            log(f"New doc: \"{doc['name']}\" — exporting...")
            try:
                raw_text = self.fetch_doc_markdown(doc)
                result = self.import_doc(doc, raw_text)
            except requests.RequestException as e:
                log(f"Failed to import \"{doc['name']}\": {e} — will retry next poll.", "error")
                continue
            sharer_email, sharer_name = self._pick_recipient(doc)
            email_on = bool(self.email.get("enabled"))
            self.state[doc["id"]] = {
                "name": doc["name"],
                "modified_time": doc.get("modifiedTime"),
                "project_id": result.get("id"),
                "imported_at": datetime.now().astimezone().isoformat(),
                "doc_url": doc.get("webViewLink"),
                "sharer_email": sharer_email,
                "sharer_name": sharer_name,
                # "pending" -> the email pass will send once generation is done.
                # "skipped" -> emailing off, or we couldn't identify the sharer.
                "email_status": "pending" if (email_on and sharer_email) else "skipped",
                "email_attempts": 0,
                # "pending" -> look for the matching post once audio is done.
                # "awaiting_reply" -> we asked which post; waiting on a human.
                "wp_status": "pending" if self.wordpress.get("enabled") else "skipped",
                "wp_attempts": 0,
            }
            save_state(self.state)
            log(f"Imported \"{doc['name']}\" — project {result.get('id')}, "
                f"{result.get('para_count')} paragraph(s), generation started.", "ok")
            if email_on and sharer_email:
                log(f"Will email \"{doc['name']}\" to {sharer_email} when audio finishes.")
            elif email_on:
                log(f"Emailing on, but couldn't determine who shared \"{doc['name']}\" "
                    f"— no completion email will be sent.", "warn")

        if self.wordpress.get("enabled"):
            self.check_replies()
        if self.email.get("enabled") or self.wordpress.get("enabled"):
            self.finish_completed_docs()
        return len(docs)

    def _pick_recipient(self, doc):
        """Who shared this doc with us. Prefer Drive's sharingUser (the person
        who shared it with the service account); fall back to the doc owner."""
        su = doc.get("sharingUser") or {}
        if su.get("emailAddress"):
            return su["emailAddress"], su.get("displayName")
        owners = doc.get("owners") or []
        if owners and owners[0].get("emailAddress"):
            return owners[0]["emailAddress"], owners[0].get("displayName")
        return None, None

    # ---- Completion emails -------------------------------------------------

    def finish_completed_docs(self):
        """Post-generation work for every imported doc whose audio is ready:
        email it back to whoever shared the doc, and/or attach it to the
        matching WordPress post.

        Runs every poll. A doc is left alone once both email_status and
        wp_status are terminal; a failure leaves the relevant one pending and
        it is retried next poll until the attempt cap."""
        changed = False
        for info in self.state.values():
            try:
                changed |= self._finish_doc(info)
            except Exception as e:
                log(f"Unexpected error finishing \"{info.get('name')}\": {e}", "error")
        if changed:
            save_state(self.state)

    def _finish_doc(self, info):
        """One doc's post-generation work. Returns True if state changed."""
        changed = False
        need_email = info.get("email_status") == "pending"
        need_wp = info.get("wp_status") in ("pending", "awaiting_reply")
        if not (need_email or need_wp):
            return False

        project_id = info.get("project_id")
        if not project_id:
            info["email_status"] = "skipped"
            info["wp_status"] = "skipped"
            return True
        if need_email and not info.get("sharer_email"):
            info["email_status"] = "skipped"
            need_email, changed = False, True
            if not need_wp:
                return changed

        try:
            r = requests.get(f"{self.app_url}/api/projects/{project_id}",
                             headers=self._app_headers(), timeout=30)
            if r.status_code == 404:
                log(f"Project for \"{info.get('name')}\" is gone — nothing to deliver.", "warn")
                if need_email:
                    info["email_status"] = "skipped"
                if need_wp:
                    info["wp_status"] = "skipped"
                return True
            r.raise_for_status()
            project = r.json()
        except requests.RequestException as e:
            log(f"Couldn't check status of \"{info.get('name')}\" ({e}) — will retry.", "warn")
            return changed

        status = str(project.get("import_status") or "")
        if not status.startswith("done"):
            return changed  # still generating (or pending) — check again next poll

        # Ordered list of stored audio ids (active take per paragraph).
        file_ids = [
            f"{p['id']}-t{p['activeTake']}"
            for p in project.get("paragraphs", [])
            if p.get("hasAudio") and p.get("activeTake")
        ]
        had_failures = status != "done"  # "done (N of M failed)"

        # A doc parked on "which post is this?" doesn't need the audio
        # re-encoded on every poll — only once there's somewhere to put it.
        wp_wants_audio = need_wp and (info.get("wp_status") == "pending"
                                      or info.get("wp_post_id"))
        m4a_bytes = None
        if file_ids and (need_email or wp_wants_audio):
            try:
                m4a_bytes = self.export_m4a(project_id, file_ids)
            except requests.RequestException as e:
                if need_email:
                    changed |= self._note_attempt(info, "email", f"export failed: {e}")
                if need_wp:
                    changed |= self._note_attempt(info, "wp", f"export failed: {e}")
                return changed

        if need_email:
            changed |= self._email_doc(info, project_id, m4a_bytes, file_ids, had_failures)
        if need_wp:
            changed |= self._wordpress_doc(info, project_id, m4a_bytes)
        return changed

    def _email_doc(self, info, project_id, m4a_bytes, file_ids, had_failures):
        """Send the finished audio back to whoever shared the doc."""
        to_email = info.get("sharer_email")
        attach = m4a_bytes
        if attach and len(attach) > MAX_ATTACH_BYTES:
            log(f"\"{info.get('name')}\" audio is {len(attach) // (1024 * 1024)} MB "
                f"— too big to attach; sending a link-only email.", "warn")
            attach = None

        # Chapter markers for the finished audio (the same JSON the app's
        # "Copy Chapters Shortcode" button produces). Best-effort: a failure
        # here shouldn't hold back the audio.
        chapters_shortcode = None
        if file_ids:
            chapters_shortcode = self.fetch_chapters_shortcode(project_id, info.get("name"))

        try:
            self.send_completion_email(
                to_email=to_email,
                to_name=info.get("sharer_name"),
                doc_name=info.get("name") or "your document",
                m4a_bytes=attach,
                had_failures=had_failures,
                have_audio=bool(file_ids),
                chapters_shortcode=chapters_shortcode,
            )
        except Exception as e:
            return self._note_attempt(info, "email", f"send failed: {e}")

        info["email_status"] = "sent"
        info["emailed_at"] = datetime.now().astimezone().isoformat()
        log(f"Emailed finished audio for \"{info.get('name')}\" to {to_email}.", "ok")
        return True

    def _note_attempt(self, info, kind, reason):
        """Record a failed delivery attempt; give up after the cap. Returns
        True (state changed) so callers can OR it into their changed flag."""
        cap = MAX_EMAIL_ATTEMPTS if kind == "email" else MAX_WP_ATTEMPTS
        attempts = int(info.get(f"{kind}_attempts", 0)) + 1
        info[f"{kind}_attempts"] = attempts
        label = "emailing" if kind == "email" else "publishing"
        if attempts >= cap:
            info[f"{kind}_status"] = "failed"
            log(f"Giving up {label} \"{info.get('name')}\" after {attempts} "
                f"attempts — {reason}", "error")
            self._notify_failure(info, kind, reason)
        else:
            log(f"{label.capitalize()} attempt {attempts}/{cap} for "
                f"\"{info.get('name')}\" — {reason} — will retry.", "warn")
        return True

    # ---- WordPress hand-off ------------------------------------------------

    def _wp_publisher(self):
        if self._wp_pub is None:
            self._wp_pub = WordPressPublisher(self.wordpress, logger=log)
        return self._wp_pub

    def _wordpress_doc(self, info, project_id, m4a_bytes):
        """Attach this doc's finished audio to its WordPress post.

        Three outcomes: "published" (exactly one post title matched, or a
        human has since told us which post it is), "awaiting_reply" (we
        emailed to ask), or a retry/give-up via _note_attempt."""
        name = info.get("name") or "document"
        try:
            pub = self._wp_publisher()
        except WordPressError as e:
            return self._note_attempt(info, "wp", f"config problem: {e}")

        post_id = info.get("wp_post_id")
        if not post_id:
            if info.get("wp_status") == "awaiting_reply":
                return False  # still waiting on a human to name the post
            try:
                post, candidates = pub.match_title(name)
            except WordPressError as e:
                return self._note_attempt(info, "wp", f"title lookup failed: {e}")
            if not post:
                return self._ask_which_post(info, candidates)
            post_id = post["ID"]
            info["wp_post_id"] = post_id
            info["wp_post_title"] = post.get("post_title")
            log(f"\"{name}\" matched post {post_id} (\"{post.get('post_title')}\").")

        if not m4a_bytes:
            return self._note_attempt(info, "wp", "no audio was generated to attach")

        # The uploaded file keeps its basename all the way into the media
        # library and the public audio URL, so write it under the doc's name
        # in a scratch directory rather than as a tempfile-random one.
        tmpdir = None
        try:
            fields = self._wp_extra_fields(info, project_id)
            tmpdir = tempfile.mkdtemp(prefix="tts-wp-")
            local = os.path.join(tmpdir, _audio_filename(name))
            with open(local, "wb") as f:
                f.write(m4a_bytes)
            # project_id tags the upload, so a retry after a timeout that
            # actually succeeded reuses it rather than uploading it twice.
            result = pub.publish(post_id, local, extra_fields=fields,
                                 media_title=name, source=project_id)
        except (WordPressError, OSError) as e:
            return self._note_attempt(info, "wp", f"publish failed: {e}")
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

        # A dry run is recorded as its own terminal state, not as "published".
        # Calling it published would be a lie the state file then makes
        # permanent: turning dry_run off wouldn't go back and publish it.
        dry = bool(self.wordpress.get("dry_run"))
        info["wp_status"] = "dry_run" if dry else "published"
        info["wp_permalink"] = result.get("permalink")
        info["wp_published_at"] = datetime.now().astimezone().isoformat()
        verb = "Would have attached" if dry else "Attached"
        log(f"{verb} audio for \"{name}\" to post {post_id} — "
            f"{result.get('permalink')}", "ok")
        self._notify_published(info, result, dry_run=dry)
        return True

    def _wp_extra_fields(self, info, project_id):
        """Build the non-audio ACF values from the config's templates.

        wordpress.extra_fields maps an ACF field name (or field key) to a
        string that may reference {chapters}, {doc_name} or {doc_url}."""
        templates = self.wordpress.get("extra_fields") or {}
        if not templates:
            return {}
        values = {
            "doc_name": info.get("name") or "",
            "doc_url": info.get("doc_url") or "",
            "chapters": "",
        }
        # Only pay for the chapter scan if a field actually asks for it.
        if any("{chapters}" in str(t) for t in templates.values()):
            values["chapters"] = self.fetch_chapters_shortcode(
                project_id, info.get("name")) or ""
        fields = {}
        for field, template in templates.items():
            try:
                fields[field] = str(template).format(**values)
            except (KeyError, IndexError) as e:
                log(f"wordpress.extra_fields[\"{field}\"] references {e} — "
                    f"writing the template literally.", "warn")
                fields[field] = str(template)
        return fields

    def _wp_notify_address(self, info):
        return (self.wordpress.get("notify") or info.get("sharer_email") or "").strip()

    def _reply_senders(self, info):
        """Addresses whose reply may name the post.

        Defaults to whoever we asked PLUS the account we send from. The
        question is asked at one address but the thread lives in the sending
        mailbox, so replying from either is the natural thing to do — and a
        reply we refuse looks exactly like one that never arrived. Override
        with wordpress.reply_from (a string or a list)."""
        configured = self.wordpress.get("reply_from")
        if configured:
            addrs = [configured] if isinstance(configured, str) else list(configured)
        else:
            addrs = [self._wp_notify_address(info),
                     self.email.get("from_address") or ""]
        return [a.strip().lower() for a in addrs if a and a.strip()]

    def _post_links(self, post_id):
        """View and edit links for a post, built from the site URL so they work
        without asking wp-cli for a permalink."""
        site = (self.wordpress.get("site_url") or "").rstrip("/")
        if not site:
            return None, None
        return (f"{site}/?p={post_id}",
                f"{site}/wp-admin/post.php?post={post_id}&action=edit")

    def _ask_which_post(self, info, candidates):
        """No single post title matched — email and ask which post this is.

        The reply is picked up by check_replies() on a later poll."""
        name = info.get("name") or "document"
        to_email = self._wp_notify_address(info)
        if not to_email:
            log(f"No post matched \"{name}\" and there's no wordpress.notify "
                f"address to ask — leaving it unpublished.", "warn")
            info["wp_status"] = "skipped"
            return True

        lines = [
            f"The audio for \"{name}\" is ready, but no WordPress post has that "
            f"exact title, so I haven't attached it to anything yet.",
            "",
            "Reply to this email with the post's URL and I'll attach it there.",
        ]
        if candidates:
            lines += ["", "Closest matches — if it's one of these, reply with its link:"]
            for post in candidates:
                view, _ = self._post_links(post["ID"])
                status = post.get("post_status", "")
                suffix = f" [{status}]" if status and status != "publish" else ""
                lines.append(f"  • {post.get('post_title')}{suffix}")
                lines.append(f"    {view or 'post id ' + str(post['ID'])}")
        else:
            lines += ["", "Nothing on the site came close to that title."]
        if info.get("doc_url"):
            lines += ["", f"The doc: {info['doc_url']}"]
        lines += ["", "— TTS Studio (automated message)"]

        try:
            sent = self.send_mail(to_email, f"Which post is \"{name}\"?", "\n".join(lines))
        except Exception as e:
            return self._note_attempt(info, "wp", f"couldn't ask which post: {e}")

        info["wp_status"] = "awaiting_reply"
        info["wp_thread_id"] = sent.get("threadId")
        info["wp_ask_message_id"] = sent.get("id")
        info["wp_asked_at"] = datetime.now().astimezone().isoformat()
        info["wp_candidates"] = [
            {"id": p["ID"], "title": p.get("post_title")} for p in candidates
        ]
        log(f"No post matched \"{name}\" — asked {to_email} which post it is.", "warn")
        return True

    def _notify_published(self, info, result, dry_run=False):
        """Tell the operator the audio landed, with links to check it."""
        to_email = self._wp_notify_address(info)
        if not to_email:
            return
        name = info.get("name") or "document"
        view = result.get("permalink") or ""
        edit = result.get("edit_link") or ""
        lines = [
            (f"DRY RUN — nothing was written. The audio for \"{name}\" WOULD "
             f"have been attached to \"{result.get('post_title')}\"."
             if dry_run else
             f"The audio for \"{name}\" is now attached to "
             f"\"{result.get('post_title')}\"."),
            "",
            f"  View: {view}",
            f"  Edit: {edit}",
        ]
        if result.get("audio_url"):
            lines.append(f"  Audio: {result['audio_url']}")
        lines += [
            "",
            "Fields that would be set:" if dry_run else "Fields set:",
        ]
        for field, change in (result.get("applied") or {}).items():
            before = change.get("before")
            was = "was empty" if before in (None, "", False) else f"was {_clip(before)}"
            lines.append(f"  • {field} -> {_clip(change.get('after'))} ({was})")
        if not dry_run:
            lines += ["", "Any audio it replaced is still in the media library — "
                          "pick it again in the editor to undo."]
        if not dry_run and result.get("cache") not in (None, "purged", "skipped"):
            lines += ["", f"Cache: {result['cache']} — the public page may be "
                          f"stale for a while."]
        if result.get("post_status") != "publish":
            lines += ["", f"Heads up: that post is still {result.get('post_status')} "
                          f"— I didn't change its status."]
        lines += ["", "— TTS Studio (automated message)"]
        subject = ("[dry run] Would attach audio: " if dry_run else "Audio attached: ")
        try:
            self.send_mail(to_email, subject + str(result.get("post_title")),
                           "\n".join(lines))
        except Exception as e:
            log(f"Published \"{name}\" but couldn't send the confirmation email: {e}", "warn")

    def _notify_failure(self, info, kind, reason):
        """Something gave up for good — say so, rather than only logging it."""
        if kind != "wp":
            return  # if email is what's broken, emailing about it won't help
        to_email = self._wp_notify_address(info)
        if not to_email:
            return
        name = info.get("name") or "document"
        try:
            self.send_mail(
                to_email,
                f"Couldn't attach audio: {name}",
                "\n".join([
                    f"I gave up attaching the audio for \"{name}\" to WordPress "
                    f"after {MAX_WP_ATTEMPTS} attempts.",
                    "",
                    f"Last error: {reason}",
                    "",
                    "The audio itself is fine — open TTS Studio and export it by hand.",
                    "",
                    "— TTS Studio (automated message)",
                ]))
        except Exception as e:
            log(f"Couldn't send the failure notice for \"{name}\": {e}", "warn")

    # ---- Reading the reply -------------------------------------------------

    def check_replies(self):
        """Look for answers to the \"which post is this?\" emails.

        Each ask recorded its Gmail thread id, so this reads those threads
        only — never the rest of the mailbox. A reply that names a post moves
        the doc back to "pending" and the next completion pass publishes it."""
        waiting = [i for i in self.state.values()
                   if i.get("wp_status") == "awaiting_reply" and i.get("wp_thread_id")]
        if not waiting:
            return
        changed = False
        for info in waiting:
            try:
                changed |= self._check_reply(info)
            except PermissionError as e:
                log(str(e), "error")
                return  # the token is short a scope; the rest will fail the same way
            except Exception as e:
                log(f"Couldn't check for a reply about \"{info.get('name')}\": {e}", "warn")
        if changed:
            save_state(self.state)

    def _check_reply(self, info):
        """One doc's thread. Returns True if state changed."""
        name = info.get("name") or "document"
        expect_from = self._reply_senders(info)
        creds = self._gmail_credentials()
        r = requests.get(
            f"{GMAIL_THREADS_URL}/{info['wp_thread_id']}",
            params={"format": "full"},
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=60,
        )
        if r.status_code in (401, 403):
            raise PermissionError(
                "Gmail refused to read the reply thread — the saved token is "
                "authorized to send but not to read. Re-run gmail_auth.py to "
                "add the gmail.readonly scope, then restart the watcher.")
        r.raise_for_status()

        for message in r.json().get("messages", []):
            # Skip our own question. Gmail labels what this account sent as
            # SENT, which holds even when from_address isn't configured and
            # the ask therefore comes from the same address we're asking —
            # without this, the watcher reads its own candidate links back as
            # the answer and publishes to the first one.
            if "SENT" in (message.get("labelIds") or []):
                continue
            if message.get("id") == info.get("wp_ask_message_id"):
                continue
            headers = {h["name"].lower(): h["value"]
                       for h in message.get("payload", {}).get("headers", [])}
            sender = (headers.get("from") or "").lower()
            if expect_from and not any(e in sender for e in expect_from):
                log(f"Ignoring a reply about \"{name}\" from {headers.get('from')!r} "
                    f"— only {', '.join(expect_from)} can name the post.", "warn")
                continue
            if message.get("id") in (info.get("wp_seen_replies") or []):
                continue
            info.setdefault("wp_seen_replies", []).append(message.get("id"))
            return self._apply_reply(info, _message_text(message.get("payload", {})))
        return False

    def _apply_reply(self, info, body):
        """Turn the human's reply into a post id, or ask again."""
        name = info.get("name") or "document"
        target = _first_post_reference(body)
        if not target:
            return self._reply_problem(
                info, "I couldn't find a post link in that reply.")
        try:
            post = self._wp_publisher().find_post_by_url(target)
        except WordPressError as e:
            return self._reply_problem(info, f"I couldn't open {target} — {e}.")

        info["wp_post_id"] = post["ID"]
        info["wp_post_title"] = post.get("post_title")
        info["wp_status"] = "pending"   # the next completion pass publishes it
        info["wp_attempts"] = 0
        log(f"Reply for \"{name}\" points at post {post['ID']} "
            f"(\"{post.get('post_title')}\") — publishing.", "ok")
        return True

    def _reply_problem(self, info, message):
        """Tell them the reply didn't work, but don't get into a loop about it."""
        name = info.get("name") or "document"
        tries = int(info.get("wp_reply_errors", 0)) + 1
        info["wp_reply_errors"] = tries
        log(f"Reply about \"{name}\": {message}", "warn")
        if tries > MAX_REPLY_PROMPTS:
            info["wp_status"] = "failed"
            log(f"Giving up on \"{name}\" after {tries} unusable replies — "
                f"attach the audio by hand.", "error")
            return True
        to_email = self._wp_notify_address(info)
        if to_email:
            try:
                self.send_mail(
                    to_email, f"Still need the post for \"{name}\"",
                    "\n".join([
                        message,
                        "",
                        "Reply again with just the post's URL (or its numeric ID) "
                        "and I'll attach the audio there.",
                        "",
                        "— TTS Studio (automated message)",
                    ]),
                    thread_id=info.get("wp_thread_id"))
            except Exception as e:
                log(f"Couldn't ask again about \"{name}\": {e}", "warn")
        return True

    def fetch_chapters_shortcode(self, project_id, doc_name=None):
        """Ask the app for the project's chapters shortcode (the JSON array of
        {title, start} the UI copies). Returns the JSON string, or None if
        there are no chapters or the app couldn't build it."""
        try:
            r = requests.get(f"{self.app_url}/api/projects/{project_id}/chapters",
                             headers=self._app_headers(), timeout=120)
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log(f"Couldn't build chapters shortcode for \"{doc_name}\" ({e}) "
                f"— emailing the audio without it.", "warn")
            return None
        if not data.get("chapters"):
            return None
        return data.get("shortcode") or json.dumps(data["chapters"], indent=2)

    def export_m4a(self, project_id, file_ids):
        """Ask the app to merge the project's segments and encode to M4A."""
        data = {
            "project_id": project_id,
            "file_ids": json.dumps(file_ids),
            "output_format": "m4a",
            "treatment_type": self.email.get("treatment", "clear"),
        }
        r = requests.post(f"{self.app_url}/api/export", data=data,
                          headers=self._app_headers(), timeout=600)
        r.raise_for_status()
        return r.content

    def _gmail_credentials(self):
        """Load (and refresh) the OAuth user credentials written by gmail_auth.py.
        Cached on the instance; google-auth refreshes the access token in memory
        from the stored refresh token, so no re-consent is needed."""
        creds = getattr(self, "_gmail_creds", None)
        if creds is None:
            token_path = os.path.expanduser(self.email.get("oauth_token") or DEFAULT_GMAIL_TOKEN)
            if not os.path.exists(token_path):
                raise RuntimeError(
                    f"Gmail OAuth token not found: {token_path} — run "
                    f"gmail_auth.py once to authorize sending (see DOC_WATCHER.md).")
            creds = UserCredentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
            self._gmail_creds = creds
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        return creds

    def send_completion_email(self, to_email, to_name, doc_name, m4a_bytes,
                              had_failures=False, have_audio=True,
                              chapters_shortcode=None):
        cfg = self.email
        edit_url = (cfg.get("edit_url") or self.app_url).rstrip("/")
        first = (to_name or "").split(" ")[0].strip()
        greeting = f"Hi {first}," if first else "Hi,"

        lines = [greeting, ""]
        if m4a_bytes:
            lines.append(f'The audio for "{doc_name}" is ready — it\'s attached as an M4A.')
        elif have_audio:
            lines.append(f'The audio for "{doc_name}" is ready. It was too large to '
                         f'attach here, so open TTS Studio to download it.')
        else:
            lines.append(f'The project "{doc_name}" finished processing, but no audio '
                         f'was generated. Open TTS Studio to take a look.')
        if had_failures:
            lines.append("")
            lines.append("Note: some paragraphs didn't generate — you may want to "
                         "review and regenerate them in the app.")
        if chapters_shortcode:
            lines += [
                "",
                "Chapters shortcode (start times in seconds, matching the audio above):",
                "",
                chapters_shortcode,
            ]
        lines += [
            "",
            "Want to make edits or re-export? Open TTS Studio here:",
            f"  {edit_url}",
            f'Then open the project named "{doc_name}".',
            "",
            "— TTS Studio (automated message)",
        ]

        attachment = None
        if m4a_bytes:
            fname = re.sub(r'[^\w\-. ]', "_", doc_name).strip() or "audio"
            attachment = (m4a_bytes, f"{fname}.m4a")
        return self.send_mail(to_email, f"Your audio is ready: {doc_name}",
                              "\n".join(lines), attachment=attachment)

    def send_mail(self, to_email, subject, body, attachment=None, thread_id=None):
        """Send one plain-text email through the Gmail API.

        Returns the API response — its "threadId" is what lets us watch for a
        reply later, which is how the "which post?" hand-off works."""
        cfg = self.email
        msg = EmailMessage()
        from_addr = (cfg.get("from_address") or "").strip()
        from_name = cfg.get("from_name") or "TTS Studio"
        if from_addr:
            msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
        msg["To"] = to_email
        # Gmail delivers to a Bcc header and strips it from the copy other
        # recipients receive, so the oversight copy stays hidden. Skip it when
        # it would just send the same person the message twice.
        bcc = (cfg.get("bcc") or "").strip()
        if bcc and bcc.lower() != to_email.lower():
            msg["Bcc"] = bcc
        if cfg.get("reply_to"):
            msg["Reply-To"] = cfg["reply_to"]
        msg["Subject"] = subject
        msg.set_content(body)
        if attachment:
            data, filename = attachment
            msg.add_attachment(data, maintype="audio", subtype="mp4", filename=filename)

        creds = self._gmail_credentials()
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread_id:
            payload["threadId"] = thread_id
        r = requests.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {creds.token}"},
            json=payload,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()


def _clip(value, limit=100):
    """A field value short enough for an email line (chapters JSON isn't)."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _audio_filename(doc_name):
    """A WordPress-friendly filename for a doc's audio.

    This becomes the media library filename and the public URL, so it wants to
    look like the devotional, not like a temp file."""
    slug = unicodedata.normalize("NFKD", doc_name or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^\w\s-]", "", slug).strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return (slug[:80] or "devotional") + ".m4a"


def _message_text(payload):
    """Plain-text body of a Gmail message, minus the quoted reply.

    Phones quote the whole original underneath the reply, which would hand us
    back the links from our own question — so everything from the quote marker
    down is dropped."""
    text = _walk_for_text(payload) or ""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            break
        if re.match(r"^On .+ wrote:$", stripped) or stripped in ("--", "___"):
            break
        if stripped.startswith("From:") and lines:
            break  # forwarded-header style quoting
        lines.append(line)
    return "\n".join(lines).strip()


def _walk_for_text(part):
    if part.get("mimeType") == "text/plain":
        data = part.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
    for child in part.get("parts", []) or []:
        found = _walk_for_text(child)
        if found:
            return found
    return None


def _first_post_reference(body):
    """The first URL in the reply, or a bare post id if that's all they sent."""
    match = re.search(r"https?://[^\s<>\"\')]+", body or "")
    if match:
        return match.group(0).rstrip(".,;:)]}>")
    bare = (body or "").strip()
    return bare if re.fullmatch(r"\d{1,9}", bare) else None



def check_wordpress(config, title=None, post_id=None):
    """Confirm the "wordpress" block really works, before a doc depends on it.

    Checks the things that are wrong in practice: the SSH host, the wp_path
    (wp-cli is the only thing that knows where WordPress actually lives),
    whether ACF is loaded, and what the configured fields REALLY are — a
    Select stores its choice key, so a field whose choices are {yes: Yes} must
    be written "yes", not "Yes". Read-only; writes nothing."""
    cfg = config.get("wordpress") or {}
    if not cfg:
        sys.exit("No \"wordpress\" block in the config — nothing to check.")
    pub = WordPressPublisher(cfg, logger=log)

    print(f"Host      {pub.host}")
    print(f"wp_path   {pub.wp_path or '(none — running wp in the login directory)'}")
    try:
        info = pub.preflight()
    except WordPressError as e:
        print(f"\nFAILED    {e}\n")
        if "Host key verification" in str(e):
            # The watcher runs non-interactively, so there is no prompt to
            # accept the key at — a fresh machine fails here every time.
            host = pub.host.split("@")[-1]
            print(f"This machine has never connected to {host}, and the watcher")
            print("can't answer the trust prompt. Compare the fingerprint against")
            print("a machine that already trusts it before adding it:")
            print(f"\n  ssh-keyscan -t rsa {host} > /tmp/k && ssh-keygen -lf /tmp/k")
            print(f"  ssh-keygen -F {host} -l          # run on the known-good machine")
            print("\nIf they match, append /tmp/k to ~/.ssh/known_hosts.")
        else:
            print("If that's a wp_path problem, ssh in and run `ls -d ~/sites/*/` to")
            print("see the install directories; if it's a key problem, check that the")
            print("public half is registered on the host and the private half is")
            print("readable here (chmod 600).")
        return 1
    print(f"wp-cli    {info['wp_version']}")
    print(f"ACF       {'loaded' if info['acf'] else 'NOT LOADED — nothing to write into'}")
    if info.get("installs"):
        print(f"Installs  {' '.join(info['installs'].split())}")
    if not info["acf"]:
        return 1

    if post_id is None:
        post_id = pub.newest_post_id()
    if not post_id:
        print(f"\nNo {'/'.join(pub.post_types)} posts found to inspect fields against.")
        return 1

    selectors = [cfg.get("audio_field")] + list((cfg.get("extra_fields") or {}).keys())
    selectors = [s for s in selectors if s]
    print(f"\nFields, as they exist on post {post_id}:")
    try:
        described = pub.describe_fields(selectors, post_id)
    except WordPressError as e:
        print(f"  couldn't read them: {e}")
        return 1

    problems = []
    wanted = dict((cfg.get("extra_fields") or {}))
    for selector, field in described["fields"].items():
        if not field.get("found"):
            print(f"  {selector}: NOT FOUND on this post's field groups")
            problems.append(f"{selector} doesn't exist (check the spelling, or "
                            f"use its field key)")
            continue
        print(f"  {selector}: {field['type']}  key={field['key']}  "
              f"label={field['label']!r}")
        if field.get("choices"):
            print(f"      choices: {field['choices']}")
            value = wanted.get(selector)
            if value and "{" not in str(value) and str(value) not in field["choices"]:
                problems.append(
                    f"{selector} is set to {value!r} but its choices are "
                    f"{list(field['choices'])} — ACF stores the choice key, so "
                    f"that value won't display")
        if selector == cfg.get("audio_field"):
            fmt = cfg.get("audio_field_format") or "attachment_id"
            if field["type"] in ("file", "image", "audio") and fmt != "attachment_id":
                problems.append(f"{selector} is a {field['type']} field — set "
                                f"audio_field_format to \"attachment_id\"")
            if field["type"] in ("text", "textarea", "url") and fmt != "url":
                problems.append(f"{selector} is a {field['type']} field — set "
                                f"audio_field_format to \"url\"")

    if title:
        print(f"\nTitle match for {title!r}:")
        match, candidates = pub.match_title(title)
        if match:
            print(f"  would publish to post {match['ID']} — "
                  f"{match['post_title']!r} [{match.get('post_status')}]")
        elif candidates:
            print("  no exact match; would email you these to choose from:")
            for post in candidates:
                print(f"    {post['ID']}  {post['post_title']!r} "
                      f"[{post.get('post_status')}]")
        else:
            print("  nothing close; would email you to ask for the link")

    if problems:
        print("\nFix before enabling:")
        for problem in problems:
            print(f"  • {problem}")
        return 1
    print("\nAll good — safe to set \"enabled\": true "
          "(leave \"dry_run\": true for the first doc).")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Watch Google Drive for shared docs and import them into TTS Studio.")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file path (default {DEFAULT_CONFIG})")
    ap.add_argument("--once", action="store_true", help="poll a single time and exit (for cron)")
    ap.add_argument("--check-wordpress", action="store_true",
                    help="check the \"wordpress\" block against the live install "
                         "(read-only: confirms wp_path, ACF and the field types) and exit")
    ap.add_argument("--title", help="with --check-wordpress: show which post this "
                                    "doc title would match")
    ap.add_argument("--post", type=int, help="with --check-wordpress: inspect the fields "
                                             "on this post id instead of the newest")
    args = ap.parse_args()

    config = load_json(args.config, None)
    if config is None:
        sys.exit(f"Config file not found or invalid: {args.config}\nSee the header of this script for the expected format.")

    if args.check_wordpress:
        sys.exit(check_wordpress(config, title=args.title, post_id=args.post))

    watcher = Watcher(config)
    poll_seconds = max(30, int(config.get("poll_seconds", 120)))

    if args.once:
        watcher.poll_once()
        return

    # The config is read once, at startup. Say what mode we came up in, so
    # "did my restart actually take?" is answerable from the log instead of by
    # comparing process ids.
    if watcher.wordpress.get("enabled"):
        site = watcher.wordpress.get("site_url") or "the site"
        log("WordPress hand-off ON — DRY RUN, nothing will be written"
            if watcher.wordpress.get("dry_run")
            else f"WordPress hand-off ON — writing live to {site}")
    elif watcher.wordpress:
        log("WordPress hand-off is configured but disabled.")
    log(f"Watching Drive for new docs every {poll_seconds}s — app at {watcher.app_url}")
    while True:
        try:
            watcher.poll_once()
        except Exception as e:
            log(f"Poll failed: {e}", "error")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
