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
    and then pulled in with `wp media import`, so it never goes through PHP's
    upload-size limit. A 45-minute devotional is fine.

Title matching is deliberately fuzzy-tolerant: Docs titles and post titles
drift (curly vs straight quotes, en dashes, trailing " - Part 2", casing), so
titles are normalized on both sides before comparing. A single normalized
match is treated as THE post; anything else returns ranked candidates and the
caller asks a human which one it is.

Nothing here changes post_status — it only ever sets the configured fields on
a post that already exists.

Config lives in the "wordpress" block of doc_watcher.json; see DOC_WATCHER.md.
"""
import base64
import difflib
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
# Marks the attachment id in wp-cli output that may also carry notices.
_ID_PREFIX = "TTSID:"

# A normalized title this similar to the doc's is worth offering as a "did you
# mean" candidate; below it, the post is almost certainly unrelated.
CANDIDATE_THRESHOLD = 0.62

# PHP run by `wp eval-file -` (source on stdin, base64 payload as argv[0]).
_APPLY_PHP = r"""<?php
$payload = json_decode( base64_decode( $args[0] ), true );
$out = array( 'ok' => false );
if ( ! function_exists( 'update_field' ) ) {
    $out['error'] = 'ACF is not active on this install (update_field missing)';
} elseif ( ! ( $post = get_post( (int) $payload['post_id'] ) ) ) {
    $out['error'] = 'post ' . (int) $payload['post_id'] . ' not found';
} else {
    $post_id = (int) $payload['post_id'];
    $applied = array();
    foreach ( (array) $payload['fields'] as $selector => $value ) {
        $before = get_field( $selector, $post_id );
        if ( empty( $payload['dry_run'] ) ) {
            update_field( $selector, $value, $post_id );
        }
        $applied[ $selector ] = array(
            'before' => is_scalar( $before ) || is_null( $before ) ? $before : wp_json_encode( $before ),
            'after'  => is_scalar( $value ) ? $value : wp_json_encode( $value ),
        );
    }
    $out['ok']          = true;
    $out['applied']     = $applied;
    $out['post_title']  = $post->post_title;
    $out['post_status'] = $post->post_status;
    $out['permalink']   = get_permalink( $post_id );
    $out['edit_link']   = admin_url( 'post.php?post=' . $post_id . '&action=edit' );
}
echo "<<<TTSJSON>>>" . wp_json_encode( $out ) . "<<<TTSEND>>>\n";
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
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class WordPressError(RuntimeError):
    pass


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
        proc = subprocess.run(
            self._ssh_argv() + [wrapper],
            input=stdin_bytes if stdin_bytes is not None else b"",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout or self.timeout,
        )
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
        """Every non-trashed post of the configured types, as {ID, title, status}."""
        out = self._wp([
            "post", "list",
            "--post_type=" + ",".join(self.post_types),
            "--post_status=any",
            "--posts_per_page=-1",
            "--fields=ID,post_title,post_status",
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

    # ---- Writing -----------------------------------------------------------

    def upload_media(self, local_path, post_id=None, title=None):
        """Stream the file to the install and import it into the media library.

        The transfer and the import happen in ONE ssh connection. WP Engine's
        gateway does not guarantee that a file written to /tmp by one
        connection is visible to the next, so uploading and importing
        separately can hand `wp media import` a path that no longer exists.
        Doing both in one session also saves two wp-cli bootstraps, which are
        ~30s each on this host.

        Sent over SSH rather than uploaded through WordPress, so PHP's
        upload_max_filesize never enters into it."""
        remote = "/tmp/tts-upload-$$-" + os.path.basename(local_path)
        # The path is referenced as "$F" rather than passed through _shquote:
        # single quotes would stop $$ expanding, and the import would then be
        # handed a filename that differs from the one just written.
        args = ["--porcelain"]
        if post_id:
            args.append(f"--post_id={post_id}")
        if title:
            args.append(f"--title={title}")
        prefix = f"cd {_shquote(self.wp_path)} && " if self.wp_path else ""
        # `cat` drains stdin first, so the later lines run with stdin at EOF.
        # pipefail keeps a wp-cli failure from being hidden by sed's success;
        # the prefix separates the id from any notices wp prints alongside it.
        script = (
            f'set -o pipefail\n'
            f'F="{remote}"\n'
            f'cat > "$F"\n'
            f'trap \'rm -f "$F"\' EXIT\n'
            f'{prefix}wp media import "$F" {" ".join(_shquote(a) for a in args)} '
            f'| sed -e "s/^/{_ID_PREFIX}/"\n'
        )
        with open(local_path, "rb") as f:
            out = self._run(script, stdin_bytes=f.read())
        ids = [line[len(_ID_PREFIX):].strip() for line in out.splitlines()
               if line.startswith(_ID_PREFIX)]
        ids = [i for i in ids if i.isdigit()]
        if not ids:
            raise WordPressError(
                f"media import returned no attachment id: {out.strip()[:300]}")
        return int(ids[-1])

    def attachment_url(self, attachment_id):
        return self._wp(["post", "get", str(attachment_id), "--field=guid"], timeout=60).strip()

    def set_fields(self, post_id, fields):
        """Set ACF fields through ACF's own API. Keys may be names or field keys."""
        payload = base64.b64encode(json.dumps({
            "post_id": post_id,
            "fields": fields,
            "dry_run": self.dry_run,
        }).encode()).decode()
        out = self._wp(["eval-file", "-", payload],
                       stdin_bytes=_APPLY_PHP.encode(), timeout=300)
        result = self._fenced_json(out)
        if not result.get("ok"):
            raise WordPressError(result.get("error") or "update_field failed")
        return result

    def flush_cache(self):
        """WP Engine serves the old page until its cache is cleared."""
        try:
            self._wp(["page-cache", "flush"], timeout=120)
            return True
        except WordPressError as e:
            self._log(f"Cache flush failed ({e}) — the post is updated, but the "
                      f"public page may serve a stale copy for a while.", "warn")
            return False

    # ---- The whole job -----------------------------------------------------

    def publish(self, post_id, m4a_path, extra_fields=None, media_title=None):
        """Upload the audio and point the post's fields at it.

        Returns the helper's result dict (permalink, edit link, before/after
        for each field written)."""
        audio_field = (self.cfg.get("audio_field") or "").strip()
        if not audio_field:
            raise WordPressError("wordpress.audio_field is not set — nothing to write the audio into")

        if self.dry_run:
            self._log(f"[dry run] would upload {os.path.basename(m4a_path)} and set "
                      f"{audio_field} on post {post_id}.")
            attachment_id, audio_value = 0, "(dry run)"
        else:
            attachment_id = self.upload_media(m4a_path, post_id=post_id, title=media_title)
            audio_value = attachment_id
            if (self.cfg.get("audio_field_format") or "attachment_id") == "url":
                audio_value = self.attachment_url(attachment_id)

        fields = {audio_field: audio_value}
        fields.update(extra_fields or {})
        result = self.set_fields(post_id, fields)
        result["attachment_id"] = attachment_id
        if not self.dry_run and self.cfg.get("flush_cache", True):
            self.flush_cache()
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
