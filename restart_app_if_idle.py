"""
Nightly app restart — release the memory generation leaks, but only when idle.

The app's memory grows with every generation (it reached an 84 GB footprint
on the 16 GB mini after 25 backfilled posts), below anything the app itself
frees. The backfill restarts it between posts; this is the standing guard for
everything else — Google Docs, and people using the app.

Run by launchd once a night (com.localtts.nightly-restart). If anything is
queued or generating it waits and checks again every 10 minutes, and gives up
for the night at the deadline rather than interrupting work. An import that a
restart does interrupt is resumed by the app itself on its next request (see
_resume_interrupted_imports in main.py), so a race here costs time, not audio.

Config — the "nightly_restart" block of doc_watcher.json (app_url/app_token
come from the top level):
    "nightly_restart": {
      "command": "launchctl kickstart -k gui/501/com.localtts.server",
      "give_up_after_minutes": 150
    }

Run:  python restart_app_if_idle.py
"""
import argparse
import subprocess
import sys
import time

import requests

import doc_watcher
from doc_watcher import load_json, log
from wp_backfill import app_busy, restart_app

RETRY_SECONDS = 600


def main():
    ap = argparse.ArgumentParser(description="Restart TTS Studio if nothing is generating.")
    ap.add_argument("--config", default=doc_watcher.DEFAULT_CONFIG)
    args = ap.parse_args()

    config = load_json(args.config, None)
    if config is None:
        sys.exit(f"Config not found or invalid: {args.config}")
    cfg = config.get("nightly_restart") or {}
    command = cfg.get("command")
    if not command:
        sys.exit("nightly_restart.command is not set — nothing to run.")
    app_url = (config.get("app_url") or "http://127.0.0.1:8001").rstrip("/")
    token = (config.get("app_token") or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.time() + 60 * int(cfg.get("give_up_after_minutes", 150))

    while True:
        try:
            busy = app_busy(app_url, headers)
        except requests.RequestException as e:
            log(f"Nightly restart: couldn't reach the app ({e}) — not restarting.", "warn")
            return
        if not busy:
            break
        if time.time() + RETRY_SECONDS > deadline:
            log("Nightly restart: the app stayed busy — skipping tonight.", "warn")
            return
        log("Nightly restart: something is generating — checking again in 10 minutes.")
        time.sleep(RETRY_SECONDS)

    try:
        restart_app(command, app_url, headers)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"Nightly restart failed: {e}", "error")
        sys.exit(1)
    log("Nightly restart: restarted the app while idle.", "ok")


if __name__ == "__main__":
    main()
