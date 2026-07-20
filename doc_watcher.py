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
        "settings": {}                // optional per-import voice settings;
                                      // empty -> app's import_defaults
      }

Run:  python doc_watcher.py [--once] [--config PATH]
Keep it running with launchd/cron on the machine that hosts the app.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

import requests

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
    sys.exit("Missing dependency: pip install google-auth")

DRIVE_API = "https://www.googleapis.com/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
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
                "fields": "nextPageToken, files(id, name, modifiedTime, webViewLink)",
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
            self.state[doc["id"]] = {
                "name": doc["name"],
                "modified_time": doc.get("modifiedTime"),
                "project_id": result.get("id"),
                "imported_at": datetime.now().astimezone().isoformat(),
            }
            save_state(self.state)
            log(f"Imported \"{doc['name']}\" — project {result.get('id')}, "
                f"{result.get('para_count')} paragraph(s), generation started.", "ok")
        return len(docs)


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
