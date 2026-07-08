# Local TTS Studio v3.6.0

Two quality-of-life improvements and one big new capability: generation can now run on a shared remote server.

## New

**Remote generation server**
- The app can offload audio synthesis to a shared always-on machine — useful if your Mac is slow. Set it up in **⚙ Settings → Remote**: paste the server URL and access token you were given, click **Test Connection**, then Save.
- Only synthesis runs remotely. Your projects, voice profiles, merging, and exported audio all stay on your machine. Voice-clone reference audio is uploaded per-request and deleted from the server after inference.
- Leave the URL empty to generate locally, exactly as before.
- Hosting one yourself? See `SERVER.md` in the repo — the same app runs in server mode behind a token, exposed via a Tailscale Funnel (or any tunnel).

**Tutorial**
- New 📚 **Tutorial** button in the header walks you through the whole workflow: pick a voice → paste Markdown → generate → review takes → export.

## Fixes

- Paragraph text boxes now grow to fit their content instead of cutting off long paragraphs at three lines. They resize as you type and when the window changes size.

## Install

Download the `.zip` below, unzip, and drag **Local TTS Studio.app** to Applications. First launch: right-click → Open (the app is unsigned).
