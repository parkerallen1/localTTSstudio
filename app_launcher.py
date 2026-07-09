"""
Local TTS Studio — desktop entrypoint / launcher.

This is the script PyInstaller bundles as "Local TTS Studio.app" (see
LocalTTSStudio.spec). It boots the FastAPI backend (main.py) and wraps it in a
native-feeling macOS desktop experience around the browser-based UI.

What it does, in order:
  • Sets MKL/OpenMP env vars BEFORE torch is imported (must come first to avoid
    libomp crashes in the frozen bundle).
  • When frozen, redirects stdout/stderr to ~/.qwen_tts_studio/app.log (rotated
    each launch) so the packaged app is debuggable.
  • Enforces a single instance via a PID + HTTP-probe lock; if the app is already
    running, it activates that instance and exits.
  • Starts uvicorn serving the main.py app on 127.0.0.1:PORT (8001, override
    with QWEN_TTS_PORT for dev) on a non-daemon thread, owning signal handling
    for a clean shutdown.
  • Opens a native app window (pywebview / WKWebView) showing a loading screen,
    then the real UI once the server is up. Closing the window quits the app —
    there is no menu-bar icon.

Note: the backend listens on 8001 here; main.py is otherwise transport-agnostic.
"""
import os
os.environ['MKL_THREADING_LAYER'] = 'sequential'
os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

import threading
import time
import webbrowser
import signal
import sys
import socket
import platform
import traceback
from datetime import datetime, timezone

# Redirect stdout/stderr to a log file so we can debug the frozen app
if getattr(sys, 'frozen', False):
    os.environ["PYTHONUNBUFFERED"] = "1"
    _log_path = os.path.expanduser("~/.qwen_tts_studio/app.log")
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    # Rotate: move existing app.log -> app.log.1 before opening a fresh log
    _log_path_1 = _log_path + ".1"
    if os.path.exists(_log_path):
        os.replace(_log_path, _log_path_1)
    _log_file = open(_log_path, mode="w", buffering=1, encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file
    _now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _mac_ver = platform.mac_ver()[0] or "unknown"
    print("================================================================")
    print("[LAUNCHER] Local TTS Studio starting")
    print(f"[LAUNCHER] Time: {_now}")
    print(f"[LAUNCHER] Platform: {platform.system()} {platform.release()} {platform.machine()} (macOS {_mac_ver})")
    print(f"[LAUNCHER] Python: {sys.version.split()[0]}")
    print(f"[LAUNCHER] Executable: {sys.executable}")
    print(f"[LAUNCHER] PID: {os.getpid()}")
    print("================================================================")
    sys.stdout.flush()
import subprocess
import atexit
import urllib.request


def _start_heartbeat():
    """Spawn a daemon thread that logs a heartbeat every 30 seconds."""
    _start_time = time.monotonic()
    _pid = os.getpid()

    def _beat():
        while True:
            time.sleep(30)
            elapsed = int(time.monotonic() - _start_time)
            print(f"[LAUNCHER] [HEARTBEAT] t+{elapsed}s pid={_pid}")
            sys.stdout.flush()

    _t = threading.Thread(target=_beat, daemon=True, name="launcher-heartbeat")
    _t.start()


_start_heartbeat()

PORT = int(os.environ.get("QWEN_TTS_PORT", "8001"))
URL = f"http://127.0.0.1:{PORT}"
DATA_DIR = os.path.expanduser("~/.qwen_tts_studio")
# Non-default ports (dev runs) get their own lock so they can't clobber the
# installed app's lock file.
LOCK_PATH = os.path.join(DATA_DIR, "launcher.pid" if PORT == 8001 else f"launcher.{PORT}.pid")

# Globals set up after start_server_thread() — used by shutdown()
_server = None
_server_thread = None

LOADING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Local TTS Studio</title>
<style>
  :root { --bg: #0d1117; --text: #e6edf3; --muted: #8b949e; --primary: #58a6ff; --green: #238636; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; overflow: hidden;
  }
  .container { text-align: center; max-width: 420px; padding: 2rem; }
  h1 {
    font-size: 2rem; font-weight: 700; margin-bottom: 0.5rem;
    background: linear-gradient(135deg, var(--primary), var(--green));
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
  }
  .subtitle { color: var(--muted); font-size: 1rem; margin-bottom: 2.5rem; }
  .spinner {
    width: 40px; height: 40px; margin: 0 auto 1.5rem;
    border: 3px solid rgba(88,166,255,0.15);
    border-top-color: var(--primary);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #status { color: var(--muted); font-size: 0.95rem; min-height: 1.5em; }
  .step { transition: opacity 0.3s; }
</style>
</head>
<body>
<div class="container">
  <h1>Local TTS Studio</h1>
  <p class="subtitle">Local text-to-speech, runs entirely on your machine.</p>
  <div class="spinner"></div>
  <p id="status">Starting server...</p>
  <p id="log-hint" style="display: none; color: var(--muted); font-size: 0.85rem; margin-top: 2rem; opacity: 0.8; line-height: 1.6;">
    Still starting up? On first launch, models may download automatically &mdash;
    this is a one-time download of 1.5&ndash;3.5 GB depending on the model size.
    On a typical home connection that takes 3&ndash;10 minutes.
    The download progress will appear in the app once the server is ready.<br><br>
    For details, check the log file at:<br>
    <code style="background: rgba(255,255,255,0.1); padding: 2px 6px; border-radius: 4px; color: var(--text);">~/.qwen_tts_studio/app.log</code>
  </p>
</div>
<script>
// The launcher polls the server port from Python and swaps this page for the
// real UI via window.load_url() — this script only animates the status text.
const status = document.getElementById("status");
const steps = [
  "Loading Python runtime...",
  "Importing audio libraries...",
  "Starting FastAPI server...",
  "Almost ready..."
];
let stepIdx = 0;

setInterval(() => {
  if (stepIdx < steps.length) {
    status.textContent = steps[stepIdx++];
  }
}, 3000);

setTimeout(() => {
  document.getElementById("log-hint").style.display = "block";
}, 12000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _http_probe(url, timeout=1.5):
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def _release_lock():
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


def acquire_single_instance():
    """Return normally if we own the lock. If another instance is already running,
    bring its window to the front and exit silently."""
    os.makedirs(DATA_DIR, exist_ok=True)

    for _attempt in range(2):
        if os.path.exists(LOCK_PATH):
            try:
                pid = int(open(LOCK_PATH).read().strip())
            except (ValueError, OSError):
                pid = None

            if pid and _pid_alive(pid) and _http_probe(URL):
                print(f"[LAUNCHER] Another instance already running (pid={pid}), activating it and exiting.")
                sys.stdout.flush()
                if getattr(sys, 'frozen', False):
                    # Bring the running app's window to the front
                    subprocess.run(["open", "-b", "com.localtts.studio"], check=False)
                else:
                    webbrowser.open(URL)
                sys.exit(0)

            # Stale lock — remove and take ownership
            print(f"[LAUNCHER] Stale lock found (pid={pid}), cleaning up.")
            sys.stdout.flush()
            try:
                os.unlink(LOCK_PATH)
            except OSError:
                pass

        try:
            # Atomic create — fails if another process sneaked in between our check and create
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            atexit.register(_release_lock)
            print(f"[LAUNCHER] Single-instance lock acquired at {LOCK_PATH}")
            sys.stdout.flush()
            return
        except FileExistsError:
            # Lost the race — loop once more to re-check
            continue

    # Extremely unlikely to reach here; just proceed without lock
    print("[LAUNCHER] WARNING: could not acquire single-instance lock, proceeding anyway.")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def start_server_thread():
    """Import uvicorn + app, build an explicit Server instance, and start it on a
    non-daemon thread so we can signal it to stop gracefully."""
    import uvicorn

    print("[LAUNCHER] Importing main module (this triggers torch/transformers — can take 5–20s on first run)")
    sys.stdout.flush()
    from main import app as fastapi_app
    print(f"[LAUNCHER] main module imported; starting uvicorn on 127.0.0.1:{PORT}")
    sys.stdout.flush()

    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=PORT, log_level="info", lifespan="on")
    server = uvicorn.Server(config)
    # We own signal handling; prevent uvicorn from fighting us for SIGINT/SIGTERM on its thread
    server.install_signal_handlers = lambda: None

    def _run():
        try:
            server.run()
        except Exception:
            print("[LAUNCHER] uvicorn raised an exception:")
            sys.stdout.flush()
            traceback.print_exc()
            sys.stdout.flush()

    thread = threading.Thread(target=_run, name="uvicorn", daemon=False)
    thread.start()
    return server, thread


def shutdown():
    """Gracefully stop uvicorn (runs lifespan shutdown), clean up, and exit."""
    global _server, _server_thread
    print("\n[LAUNCHER] Shutting down Local TTS Studio...")
    sys.stdout.flush()

    if _server is not None:
        _server.should_exit = True
        if _server_thread is not None:
            # Short grace period: the UI's SSE streams (activity log/progress)
            # hold connections open, and with a dock icon the user can see the
            # app lingering — force after 3s rather than uvicorn's leisurely wait.
            _server_thread.join(timeout=3)
            if _server_thread.is_alive():
                print("[LAUNCHER] Graceful shutdown timed out — forcing exit.")
                sys.stdout.flush()
                _server.force_exit = True
                _server_thread.join(timeout=3)

    _release_lock()
    print("[LAUNCHER] Shutdown complete.")
    sys.stdout.flush()
    os._exit(0)


def _signal_handler(sig, frame):
    shutdown()


# ---------------------------------------------------------------------------
# OTA watcher — blocks until main.py signals an update, then shuts down cleanly
# ---------------------------------------------------------------------------

def _start_ota_watcher():
    def _watch():
        try:
            from main import ota_requested
            ota_requested.wait()
            print("[LAUNCHER] OTA update signalled — initiating graceful shutdown for restart.")
            sys.stdout.flush()
            shutdown()
        except Exception:
            pass  # main module not yet imported, or no ota_requested — safe to ignore

    t = threading.Thread(target=_watch, daemon=True, name="ota-watcher")
    t.start()


# ---------------------------------------------------------------------------
# Native app window (pywebview / WKWebView)
# ---------------------------------------------------------------------------

def run_webview():
    """Open the app in a native window. Blocks until the window is closed,
    then shuts the server down — closing the window quits the app."""
    import webview

    # Exported audio is saved via <a download> on a blob URL; this makes
    # WKWebView hand those to a save dialog instead of ignoring them.
    webview.settings['ALLOW_DOWNLOADS'] = True

    window = webview.create_window(
        "Local TTS Studio",
        html=LOADING_HTML,
        width=1280,
        height=880,
        min_size=(900, 600),
        text_select=True,
    )

    def _load_ui_when_ready():
        while not port_in_use():
            time.sleep(0.5)
        print("[LAUNCHER] Server is up — loading UI in the app window")
        sys.stdout.flush()
        window.load_url(URL)

    threading.Thread(target=_load_ui_when_ready, daemon=True, name="ui-loader").start()

    # Runs the Cocoa main loop; returns when the user closes the window
    webview.start()
    print("[LAUNCHER] Window closed by user.")
    sys.stdout.flush()
    shutdown()


# ---------------------------------------------------------------------------
# Tk fallback (kept for Linux dev / PyObjC-unavailable scenarios)
# ---------------------------------------------------------------------------

def run_tk_fallback():
    import tkinter as tk

    root = tk.Tk()
    root.title("Local TTS Studio")
    root.geometry("260x110")
    root.resizable(False, False)

    tk.Label(
        root,
        text="Local TTS Studio is running.\n\nClick Quit or close this window\nto shut down the server.",
        justify="center"
    ).pack(expand=True, pady=6)

    tk.Button(root, text="Quit Server", command=lambda: _tk_quit(root), width=14).pack(pady=4)

    def _tk_quit(r):
        r.destroy()
        shutdown()

    root.protocol("WM_DELETE_WINDOW", lambda: _tk_quit(root))
    webbrowser.open(URL)
    root.mainloop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    # PyInstaller edge case: third-party libraries can re-invoke the frozen binary
    # with args like -c / --multiprocessing-fork / -m. Exit immediately to avoid
    # killing our own server or opening duplicate tabs.
    if getattr(sys, 'frozen', False) and len(sys.argv) > 1:
        if sys.argv[1] in ('-c', '--multiprocessing-fork', '-m'):
            sys.exit(0)

    import multiprocessing
    multiprocessing.freeze_support()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Enforce single instance — exits here if another app is already running
    acquire_single_instance()

    # Start the server (heavy imports happen inside start_server_thread)
    print("[LAUNCHER] Starting uvicorn in background thread")
    sys.stdout.flush()
    try:
        _server, _server_thread = start_server_thread()
    except Exception:
        print("[LAUNCHER] Failed to start server:")
        traceback.print_exc()
        sys.stdout.flush()
        sys.exit(1)

    # Start the OTA watcher (waits for main.py to signal an update)
    _start_ota_watcher()

    # Run the UI that keeps the process alive and gives the user a kill switch.
    # Priority: pywebview app window → Tk fallback → headless sleep loop
    try:
        import webview  # noqa: F401 — just checking availability
        has_webview = True
    except ImportError:
        has_webview = False

    if has_webview:
        run_webview()
    else:
        print("[LAUNCHER] pywebview not available, falling back to Tk window + browser.")
        sys.stdout.flush()
        try:
            run_tk_fallback()
        except Exception as e:
            print(f"[LAUNCHER] Tk fallback failed ({e}), running headless sleep loop.")
            sys.stdout.flush()
            webbrowser.open(URL)
            try:
                while True:
                    time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                shutdown()
