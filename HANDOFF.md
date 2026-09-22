# Handoff — Google Doc → TTS → WordPress pipeline

**Status: LIVE as of 2026-09-22 11:57 PDT; fixes redeployed 12:25 PDT (`e277870`).** The next Google Doc shared with the
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
| `test_wp_pipeline.py` | 46 checks over the state machine, reply parsing and filename slugs. All fakes — run it freely: `./venv/bin/python test_wp_pipeline.py` |
| `gmail_auth.py` | `--with-replies` adds the `gmail.readonly` scope. |

**Flow:** audio finishes → find the post whose title matches the doc → stream
the M4A into the media library → set three ACF fields → email a confirmation.
If no single post title matches, email the closest ones as tappable links and
park the doc in `awaiting_reply` until a reply names the post.

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

**Still not exercised on real traffic:** the reply loop has only processed
faked replies. And no *shared doc* has gone through yet, so the first one is
still worth watching:

```bash
ssh mini 'tail -f /tmp/localtts-docwatcher.log'
```

Then check the post: audio plays, `has_devotional_narration` = Yes, chapters
intact. If anything is wrong, flip `dry_run` back to `true` and restart.

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

1. **Watch the first shared doc** (above).
2. **Docs imported before the `wordpress` block existed have no `wp_status`**
   and are deliberately skipped — no retro-publishing of the back catalogue.
3. `AGENTS.md` is untracked at the repo root and was not created by this work.
   Left alone. (This file is untracked too.)

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
