import os
os.environ['MKL_THREADING_LAYER'] = 'sequential'
os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'

import threading
import time
import webbrowser
import signal
import sys
import socket

# Redirect stdout/stderr to a log file so we can debug the frozen app
if getattr(sys, 'frozen', False):
    _log_path = os.path.expanduser("~/.qwen_tts_studio/app.log")
    os.makedirs(os.path.dirname(_log_path), exist_ok=True)
    _log_file = open(_log_path, "w")
    sys.stdout = _log_file
    sys.stderr = _log_file
import subprocess
import tempfile

PORT = 8001
URL = f"http://127.0.0.1:{PORT}"

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

// Cycle through status messages every 3 seconds to show progress
const stepTimer = setInterval(() => {
  if (stepIdx < steps.length) {
    status.textContent = steps[stepIdx++];
  }
}, 3000);

// Poll the server every second
const poller = setInterval(async () => {
  try {
    const r = await fetch(SERVER, { mode: "no-cors" });
    // no-cors gives opaque response (status 0) but means server is up
    clearInterval(poller);
    clearInterval(stepTimer);
    status.textContent = "Ready! Redirecting...";
    setTimeout(() => { window.location.href = SERVER; }, 300);
  } catch (e) {
    // Server not up yet — keep polling
  }
}, 1000);
</script>
</body>
</html>"""


def kill_stale_server():
    """Kill any existing process holding our port so we can start fresh."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{PORT}"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        if pids:
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
            time.sleep(0.5)
            print(f"Cleared stale process on port {PORT}.")
    except FileNotFoundError:
        pass


def port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def write_loading_page():
    """Write a self-contained loading page that polls the server and redirects when ready."""
    html = LOADING_HTML.replace("PLACEHOLDER_URL", URL)
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".html", prefix="lts_loading_")
    f.write(html.encode())
    f.close()
    return f.name


def run_server():
    """Import the heavy app module here (not at top-level) so the browser opens first."""
    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=PORT)


def signal_handler(sig, frame):
    print("\nShutting down Local TTS Studio...")
    sys.exit(0)


if __name__ == '__main__':
    # PyInstaller edge case: if third-party libraries use `sys.executable -c "..."` to run Python code,
    # the frozen PyInstaller app will simply restart itself. We catch unexpected args like `-c` or `--multiprocessing-fork`
    # and exit immediately to prevent killing our own server and opening endless tabs.
    if getattr(sys, 'frozen', False) and len(sys.argv) > 1:
        if sys.argv[1] == '-c' or sys.argv[1] == '--multiprocessing-fork' or sys.argv[1] == '-m':
            sys.exit(0)

    import multiprocessing
    multiprocessing.freeze_support()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Kill any stale server left over from a previous run
    if port_in_use():
        print(f"Port {PORT} already in use — clearing stale process...")
        kill_stale_server()
        for _ in range(10):
            if not port_in_use():
                break
            time.sleep(0.3)

    # Show loading page in browser IMMEDIATELY (before heavy imports)
    loading_page = write_loading_page()
    webbrowser.open(f"file://{loading_page}")
    print("Loading page opened — starting server in background...")

    # Start the server (heavy imports happen inside the thread)
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    # Keep main thread alive via a tiny native GUI (keeps app in Dock)
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("Local TTS Studio")
        # Make the window very small and unobtrusive
        root.geometry("250x100")
        root.resizable(False, False)
        
        lbl = tk.Label(root, text="Local TTS Studio is running.\n\nClose this window to\nshut down the server.", justify="center")
        lbl.pack(expand=True)

        def on_closing():
            print("\nShutting down Local TTS Studio...")
            if loading_page:
                try:
                    os.unlink(loading_page)
                except OSError:
                    pass
            root.destroy()
            sys.exit(0)

        root.protocol("WM_DELETE_WINDOW", on_closing)

        # Polling function to clean up the loading page
        def check_loading_page():
            global loading_page
            if loading_page and port_in_use():
                try:
                    os.unlink(loading_page)
                except OSError:
                    pass
                loading_page = None
            if loading_page:
                root.after(1000, check_loading_page)

        root.after(1000, check_loading_page)
        
        # Start the native window event loop
        root.mainloop()

    except Exception as e:
        print(f"GUI failed to start, falling back to sleep loop: {e}")
        try:
            while True:
                time.sleep(1)
                # Clean up loading page once server is up
                if loading_page and port_in_use():
                    try:
                        os.unlink(loading_page)
                    except OSError:
                        pass
                    loading_page = None
        except (KeyboardInterrupt, SystemExit):
            print("\nShutting down Local TTS Studio...")
            if loading_page:
                try:
                    os.unlink(loading_page)
                except OSError:
                    pass
