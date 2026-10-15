"""
Speech audit — transcribe every narrated paragraph and flag the ones that don't
say their text (garbled words, babble, an entity read aloud). The after-the-fact
twin of the check headless imports now run on each take (speech_check.py).

Runs under the MLX venv (it has mlx-whisper):

    venv-mlx/bin/python audit_speech.py              # all WordPress narrations
    venv-mlx/bin/python audit_speech.py --sample 400 # a random slice, to calibrate
    venv-mlx/bin/python audit_speech.py --report     # rewrite the report from saved results

Results go to data/speech_audit/: results.jsonl (one line per paragraph take,
with the transcript, so the thresholds can change without transcribing again)
and report.md (the flagged paragraphs, worst first, with where they are in the
article). Resumable: a take already in results.jsonl is skipped, so a stopped
run picks up where it left off. ~15x real time on the M4 mini, ~5 h for the
72 h of backfilled narration.
"""
import argparse
import glob
import json
import os
import random
import sys
import time
from datetime import datetime

import soundfile as sf

import speech_check

DATA_DIR = "data"
OUT_DIR = os.path.join(DATA_DIR, "speech_audit")
RESULTS = os.path.join(OUT_DIR, "results.jsonl")
REPORT = os.path.join(OUT_DIR, "report.md")
MODEL = "mlx-community/whisper-large-v3-turbo"


def takes():
    """Every active take of every WordPress-narrated project, in article order,
    with its offset into the article."""
    for pj in sorted(glob.glob(os.path.join(DATA_DIR, "projects", "*", "project.json"))):
        try:
            with open(pj) as f:
                project = json.load(f)
        except (OSError, ValueError):
            continue
        source = project.get("source") or {}
        if source.get("kind") != "wordpress_post":
            continue
        audio_dir = os.path.join(os.path.dirname(pj), "audio")
        offset = 0.0
        for i, para in enumerate(project.get("paragraphs", [])):
            take = para.get("activeTake", 1)
            path = os.path.join(audio_dir, f"{para['id']}-t{take}.flac")
            if not os.path.exists(path):
                continue
            seconds = sf.info(path).duration
            yield {"key": f"{project['id']}/{para['id']}/t{take}", "project_id": project["id"],
                   "name": project.get("name"), "post_id": source.get("post_id"),
                   "url": source.get("url"), "para": i, "at": round(offset, 1),
                   "seconds": round(seconds, 1), "text": para["text"], "path": path}
            offset += seconds


def load_results():
    out = {}
    if os.path.exists(RESULTS):
        with open(RESULTS) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out[r["key"]] = r
                except (ValueError, KeyError):
                    pass                 # a line cut off by a stop mid-write
    return out


def run(sample=None):
    import mlx_whisper
    os.makedirs(OUT_DIR, exist_ok=True)
    done = load_results()
    todo = [t for t in takes() if t["key"] not in done]
    if sample:
        random.Random(0).shuffle(todo)
        todo = todo[:sample]
    total_audio = sum(t["seconds"] for t in todo)
    print(f"{len(done)} already checked; {len(todo)} to go ({total_audio / 3600:.1f} h of audio).",
          flush=True)
    t0 = time.monotonic()
    with open(RESULTS, "a") as out:
        for n, t in enumerate(todo, 1):
            heard = mlx_whisper.transcribe(t["path"], path_or_hf_repo=MODEL, language="en",
                                           condition_on_previous_text=False)["text"].strip()
            r = {k: v for k, v in t.items() if k != "path"}
            r.update(heard=heard, checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                     **speech_check.compare(t["text"], heard))
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            if n % 200 == 0:
                rate = (time.monotonic() - t0) / n
                print(f"  {n}/{len(todo)} — ~{rate * (len(todo) - n) / 60:.0f} min left", flush=True)
    write_report()


def write_report():
    results = list(load_results().values())
    bad = [r for r in results if speech_check.is_bad(r)]
    bad.sort(key=lambda r: (-r["error"], -r["longest_extra"]))
    posts = {}
    for r in bad:
        posts.setdefault((r["post_id"], r["name"], r["url"]), []).append(r)
    lines = [f"# Speech audit — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(results)} paragraph takes checked; **{len(bad)} flagged** in "
             f"**{len(posts)} posts** (error > {speech_check.MAX_ERROR} or a run of more than "
             f"{speech_check.MAX_EXTRA_RUN} words not in the text).", "",
             "`error` ≈ word error rate against the text; `run` = longest stretch of words heard "
             "that aren't in it. Full transcripts are in results.jsonl.", ""]
    for (post_id, name, url), rs in sorted(posts.items(), key=lambda kv: -max(r["error"] for r in kv[1])):
        lines.append(f"## {name} (post {post_id})")
        lines.append(url or "")
        lines.append("")
        for r in sorted(rs, key=lambda r: r["para"]):
            at = f"{int(r['at'] // 60)}:{int(r['at'] % 60):02}"
            lines.append(f"- **¶{r['para']} at {at}** — error {r['error']}, run {r['longest_extra']}, "
                         f"{r['seconds']}s for {r['words']} words")
            lines.append(f"  - text: {r['text'][:300]}")
            lines.append(f"  - heard: {r['heard'][:300]}")
        lines.append("")
    with open(REPORT, "w") as f:
        f.write("\n".join(lines))
    print(f"{len(bad)} flagged in {len(posts)} posts — {os.path.abspath(REPORT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, help="check only this many (random) takes")
    ap.add_argument("--report", action="store_true", help="rewrite report.md from results and exit")
    args = ap.parse_args()
    if args.report:
        return write_report()
    run(args.sample)


if __name__ == "__main__":
    sys.exit(main())
