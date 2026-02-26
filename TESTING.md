# Local TTS Studio — Testing Guide

## Current Status

The app builds and starts successfully. The `qwen_tts` package now loads correctly (confirmed via log). The frontend and server are working. **TTS generation has not yet been tested end-to-end in the bundled app.**

## How to Launch

### From the .app bundle
Double-click `/Applications/Local TTS Studio.app` (or from Dock).

### From source (if the .app has issues)
```bash
cd /Users/parkerallen/programming/digital_team/qwen_tts/qwen-tts-studio
venv/bin/python3 app_launcher.py
```

## Where to Find Logs

The bundled app writes logs to:
```
~/.qwen_tts_studio/app.log
```

Watch it live with:
```bash
tail -f ~/.qwen_tts_studio/app.log
```

Clear it before a fresh test:
```bash
> ~/.qwen_tts_studio/app.log
```

## What to Test

### 1. App Startup
- [ ] App opens a loading page in the browser immediately
- [ ] Loading page shows status messages ("Loading Python runtime...", etc.)
- [ ] Loading page auto-redirects to `127.0.0.1:8001` when server is ready
- [ ] Jennifer profile appears in the Voice Cloning dropdown
- [ ] No repeating console errors in browser DevTools (F12 > Console)

### 2. Generation (Voice Cloning mode)
- [ ] Select "Voice Cloning" mode, "Jennifer" profile
- [ ] Enter any text, click "Parse Paragraphs"
- [ ] Generation should start automatically
- [ ] Status badge should show "Generating..." then "Ready"
- [ ] Audio player appears and plays back the generated audio

### 3. Generation (other modes)
- [ ] Switch to "Preprogrammed Voice", select any speaker, generate
- [ ] Switch to "Voice Design", enter a description, generate

### 4. Download & Merge
- [ ] Generate 2+ paragraphs
- [ ] Click "Download Audio"
- [ ] Should merge segments, apply "Clear" treatment, download a .wav file

### 5. Restart Reliability
- [ ] Quit the app (Cmd+Q or kill from Dock)
- [ ] Relaunch — should start cleanly without "address already in use" errors
- [ ] Check that port 8001 is freed and reused

### 6. Profile Management
- [ ] Click "+" to save a new voice profile (needs name, audio file, reference text)
- [ ] New profile appears in dropdown
- [ ] Delete a user profile (trash icon) — should work
- [ ] Built-in "Jennifer" profile cannot be deleted (no trash icon shown)

## Known Issues / Things to Watch

1. **Multiple browser tabs**: The loading page opens a new tab each launch. If the app crashes and auto-relaunches, you'll get extra tabs. This is cosmetic — close the extras.

2. **First generation is slow**: The first generation downloads the model from HuggingFace (~2-5 GB depending on model size). Subsequent generations reuse the cached model.

3. **Model download progress**: The status badge in the header should show download progress. If it stays on "Idle" or shows "Model Error", check `~/.qwen_tts_studio/app.log`.

4. **flash-attn warning**: The log will show "Warning: flash-attn is not installed." — this is normal and expected. The app falls back to standard PyTorch attention.

## Debugging a Failed Generation

If generation fails:

1. Open browser DevTools (F12 > Network tab)
2. Click Generate
3. Find the `/api/generate` request, check the response body — it should have a `detail` field with the error message
4. Cross-reference with `~/.qwen_tts_studio/app.log` for the full Python traceback

## Quick API Test (from terminal)

```bash
# Check server is up
curl http://127.0.0.1:8001/api/profiles

# Test generation directly
curl -X POST http://127.0.0.1:8001/api/generate \
  -F "text=Hello, this is a test." \
  -F "language=English" \
  -F "model_size=1.7B" \
  -F "model_type=Base" \
  -F "profile_id=__builtin_default__" \
  -o test_output.wav

# If it returns JSON instead of a .wav, the JSON contains the error
```

## Rebuilding the App

If source changes are needed:
```bash
cd /Users/parkerallen/programming/digital_team/qwen_tts/qwen-tts-studio

# Kill any running instance
pkill -f LocalTTSStudio; lsof -ti tcp:8001 | xargs kill 2>/dev/null

# Rebuild
venv/bin/python3 -m PyInstaller LocalTTSStudio.spec --noconfirm

# Install
rm -rf "/Applications/Local TTS Studio.app"
cp -R "dist/Local TTS Studio.app" "/Applications/Local TTS Studio.app"
```
