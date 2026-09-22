# Handoff — Google Doc → TTS → WordPress pipeline

**Status: LIVE as of 2026-09-22; last deployed 15:02 PDT (`24f9776`). First two real docs published end to end at 14:22 and 14:35.** The next Google Doc shared with the
service account will import, generate audio, and write to
`https://deepspirituality.com` unattended. Nothing is in dry-run any more.

Read `DOC_WATCHER.md` for how the thing works. This file is only about the
current state and what to do next.

---

## What was built

Docs shared with the service account already imported and generated audio. This
work added the last step: attaching that audio to the matching WordPress post.

| File | Role |
|------|------|
| `wp_publisher.py` | New. All WordPress access: SSH transport, title matching, media upload, ACF writes. |
| `doc_watcher.py` | Completion pass now does email **and** WordPress. Also reads the "which post?" reply. |
| `test_wp_pipeline.py` | 90 checks over the state machine, reply parsing, email routing, the publish log and filename slugs. All fakes — run it freely: `./venv/bin/python test_wp_pipeline.py` |
| `static/publishes.html` + `/api/wp_publishes` | The **Published** page (`/publishes`, header button): each generated audio, playable, beside the article it went to. Reads the publish log below. |
| `gmail_auth.py` | `--with-replies` adds the `gmail.readonly` scope. |

**Flow:** audio finishes → find the post whose title matches the doc (leading
`[QQT #N]` tag dropped) → one SSH session streams the M4A into the media
library, files it in the FileBird **Audio** folder, sets three ACF fields,
reads them back, purges the post's cache → append to the publish log → email
the sharer "Audio attached".

Emails all go to **whoever shared the doc** (`notify` is only a fallback), and
the audio+shortcode email is the *fallback*, not the announcement:

| Title match | Emails |
|---|---|
| exactly one post | "Audio attached" only |
| near-matches only | "Which post is …?" list; reply a URL → publish, reply `none` → audio email |
| nothing close | audio + shortcode email |
| publish gives up (5 tries) | failure notice + audio email |

**Publish log:** `~/.qwen_tts_studio/wp_publish_log.jsonl` on the mini, one
line per live publish, including what each field held before (for undoing).

---

## Deployed state

**Mac mini** (`ssh mini`, user `peterpgrew`) runs everything.

- **The live checkout is `~/programming/qwen-tts-studio`.** There is a *second*
  clone at `~/programming/digital_team/qwen_tts/qwen-tts-studio` that launchd
  does NOT use. Pull the first one. Deploy:
  ```bash
  ssh mini 'cd ~/programming/qwen-tts-studio && git pull --ff-only && launchctl kickstart -k gui/$(id -u)/com.localtts.docwatcher'
  ```
- Config: `~/.qwen_tts_studio/doc_watcher.json` (a dated `.bak-` backup sits
  beside it). Log: `/tmp/localtts-docwatcher.log`.
- **The config is read once, at startup.** Editing it does nothing until the
  service restarts. Confirm a restart took by looking for the startup line —
  `WordPress hand-off ON — writing live to …` — not by assuming the kickstart
  worked. One `launchctl kickstart` during this session silently no-op'd.

**WordPress:** WP Engine install `dspirituality4`,
`ssh dspirituality4@dspirituality4.ssh.wpengine.net`, wp root
`/home/wpe-user/sites/dspirituality4`. Key `~/.ssh/wpengine_ed25519` is on both
machines; the host key is in both `known_hosts`.

**ACF fields** (all confirmed present on real posts):

| Field | Type | Key | Written as |
|---|---|---|---|
| `devotional_narration` | file | `field_6723eeacc850d` | attachment ID |
| `has_devotional_narration` | select | `field_6723f0356d377` | `"Yes"` |
| `devotional_chapters` | textarea | `field_6a31d2e2c0df7` | chapters JSON |

The Select's real choices are `Select` / `No` / `Yes`. ACF stores the choice
**key**, so it must be `"Yes"` — `"yes"` would store a value the dropdown can't
display, silently.

**Gmail:** the token is `parker.allen21@gmail.com` (Parker's personal Gmail —
not BACC, not Jennifer's), scopes `gmail.send` + `gmail.readonly`. `notify` is
`pallen@bacc.cc`; a reply is accepted from *either* address.

---

## Verified vs not

**Verified against the live install:** SSH + wp-cli 2.12.0 + ACF Pro 6.8.9;
title matching across 1,166 posts (the last ten real doc names all match
their post); and, as of 2026-09-22 12:20, **a real publish end to end** on a
throwaway private post: M4A imported, all three fields written by key with
their `_field` reference rows, chapters containing `’ “ ” \" \\` stored
byte-exact, a second publish reusing the same attachment, WP Engine cache
purged for the post. Post, attachment and file deleted afterwards.

**Real traffic, 2026-09-22:** two shared docs published unattended —
"How to Have a Change of Heart…" → post 10006402 and "[QQT 12] Faith Over
Feelings" → post 10015579. Both checked on the site: narration set, Yes flag,
chapters valid JSON, audio plays. FileBird filing verified on a throwaway
private post (since deleted) and backfilled onto both.

**Still not exercised on real traffic:** the reply loop (faked replies only).

To watch a doc: `ssh mini 'tail -f /tmp/localtts-docwatcher.log'`, or open
the Published page. If anything is wrong, flip `dry_run` to `true` and restart.

---

## Environment gotchas (all measured, all cost time to find)

- **Every WP Engine SSH connection gets its own container.** Two connections a
  second apart report different hostnames and share no `/tmp`. Anything that
  writes a file in one command and reads it in the next must run in ONE
  session. This already broke the upload once — `wp media import` was handed a
  path that no longer existed.
- **The gateway re-parses the command line and strips a level of quoting**, so
  raw PHP snippets arrive as `syntax error near unexpected token '('`. Commands
  are base64-wrapped to survive it. Don't "simplify" that away.
- **Every wp-cli call costs ~30s**, essentially all WordPress bootstrap — a
  direct `$wpdb` query measured 28s against `wp post list`'s 30s for ~1,200
  posts. Optimizing queries buys nothing; only fewer round trips help. A
  publish is one connection / one bootstrap (~45s), plus ~30s for the title
  lookup; it blocks the poll loop while it runs.
- **`update_field()` unslashes.** Core's `update_metadata()` runs
  `wp_unslash()`, so values must be `wp_slash()`ed first or JSON loses its
  backslashes. Don't remove the slash.
- **Posts are created by duplicating the previous one**, so a new draft
  already carries the *previous* post's narration and chapters. Overwriting
  existing values is therefore intended — don't add a "never overwrite" guard.
- On this MacBook, `xcode-select` points at an Xcode whose license isn't
  accepted, which breaks `/usr/bin/git` and `/usr/bin/python3`. Work around it
  with `export DEVELOPER_DIR=/Library/Developer/CommandLineTools`, or have
  Parker run `sudo xcodebuild -license`.

---

## Open items

1. **The reply loop has only handled faked replies.** The first real "which
   post?" email is the first real test of reading a reply.
2. **Docs imported before the `wordpress` block existed have no `wp_status`**
   and are deliberately skipped — no retro-publishing of the back catalogue.
3. **The first two live publishes replaced hand-uploaded narration** —
   post 10006402 (was attachment 10014454) and 10015579 (was 10020103). Both
   old files are still in the media library if either should go back.
4. Considered and declined: reading titles from the ds-backend Firestore
   collections instead of WordPress. Both hold *published posts only*, so the
   draft a QQT doc belongs to would never match, and the saving is ~30s.

## Bugs fixed (context for anything that looks odd)

Session 2 (2026-09-22 afternoon), found by reading the live data and code:

- **Chapters JSON corruption** — `update_field` unslashing (above). Would have
  hit the first live doc: `God\u2019s` → `Godu2019s`.
- **No doc title could ever match** — docs are named `[QQT #96] Title`, posts
  `Title`. The leading bracket tag is now dropped; `&` = "and".
- **Field names failed on never-edited posts** (no `_field` reference row);
  names now resolve through the post's field groups.
- **An SSH timeout escaped the retry cap** → infinite retries against the live
  site. Now counted as an attempt.
- **Retries could duplicate the upload** — attachments are tagged
  `_tts_source` and reused.
- Reply sender was a substring match; now exact parsed addresses.

Session 1:

- Upload and `wp media import` ran in separate SSH connections (the container
  issue above).
- The reply reader could read its own question as the answer.
- A dry run marked the doc `published`, permanently.
- The remote temp prefix leaked into the public audio URL.
- Replies were accepted only from the notify address.
