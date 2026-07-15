"""
Local TTS Studio — backend server (FastAPI).

The heart of the app: a local FastAPI server that serves the web UI (static/)
and exposes the JSON API the frontend (static/script.js) calls. It loads
Qwen3-TTS models on demand, synthesizes speech, and persists work as "projects"
on disk.

Responsibilities
  • Model lifecycle — lazily load and cache Qwen3-TTS models (modes: Base
    voice-cloning, CustomVoice, VoiceDesign; sizes: 0.6B / 1.7B), freeing the
    previous model before loading another. See get_tts_model().
  • Speech synthesis — POST /api/generate turns one paragraph of text into audio.
  • Projects — CRUD over saved projects (raw text, settings, and per-paragraph
    audio). Audio is stored as FLAC on disk (see migrate_audio_to_flac.py).
  • Voice profiles — saved CustomVoice / cloning profiles under PROFILES_DIR.
  • Audio export — merge per-paragraph clips into a single download, with
    optional M4A conversion via the bundled ffmpeg (see _ffmpeg_bin()).
  • Activity log — a ring buffer streamed to the UI over Server-Sent Events.
  • Auto-update — check GitHub Releases (GITHUB_REPO) and self-update the frozen
    .app. See /api/check_update and /api/do_update.

Run context: started in a background thread by app_launcher.py (the desktop
entrypoint). When frozen by PyInstaller, bundled files resolve under
sys._MEIPASS and data lives in ~/.qwen_tts_studio; in dev, data lives in ./data
(see DATA_DIR below). APP_VERSION is the single source of truth for the version
and is what the in-app updater compares against the latest GitHub release.
"""
import os
import sys
import io
import tempfile
import shutil
import platform
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import uuid
import numpy as np
import soundfile as sf
import torch
import asyncio
import json
import gc

def _ffmpeg_bin():
    """Path to the ffmpeg binary — bundled when frozen, else PATH, else the repo copy."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, 'ffmpeg')
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    # Running from source under launchd/cron gives a minimal PATH with no
    # ffmpeg — fall back to the macOS binary checked into the repo, otherwise
    # /api/treat and /api/convert fail and M4A exports silently become WAV.
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    if sys.platform == "darwin" and os.path.isfile(local):
        return local
    return "ffmpeg"

def _remove_quietly(*paths):
    """Best-effort removal of temp files; ignores missing paths and OS errors."""
    for p in paths:
        if not p:
            continue
        try:
            os.unlink(p)
        except OSError:
            pass

from typing import List, Optional
import requests
import subprocess
import time as _time
import text_parser
from collections import deque

APP_VERSION = "3.8.5" # Current application version
GITHUB_REPO = "parkerallen1/localTTSstudio" # Actual repo for OTA updates

# ─── Activity Log ─────────────────────────────────────────────────────────────
# Ring-buffer of recent log entries streamed to the UI via SSE.
MAX_LOG_ENTRIES = 200
_activity_log: deque = deque(maxlen=MAX_LOG_ENTRIES)
_log_version = 0  # bumped on every new entry so SSE knows when to push

def emit_log(message: str, level: str = "info"):
    """Append a structured log entry and bump the version counter."""
    global _log_version
    _log_version += 1
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,   # info | ok | warn | error
        "msg": message,
        "v": _log_version,
    }
    _activity_log.append(entry)
    # Also print to stdout for terminal debugging
    print(f"[LOG/{level}] {message}")

# We attempt to import qwen_tts but catch the error if it fails during initial import
try:
    from qwen_tts import Qwen3TTSModel
    HAS_QWEN = True
except Exception as _e:
    import traceback
    traceback.print_exc()
    print(f"qwen_tts import failed: {_e}")
    HAS_QWEN = False
    emit_log(f"qwen_tts import failed: {_e}", "error")

# Profile Storage Setup
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.expanduser("~/.qwen_tts_studio")
else:
    DATA_DIR = "data"

PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
PROFILES_FILE = os.path.join(PROFILES_DIR, "profiles.json")
os.makedirs(PROFILES_DIR, exist_ok=True)

PROJECTS_DIR = os.path.join(DATA_DIR, "projects")
os.makedirs(PROJECTS_DIR, exist_ok=True)

if not os.path.exists(PROFILES_FILE):
    with open(PROFILES_FILE, "w") as f:
        json.dump([], f)

# Built-in voice profile that is always available and cannot be deleted
BUILTIN_PROFILE_ID = "__builtin_default__"
BUILTIN_PROFILE = {
    "id": BUILTIN_PROFILE_ID,
    "name": "Jennifer",
    "ref_text": "Settle in. Take a deep breath. Turn off notifications on your phone if you can. Ask God to give you a new perspective and to help",
    "audio_path": os.path.join(os.path.dirname(__file__), "static", "builtin", "default_voice.wav"),
    "builtin": True
}

def load_profiles():
    with open(PROFILES_FILE, "r") as f:
        user_profiles = json.load(f)
    # Always prepend the built-in profile
    return [BUILTIN_PROFILE] + user_profiles

def save_profiles(profiles):
    # Filter out the built-in profile before saving
    user_profiles = [p for p in profiles if p.get("id") != BUILTIN_PROFILE_ID]
    with open(PROFILES_FILE, "w") as f:
        json.dump(user_profiles, f, indent=4)

# Signalled by the OTA endpoint so app_launcher.py can trigger a clean restart
import threading as _threading
ota_requested = _threading.Event()

# Global context for the model to keep it loaded
model = None
current_model_id = None
model_lock = None
generation_lock = None  # Serializes inference calls — MPS is not thread-safe

# Global progress state — extended schema streamed to the UI via SSE
download_progress = {
    "status": "idle",       # idle, downloading, stalled, ready, error
    "phase": "idle",        # idle, connecting, downloading, stalled — finer-grained than status
    "progress": 0.0,
    "description": "",
    "repo_id": "",
    "bytes_done": 0,
    "bytes_total": 0,
    "rate_bps": 0,
    "eta_seconds": None,
    "elapsed_seconds": 0,   # since current session started
    "idle_seconds": 0,      # since last tqdm update (0 if no updates yet)
    "current_file": "",
    "error_kind": "",       # network, auth, disk, not_found, corrupt, unknown
}

# Aggregate download-session state — reset at the start of each download
_download_session: dict = {
    "active": False,
    "repo_id": "",
    "started_ts": 0.0,
    "bytes_done": 0,
    "bytes_total": 0,
    "current_file": "",
    "_rate_window": [],     # list of (monotonic_ts, cumulative_bytes_done)
}
_session_lock = _threading.Lock()

# ─── Format helpers ───────────────────────────────────────────────────────────

def format_size(bytes_val):
    """Return a human-readable file size string (e.g. '1.2 GB', '340 MB')."""
    if bytes_val is None:
        return "unknown"
    try:
        b = float(bytes_val)
    except (TypeError, ValueError):
        return "unknown"
    if b >= 1e9:
        return f"{b / 1e9:.1f} GB"
    elif b >= 1e6:
        return f"{b / 1e6:.0f} MB"
    elif b >= 1e3:
        return f"{b / 1e3:.0f} KB"
    return f"{b:.0f} B"

# ─── HF download intercept ────────────────────────────────────────────────────

# Timestamp of the most recent tqdm update call — used by the download watchdog.
_last_tqdm_update_ts: float = 0.0

# We can hack huggingface_hub's tqdm to intercept progress
from huggingface_hub.utils import tqdm as hf_tqdm

class InterceptTqdm(hf_tqdm):
    def update(self, n=1):
        global _last_tqdm_update_ts
        super().update(n)
        now = _time.monotonic()
        _last_tqdm_update_ts = now

        if hasattr(self, 'total') and self.total and n > 0:
            # Log start of this individual file
            if not getattr(self, '_emitted_start', False):
                self._emitted_start = True
                self._emitted_pct_boundary = 0
                self._start_ts = now
                emit_log(
                    f"HF download started: {self.desc} (total={format_size(self.total)})",
                    "info"
                )

            # Update aggregate session state under lock (multiple threads download in parallel)
            with _session_lock:
                if not getattr(self, '_registered_bytes', False):
                    self._registered_bytes = True
                    _download_session["bytes_total"] += self.total

                _download_session["bytes_done"] += n
                _download_session["current_file"] = self.desc or ""

                window = _download_session["_rate_window"]
                window.append((now, _download_session["bytes_done"]))
                cutoff = now - 5.0
                while window and window[0][0] < cutoff:
                    window.pop(0)

                rate_bps = 0.0
                if len(window) >= 2:
                    dt = window[-1][0] - window[0][0]
                    db = window[-1][1] - window[0][1]
                    if dt > 0:
                        rate_bps = db / dt

                bytes_done = _download_session["bytes_done"]
                bytes_total = _download_session["bytes_total"]
                remaining = bytes_total - bytes_done
                eta_s = round(remaining / rate_bps) if rate_bps > 0 and remaining > 0 else None
                pct = round(min(100.0, bytes_done / bytes_total * 100), 1) if bytes_total > 0 else 0.0

            # Write to download_progress outside the lock (dict writes are GIL-safe)
            download_progress["status"] = "downloading"
            download_progress["phase"] = "downloading"
            download_progress["progress"] = pct
            download_progress["bytes_done"] = bytes_done
            download_progress["bytes_total"] = bytes_total
            download_progress["rate_bps"] = round(rate_bps)
            download_progress["eta_seconds"] = eta_s
            download_progress["current_file"] = self.desc or ""
            session_started = _download_session.get("started_ts", 0.0)
            if session_started > 0:
                download_progress["elapsed_seconds"] = int(now - session_started)
            download_progress["idle_seconds"] = 0

            # Log 10%-boundary and completion for this individual file
            file_pct = (self.n / self.total) * 100
            boundary = int(file_pct // 10) * 10
            last_boundary = getattr(self, '_emitted_pct_boundary', 0)
            if boundary > last_boundary and boundary > 0:
                self._emitted_pct_boundary = boundary
                emit_log(
                    f"HF download: {self.desc} {boundary}%"
                    f" ({format_size(self.n)}/{format_size(self.total)})",
                    "info"
                )
            if self.n >= self.total and not getattr(self, '_emitted_done', False):
                self._emitted_done = True
                elapsed = now - getattr(self, '_start_ts', now)
                emit_log(
                    f"HF download complete: {self.desc} ({format_size(self.total)}) in {elapsed:.1f}s",
                    "ok"
                )

# Monkey patch huggingface hub tqdm
import huggingface_hub.utils as hf_utils
hf_utils.tqdm = InterceptTqdm

# ─── Download session helpers ─────────────────────────────────────────────────

def _begin_download_session(repo_id: str):
    global _last_tqdm_update_ts
    # Reset the tqdm-update timestamp so the watchdog doesn't false-positive
    # "stalled" on the first poll of a fresh session (before any byte has flowed).
    _last_tqdm_update_ts = 0.0
    with _session_lock:
        _download_session["active"] = True
        _download_session["repo_id"] = repo_id
        _download_session["started_ts"] = _time.monotonic()
        _download_session["bytes_done"] = 0
        _download_session["bytes_total"] = 0
        _download_session["current_file"] = ""
        _download_session["_rate_window"] = []
    download_progress["repo_id"] = repo_id
    download_progress["phase"] = "connecting"
    download_progress["bytes_done"] = 0
    download_progress["bytes_total"] = 0
    download_progress["rate_bps"] = 0
    download_progress["eta_seconds"] = None
    download_progress["elapsed_seconds"] = 0
    download_progress["idle_seconds"] = 0
    download_progress["current_file"] = ""
    download_progress["error_kind"] = ""

def _end_download_session():
    with _session_lock:
        _download_session["active"] = False
    download_progress["phase"] = "idle"
    download_progress["idle_seconds"] = 0

# ─── Error classification ─────────────────────────────────────────────────────

def _classify_error(exc: Exception, repo_id: str = "") -> tuple:
    """Returns (error_kind: str, human_message: str)."""
    exc_str = str(exc)
    exc_type = type(exc).__name__

    # Try to access HF-specific exception types (location varies by version)
    _HfHubHTTPError = _RepositoryNotFoundError = _EntryNotFoundError = None
    try:
        from huggingface_hub.errors import (
            HfHubHTTPError, RepositoryNotFoundError, EntryNotFoundError,
        )
        _HfHubHTTPError = HfHubHTTPError
        _RepositoryNotFoundError = RepositoryNotFoundError
        _EntryNotFoundError = EntryNotFoundError
    except ImportError:
        try:
            from huggingface_hub.utils import (
                HfHubHTTPError, RepositoryNotFoundError, EntryNotFoundError,
            )
            _HfHubHTTPError = HfHubHTTPError
            _RepositoryNotFoundError = RepositoryNotFoundError
            _EntryNotFoundError = EntryNotFoundError
        except ImportError:
            pass

    if _HfHubHTTPError and isinstance(exc, _HfHubHTTPError):
        resp = getattr(exc, 'response', None)
        code = resp.status_code if resp is not None else 0
        if code in (401, 403):
            return ("auth",
                    "Authorization failed. The model may be private or your "
                    "HF_TOKEN may be missing or expired.")
        if code == 404:
            return ("not_found", f"Model not found on Hugging Face: {repo_id}.")
        if code >= 500:
            return ("network",
                    "Hugging Face server error. Try again in a few minutes.")

    if _RepositoryNotFoundError and isinstance(exc, _RepositoryNotFoundError):
        if "401" in exc_str or "403" in exc_str:
            return ("auth",
                    "Authorization failed. The model may be private or your "
                    "HF_TOKEN may be missing or expired.")
        return ("not_found", f"Model not found on Hugging Face: {repo_id}.")

    if _EntryNotFoundError and isinstance(exc, _EntryNotFoundError):
        return ("not_found", f"Model files not found on Hugging Face: {repo_id}.")

    # Disk / IO errors
    if isinstance(exc, OSError):
        eno = getattr(exc, 'errno', None)
        if eno == 28 or "No space left" in exc_str:
            return ("disk",
                    "Out of disk space while downloading. Free up space and retry.")
        if eno == 30:
            return ("disk",
                    "Cache directory is read-only. Check permissions on ~/.cache/huggingface.")

    # Network / connectivity errors
    network_signals = (
        "ConnectionError", "TimeoutError", "ConnectTimeout", "ReadTimeout",
        "ChunkedEncodingError", "ConnectionResetError", "gaierror",
        "Name or service not known", "nodename nor servname",
        "Connection refused", "Connection reset",
        "urlopen error", "RemoteDisconnected", "IncompleteRead",
    )
    if any(sig in exc_str or sig in exc_type for sig in network_signals):
        return ("network",
                "Network error reaching Hugging Face. Check your internet connection and retry.")

    return ("unknown", f"Unexpected error: {exc_str}")

# ─── Retry wrapper ────────────────────────────────────────────────────────────

def _with_retry(fn, *fn_args, max_attempts=3, base_delay=2.0, retry_repo_id="", **fn_kwargs):
    """Sync wrapper: call fn(*fn_args, **fn_kwargs) with exponential backoff on transient errors."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*fn_args, **fn_kwargs)
        except Exception as exc:
            kind, _ = _classify_error(exc, retry_repo_id)
            if kind != "network" or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            emit_log(
                f"HF download retry {attempt + 1}/{max_attempts} in {delay:.0f}s "
                f"after: {type(exc).__name__}",
                "warn"
            )
            _time.sleep(delay)
            # Reset session bytes so progress restarts cleanly on retry
            if _download_session["active"]:
                _begin_download_session(_download_session["repo_id"])

# ─── Disk preflight ───────────────────────────────────────────────────────────

_MODEL_SIZE_ESTIMATE_GB = {
    ("0.6B", "Base"): 1.5, ("0.6B", "CustomVoice"): 1.5, ("0.6B", "VoiceDesign"): 1.5,
    ("1.7B", "Base"): 3.5, ("1.7B", "CustomVoice"): 3.5, ("1.7B", "VoiceDesign"): 3.5,
}

def _check_disk_space(size: str, model_type: str) -> Optional[str]:
    """Return an error message string if there is insufficient disk space, else None."""
    required_gb = _MODEL_SIZE_ESTIMATE_GB.get((size, model_type), 4.0)
    required_with_overhead = required_gb * 1.3
    try:
        hf_cache = _hf_cache_dir()
        check_path = hf_cache
        while not os.path.exists(check_path) and check_path not in ("/", ""):
            check_path = os.path.dirname(check_path)
        if not os.path.exists(check_path):
            check_path = os.path.expanduser("~")
        free_gb = shutil.disk_usage(check_path).free / 1e9
        if free_gb < required_with_overhead:
            return (
                f"Not enough disk space. Need ~{required_gb:.1f} GB free in "
                f"{hf_cache}, but only {free_gb:.1f} GB available."
            )
    except Exception:
        pass
    return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, current_model_id

    emit_log("Server starting up.", "info")

    # ── Rich startup diagnostics ──────────────────────────────────────────────
    emit_log(
        f"Python {sys.version.split()[0]} on {platform.system()} "
        f"{platform.release()} {platform.machine()}",
        "info"
    )
    _mps_str = (
        str(torch.backends.mps.is_available())
        if hasattr(torch.backends, 'mps') else 'n/a'
    )
    emit_log(f"Torch {torch.__version__}, MPS available: {_mps_str}", "info")
    emit_log(f"DATA_DIR: {DATA_DIR}", "info")
    _hf_cache_dir_startup = (
        os.environ.get("HF_HOME") or
        os.path.expanduser("~/.cache/huggingface")
    )
    emit_log(f"HF cache: {_hf_cache_dir_startup}", "info")
    try:
        _disk_free_gb_startup = shutil.disk_usage(DATA_DIR).free / 1e9
        emit_log(f"Disk free: {_disk_free_gb_startup:.1f} GB", "info")
    except Exception as _de:
        emit_log(f"Could not read disk free: {_de}", "warn")

    # ── Download-hang watchdog ─────────────────────────────────────────────────
    async def _download_watchdog():
        """Background task: surfaces stalled downloads to the UI after 30s of
        silence (only once at least one tqdm update has arrived — before that
        we consider the download to be in the 'connecting' phase, which can
        legitimately take a while on slow links / TLS handshakes / DNS)."""
        POLL_INTERVAL = 5
        STALL_THRESHOLD = 30
        CONNECTING_WARN_THRESHOLD = 45   # only warn about a slow connect after this long
        _warned_this_stall = False
        _warned_slow_connect = False
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            status = download_progress.get("status")
            if status in ("downloading", "stalled"):
                now = _time.monotonic()
                session_started = _download_session.get("started_ts", 0.0)
                elapsed = int(now - session_started) if session_started > 0 else 0
                download_progress["elapsed_seconds"] = elapsed

                had_first_byte = _last_tqdm_update_ts > 0
                if had_first_byte:
                    idle = int(now - _last_tqdm_update_ts)
                    download_progress["idle_seconds"] = idle
                    if idle >= STALL_THRESHOLD:
                        if not _warned_this_stall:
                            _warned_this_stall = True
                            emit_log(
                                f"Download appears stalled — no progress in {idle}s. "
                                "Check network/firewall.",
                                "warn"
                            )
                        download_progress["status"] = "stalled"
                        download_progress["phase"] = "stalled"
                    else:
                        if _warned_this_stall:
                            _warned_this_stall = False
                        download_progress["phase"] = "downloading"
                        # If we previously flipped status to "stalled" but data has resumed,
                        # the InterceptTqdm.update() path already flipped status back to
                        # "downloading", so nothing to do here.
                else:
                    # No tqdm updates yet — we're still in connect/handshake/DNS phase.
                    download_progress["idle_seconds"] = 0
                    download_progress["phase"] = "connecting"
                    if elapsed >= CONNECTING_WARN_THRESHOLD and not _warned_slow_connect:
                        _warned_slow_connect = True
                        emit_log(
                            f"Still connecting to Hugging Face after {elapsed}s — "
                            "slow link or firewall may be involved.",
                            "warn"
                        )
            else:
                _warned_this_stall = False
                _warned_slow_connect = False

    _watchdog_task = asyncio.create_task(_download_watchdog())

    # ── Auto-preload preferred model if configured ────────────────────────────
    _settings = load_settings()
    if _settings.get("auto_preload_on_start", False):
        _pref_size = _settings.get("preferred_model_size", "0.6B")
        _pref_type = _settings.get("preferred_model_type", "CustomVoice")
        _pref_id = f"Qwen/Qwen3-TTS-12Hz-{_pref_size}-{_pref_type}"
        emit_log(f"Auto-preloading preferred model in background: {_pref_id}", "info")
        asyncio.create_task(get_tts_model(_pref_size, _pref_type))

    yield

    # ── Shutdown ───────────────────────────────────────────────────────────────
    _watchdog_task.cancel()
    try:
        await _watchdog_task
    except asyncio.CancelledError:
        pass

    emit_log("Server shutting down — clearing models.", "warn")
    if model is not None:
        del model
        model = None
        current_model_id = None
        gc.collect()
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()
        emit_log("Model unloaded and GPU memory freed.", "ok")

VALID_MODEL_SIZES = {"0.6B", "1.7B"}
VALID_MODEL_TYPES = {"Base", "CustomVoice", "VoiceDesign"}

# Longest single paragraph /api/generate will accept. The frontend combines
# short lines into ~325-char paragraphs, so anything near this limit means a
# runaway client — and generation time/VRAM grow with text length.
MAX_GENERATE_TEXT_CHARS = 5000

def _load_model_sync(model_id: str, device: str, dtype: torch.dtype):
    """Synchronous model load with retry on transient network errors."""
    from qwen_tts import Qwen3TTSModel
    def _do_load():
        return Qwen3TTSModel.from_pretrained(model_id, device_map=device, dtype=dtype)
    return _with_retry(_do_load, max_attempts=3, base_delay=2.0, retry_repo_id=model_id)

async def get_tts_model(size: str = "1.7B", model_type: str = "CustomVoice"):
    global model, current_model_id, model_lock
    
    if model_lock is None:
        model_lock = asyncio.Lock()
    
    expected_model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
    
    if current_model_id == expected_model_id and model is not None:
        emit_log(f"Model already loaded: {expected_model_id}", "info")
        return model
        
    async with model_lock:
        if current_model_id == expected_model_id and model is not None:
            return model

        if not HAS_QWEN:
            emit_log("qwen-tts package is not installed. TTS generation will not work.", "error")
            download_progress["status"] = "error"
            download_progress["description"] = "qwen-tts not installed."
            raise RuntimeError("qwen-tts package is not installed.")

        # Free old model from memory if we are swapping
        if model is not None:
            emit_log(f"Swapping model: unloading {current_model_id}...", "warn")
            del model
            model = None
            current_model_id = None
            gc.collect()
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
            emit_log("Previous model unloaded, GPU memory freed.", "ok")

        device = "cpu"
        dtype = torch.float32
        if torch.cuda.is_available():
            device = "cuda:0"
            dtype = torch.bfloat16
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = "mps"
            dtype = torch.bfloat16

        emit_log(f"Loading model {expected_model_id} → device={device}, dtype={dtype}", "info")

        # Preflight: abort early if disk space is insufficient
        if not _model_is_cached(size, model_type):
            disk_err = _check_disk_space(size, model_type)
            if disk_err:
                download_progress["status"] = "error"
                download_progress["description"] = disk_err
                download_progress["error_kind"] = "disk"
                emit_log(f"Disk preflight failed: {disk_err}", "error")
                raise RuntimeError(disk_err)

        _begin_download_session(expected_model_id)
        download_progress["status"] = "downloading"
        download_progress["description"] = f"Downloading {size} {model_type}…"
        download_progress["progress"] = 0.0

        load_t0 = _time.monotonic()
        try:
            model = await asyncio.to_thread(_load_model_sync, expected_model_id, device, dtype)
            load_elapsed = _time.monotonic() - load_t0
            current_model_id = expected_model_id
            _end_download_session()
            download_progress["status"] = "ready"
            download_progress["description"] = "Model loaded successfully."
            download_progress["progress"] = 100.0
            emit_log(f"Model loaded successfully in {load_elapsed:.1f}s", "ok")
            return model
        except Exception as e:
            load_elapsed = _time.monotonic() - load_t0
            import traceback
            traceback.print_exc()
            emit_log(f"Model load FAILED after {load_elapsed:.1f}s: {e}", "error")
            _end_download_session()
            model = None
            current_model_id = None
            kind, msg = _classify_error(e, expected_model_id)
            download_progress["status"] = "error"
            download_progress["description"] = msg
            download_progress["error_kind"] = kind
            raise RuntimeError(f"Failed to load model: {e}")

# ─── _prefetch_model ──────────────────────────────────────────────────────────

async def _prefetch_model(size: str, model_type: str):
    """Download model weights to HF cache without loading into memory/swapping
    the currently active model. Errors are surfaced to download_progress so the
    UI shows them instead of silently swallowing them."""
    from huggingface_hub import snapshot_download
    repo_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"

    # Preflight disk check
    if not _model_is_cached(size, model_type):
        disk_err = _check_disk_space(size, model_type)
        if disk_err:
            download_progress["status"] = "error"
            download_progress["description"] = disk_err
            download_progress["error_kind"] = "disk"
            emit_log(f"Disk preflight failed: {disk_err}", "error")
            return

    emit_log(f"Prefetch started: {repo_id}", "info")
    _begin_download_session(repo_id)
    download_progress["status"] = "downloading"
    download_progress["description"] = f"Downloading {size} {model_type}…"
    t0 = _time.monotonic()
    try:
        await asyncio.to_thread(
            _with_retry, snapshot_download, repo_id,
            max_attempts=3, base_delay=2.0, retry_repo_id=repo_id,
        )
        elapsed = _time.monotonic() - t0
        _end_download_session()
        download_progress["status"] = "ready"
        download_progress["description"] = "Model downloaded."
        download_progress["progress"] = 100.0
        emit_log(f"Prefetch complete: {repo_id} in {elapsed:.1f}s", "ok")
    except Exception as exc:
        elapsed = _time.monotonic() - t0
        _end_download_session()
        kind, msg = _classify_error(exc, repo_id)
        download_progress["status"] = "error"
        download_progress["description"] = msg
        download_progress["error_kind"] = kind
        emit_log(f"Prefetch FAILED for {repo_id} after {elapsed:.1f}s: {exc}", "error")

# ─── Directory size helper ────────────────────────────────────────────────────

def _dir_size_mb(path: str) -> float:
    """Return the size of a directory tree in megabytes (0.0 if missing)."""
    if not os.path.isdir(path):
        return 0.0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fname))
            except OSError:
                pass
    return total / 1e6

# ─── HF cache helpers ─────────────────────────────────────────────────────────

def _hf_cache_dir() -> str:
    return os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")

def _model_cache_subdir(size: str, model_type: str) -> str:
    """Return the expected HF cache sub-directory for a given model variant."""
    return os.path.join(
        _hf_cache_dir(), "hub",
        f"models--Qwen--Qwen3-TTS-12Hz-{size}-{model_type}"
    )

def _model_is_cached(size: str, model_type: str) -> bool:
    """True if a complete, intact model snapshot exists in the HF cache.

    Checks for: no .incomplete blobs, config.json present, and at least one
    model weight shard. Returns False for partial/corrupt downloads.
    """
    subdir = _model_cache_subdir(size, model_type)
    snapshots_dir = os.path.join(subdir, "snapshots")
    if not os.path.isdir(snapshots_dir):
        return False

    # Reject if any .incomplete blob exists (indicates an interrupted download)
    blobs_dir = os.path.join(subdir, "blobs")
    if os.path.isdir(blobs_dir):
        try:
            for fname in os.listdir(blobs_dir):
                if fname.endswith(".incomplete"):
                    return False
        except OSError:
            return False

    # Find the most recently modified snapshot directory
    try:
        snap_entries = [
            os.path.join(snapshots_dir, d)
            for d in os.listdir(snapshots_dir)
            if os.path.isdir(os.path.join(snapshots_dir, d))
        ]
    except OSError:
        return False
    if not snap_entries:
        return False
    latest_snap = max(snap_entries, key=os.path.getmtime)

    # config.json must exist and be non-empty
    config_path = os.path.join(latest_snap, "config.json")
    try:
        if not os.path.exists(config_path) or os.path.getsize(config_path) == 0:
            return False
    except OSError:
        return False

    # At least one weight shard must exist and be non-empty
    try:
        for fname in os.listdir(latest_snap):
            if fname.endswith((".safetensors", ".bin")):
                fpath = os.path.join(latest_snap, fname)
                if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
                    return True
    except OSError:
        pass
    return False

# ─── Settings ─────────────────────────────────────────────────────────────────

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
_DEFAULT_SETTINGS = {
    "preferred_model_size": "0.6B",
    "preferred_model_type": "CustomVoice",
    "auto_preload_on_start": False,
    # When remote_server_url is set (and remote_enabled is true), /api/generate
    # is forwarded there instead of loading a model locally (see Settings →
    # Remote in the UI). remote_enabled lets the Local/Cloud header toggle
    # switch back to local generation without losing the saved URL/token.
    "remote_server_url": "",
    "remote_server_token": "",
    "remote_enabled": True,
}

def load_settings() -> dict:
    """Load settings.json, writing defaults if the file doesn't exist."""
    if not os.path.exists(SETTINGS_FILE):
        save_settings(_DEFAULT_SETTINGS.copy())
        return _DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        # Merge missing keys from defaults
        for k, v in _DEFAULT_SETTINGS.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _DEFAULT_SETTINGS.copy()

def save_settings(d: dict):
    """Atomically write settings dict to disk."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=4)
    os.replace(tmp, SETTINGS_FILE)

app = FastAPI(lifespan=lifespan)

# ─── Remote server mode ───────────────────────────────────────────────────────
# Set QWEN_TTS_SERVER_TOKEN to run this app as a shared generation server:
# every /api/* request must then carry "Authorization: Bearer <token>".
# Desktop instances point at such a server via Settings → Remote, which
# forwards only /api/generate — projects/profiles/export stay on the client.
SERVER_TOKEN = os.environ.get("QWEN_TTS_SERVER_TOKEN", "").strip()
SERVER_MODE = bool(SERVER_TOKEN)

# The desktop app only ever binds to 127.0.0.1, but a malicious web page could
# still reach it via DNS rebinding (a hostname that resolves to 127.0.0.1).
# Rejecting unexpected Host headers closes that hole. In server mode requests
# arrive through a tunnel under its public hostname, so the Host check is
# dropped — the bearer-token middleware below is the gate instead.
from fastapi.middleware.trustedhost import TrustedHostMiddleware
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"] if SERVER_MODE else ["127.0.0.1", "localhost"],
)

if SERVER_MODE:
    import hmac as _hmac
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.middleware("http")
    async def _require_server_token(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            # Token comes either as a bearer header (API clients: the desktop
            # app's Remote mode, doc_watcher.py, curl) or as a cookie (the web
            # UI browsed directly on this server — <audio> tags and EventSource
            # can't attach headers, so the access-code gate in the UI stores
            # the token in a cookie instead).
            auth = request.headers.get("authorization", "")
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not supplied:
                supplied = request.cookies.get("tts_access_token", "")
            if not _hmac.compare_digest(supplied, SERVER_TOKEN):
                return _JSONResponse(status_code=401, content={"detail": "Missing or invalid access token."})
        return await call_next(request)

@app.get("/api/progress")
async def stream_progress(request: Request):
    async def event_generator():
        last_payload = None
        while True:
            if await request.is_disconnected():
                break
            payload = json.dumps(download_progress)
            # Only emit when state actually changes, plus a periodic heartbeat so the
            # connection stays warm. Prevents a reconnect+replay loop that was spamming
            # "Model initialized successfully." every couple of seconds.
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            is_active = download_progress["status"] in ("downloading", "stalled")
            await asyncio.sleep(0.5 if is_active else 2.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ─── Activity Log SSE ─────────────────────────────────────────────────────────
@app.get("/api/activity_log")
async def stream_activity_log(request: Request):
    """SSE endpoint that pushes new activity-log entries to the UI in real time."""
    last_seen = 0
    async def event_generator():
        nonlocal last_seen
        while True:
            if await request.is_disconnected():
                break
            # Gather any entries the client hasn't seen yet
            new_entries = [e for e in _activity_log if e["v"] > last_seen]
            if new_entries:
                last_seen = new_entries[-1]["v"]
                yield f"data: {json.dumps(new_entries)}\n\n"
            await asyncio.sleep(0.4)
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ─── Diagnostics ──────────────────────────────────────────────────────────────

@app.get("/api/diag")
async def get_diag():
    """Return a comprehensive diagnostic snapshot for debugging startup/download issues."""
    emit_log("Diagnostics requested via /api/diag", "info")

    hf_dir = _hf_cache_dir()

    # Compute HF cache size (may take a moment on large caches)
    def _compute_diag():
        # HF cache size
        hf_size_mb = int(_dir_size_mb(hf_dir))

        # Disk free
        try:
            disk_free = shutil.disk_usage(DATA_DIR).free / 1e9
        except Exception:
            disk_free = -1.0

        # HF reachability
        try:
            r = requests.head("https://huggingface.co", timeout=3)
            hf_reachable = r.status_code < 500
        except Exception:
            hf_reachable = False

        return hf_size_mb, round(disk_free, 2), hf_reachable

    hf_size_mb, disk_free_gb, hf_reachable = await asyncio.to_thread(_compute_diag)

    return {
        "app_version": APP_VERSION,
        "frozen": bool(getattr(sys, 'frozen', False)),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "mac_ver": platform.mac_ver()[0],
        },
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "pid": os.getpid(),
        "torch": {
            "version": torch.__version__,
            "mps_available": bool(
                hasattr(torch.backends, 'mps') and torch.backends.mps.is_available()
            ),
            "mps_built": bool(
                hasattr(torch.backends, 'mps') and torch.backends.mps.is_built()
            ) if hasattr(torch.backends, 'mps') and hasattr(torch.backends.mps, 'is_built') else False,
            "cuda_available": torch.cuda.is_available(),
        },
        "hf_cache_dir": hf_dir,
        "hf_cache_size_mb": hf_size_mb,
        "disk_free_gb": disk_free_gb,
        "hf_reachable": hf_reachable,
        "loaded_model": current_model_id,
        "data_dir": DATA_DIR,
        "log_file": os.path.expanduser("~/.qwen_tts_studio/app.log"),
        "download_progress": dict(download_progress),
    }

# ─── Model manager ────────────────────────────────────────────────────────────

@app.get("/api/models/status")
async def models_status():
    """Return cache status and loaded model for all 6 variants."""
    result = []
    for size in sorted(VALID_MODEL_SIZES):
        for mtype in sorted(VALID_MODEL_TYPES):
            subdir = _model_cache_subdir(size, mtype)
            cached = _model_is_cached(size, mtype)
            # needs_repair: a partial/corrupt cache dir exists but didn't pass integrity check
            needs_repair = (not cached) and os.path.isdir(subdir)
            size_mb = round(_dir_size_mb(subdir), 1) if (cached or needs_repair) else 0.0
            result.append({
                "id": f"Qwen/Qwen3-TTS-12Hz-{size}-{mtype}",
                "size": size,
                "type": mtype,
                "cached": cached,
                "needs_repair": needs_repair,
                "size_mb": size_mb,
            })
    return {"models": result, "loaded": current_model_id}

@app.post("/api/models/download")
async def download_model(request: Request):
    """Kick off a background prefetch (snapshot_download) for a model variant."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    size = body.get("size", "")
    mtype = body.get("type", "")

    if size not in VALID_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Invalid size. Must be one of: {', '.join(VALID_MODEL_SIZES)}")
    if mtype not in VALID_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {', '.join(VALID_MODEL_TYPES)}")

    # One download at a time — the progress banner and session state are global,
    # and parallel snapshot_downloads would fight over bandwidth anyway.
    with _session_lock:
        already_downloading = _download_session["active"]
    if already_downloading:
        raise HTTPException(
            status_code=409,
            detail=f"A download is already in progress ({_download_session['repo_id']}). Wait for it to finish."
        )

    model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{mtype}"
    # Use _prefetch_model so the currently loaded model is NOT evicted.
    asyncio.create_task(_prefetch_model(size, mtype))
    return {"status": "started", "model_id": model_id}

@app.delete("/api/models/{size}/{model_type}")
async def delete_model(size: str, model_type: str):
    """Remove a cached model variant from the HF cache on disk."""
    if size not in VALID_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Invalid size. Must be one of: {', '.join(VALID_MODEL_SIZES)}")
    if model_type not in VALID_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {', '.join(VALID_MODEL_TYPES)}")

    model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
    if current_model_id == model_id:
        raise HTTPException(status_code=409, detail="Cannot delete the currently loaded model")

    subdir = _model_cache_subdir(size, model_type)
    if not os.path.isdir(subdir):
        raise HTTPException(status_code=404, detail=f"Model not found in cache: {model_id}")

    freed_mb = round(_dir_size_mb(subdir), 1)
    shutil.rmtree(subdir)
    emit_log(f"Deleted cached model {model_id} ({freed_mb} MB freed)", "ok")
    return {"ok": True, "freed_mb": freed_mb}

@app.post("/api/models/{size}/{model_type}/repair")
async def repair_model(size: str, model_type: str):
    """Wipe a corrupt or partially-downloaded model cache so it can be re-downloaded."""
    if size not in VALID_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Invalid size. Must be one of: {', '.join(VALID_MODEL_SIZES)}")
    if model_type not in VALID_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid type. Must be one of: {', '.join(VALID_MODEL_TYPES)}")

    model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
    if current_model_id == model_id:
        raise HTTPException(status_code=409, detail="Cannot repair the currently loaded model — swap to a different model first.")

    subdir = _model_cache_subdir(size, model_type)
    if not os.path.isdir(subdir):
        raise HTTPException(status_code=404, detail=f"No cache directory found for {model_id}.")

    freed_mb = round(_dir_size_mb(subdir), 1)
    shutil.rmtree(subdir)
    emit_log(f"Repaired (cleared) corrupt cache for {model_id} ({freed_mb} MB freed)", "ok")
    return {"ok": True, "freed_mb": freed_mb}

# ─── Settings endpoints ───────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    return load_settings()

@app.put("/api/settings")
async def update_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")

    current = load_settings()

    if "preferred_model_size" in body:
        v = body["preferred_model_size"]
        if v not in VALID_MODEL_SIZES:
            raise HTTPException(status_code=400, detail=f"Invalid preferred_model_size: {v}")
        current["preferred_model_size"] = v

    if "preferred_model_type" in body:
        v = body["preferred_model_type"]
        if v not in VALID_MODEL_TYPES:
            raise HTTPException(status_code=400, detail=f"Invalid preferred_model_type: {v}")
        current["preferred_model_type"] = v

    if "auto_preload_on_start" in body:
        v = body["auto_preload_on_start"]
        if not isinstance(v, bool):
            raise HTTPException(status_code=400, detail="auto_preload_on_start must be a boolean")
        current["auto_preload_on_start"] = v

    if "remote_server_url" in body:
        v = body["remote_server_url"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="remote_server_url must be a string")
        v = v.strip().rstrip("/")
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise HTTPException(status_code=400, detail="remote_server_url must start with http:// or https:// (or be empty)")
        current["remote_server_url"] = v

    if "remote_server_token" in body:
        v = body["remote_server_token"]
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="remote_server_token must be a string")
        current["remote_server_token"] = v.strip()

    if "remote_enabled" in body:
        v = body["remote_enabled"]
        if not isinstance(v, bool):
            raise HTTPException(status_code=400, detail="remote_enabled must be a boolean")
        current["remote_enabled"] = v

    if "import_defaults" in body:
        # Voice/model settings applied to projects created via
        # /api/projects/import when the caller doesn't supply its own —
        # same shape as a project's settings object (modelType, modelSize,
        # speaker, savedVoiceId, voiceDesignPrompt, instruct, temperature,
        # bibleMode).
        v = body["import_defaults"]
        if not isinstance(v, dict):
            raise HTTPException(status_code=400, detail="import_defaults must be an object")
        current["import_defaults"] = v

    save_settings(current)
    # Don't echo the token into the activity log
    loggable = {k: ("•••" if k == "remote_server_token" and current[k] else current[k]) for k in current}
    emit_log(f"Settings updated: {loggable}", "info")
    return current

@app.get("/api/health")
def health():
    """Liveness check. In server mode this is token-gated like every /api/*
    route, so a 200 from here proves both the URL and the token are right —
    that's exactly what the Settings → Remote "Test Connection" button uses."""
    return {"status": "ok", "version": APP_VERSION, "server_mode": SERVER_MODE}

@app.post("/api/remote/test")
async def test_remote_server(request: Request):
    """Check reachability + auth of a remote generation server. Tests the
    URL/token in the request body (so the UI can test before saving), falling
    back to the saved settings."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    settings = load_settings()
    url = (body.get("url") or settings.get("remote_server_url") or "").strip().rstrip("/")
    token = (body.get("token") or settings.get("remote_server_token") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="No remote server URL to test")

    def _probe():
        return requests.get(f"{url}/api/health", headers={"Authorization": f"Bearer {token}"}, timeout=10)

    try:
        resp = await asyncio.to_thread(_probe)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Unreachable: {e.__class__.__name__}"}
    if resp.status_code == 401:
        return {"ok": False, "error": "Server reachable, but it rejected the access token."}
    if resp.status_code != 200:
        return {"ok": False, "error": f"Server returned HTTP {resp.status_code}"}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": "Server responded, but not with a Local TTS Studio health payload."}
    result = {"ok": True, "version": data.get("version"), "server_mode": bool(data.get("server_mode"))}
    if not result["server_mode"]:
        result["warning"] = "That server is not running in server mode — it may be someone's local instance."
    return result

# Mount statics
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
if not os.path.exists(os.path.join(static_dir, "index.html")):
    with open(os.path.join(static_dir, "index.html"), "w") as f:
        f.write("<html><body><h1>Local TTS Studio Placeholder</h1></body></html>")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    # Stamp asset URLs with the app version and forbid caching the HTML, so a
    # new build never runs against a stale cached script.js/style.css (which
    # left new UI elements dead until the user hard-refreshed).
    with open(os.path.join(static_dir, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__APP_VERSION__", APP_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

@app.get("/api/profiles")
def get_profiles():
    """List all saved voice profiles."""
    return load_profiles()

@app.post("/api/profiles")
async def create_profile(
    name: str = Form(...),
    ref_text: str = Form(...),
    ref_audio: UploadFile = File(...)
):
    """Save a new voice profile."""
    profile_id = str(uuid.uuid4())
    safe_filename = os.path.basename(ref_audio.filename) if ref_audio.filename else "audio.wav"
    audio_path = os.path.join(PROFILES_DIR, f"{profile_id}_{safe_filename}")
    
    with open(audio_path, "wb") as f:
        f.write(await ref_audio.read())
        
    profiles = load_profiles()
    profiles.append({
        "id": profile_id,
        "name": name,
        "ref_text": ref_text,
        "audio_path": audio_path
    })
    save_profiles(profiles)
    
    return {"message": "Profile created successfully", "id": profile_id}

@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    """Delete a saved voice profile."""
    if profile_id == BUILTIN_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Cannot delete the built-in voice profile")
    
    profiles = load_profiles()
    profile = next((p for p in profiles if p["id"] == profile_id), None)
    
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
        
    audio_path = os.path.realpath(profile["audio_path"])
    if audio_path.startswith(os.path.realpath(PROFILES_DIR)) and os.path.exists(audio_path):
        os.remove(audio_path)
        
    profiles = [p for p in profiles if p["id"] != profile_id]
    save_profiles(profiles)
    
    return {"message": "Profile deleted successfully"}

# ─── Project Management ──────────────────────────────────────────────────────

def _project_dir(project_id: str) -> str:
    d = os.path.join(PROJECTS_DIR, project_id)
    if not os.path.realpath(d).startswith(os.path.realpath(PROJECTS_DIR)):
        raise HTTPException(status_code=403, detail="Invalid project ID")
    return d

def _load_project(project_id: str):
    path = os.path.join(_project_dir(project_id), "project.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    if "schema_version" not in data:
        data["schema_version"] = 1
    return data

def _save_project(project_id: str, data: dict):
    d = _project_dir(project_id)
    os.makedirs(os.path.join(d, "audio"), exist_ok=True)
    target = os.path.join(d, "project.json")
    tmp = target + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, target)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _safe_para_id(para_id: str) -> str:
    """Reduce a client-supplied paragraph id to a safe filename stem."""
    safe = "".join(c for c in para_id if c.isalnum() or c in "-_")
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid paragraph ID")
    return safe

@app.get("/api/projects")
def list_projects():
    result = []
    if not os.path.exists(PROJECTS_DIR):
        return result
    for entry in os.scandir(PROJECTS_DIR):
        if entry.is_dir():
            try:
                project = _load_project(entry.name)
            except (json.JSONDecodeError, KeyError, TypeError, HTTPException):
                continue
            if project:
                result.append({
                    "id": project["id"],
                    "name": project["name"],
                    "created_at": project.get("created_at"),
                    "updated_at": project.get("updated_at"),
                    "para_count": len(project.get("paragraphs", []))
                })
    result.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    return result

@app.post("/api/projects")
async def create_project(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")
    name = str(data.get("name") or "Untitled Project").strip() or "Untitled Project"
    project_id = str(uuid.uuid4())
    now = _now_iso()
    project = {
        "id": project_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
        "settings": data.get("settings", {}),
        "paragraphs": [],
        "rawText": data.get("rawText", "")
    }
    _save_project(project_id, project)
    return project

# ─── Headless import (Google Docs watcher, scripts) ──────────────────────────
# POST /api/projects/import takes raw Markdown text, parses it into paragraphs
# server-side (same rules as the frontend Parse button — see text_parser.py),
# creates a project, and generates audio for every paragraph in the background
# by calling this server's own /api/generate. The project then appears in the
# UI like any other, with each paragraph's audio saved as take 1.

_import_lock: Optional[asyncio.Lock] = None  # serializes background import jobs
SELF_PORT = int(os.environ.get("QWEN_TTS_PORT", "8001"))

def _store_wav_as_flac(project_id: str, file_id: str, wav_bytes: bytes):
    """Encode WAV bytes to FLAC under the project's audio dir (same storage
    format the UI's auto-save uses). Falls back to raw WAV on encode failure."""
    audio_dir = os.path.join(_project_dir(project_id), "audio")
    os.makedirs(audio_dir, exist_ok=True)
    try:
        data, samplerate = sf.read(io.BytesIO(wav_bytes))
        sf.write(os.path.join(audio_dir, f"{file_id}.flac"), data, samplerate, format="FLAC")
    except Exception as e:
        emit_log(f"FLAC encode failed for {file_id}, storing WAV: {e}", "warn")
        with open(os.path.join(audio_dir, f"{file_id}.wav"), "wb") as f:
            f.write(wav_bytes)

def _set_import_status(project_id: str, status: str):
    project = _load_project(project_id)
    if project:
        project["import_status"] = status
        _save_project(project_id, project)

async def _run_import_generation(project_id: str, port: int):
    """Background job: generate audio for every paragraph of an imported
    project, sequentially, via this server's own /api/generate endpoint (which
    owns model loading, the MPS lock, and remote forwarding)."""
    global _import_lock
    if _import_lock is None:
        _import_lock = asyncio.Lock()

    async with _import_lock:
        project = _load_project(project_id)
        if not project:
            return
        s = project.get("settings", {})
        model_type = s.get("modelType", "CustomVoice")
        base_form = {
            "language": "English",
            "model_size": s.get("modelSize", "1.7B"),
            "model_type": model_type,
            "temperature": str(s.get("temperature", 0.85)),
        }
        if model_type == "CustomVoice":
            base_form["speaker"] = s.get("speaker", "Vivian")
            if s.get("instruct"):
                base_form["instruct"] = s["instruct"]
        elif model_type == "VoiceDesign":
            base_form["voice_design_prompt"] = s.get("voiceDesignPrompt", "")
        elif model_type == "Base":
            base_form["profile_id"] = s.get("savedVoiceId", "")

        url = f"http://127.0.0.1:{port}/api/generate"
        headers = {"Authorization": f"Bearer {SERVER_TOKEN}"} if SERVER_MODE else {}
        paragraphs = project.get("paragraphs", [])
        total = len(paragraphs)
        failures = 0
        emit_log(f"Import \"{project['name']}\": generating {total} paragraph(s)...", "info")
        _set_import_status(project_id, "generating")

        for i, para in enumerate(paragraphs):
            if not _load_project(project_id):
                emit_log(f"Import \"{project['name']}\": project deleted, stopping.", "warn")
                return
            if para.get("hasAudio"):
                continue
            form = dict(base_form, text=para["text"])
            try:
                resp = await asyncio.to_thread(
                    requests.post, url, data=form, headers=headers, timeout=1800
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"generate returned {resp.status_code}: {resp.text[:120]}")
                await asyncio.to_thread(
                    _store_wav_as_flac, project_id, f"{para['id']}-t1", resp.content
                )
            except Exception as e:
                failures += 1
                emit_log(f"Import \"{project['name']}\" [{i + 1}/{total}] failed: {e}", "error")
                continue

            # Re-load before updating so edits made in the UI mid-import (or the
            # UI's own auto-save) aren't clobbered by our stale copy.
            fresh = _load_project(project_id)
            if not fresh:
                return
            for p in fresh.get("paragraphs", []):
                if p["id"] == para["id"]:
                    p["hasAudio"] = True
                    p["takes"] = [1]
                    p["activeTake"] = 1
                    break
            fresh["updated_at"] = _now_iso()
            _save_project(project_id, fresh)
            emit_log(f"Import \"{project['name']}\" [{i + 1}/{total}] done.", "ok")

        status = "done" if failures == 0 else f"done ({failures} of {total} failed)"
        _set_import_status(project_id, status)
        level = "ok" if failures == 0 else "warn"
        emit_log(f"Import \"{project['name']}\" complete — {total - failures}/{total} paragraphs generated.", level)

@app.post("/api/projects/import")
async def import_project(request: Request):
    """Create a project from raw text and (optionally) generate all audio.

    Body: {"raw_text": str, "name"?: str, "settings"?: {...}, "generate"?: bool,
           "source"?: {...}}. Settings not supplied fall back to the
    "import_defaults" object in settings.json, then to CustomVoice defaults.
    "source" is stored verbatim (the Docs watcher records doc id / revision)."""
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")
    raw_text = str(data.get("raw_text") or "").strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="raw_text is required")

    defaults = load_settings().get("import_defaults", {})
    settings = {**defaults, **(data.get("settings") or {})}
    settings.setdefault("modelType", "CustomVoice")
    settings.setdefault("modelSize", "1.7B")
    # Imported docs are devotionals with scripture references — Bible text
    # formatting defaults ON (callers/import_defaults can still set it false).
    settings.setdefault("bibleMode", True)
    if settings["modelType"] == "Base" and not settings.get("savedVoiceId"):
        raise HTTPException(status_code=400, detail="modelType Base requires savedVoiceId (a saved voice profile id)")

    parsed = text_parser.parse_paragraphs(raw_text, bible_mode=bool(settings.get("bibleMode")))
    if not parsed:
        raise HTTPException(status_code=400, detail="No usable paragraphs after parsing")

    name = str(data.get("name") or "").strip() or text_parser.derive_title(raw_text) or "Imported Project"
    project_id = str(uuid.uuid4())
    now = _now_iso()
    batch_id = int(_time.time() * 1000)
    project = {
        "id": project_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "schema_version": 1,
        "settings": settings,
        "rawText": raw_text,
        "paragraphs": [
            {
                "id": f"para-{batch_id}-{i}",
                "text": p["text"],
                "hasAudio": False,
                "isChapter": p["chapter"],
                "takes": [],
                "activeTake": None,
            }
            for i, p in enumerate(parsed)
        ],
        "import_status": "pending",
        "source": data.get("source"),
    }
    _save_project(project_id, project)
    emit_log(f"Imported project \"{name}\" — {len(parsed)} paragraph(s).", "info")

    generate = data.get("generate", True)
    if generate:
        # Self-calls go to the port this server is actually bound to (the
        # request's local socket), not the default — dev runs use other ports.
        server_addr = request.scope.get("server") or (None, SELF_PORT)
        asyncio.create_task(_run_import_generation(project_id, server_addr[1] or SELF_PORT))
    else:
        _set_import_status(project_id, "not_requested")

    return {"id": project_id, "name": name, "para_count": len(parsed), "generating": bool(generate)}

@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    project = _load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project

@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, request: Request):
    project = _load_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON body required")
    project["name"] = data.get("name", project["name"])
    project["settings"] = data.get("settings", project.get("settings", {}))
    project["paragraphs"] = data.get("paragraphs", project.get("paragraphs", []))
    project["rawText"] = data.get("rawText", project.get("rawText", ""))
    project["updated_at"] = _now_iso()
    _save_project(project_id, project)
    return {"ok": True}

@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    project_dir = _project_dir(project_id)
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    shutil.rmtree(project_dir)
    return {"ok": True}

@app.post("/api/projects/{project_id}/audio/{para_id}")
async def save_para_audio(project_id: str, para_id: str, audio: UploadFile = File(...)):
    project_dir = _project_dir(project_id)
    if not os.path.exists(project_dir):
        raise HTTPException(status_code=404, detail="Project not found")
    audio_dir = os.path.join(project_dir, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    safe_para_id = _safe_para_id(para_id)
    flac_path = os.path.join(audio_dir, f"{safe_para_id}.flac")
    content = await audio.read()

    def _encode_flac():
        # Decode the uploaded audio (WAV from generation) and re-encode losslessly
        # as FLAC — roughly half the size of WAV with bit-identical audio.
        data, samplerate = sf.read(io.BytesIO(content))
        sf.write(flac_path, data, samplerate, format="FLAC")

    try:
        await asyncio.to_thread(_encode_flac)
    except Exception as e:
        # If FLAC encoding fails for any reason, fall back to storing raw WAV so
        # the user never loses a generation.
        emit_log(f"FLAC encode failed for {safe_para_id}, storing WAV: {e}", "warn")
        wav_path = os.path.join(audio_dir, f"{safe_para_id}.wav")
        with open(wav_path, "wb") as f:
            f.write(content)
        return {"ok": True}

    # Drop any stale WAV left over from before FLAC storage / a prior fallback.
    stale_wav = os.path.join(audio_dir, f"{safe_para_id}.wav")
    if os.path.exists(stale_wav):
        os.remove(stale_wav)
    return {"ok": True}

@app.get("/api/projects/{project_id}/audio/{para_id}")
def get_para_audio(project_id: str, para_id: str):
    project_dir = _project_dir(project_id)
    safe_para_id = _safe_para_id(para_id)
    audio_dir = os.path.join(project_dir, "audio")
    # Prefer FLAC (current format); fall back to WAV for any unmigrated files.
    candidates = [
        (os.path.join(audio_dir, f"{safe_para_id}.flac"), "audio/flac"),
        (os.path.join(audio_dir, f"{safe_para_id}.wav"), "audio/wav"),
    ]
    for audio_path, media_type in candidates:
        if os.path.exists(audio_path):
            if not os.path.realpath(audio_path).startswith(os.path.realpath(project_dir)):
                raise HTTPException(status_code=403, detail="Access denied")
            return FileResponse(audio_path, media_type=media_type)
    raise HTTPException(status_code=404, detail="Audio not found")

@app.delete("/api/projects/{project_id}/audio/{para_id}")
def delete_para_audio(project_id: str, para_id: str):
    """Remove one stored audio file (used when the user discards a take)."""
    project_dir = _project_dir(project_id)
    safe_para_id = _safe_para_id(para_id)
    audio_dir = os.path.join(project_dir, "audio")
    removed = False
    for ext in ("flac", "wav"):
        audio_path = os.path.join(audio_dir, f"{safe_para_id}.{ext}")
        if os.path.exists(audio_path):
            if not os.path.realpath(audio_path).startswith(os.path.realpath(project_dir)):
                raise HTTPException(status_code=403, detail="Access denied")
            os.remove(audio_path)
            removed = True
    return {"ok": True, "removed": removed}

async def _generate_via_remote(
    remote_url: str, token: str, *, text, language, model_size, model_type,
    speaker, voice_design_prompt, ref_text, ref_audio, profile_id, instruct, temperature
):
    """Forward a generation request to a remote server (Settings → Remote).

    Voice-clone profiles live on THIS machine, so a profile_id is resolved
    locally and its reference audio/text are uploaded with the request — the
    server never needs our profile library."""
    form = {
        "text": text,
        "language": language,
        "model_size": model_size,
        "model_type": model_type,
        "speaker": speaker,
        "temperature": str(temperature),
    }
    if voice_design_prompt:
        form["voice_design_prompt"] = voice_design_prompt
    if instruct:
        form["instruct"] = instruct

    files = None
    file_handle = None
    if model_type == "Base":
        if profile_id:
            profile = next((p for p in load_profiles() if p["id"] == profile_id), None)
            if not profile:
                emit_log(f"Profile {profile_id} not found.", "error")
                raise HTTPException(status_code=404, detail="Profile not found")
            if not os.path.exists(profile["audio_path"]):
                raise HTTPException(status_code=404, detail=f"Reference audio for profile \"{profile['name']}\" is missing on disk.")
            form["ref_text"] = profile["ref_text"]
            file_handle = open(profile["audio_path"], "rb")
            files = {"ref_audio": (os.path.basename(profile["audio_path"]), file_handle, "audio/wav")}
        elif ref_audio is not None and ref_text:
            files = {"ref_audio": (ref_audio.filename or "upload.wav", await ref_audio.read(), ref_audio.content_type or "audio/wav")}
            form["ref_text"] = ref_text
        else:
            raise HTTPException(status_code=400, detail="ref_text and ref_audio (or profile_id) are required for Voice Cloning in Base models.")

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    emit_log(f"Forwarding generation to remote server {remote_url} …", "info")
    gen_t0 = _time.monotonic()

    def _post():
        # Long read timeout: the server responds only after inference finishes,
        # and its first request may also download the model.
        return requests.post(
            f"{remote_url}/api/generate",
            data=form, files=files, headers=headers,
            timeout=(10, 3600), stream=True,
        )

    try:
        resp = await asyncio.to_thread(_post)
    except requests.exceptions.RequestException as e:
        emit_log(f"Remote server unreachable: {e.__class__.__name__}", "error")
        raise HTTPException(
            status_code=502,
            detail="Remote generation server unreachable. Check the URL in Settings → Remote, or clear it to generate locally.",
        )
    finally:
        if file_handle:
            file_handle.close()

    if resp.status_code != 200:
        if resp.status_code == 401:
            detail = "The remote server rejected the access token. Check it in Settings → Remote."
        else:
            try:
                detail = resp.json().get("detail", "")[:300] or f"Remote server returned HTTP {resp.status_code}"
            except Exception:
                detail = f"Remote server returned HTTP {resp.status_code}"
        resp.close()
        emit_log(f"Remote generation failed ({resp.status_code}): {detail}", "error")
        raise HTTPException(status_code=resp.status_code if resp.status_code >= 400 else 502, detail=detail)

    gen_elapsed = _time.monotonic() - gen_t0
    emit_log(f"Remote generation complete — {gen_elapsed:.1f}s round trip", "ok")
    return StreamingResponse(
        resp.iter_content(chunk_size=65536),
        media_type="audio/wav",
        headers={"Content-Disposition": "attachment; filename=generated.wav"},
        background=BackgroundTask(resp.close),
    )

@app.post("/api/generate")
async def generate_audio(
    request: Request,
    text: str = Form(...),
    language: str = Form("English"),
    model_size: str = Form("1.7B"),
    model_type: str = Form("CustomVoice"),
    speaker: str = Form("Vivian"),
    voice_design_prompt: str = Form(None),
    ref_text: str = Form(None),
    ref_audio: UploadFile = File(None),
    profile_id: str = Form(None),
    instruct: str = Form(None),
    temperature: float = Form(0.85)
):
    if model_size not in VALID_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Invalid model_size. Must be one of: {', '.join(VALID_MODEL_SIZES)}")
    if model_type not in VALID_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid model_type. Must be one of: {', '.join(VALID_MODEL_TYPES)}")

    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is empty — nothing to synthesize.")
    if len(text) > MAX_GENERATE_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(text)} chars). Split it into paragraphs of at most {MAX_GENERATE_TEXT_CHARS} chars."
        )
    # Clamp rather than reject — out-of-range values come from stale UIs, and the
    # nearest sane value is always what the user meant.
    temperature = min(max(temperature, 0.05), 1.5)

    text_preview = (text[:80] + "...") if len(text) > 80 else text
    emit_log(f"Generation requested — mode={model_type}, size={model_size}, text=\"{text_preview}\"", "info")

    # Remote generation: if a server URL is configured and the Local/Cloud
    # toggle is on Cloud, forward instead of loading a model here.
    # Server-mode instances never forward (no loops).
    _settings = load_settings()
    _remote_url = (_settings.get("remote_server_url") or "").strip().rstrip("/")
    if _remote_url and _settings.get("remote_enabled", True) and not SERVER_MODE:
        return await _generate_via_remote(
            _remote_url, (_settings.get("remote_server_token") or "").strip(),
            text=text, language=language, model_size=model_size, model_type=model_type,
            speaker=speaker, voice_design_prompt=voice_design_prompt, ref_text=ref_text,
            ref_audio=ref_audio, profile_id=profile_id, instruct=instruct, temperature=temperature,
        )

    global generation_lock
    if generation_lock is None:
        generation_lock = asyncio.Lock()

    # Log if we're waiting on the lock (another generation in progress)
    if generation_lock.locked():
        emit_log("Waiting for previous generation to finish (MPS serialization lock)...", "warn")

    async with generation_lock:
        # The browser may have abandoned this request (refresh, retry, stop)
        # while it sat in the queue — don't burn GPU time on audio nobody will
        # receive. This is what turns a pile of retries into a pile of work.
        if await request.is_disconnected():
            emit_log(f"Client gone before generation started — skipping \"{text_preview}\"", "warn")
            raise HTTPException(status_code=499, detail="Client disconnected")

        # Model load/swap must happen under the generation lock: swapping frees
        # the current model, which would crash an inference running on it.
        try:
            tts_model = await get_tts_model(model_size, model_type)
        except Exception as e:
            emit_log(f"Could not load model for generation: {e}", "error")
            raise HTTPException(status_code=500, detail=str(e))

        # Cap decoding relative to the text length. The codec runs at ~12
        # tokens/sec of audio and speech is ~14 chars/sec, so len(text)*3 is
        # roughly 3.5× the expected duration — enough for slow, pause-heavy
        # reads, but a generation that misses its end-of-speech token stops in
        # seconds instead of grinding to the 2048-token (~3 min audio) default.
        max_new_tokens = min(2048, max(256, len(text) * 3))

        gen_t0 = _time.monotonic()
        try:
            # Generate speech based on requested model type
            if model_type == "CustomVoice":
                emit_log(f"Starting CustomVoice inference — speaker={speaker}, lang={language}", "info")
                wavs, sr = await asyncio.to_thread(
                    tts_model.generate_custom_voice,
                    text=text,
                    language=language,
                    speaker=speaker,
                    instruct=instruct or None,
                    temperature=temperature,
                    repetition_penalty=1.1,
                    top_p=0.8,
                    subtalker_temperature=temperature,
                    max_new_tokens=max_new_tokens
                )
            elif model_type == "VoiceDesign":
                if not voice_design_prompt:
                    raise HTTPException(status_code=400, detail="voice_design_prompt is required for VoiceDesign models.")
                emit_log(f"Starting VoiceDesign inference — prompt=\"{voice_design_prompt[:60]}\"", "info")
                wavs, sr = await asyncio.to_thread(
                    tts_model.generate_voice_design,
                    text=text,
                    language=language,
                    instruct=voice_design_prompt,
                    temperature=temperature,
                    repetition_penalty=1.1,
                    top_p=0.8,
                    subtalker_temperature=temperature,
                    max_new_tokens=max_new_tokens
                )
            elif model_type == "Base":
                if profile_id:
                    # Load from saved profile
                    profiles = load_profiles()
                    profile = next((p for p in profiles if p["id"] == profile_id), None)
                    if not profile:
                        emit_log(f"Profile {profile_id} not found.", "error")
                        raise HTTPException(status_code=404, detail="Profile not found")

                    temp_audio_path = profile["audio_path"]
                    actual_ref_text = profile["ref_text"]
                    cleanup_audio = False
                    emit_log(f"Starting VoiceClone inference — profile=\"{profile['name']}\"", "info")
                else:
                    # Use uploaded ad-hoc files
                    if not ref_text or not ref_audio:
                        raise HTTPException(status_code=400, detail="ref_text and ref_audio (or profile_id) are required for Voice Cloning in Base models.")
                    safe_name = os.path.basename(ref_audio.filename) if ref_audio.filename else "upload.wav"
                    temp_audio_path = os.path.join(DATA_DIR, f"{uuid.uuid4()}_{safe_name}")
                    os.makedirs(DATA_DIR, exist_ok=True)
                    with open(temp_audio_path, "wb") as f:
                        f.write(await ref_audio.read())
                    actual_ref_text = ref_text
                    cleanup_audio = True
                    emit_log(f"Starting VoiceClone inference — ad-hoc upload", "info")

                try:
                    wavs, sr = await asyncio.to_thread(
                        tts_model.generate_voice_clone,
                        text=text,
                        language=language,
                        ref_audio=temp_audio_path,
                        ref_text=actual_ref_text,
                        temperature=temperature,
                        repetition_penalty=1.1,
                        top_p=0.8,
                        subtalker_temperature=temperature,
                        max_new_tokens=max_new_tokens
                    )
                finally:
                    # Remove the ad-hoc upload whether or not inference succeeded
                    if cleanup_audio and os.path.exists(temp_audio_path):
                        os.remove(temp_audio_path)
            else:
                raise HTTPException(status_code=400, detail=f"Unsupported model_type: {model_type}")

            gen_elapsed = _time.monotonic() - gen_t0

            # Stream audio directly from memory — no temp file needed
            audio_data = wavs[0]
            duration_sec = len(audio_data) / sr if sr else 0
            buffer = io.BytesIO()
            sf.write(buffer, audio_data, sr, format="WAV")
            buf_size = buffer.tell()
            buffer.seek(0)

            emit_log(
                f"Generation complete — {gen_elapsed:.1f}s wall time, "
                f"{duration_sec:.1f}s audio, {buf_size/1024:.0f} KB, sr={sr}",
                "ok"
            )
            return StreamingResponse(buffer, media_type="audio/wav", headers={"Content-Disposition": "attachment; filename=generated.wav"})

        except HTTPException:
            raise
        except Exception as e:
            gen_elapsed = _time.monotonic() - gen_t0
            import traceback
            traceback.print_exc()
            emit_log(f"Generation FAILED after {gen_elapsed:.1f}s: {e}", "error")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            # Return cached GPU allocator blocks between generations — long
            # sessions otherwise creep up in memory until MPS starts swapping.
            gc.collect()
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()

# Filter chains: loudness-normalize every treatment; warmth/clear add a
# low/high shelf on top for coloration.
TREATMENT_FILTERS = {
    "podcast": "loudnorm=I=-16:TP=-1.5:LRA=11",
    "warmth": "bass=g=6:f=200,loudnorm=I=-16:TP=-1.5:LRA=11",
    "clear": "treble=g=7:f=2000,loudnorm=I=-16:TP=-1.5:LRA=11",
}

def _merge_contents_to_wav(file_contents):
    # Segments may be FLAC (fetched from storage) or WAV (freshly
    # generated, still in memory) — libsndfile decodes both natively,
    # so no ffmpeg/ffprobe binary is needed here.
    chunks = []
    sr_out = None
    ch_out = None
    for idx, content in enumerate(file_contents):
        data, sr = sf.read(io.BytesIO(content), dtype="float32", always_2d=True)
        if sr_out is None:
            sr_out, ch_out = sr, data.shape[1]
        if data.shape[1] != ch_out:
            data = np.tile(data.mean(axis=1, keepdims=True), (1, ch_out))
        if sr != sr_out:
            n_out = int(round(data.shape[0] * sr_out / sr))
            x_old = np.linspace(0.0, 1.0, data.shape[0], endpoint=False)
            x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
            data = np.stack(
                [np.interp(x_new, x_old, data[:, c]) for c in range(ch_out)],
                axis=1
            ).astype(np.float32)
        if idx > 0:
            chunks.append(np.zeros((sr_out, ch_out), dtype=np.float32))  # 1 s pause
        chunks.append(data)
    merged = np.concatenate(chunks, axis=0)
    temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    temp_out.close()
    sf.write(temp_out.name, merged, sr_out, format="WAV", subtype="PCM_16")
    return temp_out.name

@app.post("/api/merge")
async def merge_audio(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    emit_log(f"Merging {len(files)} audio segments...", "info")
    merge_t0 = _time.monotonic()
    try:
        # Read all uploads into memory first (async), then merge in a thread
        file_contents = []
        for file in files:
            file_contents.append(await file.read())

        out_path = await asyncio.to_thread(_merge_contents_to_wav, file_contents)
        merge_elapsed = _time.monotonic() - merge_t0
        emit_log(f"Merge complete — {merge_elapsed:.1f}s, {len(files)} segments", "ok")

        return FileResponse(
            out_path,
            media_type="audio/wav",
            filename="merged_audio.wav",
            background=BackgroundTask(os.unlink, out_path)
        )
    except Exception as e:
        merge_elapsed = _time.monotonic() - merge_t0
        import traceback
        traceback.print_exc()
        emit_log(f"Merge FAILED after {merge_elapsed:.1f}s: {e}", "error")
        raise HTTPException(status_code=500, detail=f"Failed to merge audio: {str(e)}")

@app.post("/api/treat")
async def treat_audio(
    audio_file: UploadFile = File(...),
    treatment_type: str = Form(...)
):
    """
    Apply ffmpeg audio enhancements to an uploaded audio file and return the processed file.
    """
    if treatment_type not in TREATMENT_FILTERS:
        raise HTTPException(status_code=400, detail=f"Invalid treatment type. Must be one of: {', '.join(TREATMENT_FILTERS)}")

    emit_log(f"Audio treatment requested: {treatment_type}", "info")
    treat_t0 = _time.monotonic()

    temp_input = temp_output = None
    try:
        content = await audio_file.read()
        temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_input.write(content)
        temp_input.close()

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_output.close()

        command = [
            _ffmpeg_bin(),
            "-y",  # Overwrite output file if it exists
            "-i", temp_input.name,
            "-af", TREATMENT_FILTERS[treatment_type],
            temp_output.name
        ]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        if process.returncode != 0:
            ffmpeg_err = stderr.decode()[:200]
            emit_log(f"ffmpeg treatment failed (exit {process.returncode}): {ffmpeg_err}", "error")
            raise RuntimeError("ffmpeg processing failed")

        treat_elapsed = _time.monotonic() - treat_t0
        emit_log(f"Treatment '{treatment_type}' complete — {treat_elapsed:.1f}s", "ok")

        # Clean up input file immediately since processing is done
        os.unlink(temp_input.name)

        return FileResponse(
            temp_output.name,
            media_type="audio/wav",
            filename=f"{treatment_type}_treated.wav",
            background=BackgroundTask(os.unlink, temp_output.name)
        )

    except Exception as e:
        treat_elapsed = _time.monotonic() - treat_t0
        import traceback
        traceback.print_exc()
        emit_log(f"Treatment FAILED after {treat_elapsed:.1f}s: {e}", "error")
        _remove_quietly(temp_input and temp_input.name, temp_output and temp_output.name)
        raise HTTPException(status_code=500, detail=f"Failed to treat audio: {str(e)}")

@app.post("/api/convert")
async def convert_audio(
    audio_file: UploadFile = File(...),
    output_format: str = Form(...)
):
    """Convert audio to a different format (e.g. wav -> m4a)."""
    valid_formats = ["wav", "m4a"]
    if output_format not in valid_formats:
        raise HTTPException(status_code=400, detail=f"Invalid format. Must be one of: {', '.join(valid_formats)}")

    format_config = {
        "wav": {"suffix": ".wav", "codec": [], "media_type": "audio/wav"},
        "m4a": {"suffix": ".m4a", "codec": ["-c:a", "aac", "-b:a", "64k"], "media_type": "audio/mp4"},
    }
    cfg = format_config[output_format]

    temp_input = temp_output = None
    try:
        content = await audio_file.read()
        temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_input.write(content)
        temp_input.close()

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=cfg["suffix"])
        temp_output.close()

        command = [_ffmpeg_bin(), "-y", "-i", temp_input.name] + cfg["codec"] + [temp_output.name]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        os.unlink(temp_input.name)

        if process.returncode != 0:
            emit_log(f"ffmpeg convert failed (exit {process.returncode}): {stderr.decode()[:200]}", "error")
            raise RuntimeError("ffmpeg conversion failed")

        return FileResponse(
            temp_output.name,
            media_type=cfg["media_type"],
            filename=f"audio{cfg['suffix']}",
            background=BackgroundTask(os.unlink, temp_output.name)
        )

    except Exception as e:
        _remove_quietly(temp_input and temp_input.name, temp_output and temp_output.name)
        raise HTTPException(status_code=500, detail=f"Failed to convert audio: {str(e)}")

@app.post("/api/export")
async def export_audio(
    files: List[UploadFile] = File(...),
    treatment_type: str = Form("clear"),
    output_format: str = Form("wav"),
):
    """Merge segments, apply treatment, and convert — all in one server-side
    pass so the full-length WAV never round-trips to the browser."""
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
    if treatment_type not in TREATMENT_FILTERS and treatment_type != "none":
        raise HTTPException(status_code=400, detail=f"Invalid treatment type. Must be 'none' or one of: {', '.join(TREATMENT_FILTERS)}")
    if output_format not in ("wav", "m4a"):
        raise HTTPException(status_code=400, detail="Invalid format. Must be one of: wav, m4a")

    emit_log(f"Export: merging {len(files)} segments (treatment={treatment_type}, format={output_format})...", "info")
    export_t0 = _time.monotonic()

    merged_path = out_path = None
    try:
        file_contents = []
        for file in files:
            file_contents.append(await file.read())
        merged_path = await asyncio.to_thread(_merge_contents_to_wav, file_contents)
        emit_log(f"Export: merge complete — {_time.monotonic() - export_t0:.1f}s", "info")

        if treatment_type == "none" and output_format == "wav":
            return FileResponse(
                merged_path,
                media_type="audio/wav",
                filename="audio.wav",
                background=BackgroundTask(os.unlink, merged_path),
            )

        # One ffmpeg pass does both the treatment filter and the encode.
        suffix = ".m4a" if output_format == "m4a" else ".wav"
        temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_out.close()
        out_path = temp_out.name

        command = [_ffmpeg_bin(), "-y", "-i", merged_path]
        if treatment_type != "none":
            command += ["-af", TREATMENT_FILTERS[treatment_type]]
        if output_format == "m4a":
            command += ["-c:a", "aac", "-b:a", "64k"]
        command.append(out_path)

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        os.unlink(merged_path)
        merged_path = None

        if process.returncode != 0:
            emit_log(f"ffmpeg export failed (exit {process.returncode}): {stderr.decode()[:200]}", "error")
            raise RuntimeError("ffmpeg processing failed")

        emit_log(f"Export complete — {_time.monotonic() - export_t0:.1f}s total", "ok")
        return FileResponse(
            out_path,
            media_type="audio/mp4" if output_format == "m4a" else "audio/wav",
            filename=f"audio{suffix}",
            background=BackgroundTask(os.unlink, out_path),
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        emit_log(f"Export FAILED after {_time.monotonic() - export_t0:.1f}s: {e}", "error")
        _remove_quietly(merged_path, out_path)
        raise HTTPException(status_code=500, detail=f"Failed to export audio: {str(e)}")

@app.get("/api/check_update")
async def check_update():
    try:
        response = await asyncio.to_thread(
            requests.get,
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        
        latest_version = data.get("tag_name", "").lstrip("v")
        
        def parse_version(v):
            return tuple(int(x) for x in v.split('.') if x.isdigit())
            
        latest_tuple = parse_version(latest_version)
        app_tuple = parse_version(APP_VERSION)
        
        if latest_tuple and app_tuple and latest_tuple > app_tuple:
            download_url = None
            if getattr(sys, 'frozen', False):
                assets = data.get("assets", [])
                for asset in assets:
                    if asset["name"].endswith(".zip"):
                        download_url = asset["browser_download_url"]
                        break
            else:
                download_url = data.get("zipball_url")
            
            if download_url:
                return {"update_available": True, "latest_version": latest_version, "download_url": download_url}
                
    except Exception as e:
        print(f"Update check failed: {e}")
        
    return {"update_available": False}

@app.post("/api/do_update")
async def do_update(download_url: str = Form(...)):
    # Only install updates that actually come from GitHub — this endpoint
    # downloads and executes a replacement app, so the source must be trusted.
    allowed_prefixes = ("https://github.com/", "https://api.github.com/", "https://codeload.github.com/")
    if not download_url.startswith(allowed_prefixes):
        raise HTTPException(status_code=400, detail="Update URL must be a GitHub release URL.")

    try:
        is_frozen = getattr(sys, 'frozen', False)
        
        if is_frozen:
            app_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))))
            if not app_path.endswith(".app"):
                raise HTTPException(status_code=400, detail="Current executable is not inside a standard macOS .app bundle structure.")
        else:
            app_path = os.path.dirname(os.path.abspath(__file__))

        # Download the zip
        temp_dir = tempfile.mkdtemp(prefix="tts_update_")
        zip_path = os.path.join(temp_dir, "update.zip")
        
        def _download_and_extract():
            # timeout guards connect + between-chunk reads; a large download can
            # still take as long as it needs while data keeps flowing.
            r = requests.get(download_url, stream=True, timeout=(10, 60))
            r.raise_for_status()
            with open(zip_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            emit_log(f"Update downloaded: {zip_path}", "info")
            # Prefer `ditto` for .app bundles — Python's zipfile silently strips
            # Unix executable bits, which bricks the main binary inside the .app.
            # Fall back to zipfile for non-.app source zips (dev mode).
            ditto_ok = False
            if is_frozen:
                try:
                    result = subprocess.run(
                        ["/usr/bin/ditto", "-x", "-k", zip_path, temp_dir],
                        capture_output=True, text=True, timeout=300
                    )
                    if result.returncode == 0:
                        ditto_ok = True
                        emit_log("Update extracted with ditto (preserves executable bits)", "ok")
                    else:
                        emit_log(f"ditto failed (exit {result.returncode}): {result.stderr[:200]}", "warn")
                except Exception as ditto_err:
                    emit_log(f"ditto unavailable ({ditto_err}) — falling back to zipfile", "warn")
            if not ditto_ok:
                import zipfile
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                # zipfile strips +x bits; restore them on any .app binaries we extracted.
                for root, _dirs, files in os.walk(temp_dir):
                    if root.endswith("/Contents/MacOS"):
                        for fname in files:
                            fpath = os.path.join(root, fname)
                            try:
                                st = os.stat(fpath)
                                os.chmod(fpath, st.st_mode | 0o111)
                            except OSError:
                                pass
                emit_log("Update extracted with zipfile; +x restored on .app/Contents/MacOS/*", "info")
        
        await asyncio.to_thread(_download_and_extract)
        
        if is_frozen:
            # Look for the new .app bundle
            extracted_app_path = None
            for item in os.listdir(temp_dir):
                if item.endswith(".app"):
                    extracted_app_path = os.path.join(temp_dir, item)
                    break
                    
            if not extracted_app_path:
                raise Exception("No .app bundle found in the downloaded zip.")

            # Create bash script to replace the app
            script_path = os.path.join(temp_dir, "update.sh")
            with open(script_path, "w") as f:
                f.write(f'''#!/bin/bash
sleep 12
rm -rf "{app_path}"
mv "{extracted_app_path}" "{app_path}"
open "{app_path}"
rm -rf "{temp_dir}"
''')
        else:
            # For source code, GitHub zips extract to a top-level folder like parkerallen1-localTTSstudio-xxxxx
            extracted_source_path = None
            for item in os.listdir(temp_dir):
                full_item_path = os.path.join(temp_dir, item)
                if os.path.isdir(full_item_path) and item != "__MACOSX" and "tts_update_" not in item:
                    extracted_source_path = full_item_path
                    break
                    
            if not extracted_source_path:
                raise Exception("No source directory found in the downloaded zip.")
                
            # Create bash script to replace source files and restart python
            script_path = os.path.join(temp_dir, "update.sh")
            with open(script_path, "w") as f:
                f.write(f'''#!/bin/bash
sleep 12
cp -R "{extracted_source_path}/"* "{app_path}/"
rm -rf "{temp_dir}"
cd "{app_path}"
"{sys.executable}" app_launcher.py &
''')
        os.chmod(script_path, 0o755)
        
        # Run it detached
        subprocess.Popen(["/bin/bash", script_path], start_new_session=True)

        # Signal the launcher's OTA watcher to trigger a clean graceful shutdown.
        # The bash script's sleep 12 window covers the 10s uvicorn join timeout.
        ota_requested.set()

        return {"status": "success", "message": "Update initiated. Restarting..."}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
