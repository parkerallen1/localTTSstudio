"""Smoke test for the Google Doc -> WordPress hand-off state machine.

Runs doc_watcher's completion pass against a fake TTS app and a fake
WordPress publisher, so it exercises the branching (matched / no match /
answered / failing) without touching Drive, Gmail, or the live site.

    ./venv/bin/python test_wp_pipeline.py
"""
import sys
import types

import doc_watcher
from doc_watcher import Watcher
from wp_publisher import WordPressPublisher, WordPressError

PASS, FAIL = [], []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    mark = "ok  " if got == want else "FAIL"
    print(f"[{mark}] {label}" + ("" if got == want else f"\n        got {got!r}, want {want!r}"))


class FakePublisher:
    """Stands in for WordPressPublisher: real matching, fake SSH."""

    def __init__(self, posts, fail_publish=False):
        self.posts = posts
        self.fail_publish = fail_publish
        self.published = []

    def match_title(self, title, limit=3):
        return WordPressPublisher.match_title(
            types.SimpleNamespace(), title, limit=limit, posts=self.posts)

    def publish(self, post_id, m4a_path, extra_fields=None, media_title=None, source=""):
        if self.fail_publish:
            raise WordPressError("ACF is not active on this install")
        self.published.append((post_id, extra_fields, open(m4a_path, "rb").read()))
        self.sources = getattr(self, "sources", []) + [source]
        return {
            "ok": True, "post_title": "Faith Over Fear", "post_status": "publish",
            "permalink": f"https://example.com/?p={post_id}",
            "edit_link": f"https://example.com/wp-admin/post.php?post={post_id}",
            "applied": {"audio_file": {"before": None, "after": 999}},
        }


def make_watcher(posts, fail_publish=False, state=None):
    w = Watcher.__new__(Watcher)
    w.app_url = "http://127.0.0.1:8001"
    w.app_token = ""
    w.email = {"enabled": False}
    w.wordpress = {"enabled": True, "notify": "me@example.com",
                   "site_url": "https://example.com", "audio_field": "audio_file"}
    w._wp_pub = FakePublisher(posts, fail_publish=fail_publish)
    w.state = state or {}
    w.sent = []
    w.send_mail = lambda to, subject, body, **kw: (
        w.sent.append((to, subject, body)) or {"threadId": "thread-1"})
    w.export_m4a = lambda project_id, file_ids: b"FAKE-M4A-BYTES"
    w.fetch_chapters_shortcode = lambda project_id, name=None: '[{"title":"One","start":0}]'
    return w


def fake_project_get(url, **kwargs):
    r = types.SimpleNamespace()
    r.status_code = 200
    r.raise_for_status = lambda: None
    r.json = lambda: {
        "import_status": "done",
        "paragraphs": [{"id": "p1", "hasAudio": True, "activeTake": 1}],
    }
    return r


import json as _json
import tempfile as _tempfile
doc_watcher.PUBLISH_LOG = _tempfile.mktemp(suffix=".jsonl")   # never the real one

doc_watcher.requests = types.SimpleNamespace(
    get=fake_project_get, RequestException=Exception)

POSTS = [
    {"ID": 11, "post_title": "Faith Over Fear", "post_status": "publish"},
    {"ID": 12, "post_title": "Faith Over Fear, Again", "post_status": "draft"},
    {"ID": 13, "post_title": "Totally Different", "post_status": "publish"},
]


def entry(name, **extra):
    base = {"name": name, "project_id": "proj-1", "email_status": "skipped",
            "wp_status": "pending", "wp_attempts": 0, "doc_url": "https://docs/x"}
    base.update(extra)
    return base


print("\n--- exact title match publishes on its own ---")
w = make_watcher(POSTS)
info = entry("Faith Over Fear")          # doc title == post title
w._finish_doc(info)
check("status", info["wp_status"], "published")
check("post id", info["wp_post_id"], 11)
check("audio uploaded", w._wp_pub.published[0][2], b"FAKE-M4A-BYTES")
check("chapters field sent", w._wp_pub.published[0][1], {})
check("confirmation emailed", len(w.sent), 1)

print("\n--- smart quotes and dashes still match ---")
w = make_watcher([{"ID": 21, "post_title": "Don't Give Up - Part 2", "post_status": "publish"}])
info = entry("Don’t Give Up — Part 2")
w._finish_doc(info)
check("status", info["wp_status"], "published")

print("\n--- extra_fields templates get filled in ---")
w = make_watcher(POSTS)
w.wordpress["extra_fields"] = {"chapters": "{chapters}", "source_doc": "{doc_url}"}
info = entry("Faith Over Fear")
w._finish_doc(info)
check("templated fields", w._wp_pub.published[0][1],
      {"chapters": '[{"title":"One","start":0}]', "source_doc": "https://docs/x"})

print("\n--- ambiguous title asks a human, then waits ---")
w = make_watcher(POSTS)
info = entry("Faith Over")                # close to two posts, exactly none
w._finish_doc(info)
check("status", info["wp_status"], "awaiting_reply")
check("thread recorded", info["wp_thread_id"], "thread-1")
check("candidates offered", [c["id"] for c in info["wp_candidates"]], [11, 12])
check("asked once", len(w.sent), 1)
check("subject", w.sent[0][1], 'Which post is "Faith Over"?')
check("links are tappable", "https://example.com/?p=11" in w.sent[0][2], True)
w._finish_doc(info)                       # next poll, still no reply
check("no repeat ask", len(w.sent), 1)
check("no audio re-encoded", w._wp_pub.published, [])

print("\n--- the answer arrives and it publishes there ---")
info["wp_post_id"] = 13                   # what the reply handler sets
w._finish_doc(info)
check("status", info["wp_status"], "published")
check("published to the named post", w._wp_pub.published[0][0], 13)

print("\n--- a broken install retries, then gives up loudly ---")
w = make_watcher(POSTS, fail_publish=True)
info = entry("Faith Over Fear")
for _ in range(doc_watcher.MAX_WP_ATTEMPTS):
    w._finish_doc(info)
check("status", info["wp_status"], "failed")
check("attempts", info["wp_attempts"], doc_watcher.MAX_WP_ATTEMPTS)
check("failure emailed", w.sent[-1][1], "Couldn't attach audio: Faith Over Fear")

print("\n--- docs imported before WordPress was enabled are left alone ---")
w = make_watcher(POSTS)
info = {"name": "Old Doc", "project_id": "proj-0", "email_status": "sent"}
check("untouched", w._finish_doc(info), False)
check("no wp_status invented", info.get("wp_status"), None)

print("\n--- reading the reply ---")
import base64
from doc_watcher import _message_text, _first_post_reference


def gmail_text(text):
    return {"mimeType": "text/plain",
            "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}}


PHONE_REPLY = """https://deepspirituality.com/faith-over-fear/

Sent from my iPhone

On Sep 15, 2026, at 9:02 AM, TTS Studio <tts@example.com> wrote:

Closest matches - if it's one of these, reply with its link:
  https://example.com/?p=11
"""
MULTIPART = {"mimeType": "multipart/alternative", "parts": [
    {"mimeType": "text/html",
     "body": {"data": base64.urlsafe_b64encode(b"<p>ignored</p>").decode()}},
    gmail_text("it's this one: https://deepspirituality.com/?p=4821 thanks"),
]}

check("phone reply beats the quoted original",
      _first_post_reference(_message_text(gmail_text(PHONE_REPLY))),
      "https://deepspirituality.com/faith-over-fear/")
check("html multipart falls back to the text part",
      _first_post_reference(_message_text(MULTIPART)),
      "https://deepspirituality.com/?p=4821")
check("trailing punctuation trimmed",
      _first_post_reference("use https://x.com/a-post/."), "https://x.com/a-post/")
check("bare post id accepted", _first_post_reference("4821"), "4821")
check("links inside a > quote are not mistaken for an answer",
      _first_post_reference(_message_text(gmail_text("no idea\n> https://example.com/?p=11\n"))),
      None)
check("empty reply", _first_post_reference(_message_text(gmail_text("\n\n"))), None)

print("\n--- the watcher never answers its own question ---")


def thread_response(messages):
    r = types.SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    r.json = lambda: {"messages": messages}
    return r


def gmail_message(msg_id, sender, text, labels=()):
    return {"id": msg_id, "labelIds": list(labels),
            "payload": {"headers": [{"name": "From", "value": sender}],
                        "mimeType": "text/plain",
                        "body": {"data": base64.urlsafe_b64encode(
                            text.encode()).decode().rstrip("=")}}}


# The ask and the notify address are the same mailbox -- the case that used to
# make the watcher read its own candidate links back as the answer.
SAME = "me@example.com"
OUR_ASK = gmail_message(
    "m1", f"TTS Studio <{SAME}>",
    "Closest matches:\n  https://example.com/?p=11\n  https://example.com/?p=12",
    labels=("SENT",))

w = make_watcher(POSTS)
w.email = {"enabled": True}                      # no from_address configured
w.wordpress["notify"] = SAME
w._gmail_credentials = lambda: types.SimpleNamespace(token="fake")
info = entry("Faith Over", wp_status="awaiting_reply", wp_thread_id="t1",
             wp_ask_message_id="m1")

doc_watcher.requests.get = lambda url, **kw: thread_response([OUR_ASK])
check("our own ask is not an answer", w._check_reply(info), False)
check("still waiting", info["wp_status"], "awaiting_reply")
check("no post picked", info.get("wp_post_id"), None)

REAL_REPLY = gmail_message("m2", f"Parker <{SAME}>",
                           "https://deepspirituality.com/?p=13")
doc_watcher.requests.get = lambda url, **kw: thread_response([OUR_ASK, REAL_REPLY])
w._wp_pub.find_post_by_url = lambda url: {"ID": 13, "post_title": "Totally Different"}
check("a genuine reply is read", w._check_reply(info), True)
check("queued to publish", info["wp_status"], "pending")
check("post taken from the reply", info["wp_post_id"], 13)

print("\n--- a dry run doesn't pretend it published ---")
doc_watcher.requests.get = fake_project_get   # the reply test above stubbed it
w = make_watcher(POSTS)
w.wordpress["dry_run"] = True
info = entry("Faith Over Fear")
w._finish_doc(info)
check("own terminal state", info["wp_status"], "dry_run")
check("not counted as published", info["wp_status"] == "published", False)
check("email says so", w.sent[-1][1].startswith("[dry run]"), True)
check("body leads with the warning", w.sent[-1][2].startswith("DRY RUN"), True)
check("doc is not reprocessed", w._finish_doc(info), False)

print("\n--- the uploaded file is named after the doc ---")
from doc_watcher import _audio_filename
check("slugified", _audio_filename("Don\u2019t Give Up \u2014 Part 2"), "dont-give-up-part-2.m4a")
check("nbsp and trailing space", _audio_filename("Dare to Hope\u00a0"), "dare-to-hope.m4a")
check("empty falls back", _audio_filename("   "), "devotional.m4a")
check("length capped", len(_audio_filename("A" * 300)), 84)

print("\n--- a reply from either mailbox is accepted ---")
doc_watcher.requests.get = fake_project_get
w = make_watcher(POSTS)
w.email = {"enabled": True, "from_address": "parker.allen21@gmail.com"}
w.wordpress["notify"] = "pallen@bacc.cc"
w._gmail_credentials = lambda: types.SimpleNamespace(token="fake")
w._wp_pub.find_post_by_url = lambda url: {"ID": 13, "post_title": "Totally Different"}

ASK = gmail_message("a1", "TTS Studio <parker.allen21@gmail.com>",
                    "which post?", labels=("SENT",))

for label, sender in [("replied from the notified address", "Parker <pallen@bacc.cc>"),
                      ("replied from the sending mailbox", "Parker <parker.allen21@gmail.com>")]:
    info = entry("Faith Over", wp_status="awaiting_reply", wp_thread_id="t1",
                 wp_ask_message_id="a1")
    reply = gmail_message("r1", sender, "https://deepspirituality.com/?p=13")
    doc_watcher.requests.get = lambda url, **kw: thread_response([ASK, reply])
    w._check_reply(info)
    check(label, info.get("wp_post_id"), 13)

info = entry("Faith Over", wp_status="awaiting_reply", wp_thread_id="t1",
             wp_ask_message_id="a1")
stranger = gmail_message("r2", "Someone Else <nope@example.com>",
                         "https://deepspirituality.com/?p=11")
doc_watcher.requests.get = lambda url, **kw: thread_response([ASK, stranger])
w._check_reply(info)
check("a stranger still can't name the post", info.get("wp_post_id"), None)

print("\n--- the doc's filing tag doesn't stop the match ---")
doc_watcher.requests.get = fake_project_get
w = make_watcher([{"ID": 31, "post_title": "Fully Known. Fully Loved.", "post_status": "draft"}])
info = entry("[QQT #96] Fully Known. Fully Loved.")
w._finish_doc(info)
check("tagged doc matches its post", info.get("wp_post_id"), 31)
w = make_watcher([{"ID": 32, "post_title": "When Courage Feels Heavy", "post_status": "draft"}])
info = entry("[QQT #86]\U0001F525 When Courage Feels Heavy")
w._finish_doc(info)
check("tag plus emoji still matches", info.get("wp_post_id"), 32)
check("upload tagged with the project", w._wp_pub.sources, ["proj-1"])
w = make_watcher([{"ID": 33, "post_title": "Faith Blockers and Mountain Movers", "post_status": "publish"}])
info = entry('[QQT #46] "Faith Blockers & Mountain Movers” ')
w._finish_doc(info)
check("& matches and", info.get("wp_post_id"), 33)

print("\n--- chapters keep their real characters ---")
w = make_watcher(POSTS)
del w.fetch_chapters_shortcode            # use the real one
chapters = [{"title": "God’s love “never” runs out", "start": 0}]
def chapters_get(url, **kw):
    r = types.SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    r.json = lambda: {"chapters": chapters, "shortcode": "IGNORED"}
    return r
doc_watcher.requests.get = chapters_get
got = w.fetch_chapters_shortcode("proj-1")
check("no \\u escapes", "\\u" in got, False)
check("round-trips", __import__("json").loads(got), chapters)
doc_watcher.requests.get = fake_project_get

print("\n--- a look-alike sender can't name the post ---")
w = make_watcher(POSTS)
w.email = {"enabled": True, "from_address": "parker.allen21@gmail.com"}
w.wordpress["notify"] = "pallen@bacc.cc"
w._gmail_credentials = lambda: types.SimpleNamespace(token="fake")
w._wp_pub.find_post_by_url = lambda url: {"ID": 13, "post_title": "Totally Different"}
info = entry("Faith Over", wp_status="awaiting_reply", wp_thread_id="t1",
             wp_ask_message_id="a1")
lookalike = gmail_message("r3", "Parker <xpallen@bacc.cc.evil.example>",
                          "https://deepspirituality.com/?p=11")
doc_watcher.requests.get = lambda url, **kw: thread_response([ASK, lookalike])
w._check_reply(info)
check("look-alike rejected", info.get("wp_post_id"), None)
doc_watcher.requests.get = fake_project_get

print("\n--- an ssh timeout counts as an attempt ---")
import subprocess
import wp_publisher
real_run = wp_publisher.subprocess.run
def hang(*a, **kw):
    raise subprocess.TimeoutExpired(cmd="ssh", timeout=kw.get("timeout"))
wp_publisher.subprocess.run = hang
pub = WordPressPublisher({"ssh_host": "x@example.com", "audio_field": "audio_file"})
try:
    pub._run("true", timeout=1)
    got = "no error"
except WordPressError as e:
    got = "WordPressError"
wp_publisher.subprocess.run = real_run
check("timeout is a WordPressError", got, "WordPressError")

print("\n--- publish is one connection carrying the audio ---")
calls = []
def fake_run(self, script, stdin_bytes=None, timeout=None):
    calls.append((script, stdin_bytes))
    return 'notice\n<<<TTSJSON>>>{"ok": true, "attachment_id": 7}<<<TTSEND>>>\n'
import tempfile, os
tmp = tempfile.mkdtemp()
path = os.path.join(tmp, "dare-to-hope.m4a")
open(path, "wb").write(b"AUDIO")
pub = WordPressPublisher({"ssh_host": "x@example.com", "audio_field": "audio_file",
                          "wp_path": "/sites/x"})
pub._run = types.MethodType(fake_run, pub)
res = pub.publish(11, path, extra_fields={"chapters": "[]"}, source="proj-1")
check("one ssh call", len(calls), 1)
check("audio on stdin", calls[0][1], b"AUDIO")
check("file keeps its name", "'dare-to-hope.m4a'" in calls[0][0], True)
check("result parsed past notices", res["attachment_id"], 7)
check("PHP slashes before writing", "wp_slash( $value )" in wp_publisher._PUBLISH_PHP, True)
calls.clear()
pub.dry_run = True
pub.publish(11, path, source="proj-1")
check("dry run sends no audio", calls[0][1], b"")
def fail_run(self, script, stdin_bytes=None, timeout=None):
    return '<<<TTSJSON>>>{"ok": false, "error": "field x missing"}<<<TTSEND>>>'
pub._run = types.MethodType(fail_run, pub)
try:
    pub.publish(11, path)
    got = None
except WordPressError as e:
    got = str(e)
check("PHP error surfaces", got, "field x missing")

print("\n--- the audio email is the fallback, not the announcement ---")
doc_watcher.requests.get = fake_project_get


def emailing_watcher(posts, **kw):
    w = make_watcher(posts, **kw)
    w.email = {"enabled": True}
    return w


def audio_emails(w):
    return [s for s in w.sent if s[1].startswith("Your audio is ready")]


w = emailing_watcher(POSTS)
info = entry("Faith Over Fear", email_status="pending", sharer_email="me@example.com")
w._finish_doc(info)
check("matched: published", info["wp_status"], "published")
check("matched: no audio email", audio_emails(w), [])
check("matched: only the confirmation", [s[1] for s in w.sent], ["Audio attached: Faith Over Fear"])
check("matched: email marked not needed", info["email_status"], "not_needed")
check("matched: settled", w._finish_doc(info), False)

w = emailing_watcher(POSTS)
info = entry("Faith Over", email_status="pending", sharer_email="me@example.com")
w._finish_doc(info)
check("candidates: asks", info["wp_status"], "awaiting_reply")
check("candidates: no audio email yet", audio_emails(w), [])
check("candidates: email still held", info["email_status"], "pending")
check("candidates: offers 'none'", '"none"' in w.sent[0][2], True)

w._gmail_credentials = lambda: types.SimpleNamespace(token="fake")
w.email["from_address"] = "bot@example.com"
none_reply = gmail_message("n1", "Me <me@example.com>", "None of these, thanks")
doc_watcher.requests.get = lambda url, **kw: thread_response([none_reply])
w._check_reply(info)
check("reply 'none': no match", info["wp_status"], "no_match")
doc_watcher.requests.get = fake_project_get
w._finish_doc(info)
check("reply 'none': audio emailed", len(audio_emails(w)), 1)
check("reply 'none': email sent", info["email_status"], "sent")

w = emailing_watcher(POSTS)
info = entry("Something Entirely Unrelated Xyz", email_status="pending",
             sharer_email="me@example.com")
w._finish_doc(info)
check("no match: no ask", [s for s in w.sent if s[1].startswith("Which post")], [])
check("no match: status", info["wp_status"], "no_match")
check("no match: audio emailed", len(audio_emails(w)), 1)
check("no match: says why", "No post on the site matched" in audio_emails(w)[0][2], True)
check("no match: shortcode included", '[{"title":"One","start":0}]' in audio_emails(w)[0][2], True)

w = emailing_watcher(POSTS, fail_publish=True)
info = entry("Faith Over Fear", email_status="pending", sharer_email="me@example.com")
for _ in range(doc_watcher.MAX_WP_ATTEMPTS - 1):
    w._finish_doc(info)
check("failing: audio held while retrying", audio_emails(w), [])
w._finish_doc(info)
check("gave up: audio emailed as fallback", len(audio_emails(w)), 1)
check("gave up: failure notice points at it", "on its way to me@example.com" in
      [s for s in w.sent if s[1].startswith("Couldn't attach")][0][2], True)

print("\n--- WordPress emails go to whoever shared the doc ---")
doc_watcher.requests.get = fake_project_get
w = make_watcher(POSTS)       # notify is me@example.com
info = entry("Faith Over", sharer_email="sharer@example.com")
w._finish_doc(info)
check("ask goes to the sharer", w.sent[0][0], "sharer@example.com")
w = make_watcher(POSTS)
info = entry("Faith Over Fear", sharer_email="sharer@example.com")
w._finish_doc(info)
check("confirmation goes to the sharer", w.sent[0][0], "sharer@example.com")
w = make_watcher(POSTS)
info = entry("Faith Over Fear")
w._finish_doc(info)
check("notify only when the sharer is unknown", w.sent[0][0], "me@example.com")

print("\n--- each live publish is logged ---")
import os as _os
if _os.path.exists(doc_watcher.PUBLISH_LOG):
    _os.remove(doc_watcher.PUBLISH_LOG)
w = make_watcher(POSTS)
info = entry("Faith Over Fear", sharer_email="sharer@example.com")
w._finish_doc(info)
rows = [_json.loads(l) for l in open(doc_watcher.PUBLISH_LOG)]
check("one row", len(rows), 1)
check("row links the post", (rows[0]["post_id"], rows[0]["permalink"]), (11, "https://example.com/?p=11"))
check("row keeps what it replaced", rows[0]["replaced"], {"audio_file": None})
w = make_watcher(POSTS)
w.wordpress["dry_run"] = True
w._finish_doc(entry("Faith Over Fear"))
check("dry runs aren't logged", sum(1 for _ in open(doc_watcher.PUBLISH_LOG)), 1)
check("folder name sent to the server",
      '"media_folder"' in open(wp_publisher.__file__).read(), True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
