"""Dev helper: pre-download the Qwen3-TTS 0.6B Base model into the local
Hugging Face cache so the first real run doesn't have to. Picks MPS if available,
else CPU. Not used by the app at runtime — the backend (main.py) downloads
models on demand. Run directly: `python download_model.py`."""
import sys
print("starting script")
from qwen_tts import Qwen3TTSModel
import torch

print("Downloading model...")
device = "mps" if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available() else "cpu"
dtype = torch.bfloat16 if device == "mps" else torch.float32
model_id = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"

model = Qwen3TTSModel.from_pretrained(model_id, device_map=device, dtype=dtype)
print("Download complete!")
