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
                only_if_no_own_audio=False):
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

bf = make()
calls = {"n": 0}
def flaky(pid):
    calls["n"] += 1
    raise WordPressError("ssh timed out")
bf.pub.post_for_narration = flaky
for _ in range(wp_backfill.MAX_ATTEMPTS):
    bf.poll()
check("read failures retry, then give up", (calls["n"], bf.state["posts"]["1"]["status"]),
      (wp_backfill.MAX_ATTEMPTS, "failed"))

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

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
