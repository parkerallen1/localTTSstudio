# Qwen TTS Studio — Audit Report

**Date:** 2025-02-25
**Scope:** Security vulnerabilities, hidden bugs, size/performance optimization

---

## 1. Crash Bug

### `static_dir` referenced before it's defined
**File:** `main.py:48` (used) vs `main.py:199` (defined)

The `BUILTIN_PROFILE` dict on line 48 references `static_dir`, but that variable isn't defined until line 199. Python executes module-level code top-to-bottom, so importing `main.py` would raise `NameError: name 'static_dir' is not defined` at startup — **unless** the builtin profile code path is never actually triggered before the server mounts statics.

**Fix:** Move the `static_dir` definition (line 199) up above `BUILTIN_PROFILE`, or define it once near the top of the file.

---

## 2. Security Vulnerabilities

### 2a. Path traversal in profile upload
**File:** `main.py:223`
```python
audio_path = os.path.join(PROFILES_DIR, f"{profile_id}_{ref_audio.filename}")
```
The uploaded filename is used directly. A crafted filename like `../../etc/passwd` would write outside `PROFILES_DIR`. The UUID prefix doesn't prevent the `../` sequences in the filename portion.

**Fix:** Use `os.path.basename(ref_audio.filename)` or ignore the original filename entirely and use `{uuid}.wav`.

### 2b. No upload size limits
**Files:** `main.py:219`, `main.py:370`, `main.py:410`

All three upload endpoints (`/api/generate`, `/api/merge`, `/api/treat`) accept files with no size limit. A malicious or accidental multi-GB upload would exhaust memory (since `await file.read()` loads the entire file into RAM).

**Fix:** Add a size check or use FastAPI's streaming upload with a cap, or add middleware to limit request body size.

### 2c. Temp file accumulation (disk exhaustion)
**Files:** `main.py:353`, `main.py:392`, `main.py:432`

All three audio endpoints create `NamedTemporaryFile(delete=False)` but never clean them up after the response is sent. Over many requests, `/tmp` fills with orphaned WAV files. The comment on line 361 acknowledges this:
```python
background=None  # File will be cleaned up by the OS later
```
The OS does **not** clean up named temp files automatically — they persist until reboot.

**Fix:** Use FastAPI's `BackgroundTask` to `os.unlink()` the temp file after the response streams, e.g.:
```python
from starlette.background import BackgroundTask
return FileResponse(..., background=BackgroundTask(os.unlink, temp_file.name))
```

### 2d. No input validation on `model_size` / `model_type`
**File:** `main.py:263-264`

The `model_size` and `model_type` form fields are passed directly into the HuggingFace model ID string:
```python
expected_model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
```
While this doesn't enable code execution (it just forms a string for HF Hub), it could cause unexpected model downloads or confusing error messages. Validating against a whitelist (`["0.6B", "1.7B"]` and `["Base", "CustomVoice", "VoiceDesign"]`) would be cleaner.

---

## 3. Bugs

### 3a. Duplicate `import os`
**File:** `main.py:1` and `main.py:10`

Harmless but indicates the imports section was edited piecemeal.

### 3b. Unused `import subprocess`
**File:** `main.py:406`

`subprocess` is imported but never used — ffmpeg is invoked via `asyncio.create_subprocess_exec` instead.

### 3c. Redundant `import json` inside function
**File:** `main.py:186`

`json` is already imported at the module level (line 16). The inner import on line 186 inside `event_generator()` is unnecessary.

### 3d. `download_model.py` uses `float16` instead of `bfloat16`
**File:** `download_model.py:8`

Main app explicitly uses `bfloat16` because float16 "was causing overflow on 0.6B" (main.py:152). But the download script uses `float16`, which would produce corrupted/NaN audio on MPS with the 0.6B model.

### 3e. Bible abbreviation replacements aren't word-bounded
**File:** `script.js:550-577`

Replacements like `text.replace(/AMP/g, ...)` have no word-boundary anchors (`\b`). This means:
- "AMP" matches inside "RAMP", "AMPLE", "TRAMPOLINE"
- "ASV" matches inside "VASV..."
- "MSG" matches inside any occurrence of those three letters

Non-Bible text gets corrupted silently. Same applies to all ~25 abbreviation replacements.

**Fix:** Wrap each pattern in `\b` word boundaries: `/\bAMP\b/g`.

### 3f. Colon replacement is too aggressive
**File:** `script.js:583`
```javascript
text.replace(/:/g, ', ')
```
This replaces ALL colons in the text with commas — including colons in URLs, time notation ("3:00 PM"), or any non-Biblical context. It runs after the verse formatter but affects everything that remains.

### 3g. `requests` dependency missing from requirements.txt
**File:** `app_launcher.py:11` imports `requests`, but `requirements.txt` doesn't list it. Fresh installs from requirements.txt would fail on `app_launcher.py`.

### 3h. SSE generator runs indefinitely on the server
**File:** `main.py:181-194`

The `/api/progress` SSE generator never breaks out of its loop — even after status reaches "ready" or "error", it keeps looping (sleeping 2s). While the client closes the EventSource, the server coroutine stays alive until `request.is_disconnected()` eventually returns True. With multiple browser tabs or reconnects, these pile up.

### 3i. `test_ui.py` uses wrong port
The test file likely references port 8000, but the production server runs on port 8001. Tests would fail without manual port editing.

---

## 4. Size Reduction Opportunities

The project directory is **4.5 GB**. Here's the breakdown:

| Directory/File | Size | Notes |
|---|---|---|
| `dist/` | 3.3 GB | PyInstaller output |
| `venv/` | 1.1 GB | Python virtual environment |
| `build/` | 108 MB | PyInstaller build cache |
| `ffmpeg` (binary) | 76 MB | Bundled ffmpeg executable |
| `data/` | 13 MB | User voice profiles |
| `qwen_tts_icon.icns` | 2.6 MB | App icon |
| `static/builtin/` | 1.6 MB | Built-in voice assets |
| **App source code** | **~30 KB** | The actual app |

### 4a. Add a `.gitignore` (critical)
There is **no `.gitignore` file**. This means `dist/` (3.3 GB), `build/` (108 MB), `venv/` (1.1 GB), and `__pycache__/` could all end up tracked in version control. A basic gitignore would cut the repo from 4.5 GB to under 100 MB.

```gitignore
dist/
build/
venv/
__pycache__/
*.pyc
.DS_Store
```

### 4b. Don't bundle ffmpeg — use system ffmpeg
The 76 MB `ffmpeg` binary is the largest single file after the PyInstaller output. For the development workflow, you can require users to install ffmpeg via `brew install ffmpeg` and remove the bundled binary. For the PyInstaller build, you'd still bundle it — but keep it out of the source repo.

### 4c. Drop Google Fonts CDN — use system fonts
**File:** `index.html:9`

For a local-first app, loading Inter from `fonts.googleapis.com` adds an external network dependency and ~300 KB of font data. The system font stack already looks great:
```css
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
```
This eliminates the external request, speeds up initial paint, and works offline.

### 4d. Shrink the .icns icon
The app icon is **2.6 MB**, which is unusually large for an .icns file (most are 100-500 KB). It likely includes uncompressed high-resolution variants. Re-exporting with `iconutil` or an icon tool with compression would cut it to ~200-400 KB.

### 4e. Clean up test/dev files from distribution
`test_tts.py`, `test_ui.py`, `test_main_fix.py`, and `download_model.py` are development artifacts that don't need to ship. They could live in a `tests/` directory excluded from the PyInstaller build.

---

## 5. Performance Improvements

### 5a. Use streaming responses instead of temp files
**Files:** `main.py:343-362`

Currently, generated audio goes through: numpy array → BytesIO → temp file → FileResponse. The BytesIO-to-temp-file copy is unnecessary. You can return a `StreamingResponse` directly from the BytesIO buffer:
```python
buffer = io.BytesIO()
sf.write(buffer, audio_data, sr, format="WAV")
buffer.seek(0)
return StreamingResponse(buffer, media_type="audio/wav")
```
This eliminates the temp file entirely for `/api/generate`.

### 5b. Return compressed audio (OGG/Opus) instead of WAV
WAV is uncompressed. A 1-minute clip at 24kHz 16-bit is ~2.8 MB as WAV vs ~100-200 KB as Opus/OGG. Since the browser natively plays OGG, switching the API responses to OGG would:
- Reduce network transfer by ~15x per segment
- Speed up merge/download operations
- Reduce browser memory usage for stored blobs
- Keep WAV only as the final download format if lossless is desired

### 5c. Background CSS animations waste GPU/battery
**File:** `style.css:42-73`

The two `.glow` elements animate continuously with `blur(120px)` on 600x600px circles. On laptops, this quietly drains battery. Options:
- Add `@media (prefers-reduced-motion: reduce)` to disable them
- Use `will-change: transform` to hint the compositor
- Or simply make them static — the visual difference is subtle

### 5d. Multiple `backdrop-filter: blur()` layers
**File:** `style.css:200, 557`

`backdrop-filter: blur()` is GPU-expensive. It's applied to every `.glass` panel (at least 3), plus the activity log and modal. On lower-end machines this causes frame drops during scrolling. Consider using a simple semi-transparent background without the blur, or limiting blur to just the modal overlay.

### 5e. Adaptive concurrency for generation pool
**File:** `script.js:68`

The concurrency pool is hardcoded at 3. On CPU, running 3 concurrent model inferences causes severe thrashing (each inference is already CPU-bound). On MPS, the backend serializes them anyway. The server could expose a `/api/capabilities` endpoint indicating the device type, and the frontend could adjust concurrency (1 for CPU, 3 for GPU).

### 5f. Pin dependency versions
**File:** `requirements.txt`

No versions are pinned. A `pip install` on a fresh machine could pull incompatible versions of torch, fastapi, etc. Pin at least the major versions:
```
fastapi>=0.100,<1.0
uvicorn>=0.20,<1.0
torch>=2.0,<3.0
# etc.
```

---

## 6. Quick Wins Summary (effort vs impact)

| Priority | Item | Effort | Impact |
|---|---|---|---|
| **P0** | Fix `static_dir` crash bug (3a/sec 1) | 2 min | App may not start |
| **P0** | Add `.gitignore` | 2 min | Saves 4.4 GB from repo |
| **P1** | Fix path traversal in upload | 5 min | Security |
| **P1** | Clean up temp files with BackgroundTask | 10 min | Prevents disk exhaustion |
| **P1** | Add `\b` to Bible replacements | 15 min | Fixes text corruption |
| **P1** | Add `requests` to requirements.txt | 1 min | Fixes fresh installs |
| **P2** | StreamingResponse instead of temp files | 15 min | Fewer temp files, simpler code |
| **P2** | Drop Google Fonts for system fonts | 5 min | Works offline, faster load |
| **P2** | Fix download_model.py dtype | 1 min | Fixes test script |
| **P3** | Compressed audio (OGG) responses | 30 min | 15x smaller transfers |
| **P3** | Remove bundled ffmpeg from source | 5 min | 76 MB smaller repo |
| **P3** | Shrink .icns icon | 10 min | 2+ MB smaller |
| **P3** | Reduce backdrop-filter usage | 10 min | Better perf on low-end |
