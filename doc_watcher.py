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
     person who shared the doc — an M4A attachment plus a reminder of the app
     URL to open if they want to edit it. Enable via the "email" config block.

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
        }
      }

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
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import requests

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCredentials
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
    sys.exit("Missing dependency: pip install google-auth")

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SEND_SCOPE = ["https://www.googleapis.com/auth/gmail.send"]
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
            }
            save_state(self.state)
            log(f"Imported \"{doc['name']}\" — project {result.get('id')}, "
                f"{result.get('para_count')} paragraph(s), generation started.", "ok")
            if email_on and sharer_email:
                log(f"Will email \"{doc['name']}\" to {sharer_email} when audio finishes.")
            elif email_on:
                log(f"Emailing on, but couldn't determine who shared \"{doc['name']}\" "
                    f"— no completion email will be sent.", "warn")

        if self.email.get("enabled"):
            self.email_completed_docs()
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

    def email_completed_docs(self):
        """For each imported doc whose audio has finished generating, export the
        merged M4A and email it back to the person who shared the doc.

        Runs every poll. Only touches state entries flagged email_status ==
        "pending"; a send failure leaves the entry pending (retried next poll)
        until MAX_EMAIL_ATTEMPTS, then it's marked "failed"."""
        changed = False
        for doc_id, info in self.state.items():
            if info.get("email_status") != "pending":
                continue
            project_id = info.get("project_id")
            to_email = info.get("sharer_email")
            if not project_id or not to_email:
                info["email_status"] = "skipped"
                changed = True
                continue

            try:
                r = requests.get(f"{self.app_url}/api/projects/{project_id}",
                                 headers=self._app_headers(), timeout=30)
                if r.status_code == 404:
                    log(f"Project for \"{info.get('name')}\" is gone — not emailing.", "warn")
                    info["email_status"] = "skipped"
                    changed = True
                    continue
                r.raise_for_status()
                project = r.json()
            except requests.RequestException as e:
                log(f"Couldn't check status of \"{info.get('name')}\" ({e}) — will retry.", "warn")
                continue

            status = str(project.get("import_status") or "")
            if not status.startswith("done"):
                continue  # still generating (or pending) — check again next poll

            # Build the ordered list of stored audio ids (active take per paragraph).
            file_ids = [
                f"{p['id']}-t{p['activeTake']}"
                for p in project.get("paragraphs", [])
                if p.get("hasAudio") and p.get("activeTake")
            ]
            had_failures = status != "done"  # "done (N of M failed)"

            m4a_bytes = None
            try:
                if file_ids:
                    m4a_bytes = self.export_m4a(project_id, file_ids)
                    if len(m4a_bytes) > MAX_ATTACH_BYTES:
                        log(f"\"{info.get('name')}\" audio is "
                            f"{len(m4a_bytes) // (1024 * 1024)} MB — too big to attach; "
                            f"sending a link-only email.", "warn")
                        m4a_bytes = None
            except requests.RequestException as e:
                changed |= self._note_email_attempt(info, f"export failed: {e}")
                continue

            try:
                self.send_completion_email(
                    to_email=to_email,
                    to_name=info.get("sharer_name"),
                    doc_name=info.get("name") or "your document",
                    m4a_bytes=m4a_bytes,
                    had_failures=had_failures,
                    have_audio=bool(file_ids),
                )
            except Exception as e:
                changed |= self._note_email_attempt(info, f"send failed: {e}")
                continue

            info["email_status"] = "sent"
            info["emailed_at"] = datetime.now().astimezone().isoformat()
            changed = True
            log(f"Emailed finished audio for \"{info.get('name')}\" to {to_email}.", "ok")

        if changed:
            save_state(self.state)

    def _note_email_attempt(self, info, reason):
        """Record a failed export/send attempt; give up after the cap. Returns
        True (state changed) so callers can OR it into their changed flag."""
        attempts = int(info.get("email_attempts", 0)) + 1
        info["email_attempts"] = attempts
        if attempts >= MAX_EMAIL_ATTEMPTS:
            info["email_status"] = "failed"
            log(f"Giving up emailing \"{info.get('name')}\" after {attempts} "
                f"attempts — {reason}", "error")
        else:
            log(f"Email attempt {attempts}/{MAX_EMAIL_ATTEMPTS} for "
                f"\"{info.get('name')}\" — {reason} — will retry.", "warn")
        return True

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
            creds = UserCredentials.from_authorized_user_file(token_path, GMAIL_SEND_SCOPE)
            self._gmail_creds = creds
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        return creds

    def send_completion_email(self, to_email, to_name, doc_name, m4a_bytes,
                              had_failures=False, have_audio=True):
        cfg = self.email
        from_addr = (cfg.get("from_address") or "").strip()
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
        lines += [
            "",
            "Want to make edits or re-export? Open TTS Studio here:",
            f"  {edit_url}",
            f'Then open the project named "{doc_name}".',
            "",
            "— TTS Studio (automated message)",
        ]

        msg = EmailMessage()
        from_name = cfg.get("from_name") or "TTS Studio"
        if from_addr:
            msg["From"] = f"{from_name} <{from_addr}>" if from_name else from_addr
        msg["To"] = to_email
        # Gmail delivers to a Bcc header and strips it from the copy other
        # recipients receive, so the oversight copy stays hidden.
        bcc = (cfg.get("bcc") or "").strip()
        if bcc:
            msg["Bcc"] = bcc
        if cfg.get("reply_to"):
            msg["Reply-To"] = cfg["reply_to"]
        msg["Subject"] = f"Your audio is ready: {doc_name}"
        msg.set_content("\n".join(lines))

        if m4a_bytes:
            fname = re.sub(r'[^\w\-. ]', "_", doc_name).strip() or "audio"
            msg.add_attachment(m4a_bytes, maintype="audio", subtype="mp4",
                               filename=f"{fname}.m4a")

        creds = self._gmail_credentials()
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        r = requests.post(
            GMAIL_SEND_URL,
            headers={"Authorization": f"Bearer {creds.token}"},
            json={"raw": raw},
            timeout=120,
        )
        r.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description="Watch Google Drive for shared docs and import them into TTS Studio.")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file path (default {DEFAULT_CONFIG})")
    ap.add_argument("--once", action="store_true", help="poll a single time and exit (for cron)")
    args = ap.parse_args()

    config = load_json(args.config, None)
    if config is None:
        sys.exit(f"Config file not found or invalid: {args.config}\nSee the header of this script for the expected format.")

    watcher = Watcher(config)
    poll_seconds = max(30, int(config.get("poll_seconds", 120)))

    if args.once:
        watcher.poll_once()
        return

    log(f"Watching Drive for new docs every {poll_seconds}s — app at {watcher.app_url}")
    while True:
        try:
            watcher.poll_once()
        except Exception as e:
            log(f"Poll failed: {e}", "error")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
