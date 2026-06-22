import os
import sys
import io
import tempfile
import shutil
import platform
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import uuid
import numpy as np
import soundfile as sf
import torch
import asyncio
import json
import gc
from pydub import AudioSegment
from typing import List, Optional
import requests
import subprocess
import time as _time
from collections import deque

APP_VERSION = "3.2.0" # Current application version
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
    return False

# ─── Settings ─────────────────────────────────────────────────────────────────

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
_DEFAULT_SETTINGS = {
    "preferred_model_size": "0.6B",
    "preferred_model_type": "CustomVoice",
    "auto_preload_on_start": False,
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

    save_settings(current)
    emit_log(f"Settings updated: {current}", "info")
    return current

# Mount statics
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
if not os.path.exists(os.path.join(static_dir, "index.html")):
    with open(os.path.join(static_dir, "index.html"), "w") as f:
        f.write("<html><body><h1>Local TTS Studio Placeholder</h1></body></html>")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))

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
    data = await request.json()
    name = data.get("name", "Untitled Project")
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
    data = await request.json()
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
    safe_para_id = "".join(c for c in para_id if c.isalnum() or c in "-_")
    audio_path = os.path.join(audio_dir, f"{safe_para_id}.wav")
    content = await audio.read()
    with open(audio_path, "wb") as f:
        f.write(content)
    return {"ok": True}

@app.get("/api/projects/{project_id}/audio/{para_id}")
def get_para_audio(project_id: str, para_id: str):
    project_dir = _project_dir(project_id)
    safe_para_id = "".join(c for c in para_id if c.isalnum() or c in "-_")
    audio_path = os.path.join(project_dir, "audio", f"{safe_para_id}.wav")
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Audio not found")
    if not os.path.realpath(audio_path).startswith(os.path.realpath(project_dir)):
        raise HTTPException(status_code=403, detail="Access denied")
    return FileResponse(audio_path, media_type="audio/wav")

@app.post("/api/generate")
async def generate_audio(
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

    text_preview = (text[:80] + "...") if len(text) > 80 else text
    emit_log(f"Generation requested — mode={model_type}, size={model_size}, text=\"{text_preview}\"", "info")

    global generation_lock
    if generation_lock is None:
        generation_lock = asyncio.Lock()

    # Log if we're waiting on the lock (another generation in progress)
    if generation_lock.locked():
        emit_log("Waiting for previous generation to finish (MPS serialization lock)...", "warn")

    try:
        tts_model = await get_tts_model(model_size, model_type)
    except Exception as e:
        emit_log(f"Could not load model for generation: {e}", "error")
        raise HTTPException(status_code=500, detail=str(e))

    async with generation_lock:
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
                subtalker_temperature=temperature
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
                subtalker_temperature=temperature
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

            wavs, sr = await asyncio.to_thread(
                tts_model.generate_voice_clone,
                text=text,
                language=language,
                ref_audio=temp_audio_path,
                ref_text=actual_ref_text,
                temperature=temperature,
                repetition_penalty=1.1,
                top_p=0.8,
                subtalker_temperature=temperature
            )

            # Cleanup temp file if it was a temporary upload
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

        def _merge_sync():
            combined = AudioSegment.empty()
            silence = AudioSegment.silent(duration=1000)
            for idx, content in enumerate(file_contents):
                temp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                temp_in.write(content)
                temp_in.flush()
                temp_in.close()
                segment = AudioSegment.from_wav(temp_in.name)
                if idx > 0:
                    combined += silence
                combined += segment
                os.unlink(temp_in.name)
            temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
            temp_out.close()
            combined.export(temp_out.name, format="wav")
            return temp_out.name

        out_path = await asyncio.to_thread(_merge_sync)
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
    if not audio_file:
        raise HTTPException(status_code=400, detail="No audio file provided.")
        
    valid_treatments = ["podcast", "warmth", "clear"]
    if treatment_type not in valid_treatments:
        raise HTTPException(status_code=400, detail=f"Invalid treatment type. Must be one of: {', '.join(valid_treatments)}")

    emit_log(f"Audio treatment requested: {treatment_type}", "info")
    treat_t0 = _time.monotonic()

    try:
        # Save the uploaded file to a temporary location
        content = await audio_file.read()
        temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_input.write(content)
        temp_input.flush()
        temp_input.close()

        # Define output file
        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_output.close()

        # Determine the ffmpeg filter chain based on treatment_type
        filter_chain = ""
        if treatment_type == "podcast":
            # Loudness normalization only — zero coloration, just standardized level
            filter_chain = "loudnorm=I=-16:TP=-1.5:LRA=11"
        elif treatment_type == "warmth":
            # Strong low shelf (+6dB at 200Hz) for noticeably warm, full-bodied sound
            filter_chain = "bass=g=6:f=200,loudnorm=I=-16:TP=-1.5:LRA=11"
        elif treatment_type == "clear":
            # Strong high shelf (+7dB at 2kHz) for noticeably crisp, airy, bright sound
            filter_chain = "treble=g=7:f=2000,loudnorm=I=-16:TP=-1.5:LRA=11"

        # Execute ffmpeg
        ffmpeg_cmd = "ffmpeg"
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            ffmpeg_cmd = os.path.join(sys._MEIPASS, 'ffmpeg')
            
        command = [
            ffmpeg_cmd,
            "-y",  # Overwrite output file if it exists
            "-i", temp_input.name,
            "-af", filter_chain,
            temp_output.name
        ]
        
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            ffmpeg_err = stderr.decode()[:200]
            emit_log(f"ffmpeg treatment failed (exit {process.returncode}): {ffmpeg_err}", "error")
            raise RuntimeError(f"ffmpeg processing failed")

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
        # Ensure input file is cleaned up on error if it was created
        if 'temp_input' in locals() and os.path.exists(temp_input.name):
            os.unlink(temp_input.name)
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

    try:
        content = await audio_file.read()
        temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_input.write(content)
        temp_input.flush()
        temp_input.close()

        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=cfg["suffix"])
        temp_output.close()

        ffmpeg_cmd = "ffmpeg"
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            ffmpeg_cmd = os.path.join(sys._MEIPASS, 'ffmpeg')

        command = [ffmpeg_cmd, "-y", "-i", temp_input.name] + cfg["codec"] + [temp_output.name]

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await process.communicate()

        os.unlink(temp_input.name)

        if process.returncode != 0:
            print(f"ffmpeg convert error: {stderr.decode()}")
            raise RuntimeError("ffmpeg conversion failed")

        return FileResponse(
            temp_output.name,
            media_type=cfg["media_type"],
            filename=f"audio{cfg['suffix']}",
            background=BackgroundTask(os.unlink, temp_output.name)
        )

    except Exception as e:
        if 'temp_input' in locals() and os.path.exists(temp_input.name):
            os.unlink(temp_input.name)
        raise HTTPException(status_code=500, detail=f"Failed to convert audio: {str(e)}")

@app.get("/api/check_update")
async def check_update():
    try:
        response = await asyncio.to_thread(requests.get, f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
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
    try:
        import shutil
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
            r = requests.get(download_url, stream=True)
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
