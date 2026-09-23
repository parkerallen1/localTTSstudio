"""
WordPress backfill — narrate existing posts that have no audio of their own.

The Google Doc watcher narrates new devotionals as they're written. This does
the back catalogue: it walks the published posts in the configured categories
(Devotionals, Quick Quiet Times) that have no narration of their own, newest
first, and for each one

  1. reads the post's block content from WordPress and turns it into the
     Markdown the importer takes (headings, paragraphs, lists, scripture
     quotes; images, embeds, pull quotes, buttons and the like are left out;
     each <h2> becomes a chapter),
  2. imports it into TTS Studio, which generates every paragraph,
  3. publishes the result exactly as the watcher does — upload, the ACF
     fields, the FileBird folder, the publish log (so it shows on the
     Published page) — but only if the post STILL has no narration of its
     own at the moment of writing.

One post at a time, and never while anything else is generating: a Google Doc
always goes first. No emails — there would be hundreds; check the Published
page, and this script's log for anything skipped or failed.

"limit" caps how many posts it publishes in total (the state file keeps
count), which is how a trial run works: set it to 3, listen, raise it.

Config — the "backfill" block of doc_watcher.json (the "wordpress" block
supplies the SSH and field settings):
    "backfill": {
      "enabled": true,
      "categories": ["Devotionals", "Quick Quiet Times"],
      "limit": 3,
      "stop_at_headings": ["Next step"],  // narration ends at the first heading
                                          // starting with one of these
      "skip_sections": ["View all studies"], // these sections are left out;
                                             // narration resumes at the next heading
      "restart_app_every": 1,                // restart the app after this many posts
      "restart_app_command": "launchctl kickstart -k gui/501/com.localtts.server"
    }

Why restart the app: its memory grows with every generation (it reached an
84 GB footprint, mostly swap, after 25 posts on a 16 GB mini) even though it
empties the GPU cache after each paragraph. A restart between posts — only
when nothing is generating — keeps it flat, for one model reload (~30s) per
~35-minute post.

Run:  python wp_backfill.py            (loop; launchd keeps it running)
      python wp_backfill.py --status   (what's done, skipped, failed)
      python wp_backfill.py --text ID  (print the text a post WOULD be read as)
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from html.parser import HTMLParser

import requests

import doc_watcher
from doc_watcher import Watcher, _append_publish_log, _audio_filename, load_json, log
from wp_publisher import WordPressError, WordPressSkip

STATE_FILE = os.path.join(doc_watcher.DEFAULT_DIR, "wp_backfill_state.json")
POLL_SECONDS = 60
# Re-read the candidate list at most this often — each read is a ~25s wp-cli
# call, and the back catalogue doesn't change minute to minute.
CANDIDATE_TTL = 3600
MAX_ATTEMPTS = 3
_TERMINAL = ("published", "skipped", "failed")
# Fewer words than this after conversion means the post is mostly embeds (a
# video or podcast page) — nothing worth narrating.
MIN_WORDS = 150


# ---- Post HTML -> Markdown -------------------------------------------------

# Whole blocks that are never read aloud. Stripped by their block comments
# before parsing: images, media, embeds, pull quotes (they repeat a line from
# the body), buttons, tables, custom HTML, shortcodes, tables of contents and
# related-post lists. Self-closing blocks (<!-- wp:x /-->) carry no text.
_SKIP_BLOCKS = (
    r"html|shortcode|pullquote|cover|embed|gallery|image|video|audio|file|"
    r"buttons|button|table|spacer|separator|media-text|"
    r"canvas/[a-z0-9-]+|core-embed/[a-z0-9-]+"
)
_BLOCK_RE = re.compile(
    r"<!--\s*wp:(" + _SKIP_BLOCKS + r")(?:\s[^>]*?)?-->.*?<!--\s*/wp:\1\s*-->", re.S)
# Anything a leftover [shortcode] would add is noise to a listener.
_SHORTCODE_RE = re.compile(r"\[/?[a-z_][a-z0-9_-]*(?:\s[^\]]*)?\]", re.I)

_SKIP_TAGS = {"figure", "img", "iframe", "script", "style", "svg", "noscript",
              "button", "table", "form", "video", "audio", "object", "select",
              "textarea"}
_TEXT_TAGS = {"p", "li", "cite", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}


class _Narration(HTMLParser):
    def __init__(self, stop_prefixes, skip_prefixes=()):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self.stop_prefixes = [p.casefold() for p in stop_prefixes]
        self.skip_prefixes = [p.casefold() for p in skip_prefixes]
        self.stopped = False
        self._skipping_level = None   # inside a skipped section under this heading level
        self._skip = 0          # depth inside a tag we don't read
        self._stack = []        # open text tags
        self._buf = []

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf).replace("\xa0", " ")).strip()
        self._buf = []
        if not text or not self._stack or self.stopped:
            return
        tag = self._stack[-1]
        if tag[0] == "h" and tag[1:].isdigit():
            level, key = int(tag[1:]), text.casefold()
            if self._skipping_level is not None and level <= self._skipping_level:
                self._skipping_level = None       # the skipped section has ended
            if any(key.startswith(p) for p in self.stop_prefixes):
                self.stopped = True     # e.g. "Next steps": links, not narration
                return
            if any(key.startswith(p) for p in self.skip_prefixes):
                self._skipping_level = level      # e.g. a series' table of links
                return
            if self._skipping_level is not None:
                return
            self.lines.append("#" * level + " " + text)
            return
        if self._skipping_level is not None:
            return
        if tag == "li":
            self.lines.append("- " + text)
        else:
            self.lines.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            if tag != "img":            # void element: no end tag to balance
                self._skip += 1
            return
        if self._skip:
            return
        if tag in _TEXT_TAGS:
            self._flush()               # text before a nested list item
            self._stack.append(tag)
        elif tag == "br":
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _TEXT_TAGS and self._stack:
            self._flush()
            if tag in self._stack:
                while self._stack and self._stack.pop() != tag:
                    pass

    def handle_data(self, data):
        if not self._skip and self._stack:
            self._buf.append(data)


def post_markdown(title, content, stop_at_headings=(), skip_sections=()):
    """A post's block HTML as the Markdown the importer takes: "# Title" first
    (the parser's title line), then one line per paragraph, heading (h2 = a
    chapter), list item or quote citation."""
    html = content or ""
    while True:
        stripped = _BLOCK_RE.sub("", html)
        if stripped == html:
            break
        html = stripped
    html = _SHORTCODE_RE.sub("", html)
    parser = _Narration(stop_at_headings, skip_sections)
    parser.feed(html)
    parser.close()
    parser._flush()
    return "\n\n".join([f"# {title}"] + parser.lines)


# ---- The app ----------------------------------------------------------------

def app_busy(app_url, headers):
    """True while the app is generating or has imports queued — a Google Doc,
    the backfill, or someone using the app. Also used by
    restart_app_if_idle.py.

    Asks the app (/api/busy) rather than reading projects' saved
    import_status: a job a restart killed leaves its project saying
    "generating" forever, which would read as busy forever. The project scan
    is only the fallback for an app too old to have /api/busy."""
    def get(path):
        r = requests.get(f"{app_url}{path}", headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()
    try:
        return bool(get("/api/busy")["busy"])
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
    for p in get("/api/projects")[:8]:
        if str(get(f"/api/projects/{p['id']}").get("import_status") or "") in ("pending", "generating"):
            return True
    return False


def restart_app(command, app_url, headers):
    """Run the restart command, then wait (up to ~2 min) for the app to answer.
    Raises OSError/SubprocessError if the command itself fails."""
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    subprocess.run(argv, check=True, capture_output=True, timeout=60)
    for _ in range(60):
        time.sleep(2)
        try:
            requests.get(f"{app_url}/api/health", headers=headers, timeout=5)
            return
        except requests.RequestException:
            continue


# ---- The queue --------------------------------------------------------------

def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


class Backfill:
    def __init__(self, config):
        self.cfg = config.get("backfill") or {}
        # The watcher already knows how to talk to the app and to WordPress —
        # export, chapters, field templates, the publisher.
        self.watcher = Watcher(config)
        self.pub = self.watcher._wp_publisher()
        self.state = load_json(STATE_FILE, {}) or {}
        self.state.setdefault("posts", {})
        self.state.setdefault("current", None)
        self._candidates, self._candidates_at = None, 0
        self._said_limit = False

    # -- helpers --

    @property
    def limit(self):
        return int(self.cfg.get("limit", 0))

    def published_count(self):
        return sum(1 for p in self.state["posts"].values() if p.get("status") == "published")

    def _app(self, path, **kw):
        r = requests.get(f"{self.watcher.app_url}{path}",
                         headers=self.watcher._app_headers(), timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    def app_busy(self):
        return app_busy(self.watcher.app_url, self.watcher._app_headers())

    def restart_due(self):
        every = int(self.cfg.get("restart_app_every", 0))
        return every > 0 and int(self.state.get("since_restart", 0)) >= every

    def restart_app(self):
        """Restart the app while it's idle (poll() checked), then wait for it
        to answer before the next post is imported."""
        command = self.cfg.get("restart_app_command")
        if not command:
            log("restart_app_every is set but restart_app_command isn't — not restarting.", "warn")
            self.state["since_restart"] = 0
            return save_state(self.state)
        try:
            restart_app(command, self.watcher.app_url, self.watcher._app_headers())
        except (OSError, subprocess.SubprocessError) as e:
            log(f"Couldn't restart the app ({e}) — carrying on without.", "warn")
        else:
            log("Restarted the app to release the memory generation holds on to.")
        self.state["since_restart"] = 0
        save_state(self.state)

    def next_candidate(self):
        now = time.time()
        if self._candidates is None or now - self._candidates_at > CANDIDATE_TTL:
            self._candidates = self.pub.backfill_candidates(
                self.cfg.get("categories") or ["Devotionals", "Quick Quiet Times"])
            self._candidates_at = now
            log(f"{len(self._candidates)} post(s) without narration of their own.")
        done = self.state["posts"]
        for post in self._candidates:
            if done.get(str(post["ID"]), {}).get("status") not in _TERMINAL:
                return post
        return None

    def _entry(self, post_id):
        return self.state["posts"].setdefault(str(post_id), {})

    def _finish(self, post_id, status, note=None):
        entry = self._entry(post_id)
        entry["status"] = status
        entry["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if note:
            entry["note"] = note
        if str(self.state.get("current")) == str(post_id):
            self.state["current"] = None
        save_state(self.state)

    # -- one step --

    def poll(self):
        if self.state.get("current"):
            return self.check_current()
        if self.published_count() >= self.limit:
            if not self._said_limit:
                log(f"Published {self.published_count()} of the limit {self.limit} — "
                    f"paused. Raise backfill.limit and restart to continue.")
                self._said_limit = True
            return
        if self.app_busy():
            return
        if self.restart_due():
            return self.restart_app()
        post = self.next_candidate()
        if not post:
            if not self._said_limit:
                log("Nothing left to narrate.", "ok")
                self._said_limit = True
            return
        self.start(post)

    def start(self, candidate):
        post_id = candidate["ID"]
        entry = self._entry(post_id)
        entry.update({"title": candidate.get("post_title"), "status": "starting"})
        try:
            post = self.pub.post_for_narration(post_id)
        except WordPressError as e:
            return self._attempt_failed(post_id, f"couldn't read the post: {e}")
        if post.get("own_audio"):
            return self._finish(post_id, "skipped", "has narration of its own now")
        markdown = post_markdown(post["post_title"], post["content"],
                                 self.cfg.get("stop_at_headings") or ["Next step"],
                                 self.cfg.get("skip_sections") or [])
        words = len(markdown.split())
        if words < MIN_WORDS:
            log(f"\"{post['post_title']}\" is only {words} words once embeds are "
                f"left out — skipping.", "warn")
            return self._finish(post_id, "skipped", f"only {words} words of text")
        payload = {
            "name": post["post_title"],
            "raw_text": markdown,
            "source": {"kind": "wordpress_post", "post_id": post_id,
                       "url": post.get("permalink")},
        }
        if self.watcher.settings:
            payload["settings"] = self.watcher.settings
        try:
            r = requests.post(f"{self.watcher.app_url}/api/projects/import", json=payload,
                              headers=self.watcher._app_headers(), timeout=60)
            r.raise_for_status()
            result = r.json()
        except requests.RequestException as e:
            return self._attempt_failed(post_id, f"import failed: {e}")
        entry.update({
            "status": "generating", "project_id": result.get("id"),
            "permalink": post.get("permalink"), "words": words,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        self.state["current"] = post_id
        save_state(self.state)
        log(f"Narrating \"{post['post_title']}\" (post {post_id}, {words} words, "
            f"{result.get('para_count')} paragraphs).", "ok")

    def check_current(self):
        post_id = self.state["current"]
        entry = self._entry(post_id)
        title = entry.get("title") or f"post {post_id}"
        try:
            project = self._app(f"/api/projects/{entry['project_id']}")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return self._finish(post_id, "failed", "the project was deleted")
            log(f"Couldn't check \"{title}\": {e}", "warn")
            return
        except requests.RequestException as e:
            log(f"Couldn't check \"{title}\": {e}", "warn")
            return
        status = str(project.get("import_status") or "")
        if not status.startswith("done"):
            return
        if status != "done":
            # Unlike a Doc, nobody is emailed about this one: publishing it
            # would put narration with missing paragraphs on a live post.
            log(f"\"{title}\" finished with gaps ({status}) — not publishing it. "
                f"Regenerate the missing paragraphs in the app to use it.", "error")
            return self._finish(post_id, "failed", status)
        self.publish(post_id, entry, project)

    def publish(self, post_id, entry, project):
        title = entry.get("title") or f"post {post_id}"
        project_id = entry["project_id"]
        file_ids = [f"{p['id']}-t{p['activeTake']}" for p in project.get("paragraphs", [])
                    if p.get("hasAudio") and p.get("activeTake")]
        info = {"name": title, "doc_url": entry.get("permalink")}
        tmpdir = None
        try:
            m4a = self.watcher.export_m4a(project_id, file_ids)
            fields = self.watcher._wp_extra_fields(info, project_id)
            tmpdir = tempfile.mkdtemp(prefix="tts-backfill-")
            local = os.path.join(tmpdir, _audio_filename(title))
            with open(local, "wb") as f:
                f.write(m4a)
            result = self.pub.publish(post_id, local, extra_fields=fields, media_title=title,
                                      source=project_id, only_if_no_own_audio=True)
        except WordPressSkip as e:
            log(f"Not attaching \"{title}\": {e}.", "warn")
            return self._finish(post_id, "skipped", str(e))
        except (WordPressError, requests.RequestException, OSError) as e:
            return self._attempt_failed(post_id, f"publish failed: {e}", keep_current=True)
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
        _append_publish_log(info, post_id, result, matched_by="backfill")
        entry["attachment_id"] = result.get("attachment_id")
        self.state["since_restart"] = int(self.state.get("since_restart", 0)) + 1
        self._finish(post_id, "published")
        log(f"Attached narration for \"{title}\" — {result.get('permalink')} "
            f"({self.published_count()} of {self.limit}).", "ok")

    def _attempt_failed(self, post_id, reason, keep_current=False):
        entry = self._entry(post_id)
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_error"] = reason
        title = entry.get("title") or f"post {post_id}"
        if entry["attempts"] >= MAX_ATTEMPTS:
            log(f"Giving up on \"{title}\" after {entry['attempts']} attempts — {reason}", "error")
            return self._finish(post_id, "failed", reason)
        log(f"\"{title}\": {reason} — will retry ({entry['attempts']}/{MAX_ATTEMPTS}).", "warn")
        if not keep_current:
            entry["status"] = "retry"   # not terminal: the next poll picks it again
        save_state(self.state)


def print_status():
    state = load_json(STATE_FILE, {}) or {}
    posts = state.get("posts", {})
    by = {}
    for pid, p in posts.items():
        by.setdefault(p.get("status", "?"), []).append((pid, p))
    print(f"current: {state.get('current')}")
    for status, items in sorted(by.items()):
        print(f"\n{status} ({len(items)}):")
        for pid, p in items:
            note = f" — {p['note']}" if p.get("note") else ""
            print(f"  {pid}  {p.get('title')}{note}")


def main():
    ap = argparse.ArgumentParser(description="Narrate existing WordPress posts that have no audio of their own.")
    ap.add_argument("--config", default=doc_watcher.DEFAULT_CONFIG)
    ap.add_argument("--status", action="store_true", help="show progress and exit")
    ap.add_argument("--text", type=int, metavar="POST_ID",
                    help="print the text a post would be narrated from, and exit")
    ap.add_argument("--once", action="store_true", help="one poll, then exit")
    args = ap.parse_args()

    if args.status:
        return print_status()
    config = load_json(args.config, None)
    if config is None:
        sys.exit(f"Config not found or invalid: {args.config}")
    cfg = config.get("backfill") or {}
    if args.text:
        bf = Backfill(config)
        post = bf.pub.post_for_narration(args.text)
        print(post_markdown(post["post_title"], post["content"],
                            cfg.get("stop_at_headings") or ["Next step"],
                            cfg.get("skip_sections") or []))
        return
    if not cfg.get("enabled"):
        # A clean exit, so launchd (KeepAlive: SuccessfulExit false) leaves it
        # stopped instead of restarting it every few seconds.
        log("backfill.enabled is not true in the config — exiting.")
        return

    bf = Backfill(config)
    log(f"Backfill ON — {', '.join(cfg.get('categories') or [])}; "
        f"published {bf.published_count()} of limit {bf.limit}.")
    while True:
        try:
            bf.poll()
        except Exception as e:
            log(f"Backfill poll failed: {e}", "error")
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
