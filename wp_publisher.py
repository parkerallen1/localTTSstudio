"""
WordPress publisher — attach finished TTS audio to a post via SSH + WP-CLI.

Used by doc_watcher.py once a Google Doc's audio has finished generating:
find the post whose title matches the doc, upload the M4A into the media
library, and set the ACF fields that point the post at it.

Everything runs over SSH against the WP Engine install's wp-cli. Two things
are worth knowing about how it writes:

  • ACF fields are set with ACF's own update_field() (via `wp eval-file`),
    NOT `wp post meta update`. The plain meta write stores the value but skips
    the "_fieldname" -> field-key mapping row ACF needs, which leaves the
    field blank in the post editor if it had never been set before.
  • The M4A is streamed over the SSH connection to a temp path on the server
    and imported by the same PHP run that sets the fields, so it never goes
    through PHP's upload-size limit and a publish is a single transaction —
    see _PUBLISH_PHP for why each step is shaped the way it is.

Title matching is deliberately fuzzy-tolerant: Docs titles and post titles
drift (curly vs straight quotes, en dashes, trailing " - Part 2", casing), so
titles are normalized on both sides before comparing. A leading bracketed
tag on the doc name ("[QQT #96] Fully Known. Fully Loved.") is a filing label
the post never carries, so it's dropped too. A single normalized
match is treated as THE post; anything else returns ranked candidates and the
caller asks a human which one it is.

Nothing here changes post_status — it only ever sets the configured fields on
a post that already exists.

Config lives in the "wordpress" block of doc_watcher.json; see DOC_WATCHER.md.
"""
import base64
import difflib
import gzip
import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime

# wp-cli prints deprecation notices and PHP warnings on stdout, so the helper
# fences its JSON and we pull it back out from between these markers.
_JSON_OPEN = "<<<TTSJSON>>>"
_JSON_CLOSE = "<<<TTSEND>>>"

# A normalized title this similar to the doc's is worth offering as a "did you
# mean" candidate; below it, the post is almost certainly unrelated.
CANDIDATE_THRESHOLD = 0.62

# The whole publish, run by `wp eval-file` in ONE WordPress bootstrap:
# import the audio, point the fields at it, read them back, purge the post's
# cache. argv: [0] base64 JSON payload, [1] path of the streamed audio.
#
# Why it is shaped like this:
#   • Values are wp_slash()ed. update_field() ends in update_metadata(), which
#     wp_unslash()es — it expects slashed $_POST data. Unslashed, the chapters
#     JSON loses every backslash: "’" becomes "u2019" and an escaped \"
#     ends the string early. Every field is read back and compared to prove it.
#   • Fields are written by field KEY. A name only resolves through the post's
#     own "_name" reference rows or a field-group lookup; the key always does.
#   • Anything that fails after the first write puts the old values back, so a
#     post is never left half-updated (e.g. new audio, old chapters).
#   • The attachment is tagged with the doc's id. If an earlier attempt timed
#     out after importing, the retry reuses that attachment instead of adding a
#     duplicate to the media library.
_PUBLISH_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$file    = isset( $args[1] ) ? $args[1] : '';
$post_id = (int) $payload['post_id'];
$dry_run = ! empty( $payload['dry_run'] );
$out     = array( 'ok' => false );

function tts_done( $out ) {
    echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
    exit( 0 );
}
// A field NAME resolves through the post's "_name" reference row, which only
// exists once the post has been saved in the editor (or copied from one that
// was). Fall back to the field groups that apply to this post — the same
// lookup the editor does — so a never-edited post still works.
function tts_field_key( $selector, $post_id ) {
    if ( acf_is_field_key( $selector ) ) {
        $field = acf_get_field( $selector );
        return $field ? $field['key'] : null;
    }
    $field = acf_maybe_get_field( $selector, $post_id );
    if ( $field && ! empty( $field['key'] ) ) {
        return $field['key'];
    }
    $groups = acf_get_field_groups( array( 'post_id' => $post_id, 'post_type' => get_post_type( $post_id ) ) );
    foreach ( $groups as $group ) {
        foreach ( (array) acf_get_fields( $group ) as $field ) {
            if ( $field['name'] === $selector ) {
                return $field['key'];
            }
        }
    }
    return null;
}
function tts_raw( $key, $post_id ) {
    $v = get_field( $key, $post_id, false );
    return is_scalar( $v ) || is_null( $v ) ? $v : wp_json_encode( $v );
}

if ( ! function_exists( 'update_field' ) ) {
    $out['error'] = 'ACF is not active on this install (update_field missing)';
    tts_done( $out );
}
if ( ! ( $post = get_post( $post_id ) ) ) {
    $out['error'] = "post $post_id not found";
    tts_done( $out );
}

// Resolve every selector BEFORE touching anything: a typo'd field should fail
// the publish, not leave an orphaned upload behind it.
$selectors = array_merge( array( $payload['audio_field'] ), array_keys( (array) $payload['fields'] ) );
$keys = array();
foreach ( $selectors as $selector ) {
    $key = tts_field_key( $selector, $post_id );
    if ( ! $key ) {
        $out['error'] = "ACF field '$selector' isn't in any field group on post $post_id";
        tts_done( $out );
    }
    $keys[ $selector ] = $key;
}

// The backfill only fills posts that have no narration of their own. Checked
// here, in the same run as the write, so a narration someone attached while
// the audio was generating is never replaced. (A narration pointing at ANOTHER
// post's attachment — what duplicating a post leaves behind — doesn't count.)
if ( ! empty( $payload['only_if_no_own_audio'] ) ) {
    $current = (int) get_field( $keys[ $payload['audio_field'] ], $post_id, false );
    if ( $current && ( $att = get_post( $current ) ) && (int) $att->post_parent === $post_id ) {
        $out['skipped'] = "the post already has its own narration (attachment $current)";
        tts_done( $out );
    }
}
// A re-narration replaces the post's narration, but only the one it was made
// to replace: if someone changed the audio while the new take was generating,
// theirs stays.
if ( isset( $payload['expect_audio'] ) && null !== $payload['expect_audio'] ) {
    $current = (int) get_field( $keys[ $payload['audio_field'] ], $post_id, false );
    $own     = $current && ( $att = get_post( $current ) ) && (int) $att->post_parent === $post_id;
    if ( ( $own ? $current : 0 ) !== (int) $payload['expect_audio'] ) {
        $out['skipped'] = "the post's narration changed while the new one was generating"
            . " (expected attachment {$payload['expect_audio']}, found " . ( $own ? $current : 'none' ) . ')';
        tts_done( $out );
    }
}

// ---- the audio -------------------------------------------------------------
$attachment_id = 0;
$reused        = false;
if ( ! $dry_run ) {
    $existing = get_posts( array(
        'post_type'   => 'attachment',
        'post_status' => 'inherit',
        'post_parent' => $post_id,
        'meta_key'    => '_tts_source',
        'meta_value'  => (string) $payload['source'],
        'fields'      => 'ids',
        'numberposts' => 1,
    ) );
    if ( $payload['source'] !== '' && $existing ) {
        $attachment_id = (int) $existing[0];
        $reused        = true;
    } else {
        require_once ABSPATH . 'wp-admin/includes/file.php';
        require_once ABSPATH . 'wp-admin/includes/media.php';
        require_once ABSPATH . 'wp-admin/includes/image.php';
        if ( ! $file || ! is_file( $file ) || ! filesize( $file ) ) {
            $out['error'] = 'the audio did not arrive on the server';
            tts_done( $out );
        }
        $id = media_handle_sideload(
            array( 'name' => $payload['filename'], 'tmp_name' => $file ),
            $post_id, $payload['title'] );
        if ( is_wp_error( $id ) ) {
            $out['error'] = 'media import failed: ' . $id->get_error_message();
            tts_done( $out );
        }
        $attachment_id = (int) $id;
        if ( $payload['source'] !== '' ) {
            update_post_meta( $attachment_id, '_tts_source', (string) $payload['source'] );
        }
    }
}
// FileBird folders are virtual (a table row, not a directory), so this only
// files the upload in the media library's folder view — the URL is unchanged.
// "Audio" or "Audio/Voices"; a missing folder or plugin is reported, not fatal.
$folder = 'not requested';
if ( ! $dry_run && $attachment_id && ! empty( $payload['media_folder'] ) ) {
    global $wpdb;
    $folder_id = 0;
    foreach ( explode( '/', trim( $payload['media_folder'], '/' ) ) as $part ) {
        $folder_id = (int) $wpdb->get_var( $wpdb->prepare(
            "SELECT id FROM {$wpdb->prefix}fbv WHERE name = %s AND parent = %d LIMIT 1",
            trim( $part ), $folder_id ) );
        if ( ! $folder_id ) break;
    }
    if ( ! class_exists( '\\FileBird\\Model\\Folder' ) ) {
        $folder = 'FileBird is not active';
    } elseif ( ! $folder_id ) {
        $folder = "no FileBird folder named '{$payload['media_folder']}'";
    } else {
        \FileBird\Model\Folder::setFoldersForPosts( $attachment_id, $folder_id );
        $folder = 'filed';
    }
}
$audio_value = $dry_run ? '(dry run)'
    : ( $payload['audio_format'] === 'url' ? wp_get_attachment_url( $attachment_id ) : $attachment_id );

// ---- the fields ------------------------------------------------------------
$values = array( $payload['audio_field'] => $audio_value ) + (array) $payload['fields'];
$before = array();
foreach ( $values as $selector => $value ) {
    $before[ $selector ] = tts_raw( $keys[ $selector ], $post_id );
}
$problem = null;
if ( ! $dry_run ) {
    foreach ( $values as $selector => $value ) {
        update_field( $keys[ $selector ], wp_slash( $value ), $post_id );
    }
    wp_cache_delete( $post_id, 'post_meta' );
    foreach ( $values as $selector => $value ) {
        $stored = tts_raw( $keys[ $selector ], $post_id );
        if ( (string) $stored !== (string) $value ) {
            $problem = "$selector read back differently from what was written";
            break;
        }
    }
    if ( $problem ) {
        foreach ( $before as $selector => $old ) {
            if ( $old === null || $old === '' ) {
                delete_field( $keys[ $selector ], $post_id );
            } else {
                update_field( $keys[ $selector ], wp_slash( $old ), $post_id );
            }
        }
        $out['error'] = "$problem — restored the previous values";
        $out['attachment_id'] = $attachment_id;
        tts_done( $out );
    }
}

// What this narration was made from, for ds-tts-sync.php and the backfill to
// tell when the post's text has changed since. The content hash defaults to
// the post as it is now; a text hash is only known when the caller converted
// the post itself (the backfill), so a stale one is removed rather than kept.
if ( ! $dry_run ) {
    $content_hash = ! empty( $payload['content_hash'] ) ? (string) $payload['content_hash']
        : hash( 'sha256', $post->post_content );
    update_post_meta( $post_id, '_tts_content_hash', $content_hash );
    if ( ! empty( $payload['text_hash'] ) ) {
        update_post_meta( $post_id, '_tts_text_hash', (string) $payload['text_hash'] );
    } else {
        delete_post_meta( $post_id, '_tts_text_hash' );
    }
}

$applied = array();
foreach ( $values as $selector => $value ) {
    $applied[ $selector ] = array( 'before' => $before[ $selector ], 'after' => $value );
}

// ---- the public page -------------------------------------------------------
$cache = 'skipped';
if ( ! $dry_run && ! empty( $payload['purge_cache'] ) ) {
    clean_post_cache( $post_id );
    if ( class_exists( 'WpeCommon' ) && method_exists( 'WpeCommon', 'purge_varnish_cache' ) ) {
        WpeCommon::purge_varnish_cache( $post_id );
        $cache = 'purged';
    } else {
        $cache = 'no WP Engine cache API — object cache cleared only';
    }
}

$out = array(
    'ok'            => true,
    'applied'       => $applied,
    'attachment_id' => $attachment_id,
    'attachment_reused' => $reused,
    'audio_url'     => $attachment_id ? wp_get_attachment_url( $attachment_id ) : null,
    'cache'         => $cache,
    'folder'        => $folder,
    'post_title'    => $post->post_title,
    'post_status'   => $post->post_status,
    'permalink'     => get_permalink( $post_id ),
    'edit_link'     => admin_url( 'post.php?post=' . $post_id . '&action=edit' ),
);
tts_done( $out );
"""

# Reports what the configured ACF fields actually are, so a setup can be
# checked before a doc is trusted to it. A Select's "choices" matter most:
# ACF stores the choice KEY, so writing "Yes" to a field whose key is "yes"
# silently stores a value the field can't display.
_DESCRIBE_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$post_id = (int) $payload['post_id'];
$out = array( 'ok' => true, 'post_id' => $post_id, 'fields' => array() );
if ( ! function_exists( 'get_field_object' ) ) {
    $out = array( 'ok' => false, 'error' => 'ACF is not active on this install' );
} else {
    foreach ( (array) $payload['selectors'] as $selector ) {
        $field = get_field_object( $selector, $post_id );
        if ( ! $field || empty( $field['key'] ) ) {
            $out['fields'][ $selector ] = array( 'found' => false );
            continue;
        }
        $value = isset( $field['value'] ) ? $field['value'] : null;
        $out['fields'][ $selector ] = array(
            'found'   => true,
            'key'     => $field['key'],
            'name'    => $field['name'],
            'label'   => $field['label'],
            'type'    => $field['type'],
            'choices' => isset( $field['choices'] ) ? $field['choices'] : null,
            'value'   => is_scalar( $value ) || is_null( $value ) ? $value : wp_json_encode( $value ),
        );
    }
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
"""


# Posts in the given categories that have no narration of their own, newest
# first. "Own" means the narration's attachment belongs to this post: a post
# made by duplicating another points at the ORIGINAL's audio, which is the
# wrong narration, so it counts as missing.
_CANDIDATES_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$term_ids = array();
$missing  = array();
foreach ( (array) $payload['categories'] as $name ) {
    $term = get_term_by( 'name', $name, 'category' );
    if ( ! $term ) {
        $missing[] = $name;
        continue;
    }
    // Sub-categories count: Deep Dives, DIY Studies and Features are
    // Devotionals too, filed one level down.
    $term_ids[] = (int) $term->term_id;
    foreach ( (array) get_term_children( $term->term_id, 'category' ) as $child ) {
        $term_ids[] = (int) $child;
    }
}
$term_ids = array_values( array_unique( $term_ids ) );
$out = array( 'ok' => true, 'missing_categories' => $missing, 'posts' => array() );
if ( $term_ids ) {
    $ids = get_posts( array(
        'post_type' => 'post', 'post_status' => 'publish', 'category__in' => $term_ids,
        'numberposts' => -1, 'fields' => 'ids', 'orderby' => 'date', 'order' => 'DESC',
    ) );
    foreach ( $ids as $id ) {
        $a = (int) get_post_meta( $id, $payload['audio_meta'], true );
        if ( $a && ( $att = get_post( $a ) ) && (int) $att->post_parent === (int) $id ) continue;
        $out['posts'][] = array( 'ID' => (int) $id, 'post_title' => get_the_title( $id ) );
    }
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
"""

# One post's raw block content, for narrating it.
_POST_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$post = get_post( (int) $payload['post_id'] );
if ( ! $post ) {
    $out = array( 'ok' => false, 'error' => 'post not found' );
} else {
    $a = (int) get_post_meta( $post->ID, $payload['audio_meta'], true );
    $out = array(
        'ok'          => true,
        'ID'          => (int) $post->ID,
        'post_title'  => get_the_title( $post ),
        'post_status' => $post->post_status,
        'permalink'   => get_permalink( $post ),
        'content'     => $post->post_content,
        'own_audio'   => $a && ( $att = get_post( $a ) ) && (int) $att->post_parent === (int) $post->ID,
        'content_hash_meta' => (string) get_post_meta( $post->ID, '_tts_content_hash', true ),
        'text_hash_meta'    => (string) get_post_meta( $post->ID, '_tts_text_hash', true ),
    );
    $out['audio_id'] = $out['own_audio'] ? $a : 0;
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
"""


# Narrated posts in the given categories that the re-narration check needs to
# hear about: `baseline` — no text hash yet (a narration from before hashes,
# from the Doc watcher, or uploaded by hand), sent with their content so the
# caller can stamp one; `drifted` — content no longer matching the hash of what
# was narrated, i.e. an edit whose queue request never arrived.
_NARRATED_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$term_ids = array();
foreach ( (array) $payload['categories'] as $name ) {
    $term = get_term_by( 'name', $name, 'category' );
    if ( ! $term ) continue;
    $term_ids[] = (int) $term->term_id;
    foreach ( (array) get_term_children( $term->term_id, 'category' ) as $child ) {
        $term_ids[] = (int) $child;
    }
}
$limit = isset( $payload['baseline_limit'] ) ? (int) $payload['baseline_limit'] : 15;
$out = array( 'ok' => true, 'narrated' => 0, 'baseline' => array(), 'baseline_remaining' => 0,
    'drifted' => array() );
if ( $term_ids ) {
    $ids = get_posts( array(
        'post_type' => 'post', 'post_status' => 'publish', 'category__in' => array_values( array_unique( $term_ids ) ),
        'numberposts' => -1, 'fields' => 'ids',
    ) );
    foreach ( $ids as $id ) {
        $a = (int) get_post_meta( $id, $payload['audio_meta'], true );
        if ( ! $a || ! ( $att = get_post( $a ) ) || (int) $att->post_parent !== (int) $id ) continue;
        $out['narrated']++;
        $post  = get_post( $id );
        $hash  = hash( 'sha256', $post->post_content );
        $chash = (string) get_post_meta( $id, '_tts_content_hash', true );
        $thash = (string) get_post_meta( $id, '_tts_text_hash', true );
        if ( '' !== $chash && $chash !== $hash ) {
            $out['drifted'][] = array( 'ID' => (int) $id, 'post_title' => get_the_title( $id ),
                'content_hash' => $hash, 'modified' => strtotime( $post->post_modified_gmt . ' UTC' ) );
        } elseif ( '' === $thash ) {
            // Content is the bulk of the reply, so only a batch per call.
            if ( count( $out['baseline'] ) < $limit ) {
                $out['baseline'][] = array( 'ID' => (int) $id, 'post_title' => get_the_title( $id ),
                    'content' => $post->post_content );
            } else {
                $out['baseline_remaining']++;
            }
        }
    }
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
"""

# Stamp narration hashes: [{ID, content_hash, text_hash}]. With `only_if`, a
# post is skipped unless its content still hashes to content_hash (it may have
# been edited since it was read).
_SET_HASHES_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$out = array( 'ok' => true, 'written' => 0, 'changed' => array() );
foreach ( (array) $payload['posts'] as $p ) {
    $post = get_post( (int) $p['ID'] );
    if ( ! $post ) continue;
    if ( hash( 'sha256', $post->post_content ) !== $p['content_hash'] ) {
        $out['changed'][] = (int) $p['ID'];
        continue;
    }
    update_post_meta( $post->ID, '_tts_content_hash', (string) $p['content_hash'] );
    update_post_meta( $post->ID, '_tts_text_hash', (string) $p['text_hash'] );
    $out['written']++;
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
"""


_SMART = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "‒": "-", "−": "-",
    " ": " ", "…": "...",
}


def normalize_title(title):
    """Casefold a title down to the part that actually identifies it.

    Google Docs and WordPress disagree constantly about punctuation — Docs
    autocorrects quotes and dashes, WordPress applies wptexturize — so strip
    all of it and compare the words."""
    text = unicodedata.normalize("NFKC", title or "")
    # "[QQT #96] Fully Known. Fully Loved." is post "Fully Known. Fully Loved."
    text = re.sub(r"^\s*\[[^\]]*\]", "", text)
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    text = text.casefold()
    # "Faith Blockers & Mountain Movers" is post "...Blockers and Mountain..."
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class WordPressError(RuntimeError):
    pass


class WordPressSkip(WordPressError):
    """The publish declined on purpose (e.g. the post has its own narration
    now) — not a failure to retry."""


class WordPressPublisher:
    def __init__(self, config, logger=None):
        self.cfg = config or {}
        self.host = (self.cfg.get("ssh_host") or "").strip()
        self.key = os.path.expanduser(self.cfg.get("ssh_key") or "")
        self.wp_path = (self.cfg.get("wp_path") or "").strip()
        self.post_types = self.cfg.get("post_types") or ["post"]
        self.dry_run = bool(self.cfg.get("dry_run"))
        self.timeout = int(self.cfg.get("ssh_timeout", 900))
        self._log = logger or (lambda msg, level="info": None)
        if not self.host:
            raise WordPressError('wordpress.ssh_host is required (e.g. "install@install.ssh.wpengine.net")')

    # ---- SSH plumbing ------------------------------------------------------

    def _ssh_argv(self):
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
        if self.key:
            argv += ["-i", self.key, "-o", "IdentitiesOnly=yes"]
        return argv + [self.host]

    def _run(self, remote_cmd, stdin_bytes=None, timeout=None):
        """Run one command on the install.

        The command is base64-wrapped rather than sent as a plain string.
        WP Engine's SSH gateway re-parses what it receives and strips a level
        of quoting, which turns any PHP snippet into a bash syntax error
        (`syntax error near unexpected token '('`). Base64 contains nothing a
        shell will touch, so it arrives intact. It's decoded to a temp script
        and run from there rather than piped into `bash`, so the script's
        stdin is still ours — that's what carries the audio upload and the
        PHP for `wp eval-file -`."""
        payload = base64.b64encode(remote_cmd.encode()).decode()
        wrapper = (f"S=/tmp/tts-cmd-$$.sh; echo {payload} | base64 -d > $S; "
                   f"bash $S; R=$?; rm -f $S; exit $R")
        try:
            proc = subprocess.run(
                self._ssh_argv() + [wrapper],
                input=stdin_bytes if stdin_bytes is not None else b"",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired:
            # Not an OSError, so uncaught it would skip the caller's attempt
            # counter and retry against the live site every poll, forever.
            raise WordPressError(
                f"ssh command timed out after {timeout or self.timeout}s "
                f"(it may still have finished on the server)")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip() or \
                  proc.stdout.decode("utf-8", "replace").strip()
            raise WordPressError(f"ssh command failed ({proc.returncode}): {err[:500]}")
        return proc.stdout.decode("utf-8", "replace")

    def _wp(self, wp_args, stdin_bytes=None, timeout=None):
        """Run one wp-cli command in the install directory."""
        quoted = " ".join(_shquote(a) for a in wp_args)
        prefix = f"cd {_shquote(self.wp_path)} && " if self.wp_path else ""
        return self._run(f"{prefix}wp {quoted}", stdin_bytes=stdin_bytes, timeout=timeout)

    @staticmethod
    def _fenced_json(output):
        start = output.find(_JSON_OPEN)
        end = output.find(_JSON_CLOSE, start + 1)
        if start < 0 or end < 0:
            raise WordPressError(f"unexpected wp-cli output: {output.strip()[:500]}")
        return json.loads(output[start + len(_JSON_OPEN):end])

    # ---- Checks ------------------------------------------------------------

    def preflight(self):
        """Report on the connection, wp-cli and ACF rather than assuming them.

        Raises only if the install can't be reached at all; a missing ACF comes
        back as a flag so the caller can say which part of the setup is wrong."""
        version = self._wp(["--version"], timeout=60).strip()
        acf = self._wp(
            ["eval", 'echo function_exists("update_field") ? "yes" : "no";'],
            timeout=60).strip()
        installs = self._run("ls -d ~/sites/*/ 2>/dev/null", timeout=60).strip()
        return {"wp_version": version, "acf": "yes" in acf, "installs": installs}

    def newest_post_id(self):
        """A post to inspect field definitions against. ACF resolves a field
        NAME through the field groups attached to a specific post, so the
        lookup needs one."""
        ids = self._wp(["post", "list", "--post_type=" + ",".join(self.post_types),
                        "--post_status=publish", "--posts_per_page=1",
                        "--field=ID", "--format=ids"], timeout=120).split()
        return int(ids[0]) if ids else None

    def describe_fields(self, selectors, post_id):
        """What the named ACF fields actually are on a real post."""
        payload = base64.b64encode(json.dumps({
            "post_id": post_id, "selectors": list(selectors),
        }).encode()).decode()
        out = self._wp(["eval-file", "-", payload],
                       stdin_bytes=_DESCRIBE_PHP.encode(), timeout=120)
        result = self._fenced_json(out)
        if not result.get("ok"):
            raise WordPressError(result.get("error") or "could not read the field definitions")
        return result

    # ---- Matching ----------------------------------------------------------

    def list_posts(self):
        """Every non-trashed post of the configured types, as {ID, post_title,
        post_status, url}. `url` is the public permalink (a draft only has
        ?p=ID); asking for it measured no slower than leaving it out."""
        out = self._wp([
            "post", "list",
            "--post_type=" + ",".join(self.post_types),
            "--post_status=any",
            "--posts_per_page=-1",
            "--fields=ID,post_title,post_status,url",
            "--format=json",
        ], timeout=600)
        start = out.find("[")
        if start < 0:
            raise WordPressError(f"could not read the post list: {out.strip()[:300]}")
        return json.loads(out[start:])

    def match_title(self, title, limit=3, posts=None):
        """Find the post this doc belongs to.

        Returns (exact, candidates). `exact` is the post when exactly one has
        the same normalized title — the unambiguous case the pipeline can act
        on by itself. Otherwise `exact` is None and `candidates` holds the
        closest matches (including every tied exact match) for a human."""
        posts = self.list_posts() if posts is None else posts
        target = normalize_title(title)
        if not target:
            return None, []

        scored = []
        exacts = []
        for post in posts:
            norm = normalize_title(post.get("post_title"))
            if norm == target:
                exacts.append(post)
            ratio = difflib.SequenceMatcher(None, target, norm).ratio()
            if ratio >= CANDIDATE_THRESHOLD:
                scored.append((ratio, post))

        if len(exacts) == 1:
            return exacts[0], []
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return None, [post for _, post in scored[:limit]]

    def find_post_by_url(self, url):
        """Resolve a post URL (or bare ID) the human replied with."""
        url = (url or "").strip()
        if re.fullmatch(r"\d+", url):
            post_id = int(url)
        else:
            post_id = _post_id_from_url(url)
            if post_id is None:
                slug = _slug_from_url(url)
                if not slug:
                    raise WordPressError(f"couldn't read a post from {url!r}")
                found = self._wp(["post", "list", "--post_type=" + ",".join(self.post_types),
                                  "--post_status=any", "--name=" + slug,
                                  "--field=ID", "--format=ids"], timeout=120).split()
                if not found:
                    raise WordPressError(f"no post found with the slug {slug!r} (from {url})")
                if len(found) > 1:
                    raise WordPressError(f"the slug {slug!r} matches {len(found)} posts")
                post_id = int(found[0])
        title = self._wp(["post", "get", str(post_id), "--field=post_title"], timeout=60).strip()
        return {"ID": post_id, "post_title": title}

    # ---- Backfill ----------------------------------------------------------

    def _eval_json(self, php, payload, timeout=300, compress=False):
        """Run a PHP helper (source on stdin, base64 JSON payload as argv[0]).

        `compress` sends the reply back gzipped and base64'd. Needed for replies
        carrying post content in bulk: those hang in WP Engine's SSH gateway
        (a 69 KB one never arrived; the same gzipped and base64'd, 85 KB, came
        back in 39s) — plain ASCII gets through."""
        arg = base64.b64encode(json.dumps(payload).encode()).decode()
        if compress:
            prefix = f"cd {_shquote(self.wp_path)} && " if self.wp_path else ""
            out = self._run(f"{prefix}wp eval-file - {_shquote(arg)} | gzip -c | base64",
                            stdin_bytes=php.encode(), timeout=timeout)
            try:
                out = gzip.decompress(base64.b64decode(out)).decode("utf-8", "replace")
            except (ValueError, OSError) as e:
                raise WordPressError(f"couldn't decode the compressed reply: {e}")
        else:
            out = self._wp(["eval-file", "-", arg], stdin_bytes=php.encode(), timeout=timeout)
        result = self._fenced_json(out)
        if not result.get("ok"):
            raise WordPressError(result.get("error") or "the WordPress helper failed")
        return result

    def _audio_meta(self):
        # The candidate/post helpers read the meta row directly, which needs the
        # field NAME (a field key can't be a meta key).
        field = (self.cfg.get("audio_field") or "").strip()
        if field.startswith("field_"):
            raise WordPressError("the backfill needs wordpress.audio_field as a field name, not a key")
        return field

    def backfill_candidates(self, categories):
        """Published posts in these categories with no narration of their own,
        newest first, as [{ID, post_title}]."""
        result = self._eval_json(_CANDIDATES_PHP, {
            "categories": list(categories), "audio_meta": self._audio_meta()}, timeout=600)
        if result.get("missing_categories"):
            self._log(f"No such category on the site: {result['missing_categories']}", "warn")
        return result["posts"]

    def post_for_narration(self, post_id):
        """One post's title, status, permalink, raw block content, and whether
        it has narration of its own."""
        return self._eval_json(_POST_PHP, {"post_id": int(post_id),
                                           "audio_meta": self._audio_meta()})

    def narrated_posts(self, categories, baseline_limit=15):
        """Narrated posts needing a baseline hash or re-narration; see
        _NARRATED_PHP. {narrated, baseline: [{ID, post_title, content}] (at
        most baseline_limit), baseline_remaining, drifted: [{ID, post_title,
        content_hash, modified}]}."""
        return self._eval_json(_NARRATED_PHP, {
            "categories": list(categories), "audio_meta": self._audio_meta(),
            "baseline_limit": int(baseline_limit)}, timeout=600, compress=True)

    def set_narration_hashes(self, posts):
        """Stamp [{ID, content_hash, text_hash}] where the content still
        matches. Returns {written, changed: [ids edited since]}. Batched so the
        payload stays well inside the command-line limit."""
        written, changed = 0, []
        for i in range(0, len(posts), 100):
            r = self._eval_json(_SET_HASHES_PHP, {"posts": posts[i:i + 100]})
            written += r["written"]
            changed += r["changed"]
        return {"written": written, "changed": changed}

    # ---- Writing -----------------------------------------------------------

    def publish(self, post_id, m4a_path, extra_fields=None, media_title=None, source="",
                only_if_no_own_audio=False, expect_audio=None, content_hash=None,
                text_hash=None):
        """Upload the audio and point the post's fields at it — one SSH
        connection, one WordPress bootstrap (see _PUBLISH_PHP).

        One connection because WP Engine gives every connection its own
        container: a file streamed to /tmp in one is not there in the next.
        One bootstrap because each costs ~30s here. The audio travels on the
        SSH stdin, so PHP's upload_max_filesize (2M on this host) never
        applies.

        `source` (the project id) tags the attachment so a retry after a
        timeout reuses it. `only_if_no_own_audio` makes the write decline
        (WordPressSkip) if the post has gained narration of its own;
        `expect_audio` (an attachment id, or 0 for none) makes a replacement
        decline if the post's narration is no longer that one.

        `content_hash`/`text_hash` record what the narration was made from
        (sha256 of the post content, and of the text read aloud); see
        _PUBLISH_PHP. Returns the helper's result dict: permalink, edit link,
        before/after for each field written, attachment id."""
        audio_field = (self.cfg.get("audio_field") or "").strip()
        if not audio_field:
            raise WordPressError("wordpress.audio_field is not set — nothing to write the audio into")

        filename = os.path.basename(m4a_path)
        payload = base64.b64encode(json.dumps({
            "post_id": int(post_id),
            "audio_field": audio_field,
            "audio_format": self.cfg.get("audio_field_format") or "attachment_id",
            "fields": extra_fields or {},
            "filename": filename,
            "title": media_title or os.path.splitext(filename)[0],
            "source": str(source or ""),
            "dry_run": self.dry_run,
            "purge_cache": bool(self.cfg.get("flush_cache", True)),
            "media_folder": (self.cfg.get("media_folder") or "").strip(),
            "only_if_no_own_audio": bool(only_if_no_own_audio),
            "expect_audio": None if expect_audio is None else int(expect_audio),
            "content_hash": content_hash or "",
            "text_hash": text_hash or "",
        }).encode()).decode()
        php = base64.b64encode(_PUBLISH_PHP.encode()).decode()
        prefix = f"cd {_shquote(self.wp_path)} && " if self.wp_path else ""
        # The paths are referenced as "$F"/"$P" rather than _shquote()d, so the
        # mktemp result is what's used; the filename itself is quoted. `cat`
        # drains stdin (the audio) before wp starts.
        script = (
            "set -euo pipefail\n"
            'D=$(mktemp -d /tmp/tts-publish-XXXXXX)\n'
            "trap 'rm -rf \"$D\"' EXIT\n"
            f'F="$D/"{_shquote(filename)}\n'
            'P="$D/publish.php"\n'
            'cat > "$F"\n'
            f'echo {php} | base64 -d > "$P"\n'
            f'{prefix}wp eval-file "$P" {payload} "$F"\n'
        )
        if self.dry_run:
            self._log(f"[dry run] would upload {filename} and set {audio_field} on post {post_id}.")
            audio = b""
        else:
            with open(m4a_path, "rb") as f:
                audio = f.read()
        result = self._fenced_json(self._run(script, stdin_bytes=audio))
        if result.get("skipped"):
            raise WordPressSkip(result["skipped"])
        if not result.get("ok"):
            raise WordPressError(result.get("error") or "publish failed")
        return result


def _shquote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def _post_id_from_url(url):
    """?p=123 / ?page_id=123 style permalinks."""
    m = re.search(r"[?&](?:p|page_id|post)=(\d+)", url)
    return int(m.group(1)) if m else None


def _slug_from_url(url):
    """Last non-empty path segment of a pretty permalink."""
    path = re.sub(r"[?#].*$", "", url.strip())
    segments = [s for s in path.split("/") if s and "." not in s]
    if not segments:
        return None
    slug = segments[-1]
    return None if slug in ("http:", "https:") else slug
