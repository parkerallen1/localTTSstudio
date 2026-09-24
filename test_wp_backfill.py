"""Checks for wp_backfill.py: the post-HTML-to-narration converter (fixtures
cut from real deepspirituality.com posts) and the queue's decisions, against
a fake app and a fake WordPress. Touches nothing live.

    ./venv/bin/python test_wp_backfill.py
"""
import sys
import tempfile
import types

import doc_watcher
import wp_backfill
from wp_backfill import Backfill, post_markdown
from wp_publisher import WordPressError, WordPressSkip

PASS, FAIL = [], []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    mark = "ok  " if got == want else "FAIL"
    print(f"[{mark}] {label}" + ("" if got == want else f"\n        got {got!r}\n       want {want!r}"))


print("--- converter ---")
POST = """
<!-- wp:group {"className":"kt"} -->
<div class="wp-block-group kt"><!-- wp:heading {"level":4} -->
<h4 class="wp-block-heading">key takeaways</h4>
<!-- /wp:heading -->
<!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li><strong>Recognize your body matters to God</strong> — our physical “strength” is part of it.&nbsp;</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list --></div>
<!-- /wp:group -->

<!-- wp:quote -->
<blockquote class="wp-block-quote"><!-- wp:paragraph -->
<p>The spirit indeed is willing, but <strong>the flesh is weak.</strong></p>
<!-- /wp:paragraph --><cite>Matthew 26:41 AMPC</cite></blockquote>
<!-- /wp:quote -->

<!-- wp:canvas/section-heading {"tag":"h3","content":"Listen to the podcast:"} /-->
<!-- wp:embed {"url":"https://open.spotify.com/episode/x"} -->
<figure class="wp-block-embed"><div class="wp-block-embed__wrapper">
https://open.spotify.com/episode/x
</div></figure>
<!-- /wp:embed -->

<!-- wp:image {"id":1} -->
<figure class="wp-block-image"><img src="a.jpg" alt="a boy"/><figcaption>A caption</figcaption></figure>
<!-- /wp:image -->

<!-- wp:pullquote -->
<figure class="wp-block-pullquote"><blockquote><p>A line repeated from the body.</p></blockquote></figure>
<!-- /wp:pullquote -->

<!-- wp:heading -->
<h2 class="wp-block-heading">What Jobs understood</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>I developed <a href="https://example.com">Hashimoto’s disease</a>, which<br>affects the thyroid.</p>
<!-- /wp:paragraph -->

<!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li>For more, check out our two articles:<!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li>Surviving Social Media</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list --></li>
<!-- /wp:list-item --></ul>
<!-- /wp:list -->

<!-- wp:html -->
<div><script>alert(1)</script>Custom embed</div>
<!-- /wp:html -->

<!-- wp:block {"ref":10016854} /-->

<!-- wp:paragraph -->
<p></p>
<!-- /wp:paragraph -->

<!-- wp:heading {"className":"toc-exclude"} -->
<h2 class="wp-block-heading toc-exclude">Next steps&nbsp;</h2>
<!-- /wp:heading -->

<!-- wp:paragraph -->
<p>Watch this video and read this article.</p>
<!-- /wp:paragraph -->
"""
md = post_markdown("Strong in Body", POST, ["Next step"])
lines = md.split("\n\n")
check("title line first", lines[0], "# Strong in Body")
check("h4 kept as a heading", "#### key takeaways" in lines, True)
check("list item, nbsp trimmed", "- Recognize your body matters to God — our physical “strength” is part of it." in lines, True)
check("quote text", "The spirit indeed is willing, but the flesh is weak." in lines, True)
check("citation read after the quote", "Matthew 26:41 AMPC" in lines, True)
check("h2 is a chapter heading", "## What Jobs understood" in lines, True)
check("link text kept, br is a space", "I developed Hashimoto’s disease, which affects the thyroid." in lines, True)
check("nested list: parent text", "- For more, check out our two articles:" in lines, True)
check("nested list: child item", "- Surviving Social Media" in lines, True)
for gone in ("Listen to the podcast", "spotify", "A caption", "a boy",
             "A line repeated", "alert", "Custom embed", "Next steps", "Watch this video"):
    check(f"left out: {gone!r}", gone in md, False)

SERIES = ("<h2>View all studies in the series</h2><ul><li>Introduction</li><li>Chapters 1-3</li></ul>"
          "<h3>Sub part</h3><p>still skipped</p>"
          "<h2>What John says</h2><p>Real reading.</p>")
got = post_markdown("T", SERIES, ["Next step"], ["View all studies"])
check("skipped section left out, next h2 resumes", got, "# T\n\n## What John says\n\nReal reading.")
check("skip list empty: section read",
      "Chapters 1-3" in post_markdown("T", SERIES, ["Next step"], []), True)
check("empty post is just the title", post_markdown("T", "", []), "# T")
check("leftover shortcodes dropped",
      post_markdown("T", "<p>[fusion_text]Hello there[/fusion_text]</p>", []), "# T\n\nHello there")
check("no stop heading reads to the end",
      post_markdown("T", "<h2>Next week</h2><p>More.</p>", ["Next step"]), "# T\n\n## Next week\n\nMore.")

print("\n--- the queue ---")
wp_backfill.STATE_FILE = tempfile.mktemp(suffix=".json")
doc_watcher.PUBLISH_LOG = tempfile.mktemp(suffix=".jsonl")
LONG = "<p>" + " ".join(["word"] * 200) + "</p>"


class FakePub:
    def __init__(self):
        self.posts = {1: {"own": False}, 2: {"own": True}, 3: {"own": False, "short": True},
                      4: {"own": False}}
        self.published, self.skip_on_publish = [], set()

    def backfill_candidates(self, cats):
        return [{"ID": i, "post_title": f"Post {i}"} for i in (1, 2, 3, 4)]

    def post_for_narration(self, pid):
        p = self.posts[pid]
        return {"ok": True, "ID": pid, "post_title": f"Post {pid}", "post_status": "publish",
                "permalink": f"https://example.com/p{pid}/", "own_audio": p["own"],
                "content": "<p>too short</p>" if p.get("short") else LONG}

    def publish(self, post_id, path, extra_fields=None, media_title=None, source="",
                only_if_no_own_audio=False, expect_audio=None, content_hash=None,
                text_hash=None):
        self.publish_kw = {"expect_audio": expect_audio, "content_hash": content_hash,
                           "text_hash": text_hash, "only_if_no_own_audio": only_if_no_own_audio}
        if post_id in self.skip_on_publish:
            raise WordPressSkip("the post already has its own narration (attachment 9)")
        self.published.append((post_id, only_if_no_own_audio, source))
        return {"ok": True, "permalink": f"https://example.com/p{post_id}/", "post_title": media_title,
                "post_status": "publish", "attachment_id": 500 + post_id, "applied": {}}


def make(limit=3, busy=False, status="done"):
    bf = Backfill.__new__(Backfill)
    bf.cfg = {"enabled": True, "limit": limit, "categories": ["Devotionals"]}
    bf.pub = FakePub()
    bf.state = {"posts": {}, "current": None}
    bf._candidates, bf._candidates_at, bf._said_limit = None, 0, False
    bf.rcfg, bf._renarrate, bf._queue_at, bf._sweep_at, bf._backoff = {}, {}, 0, 0, {}
    bf.imports = []
    w = types.SimpleNamespace(app_url="http://app", settings=None, _app_headers=lambda: {},
                              export_m4a=lambda pid, ids: b"M4A",
                              _wp_extra_fields=lambda info, pid: {"devotional_chapters": "[]"})
    bf.watcher = w
    bf.app_busy = lambda: busy
    bf.project_status = status
    bf._app = lambda path, **kw: {"import_status": bf.project_status,
                                  "paragraphs": [{"id": "p1", "hasAudio": True, "activeTake": 1}]}

    def fake_post(url, json=None, **kw):
        bf.imports.append(json)
        return types.SimpleNamespace(raise_for_status=lambda: None,
                                     json=lambda: {"id": f"proj-{json['source']['post_id']}", "para_count": 3})
    wp_backfill.requests.post = fake_post
    return bf


bf = make(busy=True)
bf.poll()
check("waits while the app is busy", bf.imports, [])

bf = make()
bf.poll()
check("starts the newest candidate", bf.state["current"], 1)
check("imports the post text with a title line", bf.imports[0]["raw_text"].startswith("# Post 1"), True)
check("records where it came from", bf.imports[0]["source"]["kind"], "wordpress_post")
bf.project_status = "generating"
bf.poll()
check("still generating: nothing published", bf.pub.published, [])
bf.project_status = "done"
bf.poll()
check("publishes with the own-audio guard, tagged by project", bf.pub.published, [(1, True, "proj-1")])
check("marked published", bf.state["posts"]["1"]["status"], "published")
import json as _json
rows = [_json.loads(l) for l in open(doc_watcher.PUBLISH_LOG)]
check("logged as backfill", rows[-1]["matched_by"], "backfill")

bf.poll()      # next: post 2 has its own audio
check("skips a post with its own audio", bf.state["posts"]["2"]["status"], "skipped")
bf.poll()      # post 3 is too short
check("skips a post that's mostly embeds", bf.state["posts"]["3"]["status"], "skipped")
bf.poll()
check("moves on to the next", bf.state["current"], 4)

bf = make()
bf.pub.skip_on_publish = {1}
bf.poll(); bf.poll()
check("narration attached meanwhile: skipped, not overwritten", bf.state["posts"]["1"]["status"], "skipped")
check("nothing written", bf.pub.published, [])

bf = make(status="done (2 of 30 failed)")
bf.poll(); bf.poll()
check("gaps in the audio: not published", (bf.state["posts"]["1"]["status"], bf.pub.published),
      ("failed", []))

bf = make(limit=1)
for _ in range(4):
    bf.poll()
check("stops at the limit", (bf.published_count(), bf.state["current"]), (1, None))

print("\n--- failures are retried spaced out ---")
clock = {"t": 1_000_000.0}
wp_backfill._now = lambda: clock["t"]
wp_backfill.time.time = lambda: clock["t"]

bf = make()
bf.pub.backfill_candidates = lambda cats: [{"ID": 1, "post_title": "Post 1"}]
calls = {"n": 0}
def flaky(pid):
    calls["n"] += 1
    raise WordPressError("ssh timed out")
bf.pub.post_for_narration = flaky
bf.poll()
check("a read failure schedules a retry", (calls["n"], bf.state["posts"]["1"]["status"]), (1, "retry"))
bf.poll(); bf.poll()
check("not retried before the delay", calls["n"], 1)
waits = []
for delay in wp_backfill.RETRY_DELAYS:
    waits.append((bf.state["posts"]["1"]["retry_at"] - clock["t"]) / 60)
    clock["t"] += delay * 60
    bf.poll()
check("waits 5, 15, then 45 minutes", waits, [5, 15, 45])
check("then gives up", (calls["n"], bf.state["posts"]["1"]["status"]),
      (wp_backfill.MAX_ATTEMPTS, "failed"))

bf = make()
bf.pub.backfill_candidates = lambda cats: [{"ID": i, "post_title": f"Post {i}"} for i in (1, 2)]
real_read = bf.pub.post_for_narration
bf.pub.post_for_narration = lambda pid: flaky(pid) if pid == 1 else real_read(pid)
bf.poll(); bf.poll()
check("a post waiting to retry doesn't hold up the next", bf.state["posts"]["2"]["status"], "skipped")

print("\n--- a failed upload is parked, not redone ---")
bf = make(limit=10)
fails = {"left": 2}
real_publish = bf.pub.publish
def flaky_publish(post_id, *a, **k):
    if post_id == 1 and fails["left"]:
        fails["left"] -= 1
        raise WordPressError("ssh command failed (255): Connection reset by peer")
    return real_publish(post_id, *a, **k)
bf.pub.publish = flaky_publish
bf.poll(); bf.poll()                    # narrate post 1; its upload fails
e1 = bf.state["posts"]["1"]
check("upload failure: pending, not failed", e1["status"], "upload_pending")
check("frees the queue for the next post", bf.state["current"], None)
check("first upload retry in 5 min", (e1["retry_at"] - clock["t"]) / 60, 5)
bf.poll()                               # post 2 (own audio) — not yet time for post 1
check("carries on with the next post", bf.state["posts"]["2"]["status"], "skipped")
check("upload not retried early", fails["left"], 1)
clock["t"] += 5 * 60
bf.poll()                               # retry due: fails again, next in 15
check("second upload retry in 15 min", (e1["retry_at"] - clock["t"]) / 60, 15)
bf.busy = True
bf.app_busy = lambda: True
clock["t"] += 15 * 60
bf.poll()
check("no upload retry while the app is busy", e1["status"], "upload_pending")
bf.app_busy = lambda: False
bf.poll()
check("uploaded on the retry", e1["status"], "published")
check("narrated once, never re-imported", [i["source"]["post_id"] for i in bf.imports], [1])
check("published from the original project", bf.pub.published[0], (1, True, "proj-1"))
check("retry_at cleared", "retry_at" in e1, False)

bf = make()
bf.pub.backfill_candidates = lambda cats: [{"ID": 1, "post_title": "Post 1"}]
def always_fail(*a, **k):
    raise WordPressError("down")
bf.pub.publish = always_fail
bf.poll(); bf.poll()
for delay in wp_backfill.UPLOAD_RETRY_DELAYS:
    clock["t"] += delay * 60
    bf.poll()
check("gives up on an upload after ~15 hours of tries",
      (bf.state["posts"]["1"]["status"], bf.state["posts"]["1"]["upload_attempts"]),
      ("failed", len(wp_backfill.UPLOAD_RETRY_DELAYS) + 1))

bf = make()
bf.state["posts"]["7"] = {"status": "upload_pending", "retry_at": 0, "project_id": "proj-7", "title": "Post 7"}
bf.poll()
check("an upload pending from before a restart is picked up", bf.state["posts"]["7"]["status"], "published")

print("\n--- re-narration when a post's text changes ---")
import hashlib
sha = lambda t: hashlib.sha256(t.encode()).hexdigest()
TEXT_V1 = "<p>" + " ".join(["first"] * 200) + "</p>"
TEXT_V2 = "<p>" + " ".join(["second"] * 200) + "</p>"


class RenarratePub(FakePub):
    """Post 9 is narrated (attachment 77) and was edited since."""
    def __init__(self):
        super().__init__()
        self.content = {9: TEXT_V2}
        self.text_hash_meta = {9: "old"}
        self.hashes, self.narrated_calls = [], 0

    def backfill_candidates(self, cats):
        return []

    def post_for_narration(self, pid):
        return {"ok": True, "ID": pid, "post_title": f"Post {pid}", "post_status": "publish",
                "permalink": f"https://example.com/p{pid}/", "own_audio": True, "audio_id": 77,
                "content": self.content[pid], "text_hash_meta": self.text_hash_meta.get(pid, "")}

    def set_narration_hashes(self, posts):
        self.hashes += posts
        return {"written": len(posts), "changed": []}

    def narrated_posts(self, cats, baseline_limit=50):
        self.narrated_calls += 1
        return {"narrated": 1, "baseline": [], "drifted": []}


queue = {"items": [], "acks": []}
def fake_request(method, url, json=None, **kw):
    if method == "GET":
        payload = {"ok": True, "now": clock["t"] * 1000, "items": queue["items"]}
    else:
        queue["acks"].append(json)
        payload = {"ok": True, "result": "done"}
    return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
wp_backfill.requests.request = fake_request


def make_r():
    bf = make(limit=0)
    bf.pub = RenarratePub()
    bf.rcfg = {"enabled": True, "queue_url": "http://q", "queue_key": "k", "quiet_minutes": 10}
    bf._sweep_at = clock["t"]          # no sweep unless a test asks
    queue["acks"].clear()
    return bf


bf = make_r()
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9",
                   "lastSavedAt": (clock["t"] - 120) * 1000}]
bf.poll()
check("waits out the quiet period after the last save", bf.imports, [])
clock["t"] += 8 * 60 + 1
bf._queue_at = 0
bf.poll()
check("then re-narrates, even past the backfill limit", bf.state["current"], 9)
check("imports the new text", "second" in bf.imports[0]["raw_text"], True)
bf.poll()
check("replaces only the narration it saw", bf.pub.publish_kw["expect_audio"], 77)
check("not guarded by own-audio (it has audio)", bf.pub.publish_kw["only_if_no_own_audio"], False)
check("records what it was made from", bf.pub.publish_kw["content_hash"], sha(TEXT_V2))
check("acked with the content it narrated", queue["acks"][-1],
      {"postId": 9, "contentHash": sha(TEXT_V2), "outcome": "renarrated", "note": ""})
e9 = bf.state["posts"]["9"]
check("marked re-narrated, not counted toward the limit",
      (e9["status"], bf.published_count()), ("renarrated", 0))
check("no leftover re-narration fields", [k for k in ("kind", "expect_audio", "prev_status") if k in e9], [])

bf = make_r()
bf.state["posts"]["9"] = {"status": "published", "title": "Post 9"}
bf.pub.text_hash_meta[9] = None
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
bf.poll(); bf.poll()
check("a backfilled post stays counted as published",
      (bf.state["posts"]["9"]["status"], bf.state["posts"]["9"]["renarrations"]), ("published", 1))

bf = make_r()
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
text_now = wp_backfill.post_markdown("Post 9", TEXT_V2, ["Next step"])
bf.pub.text_hash_meta[9] = sha(text_now)
bf.poll()
check("markup-only edit: nothing narrated", bf.imports, [])
check("  hashes updated instead", bf.pub.hashes, [{"ID": 9, "content_hash": sha(TEXT_V2), "text_hash": sha(text_now)}])
check("  acked as unchanged", queue["acks"][-1]["outcome"], "unchanged")

bf = make_r()
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
bf.pub.skip_on_publish = {9}
bf.poll(); bf.poll()
check("audio changed meanwhile: skipped, acked", queue["acks"][-1]["outcome"], "skipped")
check("  current cleared", bf.state["current"], None)

bf = make_r()
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
def boom(pid):
    raise WordPressError("ssh timed out")
bf.pub.post_for_narration = boom
bf.poll()
check("read failure: not acked, backed off", (queue["acks"], 9 in bf._backoff), ([], True))
bf.poll()
check("  not retried inside the backoff", bf.imports, [])

bf = make_r()
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
bf.project_status = "done (1 of 3 failed)"
bf.state["posts"]["9"] = {"status": "published", "title": "Post 9"}
bf.poll(); bf.poll()
check("gaps in the new take: old narration stands",
      (bf.state["posts"]["9"]["status"], queue["acks"][-1]["outcome"], bf.pub.published), ("published", "failed", []))

bf = make_r()
queue["items"] = []
bf._sweep_at = 0
bf.pub.narrated_posts = lambda cats, **kw: {"narrated": 2, "baseline_remaining": 7, "baseline": [
    {"ID": 5, "post_title": "Post 5", "content": TEXT_V1}],
    "drifted": [{"ID": 9, "post_title": "Post 9", "content_hash": sha(TEXT_V2), "modified": clock["t"] - 3600}]}
bf.poll()
check("sweep stamps a baseline, title included",
      bf.pub.hashes, [{"ID": 5, "content_hash": sha(TEXT_V1),
                       "text_hash": sha(wp_backfill.post_markdown("Post 5", TEXT_V1, ["Next step"]))}])
check("sweep queues a drifted post the queue missed", 9 in bf._renarrate, True)
check("more baselines left: next sweep in 5 min, not an hour",
      round(bf._sweep_at + wp_backfill.SWEEP_TTL - clock["t"]), wp_backfill.QUEUE_POLL)
bf.poll()
check("  and it's re-narrated", bf.state["current"], 9)

bf = make_r()
bf._sweep_at = 0
bf.pub.narrated_posts = lambda cats, **kw: {"narrated": 50, "baseline": [], "drifted": [
    {"ID": 100 + i, "post_title": "x", "content_hash": "h", "modified": 0} for i in range(wp_backfill.MAX_DRIFT + 1)]}
bf.poll()
check("a bulk content change queues nothing", bf._renarrate, {})

bf = make_r()
bf.rcfg = {}
queue["items"] = [{"postId": 9, "contentHash": sha(TEXT_V2), "title": "Post 9", "lastSavedAt": 0}]
bf.poll()
check("off unless renarrate.enabled", bf.imports, [])

print("\n--- the app is restarted between posts ---")
bf = make(limit=5)
bf.cfg.update(restart_app_every=1, restart_app_command="true")
restarts = []
real_run = wp_backfill.subprocess.run
wp_backfill.subprocess.run = lambda argv, **kw: restarts.append(argv)
wp_backfill.time.sleep = lambda s: None
wp_backfill.requests.get = lambda *a, **k: types.SimpleNamespace(status_code=200)
bf.poll(); bf.poll()                    # start post 1, publish it
check("no restart before the first post", restarts, [])
bf.poll()                               # restart instead of starting post 2
check("restarts after a published post", restarts, [["true"]])
check("nothing started on the restart poll", bf.state["current"], None)
bf.poll()
check("then carries on with the next post", bf.state["posts"].get("2", {}).get("status"), "skipped")
bf2 = make(limit=5, busy=True)
bf2.cfg.update(restart_app_every=1, restart_app_command="true")
bf2.state["since_restart"] = 1
restarts.clear(); bf2.poll()
check("never restarts while something is generating", restarts, [])
wp_backfill.subprocess.run = real_run

print("\n--- busy comes from the app's live state ---")
def resp(status, payload):
    import requests as _rq
    r = types.SimpleNamespace(status_code=status, json=lambda: payload)
    def rfs():
        if status >= 400:
            raise _rq.HTTPError(response=r)
    r.raise_for_status = rfs
    return r
calls = []
def live_get(url, **kw):
    calls.append(url)
    return resp(200, {"busy": False, "generating": False, "imports": 0})
wp_backfill.requests.get = live_get
check("asks /api/busy", (wp_backfill.app_busy("http://app", {}), calls), (False, ["http://app/api/busy"]))
def old_app_get(url, **kw):
    if url.endswith("/api/busy"):
        return resp(404, {})
    if url.endswith("/api/projects"):
        return resp(200, [{"id": "a"}])
    return resp(200, {"import_status": "generating"})
wp_backfill.requests.get = old_app_get
check("falls back to project status on an older app", wp_backfill.app_busy("http://app", {}), True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
