# Plan: email review loop + WordPress publishing

**Status: plan only — nothing here is implemented yet.**

Today the pipeline ends when the audio lands in someone's inbox
(`doc_watcher.py` → `send_completion_email`). This plan adds the two steps that
close the loop:

1. **Review by reply** — the recipient listens, replies to that same email with
   structured commands (`redo 27`, `redo 27: <new wording>`), and the Mac mini
   regenerates just those paragraphs and emails a fresh cut back.
2. **Publish by reply** — when they're happy, they reply `publish` and the mini
   uploads the audio to WordPress and sets it on the Deep Spirituality post
   whose title exactly matches the devotional, via its ACF audio field.

Everything stays on the mini. No new always-on service, no webhooks, no inbound
ports — the watcher already polls, so it polls the mailbox too.

---

## 1. Shape of the thing

```
                       ┌──────────────────────────── the mini, every 2 min ─┐
share doc ──► Drive ──►│ doc_watcher.py                                     │
                       │   ├─ poll Drive        (existing)                  │
                       │   ├─ import + generate (existing) ──► main.py       │
                       │   └─ review_loop.tick()          (NEW)             │
                       │        ├─ poll Gmail for replies in tracked threads│
                       │        ├─ parse commands (review_commands.py)      │
                       │        ├─ dispatch:                                │
                       │        │    redo N     ──► POST /api/projects/…/regenerate
                       │        │    publish    ──► wp_publish.py ──► WP REST (ACF)
                       │        │    status/help──► reply only              │
                       │        └─ send follow-up email w/ new manifest     │
                       └────────────────────────────────────────────────────┘

reviewer's inbox:  [1] "Your audio is ready …[TTS-A1B2C3]"   ← full numbered
                    │                                          script + M4A
                    └─ reply "redo 27: try this wording"
                   [2] "Revised: paragraph 27 …[TTS-A1B2C3]" ← new script + M4A
                    └─ reply "publish"
                   [3] "Confirm: post 'Choosing Trust…'"     ← what it matched
                    └─ reply "confirm publish"
                   [4] "Published …[TTS-A1B2C3]"             ← links to the post
```

The subject-line token `[TTS-A1B2C3]` is the correlation key. Gmail's
`threadId` is the primary match; the token is the fallback when someone breaks
threading (forwards it, replies from a different client, changes the subject).

### New files

| File | Role |
|------|------|
| `review_commands.py` | **Pure functions.** Reply body → list of commands. No network, no state. This is where the test weight lives. |
| `mailer.py` | Gmail send **and** read. Extracted from `doc_watcher.py` so both directions share credentials, threading headers, and a pluggable transport (real Gmail / local files, see §7). |
| `review_loop.py` | The state machine. Owns `review_state.json`, decides what to do with each parsed command, composes the follow-up emails. |
| `wp_publish.py` | WordPress REST client: upload media, resolve the post by exact title, set the ACF audio field, verify by read-back. |
| `REVIEW_LOOP.md` | Operator setup guide (Gmail scopes, WP application password, config, rollout switches). |
| `test_review_commands.py`, `test_review_loop.py`, `test_wp_publish.py`, `tests/fake_wp.py` | See §7. |

`doc_watcher.py` keeps only what it has now plus one call into
`review_loop.tick()` per poll. It should not grow into a 1,500-line file.

### Changes to existing files

| File | Change |
|------|--------|
| `main.py` | Two new endpoints (§4): batch paragraph regeneration, and a manifest endpoint that reports per-paragraph durations/offsets. Refactor the per-paragraph generate-and-store body out of `_run_import_generation` so both callers share it. |
| `gmail_auth.py` | Add the Gmail **read** scope. Re-consent required (§6). |
| `text_parser.py` | Extract a `clean_paragraph_text(text, bible_mode)` helper so replacement text emailed in by a reviewer goes through exactly the same cleaning as imported text. Mirror in `script.js` only if the rules change (they shouldn't). |
| `doc_watcher.py` | Send via `mailer`; record `threadId` / `Message-Id` / job token in state; call `review_loop.tick()`. |
| `DOC_WATCHER.md`, `CLAUDE.md` | Cross-link the new doc; add the new files to the file map. |
| `requirements.txt` | Unchanged — `requests` and `google-auth` cover it. (`google-auth-oauthlib` stays a one-time dev install.) |

---

## 2. Making "paragraph 27" mean something

This is the crux. The reviewer hears one merged M4A; the command references an
index. Three things make that reliable:

**a. Every email carries the full script, numbered, with timestamps.** Not
previews — the complete text of every paragraph, exactly as it was fed to the
model. The reviewer reads along while listening, and when something's off they
already have the number *and* the text in front of them.

The merge inserts exactly 1 second of silence between segments
(`main.py:1920`) and all three treatments are duration-preserving (EQ +
`loudnorm`), so segment offsets in the exported file are *exactly* computable:

```
offset(i) = Σ duration(0..i-1) + i × 1.0s
```

New `GET /api/projects/{id}/manifest` returns that. The email body renders it:

```
────────────────────────────────────────────────────────────
 1.  00:00  (1:12)
Choosing Trust When Nothing Is Certain.

 2.  01:13  (0:48)
Have you ever noticed how quickly your mind fills a silence with the worst
version of what might be happening? You are not broken for doing that. It is
what a mind does when it has been hurt before and is trying not to be hurt
again.

…

 27.  09:41  (0:21)
Paul writes that we walk by faith and not by sight, which sounds poetic until
you are the one being asked to take the next step without seeing where it
lands.
────────────────────────────────────────────────────────────
```

Each entry is `index. start (duration)` then the full paragraph text, wrapped at
78 columns. Chapter paragraphs get a `▸` marker so the structure is visible.

Reply `redo 27` and there is no ambiguity about which one that is — they can
scrub to 9:41, read the exact sentence, and confirm. It also makes
`redo 27: <fixed wording>` a copy-paste-and-edit operation instead of retyping
from memory.

**Long scripts:** everything is inlined up to 60 paragraphs / 40 KB of body. Past
that, the email inlines the first 60 and attaches the complete numbered script as
`script.txt` (same format), so a 100-paragraph piece never produces a
Gmail-clipped email where paragraph 80 is invisible. Plain text only — no HTML
alternative — because predictable quoting on the reply side is worth more than
typography here.

**b. Each send snapshots its own index map.** `review_state.json` stores
`{"27": "para-1739…-26", …}` for the manifest that was actually emailed, plus a
hash of it. Commands are resolved against the map from *the most recent email in
that thread*, never against a live re-read of the project. If a `para_id` in the
map no longer exists (someone deleted paragraphs in the app), the command is
**refused with an explanatory reply** — never silently applied to whatever is at
position 27 now.

**c. Out-of-range or unparseable indices bounce.** `redo 0`, `redo 400`,
`redo twenty-seven` → a reply that repeats the manifest and asks again. Nothing
guesses.

*Optional phase-2 nicety:* embed M4A chapter markers (`ffmpeg -i chapters.txt
-map_metadata 1`) so the file itself is scrubable by paragraph in QuickTime /
Apple Podcasts. Needs a `chapters` option on `/api/export`. Not required for v1;
the timestamp list does the job.

---

## 3. The reply command grammar

Deliberately small, line-oriented, case-insensitive. One command per line.

| Command | Effect |
|---|---|
| `redo 27` | Regenerate paragraph 27, same text, new take. Aliases: `rerun`, `regen`, `regenerate`, `again`. |
| `redo 27, 31, 40` / `redo 27-31` | Same, several paragraphs. |
| `redo 27: <new text>` | Replace paragraph 27's text with `<new text>`, then regenerate. Text runs through `text_parser.clean_paragraph_text` with the project's `bibleMode`. |
| `publish` | Start the publish flow. Target resolves from the devotional title automatically (§5) — normally nothing to type. |
| `publish <exact title>` | Override the title to match against. |
| `publish <post URL or id>` | Escape hatch when the title is ambiguous or has been renamed on the site. |
| `confirm publish` | Execute the publish (see §5 — two-step by default). |
| `status` | Reply with the current state and manifest. |
| `help` | Reply with this table. |
| `stop` / `cancel` | Freeze the job. No further replies acted on until re-opened by hand. |

**Multi-line replacement text**: everything after `redo 27:` on that line, plus
any following lines, up to a blank line or the next recognized command. So this
works naturally:

```
redo 27: Paul writes that we walk by faith and not by sight.
That's not a slogan — it's a description of how trust actually feels.

redo 31
publish
```

**Quoted-text stripping** (in `review_commands.py`, before anything else):

- Prefer the `text/plain` MIME part; fall back to HTML→text with
  `<blockquote>` contents removed.
- Cut the body at the first line matching the quote-header patterns:
  `^On .* wrote:$`, `^-{2,}\s*Original Message`, `^From: `, `^_{10,}$`,
  `^\s*Sent from my `.
- Drop lines beginning with `>`, and everything after a `-- ` signature line.
- Only the surviving top portion is scanned for commands.

**Unrecognized lines**: recognized commands still run; unrecognized lines are
quoted back in the acknowledgement email under "I didn't understand these
lines". If **zero** commands were recognized, no action at all — send the help
reply. Exception: a reply containing anything unparsed **and** a `publish` is
treated as publish-with-questions → ask before acting (§5). Regeneration is
cheap and reversible; publishing is neither.

Every accepted reply gets an immediate ack ("Got it — regenerating 27 and 31,
I'll send a new cut in a few minutes") so the reviewer never wonders whether the
mini heard them.

---

## 4. Backend additions (`main.py`)

### `POST /api/projects/{project_id}/regenerate`

```json
{ "items": [ {"para_id": "para-1739-26", "text": "optional replacement"},
             {"para_id": "para-1739-30"} ] }
```

- Validates every `para_id` exists **before** queueing anything (all-or-nothing).
- If `text` is present: clean it via `text_parser`, write it to the paragraph,
  keep the previous value in `para["textHistory"]` (append-only, for the audit
  trail and for a future undo).
- Queues a background job on the **same** `_import_lock` the import generation
  uses, so the MPS serialization guarantee is unchanged and a regeneration
  never races an import.
- Each paragraph gets a **new take**: `take = max(takes or [0]) + 1`, stored as
  `{para_id}-t{take}.flac`, `takes.append(take)`, `activeTake = take`. Old takes
  stay on disk, so the reviewer's earlier version is still selectable in the UI
  and a bad regeneration is one click from being undone.
- Sets `project["regen_status"] = "generating"` → `"done"` /
  `"done (N of M failed)"`. A separate field from `import_status` so neither
  the watcher's existing completion-email logic nor the UI's import badge is
  disturbed.
- Returns `{"queued": [{"para_id":…, "take": 2}, …]}` immediately.

The watcher considers the job finished when `regen_status` is not `"generating"`
**and** every expected `(para_id, take)` is present in the project — belt and
braces, so a crash mid-job can't be read as success.

Remote generation keeps working unchanged: this endpoint calls the same
`/api/generate` that already forwards to `remote_server_url` when configured.

### `GET /api/projects/{project_id}/manifest`

```json
{ "project_id": "…", "name": "…", "regen_status": "done",
  "total_seconds": 612.4, "gap_seconds": 1.0,
  "paragraphs": [ {"index": 1, "para_id": "para-…-0", "is_chapter": true,
                   "active_take": 1, "has_audio": true,
                   "duration": 11.2, "offset": 0.0,
                   "text": "full paragraph text",
                   "preview": "first 70 chars…"}, … ] }
```

Durations come from `soundfile.info()` on each active take's FLAC — no ffmpeg,
no decode. Paragraphs without audio get `duration: null` and are **excluded from
offset accumulation**, matching what `/api/export` actually merges. This is also
useful on its own (a "copy timestamps" button in the UI later).

Both endpoints honor server-mode token auth like every other `/api/*` route.

---

## 5. WordPress publishing (`wp_publish.py`)

Two decisions shape this whole section, and both make it safer than the generic
version: the audio goes into an **ACF field** (so post content is never
touched), and the target post is found by **exact devotional title** (so the
reviewer normally types just `publish`).

**Auth**: WordPress **Application Password** (Users → Profile → Application
Passwords), sent as HTTP Basic over HTTPS. Stored in the watcher config file,
`chmod 600`, never in the repo. A dedicated WP user whose role can edit the
target post type and upload files — not an admin account.

### Step 1 — resolve the post by title

The devotional title is already the project name (`text_parser.derive_title`
takes it from the doc's first line), so `publish` with no argument uses it.

```
GET /wp-json/wp/v2/{type}?search=<title>&status=any&context=edit&per_page=100
```

across each configured post type, then **filter in Python for an exact match on
`title.raw`** — WP's `search` is fuzzy and will happily return ten near-misses.
Matching is normalized before comparison, which matters more than it sounds:

- HTML entities decoded (`&#8217;` → `’`) — WP stores titles entity-encoded
- curly quotes/apostrophes → straight, en/em dashes → hyphen
- collapse whitespace, strip trailing `.` (the parser adds one to the title
  line, WP won't have it), case-insensitive compare

Outcomes:

| Result | Behavior |
|---|---|
| Exactly one match | Proceed to the confirmation email naming it. |
| Two or more exact matches | **Refuse.** Reply listing each candidate — title, status, date, edit link — and ask for `publish <url>`. |
| Zero matches | **Refuse.** Reply with the title it searched for, the closest fuzzy hits as a hint ("did you mean…"), and ask for `publish <url>`. |

Never publish to a guessed post. The host of any URL given must be in
`allowed_hosts`; a reply naming another site is refused and logged.

### Step 2 — upload the audio

`POST /wp-json/wp/v2/media`, body = the M4A bytes, `Content-Disposition:
attachment; filename="<slug>-audio.m4a"`, `Content-Type: audio/mp4`, with
`title` carrying the job token (`Audio — <doc name> [TTS-A1B2C3]`). Returns
`id` and `source_url`.

Before uploading, `GET /wp-json/wp/v2/media?search=TTS-A1B2C3` — if that token
already has media, reuse it. That's what makes a crash mid-publish safe to retry
without littering the media library. On a re-publish after a `redo`, the *new*
audio does need a new upload: the token gets a `-r2` suffix per revision round,
and the previous attachment id is kept in state (deleting it is deliberately not
automatic — WP media deletion is not something this loop should do unattended).

WP rejects uploads over `upload_max_filesize`; on 413, report the server's limit
in the reply rather than retrying.

### Step 3 — set the ACF field

```
POST /wp-json/wp/v2/{type}/{id}
{ "acf": { "<field_name>": 4567 } }
```

The request body contains **only** the `acf` object. No `content`, no `title`,
no `status`, no `date` — so it is structurally incapable of altering the post's
text or making a draft live. That's the strongest safety property in this plan
and should be asserted in a test, not just documented.

The value shape depends on the field type, set by config
`wordpress.acf.value`:

- **File / Audio field** → the attachment **id** (integer). This is the likely
  case and the default.
- **URL or text field** → the `source_url` string.

**Prerequisite on the WP side:** the field group must have **"Show in REST API"**
enabled (ACF 5.11+/6.x native REST support). If it doesn't, options in order of
preference: (a) toggle it on in the field group settings — usually a
five-second fix; (b) register the underlying meta key with `show_in_rest` in a
small mu-plugin and write via `{"meta": {...}}`; (c) the legacy
`acf-to-rest-api` plugin at `/wp-json/acf/v3/{type}/{id}`. `wp_publish.py`
should probe this once at startup (`GET` a known post, check for an `acf` key)
and fail loudly with the specific remedy rather than silently writing nothing —
a `POST` with an unrecognized `acf` key returns **200** and ignores it, which is
exactly the kind of quiet no-op that wastes an afternoon.

### Step 4 — verify by read-back

`GET …/{type}/{id}?context=edit` and assert `acf.<field>` now equals what was
written. Only then is the job `published`. This is why the ACF route needs no
sentinel scheme: setting the same field twice is the same result, so publishing
is naturally idempotent, and a write that silently didn't take is *detected*
rather than assumed.

The previous field value is recorded in state (`acf_value_before`) before the
write, so a wrong-post publish can be reverted by hand from the audit log.

### Step 5 — reply

The public permalink, the edit-screen link, the media URL, the post's current
status (unchanged, and say so explicitly — "this post is still a draft; adding
audio didn't change that"), and a note that `redo N` still works and
re-publishing replaces the field rather than adding a second file.

**"Publish" never makes the post live.** It attaches audio. There is no config
flag for status changes and none should be added — if a post needs to go live, a
human does that in WordPress.

### Two-step confirmation

Default on (`require_confirm: true`): `publish` replies with exactly what will
happen — matched post title, current status, whether the ACF field is already
populated (and with what), the file about to be uploaded — and asks for
`confirm publish`. Only then does anything touch the live site. With 8–9
reviewers able to trigger this, the confirmation step is also the record of who
approved it.

---

## 6. Reading the mailbox safely

### Scopes and re-consent

`gmail_auth.py` currently requests `gmail.send` only. Reading replies needs
`https://www.googleapis.com/auth/gmail.readonly` as well; add
`gmail.modify` too if we want to label processed messages (`modify` covers
labels; keep `send` explicitly since `modify` does not imply it).

Adding a scope **invalidates the existing token** — the operator must re-run
`gmail_auth.py` and copy the new token to the mini. The watcher must detect an
insufficient-scope 403 and log *"Gmail token lacks read scope — re-run
gmail_auth.py (see REVIEW_LOOP.md)"* rather than crashing the poll loop.

### Poll and match

```
GET users/me/messages?q=in:inbox newer_than:30d -from:me
  → for each id not in state.processed_message_ids:
      GET users/me/messages/{id}?format=full
      match by threadId, else by [TTS-XXXXXX] in Subject,
      else by In-Reply-To / References vs stored Message-Ids
      → no match: ignore, do not record (it's just mail)
```

Processed message ids are recorded in state (works with `readonly` alone); if
`modify` is granted, also apply a `TTS/Processed` label so the mailbox is
legible to a human. Recording happens **before** dispatch for
non-idempotent actions and the record includes the outcome — a command that
crashes mid-flight is marked `errored`, not retried into a loop.

### Guards — this is untrusted input arriving over email

| Risk | Guard |
|---|---|
| Anyone can email the from-address | **Sender allowlist is the only gate**: `review.allowed_senders`, the 8–9 people on the team. Anything else is ignored, logged, and gets **no reply** — no oracle for outsiders. Compared case-insensitively on the parsed `From` address, ignoring display name and `+tag` suffixes. |
| Someone on the team replies to a job they didn't start | **Allowed** — any allowlisted reviewer can act on any job, which is what a team of nine actually needs. But the actor's address is recorded on every action in the audit log and named in the confirmation and result emails ("published by jennifer@…"), and the original sharer is CC'd on those, so it's visible rather than silent. |
| From-header spoofing | Require `dkim=pass`/`spf=pass` for the sender's domain in `Authentication-Results` (Gmail's own header on delivery). Reject if the header is absent when `require_auth_pass` is on. |
| Our own BCC copy lands in the same inbox | Filter `-from:me`, and skip any message carrying our `X-TTS-Job` header. |
| Auto-replies / vacation responders / mailing lists | Skip `Auto-Submitted: auto-*`, `X-Autoreply`, `Precedence: bulk|list`, and any subject starting `Auto:`/`Out of office`. |
| Mail loop (our reply triggers a reply triggers…) | Hard caps per job: 20 outbound emails, 50 regenerations, 5 publishes. Cap hit → one final "this thread is capped, open the app" email, job → `capped`. |
| Prompt-ish / injection content in the body | **There is no model anywhere in this loop, by design.** Commands are matched by regex against a closed grammar; a body full of instructions is just unrecognized lines. Replacement text is treated as text only — cleaned by `text_parser`, handed to the TTS model as words to say, never interpreted as intent. Nothing in this pipeline asks an LLM what a reply means. |
| Reply asks to publish somewhere else | `allowed_hosts` check + two-step confirm. |
| Very large replies | Cap the parsed body at 64 KB and replacement text at `MAX_GENERATE_TEXT_CHARS` (the existing `/api/generate` limit), reply with the limit if exceeded. |

---

## 7. State, bookkeeping, and audit

`doc_watcher_state.json` stays what it is (Drive dedupe: "have I imported this
doc"). Review/publish state goes in a **new file**, `review_state.json`, keyed
by job token, so the two concerns stay separable and the existing file's
migration risk is zero.

```json
{
  "TTS-A1B2C3": {
    "doc_id": "1AbC…", "doc_url": "https://docs.google.com/…",
    "project_id": "3f2a…", "doc_name": "Choosing Trust",
    "reviewer_email": "jennifer@…", "reviewer_name": "Jennifer",
    "state": "awaiting_review",
    "thread_id": "18f2c…",
    "sent": [ {"n": 1, "kind": "ready", "message_id": "<CA…@mail.gmail.com>",
               "at": "2026-07-27T09:02:11-07:00",
               "manifest_hash": "9c1f…",
               "index_map": {"1": "para-…-0", "2": "para-…-1"}} ],
    "received": [ {"message_id": "<CB…>", "at": "…",
                   "commands": [{"op":"redo","index":27,"text":"…"}],
                   "outcome": "queued"} ],
    "regen_jobs": [ {"items": [{"para_id":"para-…-26","take":2}],
                     "started": "…", "finished": "…", "failures": 0} ],
    "publish": { "matched_title": "Choosing Trust When Nothing Is Certain",
                 "target": {"host":"deepspirituality.com","type":"posts","id":12345,
                            "status":"draft","permalink":"https://…"},
                 "media_id": 4567, "media_url": "https://…/audio.m4a",
                 "acf_field": "devotional_audio",
                 "acf_value_before": null, "acf_value_after": 4567,
                 "requested_by": "jennifer@example.com",
                 "confirmed_by": "jennifer@example.com",
                 "confirmed_at": "…", "published_at": "…", "attempts": 1 },
    "emails_sent": 2, "regen_count": 3, "publishes": 1,
    "last_activity": "…"
  }
}
```

**State machine:**

```
imported ──► generating ──► awaiting_review ──┬─► revising ──► awaiting_review
                                              │      (loops, capped)
                                              ├─► publish_requested ──► awaiting_confirm
                                              │                              │
                                              │                              ▼
                                              │                          publishing ──► published
                                              ├─► cancelled  (reply "stop")
                                              ├─► capped     (limit hit)
                                              ├─► expired    (no reply, see below)
                                              └─► failed     (unrecoverable, operator notified)
```

`expired`: no reply for `nudge_after_days` (default 3) → one nudge email; no
reply for `expire_after_days` (default 14) → job closed, thread untracked.
Prevents an inbox of half-finished jobs from being polled forever.

**Audit log**: append-only `~/.qwen_tts_studio/review_log.jsonl`, one JSON
object per event — `email_sent`, `reply_received`, `command_parsed`,
`command_refused`, `regen_started`, `regen_finished`, `publish_attempted`,
`publish_succeeded`, `sender_rejected`. Includes the job token and message id.
This is the "keeping track of what emails are sent or replies" requirement: one
grep-able file that answers *what did the mini do and why*, independent of the
mutable state file. Rotate at 10 MB.

All state writes use the existing atomic tmp-then-`os.replace` pattern.

**Known divergence to accept and document**: editing a paragraph's text via
`redo 27: …` changes `paragraphs[27].text` but not `project["rawText"]` (the
original doc Markdown). Hitting **Parse** in the UI would revert it. Fix is out
of scope for v1 — record the edit in `textHistory` + the audit log, and say so
in `REVIEW_LOOP.md`. (Proper fix later: have the UI warn when
`textHistory` is non-empty, or reconstruct `rawText` from paragraphs.)

---

## 8. Testing before this goes anywhere near the live site

The whole point of the design below is that **every layer can be tested without
Gmail, without WordPress, and without the TTS model.**

### 8.1 Unit tests — no I/O

- **`test_review_commands.py`** (the big one; the parser is where bugs are
  cheapest to catch and most dangerous to miss). Table-driven, with fixture
  `.eml`/text bodies from real clients:
  - every command form and alias, singular / list / range
  - `redo 27:` with same-line text, multi-line text, text containing a colon,
    text containing something that looks like another command
  - Gmail quoted reply, Apple Mail quoted reply, Outlook `-----Original
    Message-----`, "Sent from my iPhone", HTML-only reply, `-- ` signature
  - adversarial: `redo 0`, `redo 400`, `redo -3`, `redo twenty seven`,
    `publish` with no target, `publish evil.example.com/x`, empty body,
    64 KB body, unicode en-dash ranges (`27–31`), commands inside quoted text
    (must be ignored — a reviewer forwarding an old email must not re-trigger)
  - **assert nothing is returned that wasn't asked for**: the parser's contract
    is "recognized commands only", and the test suite should include a fuzz pass
    (random text → parse must never raise and never yield a `publish`).
- **`test_manifest.py`** — write fixture FLACs of known length with
  `soundfile` (silence; no model needed), assert `offset(i) = Σdur + i×1.0`,
  assert paragraphs with no audio are skipped, assert the manifest total matches
  the byte length of an actual `/api/export` WAV. This is the test that proves
  "9:41" points at paragraph 27.
- **`test_wp_publish.py`** — against the fake WP (below):
  - **title resolution** is the bug farm, so it gets the most cases: exact
    match; entity-encoded title (`&#8217;`); curly vs straight apostrophe;
    en-dash vs hyphen; trailing period from the parser; case difference;
    leading/trailing whitespace; fuzzy `search` returning 10 near-misses (must
    pick the one exact match, or refuse); two exact matches (must refuse and
    list); zero matches (must refuse with hints); title longer than WP's search
    behaves well with; two post types where both have a same-titled post
  - **the request body contains only `acf`** — assert `content`, `title`,
    `status`, `date`, `slug` are absent from every write. This is the test that
    guarantees publishing can't alter the post or make a draft live.
  - ACF field missing / REST not enabled → the 200-but-ignored case must be
    caught by read-back verification and reported, not silently "succeed"
  - value shape for `media_id` vs `url` config
  - re-publish sets the same field again (idempotent), `acf_value_before`
    captured, media dedupe by job token, `-r2` token on a post-`redo` republish
  - host allowlist rejection
- **`test_review_loop.py`** — the state machine with mailer and WP stubbed:
  every transition, cap enforcement, expiry, duplicate reply (same message id
  twice → one action), two replies racing in one poll (processed in date order),
  stale `index_map`, crash-and-resume (kill between "queued" and "done" → no
  double regeneration, no double publish).

### 8.2 A local, offline harness for the whole loop

- **File-backed mail transport.** `mailer.py` takes a transport;
  `--mail-transport=file` writes outgoing messages as `.eml` into
  `~/.qwen_tts_studio/outbox/` and reads "replies" from `inbox/*.eml`. To test,
  drop a file in `inbox/`. This is the single highest-value item in this plan:
  the full loop — manifest, ack, regeneration, second manifest, publish — can be
  rehearsed end to end with zero Google involvement, and it's what CI runs.
- **`tests/fake_wp.py`** — a ~120-line FastAPI app implementing exactly the calls
  we make (`GET/POST media`, `GET {type}?search=`, `GET/POST {type}/{id}` with
  `context=edit` semantics and Basic auth), storing posts in memory with an
  `acf` dict. It should faithfully reproduce WP's two nastiest behaviors:
  **fuzzy `search`** (substring match, returns near-misses) and
  **entity-encoded titles**, plus the **200-but-ignored** response for an
  unknown `acf` key. A fake that's nicer than the real thing tests nothing.
- **`review_loop_sim.py`** — dev script that boots a real app instance, imports
  a short fixture doc with `generate: false` (so no model), fakes audio by
  writing short FLACs into the project's audio dir, then drives:
  ready-email → `redo 3: new text` → verify take 2 + text history → `publish`
  (title auto-resolved against the fake WP) → `confirm publish` → verify the
  fake post's ACF field and that its content/status are byte-identical →
  `publish` again → verify idempotence. One command, exits non-zero on any
  mismatch.

### 8.3 Failure injection (part of `test_review_loop.py`)

Each of these must produce a *clear reply or log line and a recoverable state*,
never a crash or a silent stall: Gmail 401 (expired/revoked token) · Gmail 403
(missing scope) · Gmail 429 · app offline (connection refused) · project deleted
mid-review · regeneration fails for one of three paragraphs (email must say
which) · WP 401 (bad app password) · WP 403 (user can't edit that post type or
upload) · WP 404 (post deleted between resolve and confirm) · WP 413 (file too
large) · WP 200-but-`acf`-ignored · WP times out *after* the write landed (retry
must read back, find the field already set, and report success once) · post
retitled between `publish` and `confirm publish` (must re-resolve at confirm
time and refuse if it no longer matches) · disk full on state write.

### 8.4 Staged rollout on the real thing

Config switch `review.mode`, walked forward one step at a time:

1. **`observe`** — poll replies, parse, log intended actions, send *nothing*, do
   *nothing*. Run for a few real docs. Read the audit log: did it parse what
   Jennifer actually wrote? This catches the real-world reply styles no fixture
   anticipates, at zero risk.
2. **`echo`** — reply "I understood: regenerate 27, 31" but still take no
   action. Confirms the reviewer's mental model matches the parser's.
3. **`regenerate`** — regeneration live, publishing still refused with "not
   enabled yet". The loop is now genuinely useful; the live site is untouched.
4. **`publish_staging`** — publishing enabled, pointed at a **WP Engine staging
   environment** with a real copy of the site's ACF field group. Verify by hand:
   the field is populated, the media library entry looks right, the front end
   renders the player, the post's content and status are untouched, and a second
   `publish` changes nothing. Also confirm a real devotional title from the live
   content resolves to exactly one post — that's the assumption most likely to
   be wrong at scale.
5. **`publish`** — production, `require_confirm: true`, `allowed_hosts` set to
   exactly the real host. First real run supervised: watch
   `tail -f /tmp/doc_watcher.log` while replying, and have the WP revision
   history open to confirm the post shows no content revision at all.

`review.enabled` defaults to **false**. With it off or the `review` block
absent, `doc_watcher.py` behaves exactly as it does today — that's an explicit
regression test (`test_review_loop.py::test_disabled_is_noop`).

### 8.5 Manual checklist (goes in `REVIEW_LOOP.md`, TESTING.md style)

- [ ] Share a doc → ready email arrives with M4A + full numbered script
- [ ] Every paragraph's complete text is in the email, in order, none truncated
- [ ] A 100-paragraph doc doesn't get Gmail-clipped (inline cap + `script.txt`)
- [ ] Timestamps in the script match where the audio actually is
- [ ] Reply `redo 3` → ack within one poll; new cut within ~a few minutes
- [ ] Reply `redo 3: <new wording>` → new cut says the new words; take 1 still
      selectable in the app; old text in `textHistory`
- [ ] Reply `redo 99` → refusal + manifest repeated, nothing regenerated
- [ ] Reply from a non-allowlisted address → ignored, logged, no reply sent
- [ ] Reply `status` / `help` → sensible replies
- [ ] Reply `publish` → matches the right post by title, confirmation email
      names it with status + edit link
- [ ] Reply `confirm publish` → media uploaded, ACF field set, links replied
- [ ] The post's content and status are provably unchanged (no new WP revision)
- [ ] Reply `publish` again → field re-set, no duplicate file on the post
- [ ] A title with a curly apostrophe resolves correctly
- [ ] A title that matches two posts → refusal listing both, nothing published
- [ ] A title that matches nothing → refusal with hints, nothing published
- [ ] Reply from reviewer B on reviewer A's job → works, and A is CC'd on the
      result with B named as the actor
- [ ] Reply `stop` → job frozen; further replies ignored with one notice
- [ ] Kill the watcher mid-regeneration and mid-publish → restart resumes
      cleanly, no duplicate work
- [ ] With `review.enabled: false`, the old pipeline is byte-for-byte unchanged

---

## 9. Config (all new keys, `doc_watcher.json`)

```json
{
  "review": {
    "enabled": true,
    "mode": "observe",
    "allowed_senders": ["jennifer@example.com", "pallen@bacc.cc", "…the 8–9 reviewers"],
    "require_auth_pass": true,
    "poll_query": "in:inbox newer_than:30d -from:me",
    "require_confirm": true,
    "max_emails_per_job": 20,
    "max_regens_per_job": 50,
    "nudge_after_days": 3,
    "expire_after_days": 14
  },
  "wordpress": {
    "base_url": "https://deepspirituality.com",
    "allowed_hosts": ["deepspirituality.com", "www.deepspirituality.com"],
    "username": "tts-bot",
    "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx",
    "post_types": ["posts"],
    "acf": { "field": "devotional_audio", "value": "media_id" },
    "dry_run": false
  }
}
```

Secrets live only in this file (`chmod 600`) — same rule as `SERVER.md`. Nothing
new goes in the repo, and none of it is bundled into the `.app` (this whole
pipeline is watcher-side, like `doc_watcher.py` itself).

---

## 10. Settled, and still open

**Settled:**

- Audio goes into an **ACF field** on a Deep Spirituality post. Post content is
  never written.
- **`publish` never makes a post live.** No config flag for it.
- Target post is found by **exact devotional title**, with URL/id as an escape
  hatch.
- **Allowlist of the 8–9 reviewers is the only sender gate**; any of them can act
  on any job, with the actor recorded.
- **No model in the loop**, anywhere, ever.
- The ready email carries the **full numbered script**, not previews.

**Still open — small, but needed before step 6:**

1. **The ACF field's name and type.** `devotional_audio` is a placeholder. If
   it's a File/Audio field the value is the attachment id; if it's a URL/text
   field it's `source_url`. One look at the field group settings answers both,
   along with whether "Show in REST API" is already on.
2. **Post type.** Assumed the standard `post`. If devotionals are a CPT, it's
   one config value (`post_types`).
3. **Are devotional titles unique on the site?** The whole title-matching design
   rests on this. Worth a quick check for repeats across the archive — if
   duplicates exist (e.g. an annual series reusing titles), the refusal path
   handles it safely, but it'd be worth adding date-window narrowing
   (`after=<published this year>`) to cut the ambiguity.
4. **Should the app UI show review state?** An "Awaiting review / Published"
   badge on the project card would help, but it means the app reads
   `review_state.json`. Deferred; easy to add later.
5. **Multiple docs in flight in one thread?** Assumed no — one job per doc per
   thread. Replies that match two jobs are refused with a request to reply on
   the right email.

---

## 11. Suggested build order

Each step is independently useful and independently testable; nothing touches
the live site until step 6.

1. **Manifest** — `GET /api/projects/{id}/manifest` + `test_manifest.py`, and
   wire the full numbered script into the *existing* completion email. This is
   worth shipping on its own: reviewers get "paragraph 27 needs work" language to
   use in Slack today, before any of the reply machinery exists.
2. **Regenerate endpoint** — `POST /api/projects/{id}/regenerate`, refactor of
   the shared per-paragraph generation body, `regen_status`, takes/`textHistory`.
   Exercisable by `curl` alone.
3. **`mailer.py`** — extract sending from `doc_watcher.py`, add the file
   transport, add reading. Ship with `review.enabled: false`; nothing changes
   behaviorally. Re-consent for the read scope.
4. **`review_commands.py`** + its test suite. Pure functions, no wiring.
5. **`review_loop.py`** in `observe` → `echo` → `regenerate` modes, with
   `review_state.json`, the audit log, and `review_loop_sim.py`. The loop is now
   fully useful without WordPress.
6. **`wp_publish.py`** + `tests/fake_wp.py` — title resolution first (it's
   testable against the real site read-only, with no write capability in the app
   password at all), then media upload, then the ACF write on staging, then
   production behind `require_confirm`.
7. **Docs** — `REVIEW_LOOP.md`, cross-links from `DOC_WATCHER.md`, file-map rows
   in `CLAUDE.md`.
