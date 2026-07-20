# Remote generation server

TTS Studio can offload audio synthesis to a shared always-on machine
(e.g. a Mac mini) so teammates with slower Macs can still use it. Only
`/api/generate` runs remotely — projects, voice profiles, merging, and exports
all stay on each person's own machine. Voice-clone reference audio is uploaded
per-request and deleted from the server after inference; nothing personal is
stored server-side.

**No secrets live in this repo.** The server URL and access token are stored in
each user's local `settings.json` and in the server's environment — share them
privately (DM, password manager), never commit them.

## How it works

```
Teammate's Mac                              Server (always on)
┌─────────────────────────┐                 ┌──────────────────────────┐
│ Desktop app (UI, data)  │  HTTPS + token  │ Same app, server mode    │
│ /api/generate ──────────┼────────────────►│ QWEN_TTS_SERVER_TOKEN set│
│ everything else local   │ ◄── WAV bytes ──│ runs the TTS model       │
└─────────────────────────┘                 └──────────────────────────┘
```

Server mode is just this same app started with the `QWEN_TTS_SERVER_TOKEN`
environment variable set. When it's set, **every** `/api/*` request must carry
`Authorization: Bearer <token>`, and the instance never forwards generations
itself (no loops).

## Server setup (macOS)

### 1. Install

```bash
git clone https://github.com/<you>/qwen-tts-studio.git
cd qwen-tts-studio
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Create a token

```bash
openssl rand -hex 32
```

Keep this somewhere safe — it's what you'll hand to teammates.

### 3. Run as an always-on service (launchd)

Create `~/Library/LaunchAgents/com.localtts.server.plist` (fix the paths and
token):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.localtts.server</string>
    <key>WorkingDirectory</key><string>/Users/YOU/qwen-tts-studio</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/qwen-tts-studio/venv/bin/python</string>
        <string>-m</string><string>uvicorn</string>
        <string>main:app</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>8002</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>QWEN_TTS_SERVER_TOKEN</key><string>PASTE_TOKEN_HERE</string>
    </dict>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/tmp/localtts-server.log</string>
    <key>StandardErrorPath</key><string>/tmp/localtts-server.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.localtts.server.plist
```

Port 8002 keeps it clear of a desktop copy of the app (8001). It binds only to
127.0.0.1 — the tunnel below is the sole way in from outside.

Optionally pre-download the model so the first teammate request isn't slow:
`./venv/bin/python download_model.py`.

### 4. Expose it with Tailscale Funnel

If the server is already on Tailscale, Funnel gives you a stable public HTTPS
URL without putting anyone on your tailnet, without opening router ports, and
without revealing your home IP (traffic relays through Tailscale's edge):

```bash
tailscale funnel --bg 8002
```

It prints the public URL, e.g. `https://mini.tail1234.ts.net`. That's the
Server URL teammates use. The bearer token is what keeps the public URL private.

(Alternative: a Cloudflare Tunnel works the same way if you'd rather not use
Funnel; the app doesn't care what the tunnel is.)

## Teammate setup

There are two ways to use the server, and they can be mixed freely:

### A. In the browser — nothing to install

Teammates just open the server's URL (the Funnel/tunnel address). The page
asks for the access code once, remembers it in a cookie on that device, and
then they have the full Studio UI — projects, generation, takes, export — all
living on the server. This is the mode to pair with the Google Docs
auto-import pipeline (see `DOC_WATCHER.md`): shared docs appear in this
project list, already generated.

### B. In the desktop app — local projects, remote generation

1. Open TTS Studio → **⚙ Settings → Remote**.
2. Paste the **Server URL** and **Access Token** you were given.
3. Click **Test Connection** — you should see the server version.
4. **Save.** All generation now runs on the server; leave the URL empty to go
   back to local generation. Projects stay on your own machine in this mode.

## Notes

- Generations from multiple people are serialized on the server (one at a
  time on the GPU); the activity log shows when a request is waiting.
- To revoke access, change `QWEN_TTS_SERVER_TOKEN` in the plist and
  `launchctl unload` + `load` it again, then hand out the new token.
