# Version 1.0.3

This release introduces several highly requested features and quality-of-life improvements, especially for users running the app from source!

## What's New

### 🚀 Self-Updating Source Code
Good news for users running directly from Python source (`python main.py`)! The OTA update feature now fully supports source code deployments. 
- If you're running from source, clicking **"Update Now"** will automatically download the latest source from GitHub, neatly overwrite your local files, and restart the server. 
- The version checking logic was also overhauled to strictly use numeric comparisons (fixing a bug where the update banner stubbornly persisted on the latest code).

### 🗑️ Paragraph Deletion
You can now easily curate your generation queue! 
- Each parsed paragraph box now features a small **`X`** in the top right corner. 
- Clicking it instantly removes the paragraph and any audio it generated, and intelligently updates your total counts and the "Download All" requirement logic.

### 🔍 Enhanced Debugging & Logs
Behind-the-scenes failures are now front-and-center so you don't have to go digging for answers:
- **Activity Log Integration:** Model downloading progress and detailed connection errors are now piped directly into the Studio Activity Log panel in the bottom right corner of the UI.
- **Helpful Startup Hints:** If the initial boot-up loading screen hangs for more than 12 seconds, a hint will automatically appear revealing the exact location of the `app.log` file so you can easily see what went wrong.

---

### Installation
- **macOS Users:** Download the `Local_TTS_Studio_macOS_v1.0.3.zip` below, extract it, and run the `.app` bundle.
- **Source Users:** Download the `Source code` (zip or tar.gz), install the requirements in your venv, and run `python app_launcher.py`.
