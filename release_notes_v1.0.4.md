# Version 1.0.4

A stability-focused patch that fixes a critical crash affecting multi-paragraph generation.

## What's Fixed

### 🛡️ Fixed: Crash During Multi-Paragraph Generation
Resolved a `SIGSEGV` (segmentation fault) that occurred when generating audio for multiple paragraphs. The root cause was concurrent PyTorch inference calls racing on the same MPS device — now all model inference is properly serialized with an async lock.

- **Backend:** Added a `generation_lock` to ensure only one inference request runs at a time on the GPU, preventing memory corruption in PyTorch/MPS.
- **Frontend:** Generation requests are now sent sequentially (one at a time) instead of in parallel, matching the backend serialization.

> Multi-paragraph workflows should now complete reliably without crashes.

---

### Installation
- **macOS Users:** Download the `Local_TTS_Studio_macOS_v1.0.4.zip` below, extract it, and run the `.app` bundle.
- **Source Users:** Download the `Source code` (zip or tar.gz), install the requirements in your venv, and run `python app_launcher.py`.
