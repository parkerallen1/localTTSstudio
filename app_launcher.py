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
import tempfile
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

PORT = 8001
URL = f"http://127.0.0.1:{PORT}"
DATA_DIR = os.path.expanduser("~/.qwen_tts_studio")
LOCK_PATH = os.path.join(DATA_DIR, "launcher.pid")

# Globals set up after start_server_thread() — used by shutdown()
_server = None
_server_thread = None
_loading_page = None

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
const SERVER = "PLACEHOLDER_URL";
const status = document.getElementById("status");
const steps = [
  "Loading Python runtime...",
  "Importing audio libraries...",
  "Starting FastAPI server...",
  "Almost ready..."
];
let stepIdx = 0;

const stepTimer = setInterval(() => {
  if (stepIdx < steps.length) {
    status.textContent = steps[stepIdx++];
  }
}, 3000);

const hintTimer = setTimeout(() => {
  document.getElementById("log-hint").style.display = "block";
}, 12000);

const poller = setInterval(async () => {
  try {
    const r = await fetch(SERVER, { mode: "no-cors" });
    clearInterval(poller);
    clearInterval(stepTimer);
    clearTimeout(hintTimer);
    status.textContent = "Ready! Redirecting...";
    setTimeout(() => { window.location.href = SERVER; }, 300);
  } catch (e) {
    // Server not up yet — keep polling
  }
}, 1000);
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
    open a browser tab pointing at it and exit silently."""
    os.makedirs(DATA_DIR, exist_ok=True)

    for _attempt in range(2):
        if os.path.exists(LOCK_PATH):
            try:
                pid = int(open(LOCK_PATH).read().strip())
            except (ValueError, OSError):
                pid = None

            if pid and _pid_alive(pid) and _http_probe(URL):
                print(f"[LAUNCHER] Another instance already running (pid={pid}), opening browser and exiting.")
                sys.stdout.flush()
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


def write_loading_page():
    html = LOADING_HTML.replace("PLACEHOLDER_URL", URL)
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".html", prefix="lts_loading_")
    f.write(html.encode())
    f.close()
    return f.name


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
    global _server, _server_thread, _loading_page
    print("\n[LAUNCHER] Shutting down Local TTS Studio...")
    sys.stdout.flush()

    if _server is not None:
        _server.should_exit = True
        if _server_thread is not None:
            _server_thread.join(timeout=10)
            if _server_thread.is_alive():
                print("[LAUNCHER] Graceful shutdown timed out — forcing exit.")
                sys.stdout.flush()
                _server.force_exit = True
                _server_thread.join(timeout=3)

    if _loading_page:
        try:
            os.unlink(_loading_page)
        except OSError:
            pass
        _loading_page = None

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
# macOS menu-bar app (rumps)
# ---------------------------------------------------------------------------

def _menubar_icon_path():
    """Return the path to the template icon, whether running frozen or from source."""
    if getattr(sys, 'frozen', False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", "menubar_iconTemplate.png")


def run_menubar():
    import rumps

    class MenuBarApp(rumps.App):
        def __init__(self):
            icon = _menubar_icon_path()
            super().__init__(
                name="Local TTS Studio",
                icon=icon if os.path.exists(icon) else None,
                quit_button=None,
            )
            self.status_item = rumps.MenuItem("● Server running (8001)")
            self.status_item.set_callback(None)  # non-clickable label

            self.menu = [
                self.status_item,
                rumps.MenuItem("Open in browser", callback=self.open_browser),
                None,  # separator
                rumps.MenuItem("Quit Local TTS Studio", callback=self.quit_app),
            ]

            # Poll once per second to clean up the loading page tmp file
            self._lp_timer = rumps.Timer(self._check_loading_page, 1)
            self._lp_timer.start()

        def open_browser(self, _):
            webbrowser.open(URL)

        def quit_app(self, _):
            self.status_item.title = "● Shutting down..."
            # Run shutdown in a thread so the menu item title update can render,
            # but the NSApp exit happens only after the thread finishes.
            def _do():
                shutdown()  # calls os._exit(0) internally
            threading.Thread(target=_do, daemon=True).start()

        def _check_loading_page(self, _timer):
            global _loading_page
            if _loading_page and port_in_use():
                try:
                    os.unlink(_loading_page)
                except OSError:
                    pass
                _loading_page = None
                self._lp_timer.stop()

    MenuBarApp().run()


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

    def _check_loading_page():
        global _loading_page
        if _loading_page and port_in_use():
            try:
                os.unlink(_loading_page)
            except OSError:
                pass
            _loading_page = None
        if _loading_page:
            root.after(1000, _check_loading_page)

    root.after(1000, _check_loading_page)
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

    # Show loading page in browser immediately (before heavy imports)
    _loading_page = write_loading_page()
    webbrowser.open(f"file://{_loading_page}")
    print(f"[LAUNCHER] Loading page opened at file://{_loading_page}, starting uvicorn in background thread")
    sys.stdout.flush()

    # Start the server (heavy imports happen inside start_server_thread)
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
    # Priority: rumps (macOS menu bar) → Tk fallback → headless sleep loop
    try:
        import rumps  # noqa: F401 — just checking availability
        run_menubar()
    except ImportError:
        print("[LAUNCHER] rumps not available, falling back to Tk window.")
        sys.stdout.flush()
        try:
            run_tk_fallback()
        except Exception as e:
            print(f"[LAUNCHER] Tk fallback failed ({e}), running headless sleep loop.")
            sys.stdout.flush()
            try:
                while True:
                    time.sleep(1)
                    if _loading_page and port_in_use():
                        try:
                            os.unlink(_loading_page)
                        except OSError:
                            pass
                        _loading_page = None
            except (KeyboardInterrupt, SystemExit):
                shutdown()
