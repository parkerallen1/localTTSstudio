import torch
import numpy as np
import soundfile as sf
import os
import sys

try:
    from qwen_tts import Qwen3TTSModel
except ImportError:
    print("qwen_tts not installed")
    sys.exit()

def test_model(size, model_type, dtype, device, test_func):
    model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
    print(f"Loading {model_id} on {device} with {dtype}")
    try:
        model = Qwen3TTSModel.from_pretrained(model_id, device_map=device, dtype=dtype)
        wav, sr = test_func(model)
        print(f"Success for {model_id}!")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Failed for {model_id} with {dtype}: {e}")

def test_custom_voice(model):
    return model.generate_custom_voice(
        text="Testing this audio generation.",
        language="English",
        speaker="Vivian",
        temperature=0.3,
        repetition_penalty=1.1,
        top_p=0.8,
        subtalker_temperature=0.3
    )

device = "mps" if torch.backends.mps.is_available() else "cpu"

print("--- Testing 0.6B with Float16 ---")
test_model("0.6B", "CustomVoice", torch.float16, device, test_custom_voice)

print("--- Testing 0.6B with Float32 ---")
test_model("0.6B", "CustomVoice", torch.float32, device, test_custom_voice)
