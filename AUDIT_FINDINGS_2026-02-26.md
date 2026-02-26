# Qwen TTS Studio Audit Findings (2026-02-26)

## Scope

Static review of the local source and packaging artifacts in this workspace (`main.py`, `app_launcher.py`, `static/*`, test scripts, PyInstaller spec, requirements, and top-level file sizes). I did not run a full end-to-end app session.

## Executive Summary

The app is functional in concept, but there are a few high-impact issues:

- A likely startup crash in `main.py` (`static_dir` is used before it is defined).
- Multiple temporary-file leaks (generated/merged/treated audio files are never deleted).
- Async endpoints run heavy synchronous work, which blocks the event loop and makes the app feel slower than it should.
- Localhost API is not hardened (no auth/CSRF protection, no upload limits, path trust issues for stored profile audio paths).
- The app is very large mostly because build artifacts and a full local `venv` are kept in the project, and the packaged app contains duplicated large runtimes/dependencies.

## High-Priority Findings (Bugs / Reliability)

### 1. Startup bug: `static_dir` referenced before assignment

- `main.py:48` uses `static_dir` inside `BUILTIN_PROFILE`.
- `static_dir` is only defined later at `main.py:199`.
- This should raise `NameError` during module import.

Impact:

- App may fail to start before FastAPI is initialized.

### 2. Temp file leaks on every generation / merge / treat request

Files are created with `NamedTemporaryFile(delete=False)` and returned via `FileResponse`, but they are not cleaned up afterward:

- `main.py:353` to `main.py:362` (`/api/generate`)
- `main.py:392` to `main.py:400` (`/api/merge`)
- `main.py:474` to `main.py:478` (`/api/treat`)

Additional leak path:

- In `/api/generate`, uploaded clone audio temp files are only deleted on success (`main.py:334` to `main.py:336`). If generation fails, temp input remains.

Impact:

- Temp directory growth over time, disk bloat, degraded reliability.

### 3. Heavy synchronous model/audio work blocks async server

`async` endpoints call CPU/GPU-heavy synchronous functions directly:

- `main.py:279` (`generate_custom_voice`)
- `main.py:291` (`generate_voice_design`)
- `main.py:323` (`generate_voice_clone`)
- `main.py:375` to `main.py:395` (`pydub` merge/export)

Impact:

- Uvicorn event loop is blocked during generation/merge.
- UI appears slower or frozen under concurrent requests.
- Frontend concurrency (`static/script.js:65` to `static/script.js:81`) does not produce true parallelism and may worsen queueing/memory pressure.

### 4. Frontend object URL memory leak during regenerate

In `window.generateSingle`, old audio URLs are nulled before revoke:

- `static/script.js:161` to `static/script.js:163`
- `static/script.js:199` to `static/script.js:201`

Because `para.audioUrl` is set to `null` first, the previous object URL is never revoked.

Impact:

- Browser memory usage grows with repeated regenerations.

## Security / Hardening Findings (Local App, but still worth fixing)

These are lower-risk than internet-exposed services because the launcher binds to `127.0.0.1` (`app_launcher.py:15`, `app_launcher.py:18`), but they still matter for local abuse and robustness.

### 5. No upload size limits; endpoints read whole files into memory

Examples:

- `main.py:226` (`await ref_audio.read()`)
- `main.py:319` (`await ref_audio.read()`)
- `main.py:378` (`await file.read()`)
- `main.py:425` (`await audio_file.read()`)

Impact:

- Easy memory exhaustion / app crash from large uploads.
- Slow behavior on large files.

### 6. Local API has no auth / CSRF protection

The app exposes state-changing localhost endpoints:

- `/api/profiles` create/delete
- `/api/generate`
- `/api/merge`
- `/api/treat`

Impact:

- Any local process can call the API.
- Browser-based loopback abuse (cross-site POSTs to localhost) is possible even if responses are not readable cross-origin.

### 7. Profile deletion trusts stored `audio_path` without confinement check

`delete_profile` removes whatever path is stored in `profiles.json`:

- `main.py:251` to `main.py:253`

If `profiles.json` is tampered with, the app can delete arbitrary files accessible to the user account.

Impact:

- Arbitrary local file deletion via corrupted/malicious profile metadata.

### 8. UI log uses `innerHTML` with unsanitized message text (XSS vector)

- `static/script.js:23` to `static/script.js:31`

`log()` writes `msg` via `innerHTML`. Some log messages include user-controlled strings (for example profile names via delete/save flows).

Impact:

- DOM injection/XSS within the local UI if a malicious profile name/message is introduced.

### 9. Internal file paths exposed to frontend

`/api/profiles` returns profile objects containing `audio_path`:

- `main.py:210` to `main.py:213`
- `main.py:233`

Impact:

- Unnecessary leakage of local filesystem paths to browser JS.

## Medium-Priority Bugs / Maintenance Issues

### 10. `torch.has_mps` compatibility risk

Used directly in cleanup paths:

- `main.py:102`
- `main.py:138`

Elsewhere the code correctly checks `torch.backends.mps.is_available()` (`main.py:148`).

Impact:

- Potential attribute/version compatibility issues on some PyTorch builds.

### 11. Tests are stale / not aligned with current UI

`test_ui.py` references UI IDs that do not exist in current `static/index.html`:

- `test_ui.py:17` expects `model-size-select`
- `test_ui.py:21` expects `global-treatment-select`

Also uses port `8000` (`test_ui.py:12`) while launcher uses `8001` (`app_launcher.py:14`).

Impact:

- False confidence; tests do not validate current app.

### 12. Requirements are incomplete and unpinned

- `app_launcher.py:11` imports `requests`, but `requirements.txt` does not include `requests`.
- `test_ui.py` requires `selenium`, also not in `requirements.txt`.
- `requirements.txt` is fully unpinned (`requirements.txt:1` to `requirements.txt:7`).

Impact:

- Reproducibility issues.
- Fresh installs may fail depending on transitive deps.
- Version drift can introduce regressions/security issues.

### 13. `download_model.py` appears outdated vs current MPS dtype strategy

- `download_model.py:8` uses `torch.float16` on MPS.
- `main.py:149` to `main.py:154` switched to `torch.bfloat16` for stability.

Impact:

- Auxiliary script may fail or mislead users during model setup.

## Size / Footprint Findings (Why the app feels big)

Measured top-level directory sizes in this workspace:

- `dist/`: ~3.3G
- `venv/`: ~1.1G
- `build/`: ~108M
- `ffmpeg`: ~76M
- `data/`: ~13M

### Biggest packaged artifacts (examples)

From `dist/Qwen3TTS-Studio.app/Contents/Frameworks` and `dist/Qwen3TTS-Studio/_internal`:

- `torch`: ~436M to ~457M (per copy)
- `ffmpeg`: ~77M (per copy)
- `libLLVM-11.dylib`: ~81M
- `bokeh`: ~78M (`_internal`)
- Many MKL libraries (multiple 10s of MB each), including duplicated variants

### Major issue: duplicated packaged output in `dist/`

The project currently contains both:

- `dist/Qwen3TTS-Studio.app`
- `dist/Qwen3TTS-Studio/`

These appear to duplicate most bundled binaries/runtime files, which massively inflates repository and disk usage.

### `venv` is carrying many heavy packages likely unrelated to runtime needs

Examples from `venv/lib/python3.10/site-packages`:

- `torch` ~382M
- `llvmlite` ~112M
- `transformers` ~95M
- `scipy` ~92M
- `onnxruntime` ~66M
- `pandas` ~60M
- `gradio` ~56M
- `sklearn` ~42M
- `numba` ~23M

This strongly suggests the build environment is “dirty” (extra packages installed beyond what this app uses directly), which causes PyInstaller to pull in far more than needed.

## Performance / UX Findings

### 14. Launcher imports heavy backend on startup (slower app launch)

`app_launcher.py` imports `from main import app` at module import time (`app_launcher.py:12`), which imports `torch`, `numpy`, audio libs, and backend setup before the server thread starts.

Impact:

- Slower startup.
- More brittle startup failures (one import error kills launch).

### 15. “Local / offline” UX inconsistency

`static/index.html:9` loads Google Fonts from the internet.

Impact:

- App is not fully offline/self-contained on first load.
- Extra startup latency or failures on restricted networks.

## Ideas To Make It Smaller, Faster, and Easier To Run (No Changes Applied Yet)

## Quick Wins (highest ROI)

1. Stop storing generated artifacts in the project folder by default

- Add a `.gitignore` (none is present in this folder).
- Ignore `dist/`, `build/`, `venv/`, `__pycache__/`, temp `.wav` outputs.
- Keep release artifacts outside the source workspace or attach them to releases.

2. Fix temp-file lifecycle

- Use FastAPI `BackgroundTasks` to delete temp outputs after response send.
- Add `try/finally` cleanup for all temp inputs and failure paths.

3. Add server-side limits and validation

- Upload size caps (FastAPI/ASGI middleware or manual stream limits).
- Enum validation for `model_type`, `model_size`, `treatment_type`.
- Validate profile audio path stays under `PROFILES_DIR` before delete/use.

4. Make UI log safe

- Replace `innerHTML` with `textContent` + explicit DOM nodes.

5. Pin and split dependencies

- `requirements-runtime.txt` (app only)
- `requirements-dev.txt` (tests/tools)
- Explicitly add `requests` and `selenium` only where needed

## Bigger Size Reductions (Packaging)

1. Build from a clean, minimal virtual environment

- Install only runtime deps.
- Avoid building from a general-purpose/Anaconda-heavy environment.
- This is likely the single biggest lever for shrinking `dist`.

2. Audit PyInstaller imports and exclude unused transitive stacks

Current `dist` includes packages that look unrelated to this app (examples: `bokeh`, `boto`, conda-related modules, etc.).

- Tighten `hiddenimports`
- Add more `excludes`
- Inspect PyInstaller analysis/xref output to identify why these are included

3. Separate release variants

- CPU-only build
- Apple Silicon/MPS build
- CUDA build

This avoids shipping unnecessary binaries to every user.

4. Reconsider bundling `ffmpeg`

- Optional external dependency (system install) or first-run download
- Or platform-specific compressed asset fetched on demand

Tradeoff:

- Smaller app bundle vs easier first-run setup.

5. Avoid duplicate packaged outputs in `dist`

- Keep only the `.app` (macOS release) or only the unpacked folder for local testing, not both in source storage.

## Performance Improvements

1. Offload generation/merge to worker threads/processes

- Use `asyncio.to_thread(...)` (or a dedicated worker queue) for blocking generation and audio processing.
- Add a server-side semaphore to cap concurrent generations based on device.

2. Stream/pipe audio processing where possible

- Avoid reading full uploads into memory for large files.
- Consider ffmpeg concat flow instead of `AudioSegment` accumulation for long multi-segment jobs.

3. Lazy-load heavy modules

- Move heavy imports/model setup into request paths or startup hooks where appropriate.
- In launcher, import backend inside `run_server()` to reduce initial failure surface.

4. Cache/cleanup strategy

- Periodic cleanup of temp outputs.
- Optional generated-audio cache with TTL if users regenerate frequently.

## Easier To Run / Maintain

1. Replace ad hoc scripts with a real test split

- Unit tests for profile CRUD and path validation
- API tests for upload/generate/treat failure paths
- One optional integration test for model generation (skipped by default)

2. Update/remove stale test files

- `test_ui.py` and some helper scripts appear out of sync with current UI/backend behavior.

3. Add a simple startup self-check

- Verify `ffmpeg` availability
- Verify writable data dir
- Verify model package importability
- Show actionable errors in UI

## Suggested Prioritization (if you want me to implement later)

1. Fix startup crash + temp file leaks
2. Add upload limits/path validation/XSS-safe logging
3. Move blocking work off event loop
4. Clean packaging environment + shrink PyInstaller bundle
5. Refresh tests and dependency manifests

