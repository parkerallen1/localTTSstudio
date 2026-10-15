"""
MLX generation worker — Qwen3-TTS on Apple's MLX, in its own process.

Why a separate process: mlx-audio needs transformers>=5.14, and the PyTorch
engine (qwen-tts) pins transformers==4.57.3, so the two can't share an
environment. This runs under its own venv (requirements-mlx.txt) and main.py
talks to it through mlx_engine.py. Measured on the 16 GB M4 mini, the 1.7B
Base model at 8-bit ran 1.66x real time with flat memory over a whole article,
against PyTorch's 0.47x and ~65 MB of growth per paragraph.

Protocol — one JSON object per line on stdin, one reply per line on stdout:
  request: {"model": "mlx-community/...-8bit", "text": ..., "out": "/path.wav",
            "ref_audio"?, "ref_text"?, "voice"?, "instruct"?,
            "temperature", "top_p", "top_k", "repetition_penalty", "lang_code"}
  reply:   {"ok": true, "sample_rate": 24000, "seconds": 12.3, "wall": 7.4}
           {"ok": false, "error": "..."}
  or, to check a take: {"op": "transcribe", "audio": "/path.wav"}
  reply:   {"ok": true, "text": "...", "wall": 0.9}
One TTS model is held at a time; asking for another frees the first. Whisper
(for the speech check, speech_check.py) loads on first use and stays. The worker
exits when stdin closes, i.e. when the app that started it goes away.
"""
import json
import os
import sys
import time
import traceback

# Replies go on the real stdout; everything else — library progress bars and
# prints — goes to stderr, so it can never corrupt the protocol stream.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

import numpy as np            # noqa: E402
import soundfile as sf        # noqa: E402
import mlx.core as mx         # noqa: E402
from mlx_audio.tts.utils import load_model   # noqa: E402

_model, _model_id = None, None
WHISPER = "mlx-community/whisper-large-v3-turbo"


def _get_model(model_id):
    global _model, _model_id
    if _model_id != model_id:
        _model, _model_id = None, None
        mx.clear_cache()
        _model = load_model(model_id)
        _model_id = model_id
    return _model


def _generate(req):
    model = _get_model(req["model"])
    kwargs = {k: req[k] for k in ("temperature", "top_p", "top_k", "repetition_penalty",
                                  "lang_code", "voice", "instruct", "ref_audio", "ref_text",
                                  "max_tokens")
              if req.get(k) is not None}
    t0 = time.monotonic()
    # split_pattern="" keeps a paragraph as one segment, like the PyTorch path.
    chunks = [np.array(r.audio) for r in model.generate(text=req["text"], split_pattern="", **kwargs)]
    wall = time.monotonic() - t0
    if not chunks:
        raise RuntimeError("the model returned no audio")
    audio = np.concatenate(chunks)
    sf.write(req["out"], audio, model.sample_rate, format="WAV")
    return {"ok": True, "sample_rate": model.sample_rate,
            "seconds": round(len(audio) / model.sample_rate, 2), "wall": round(wall, 2)}


def _transcribe(req):
    import mlx_whisper      # only when asked: generation-only setups needn't have it
    t0 = time.monotonic()
    # mlx_whisper keeps the loaded model between calls (ModelHolder).
    r = mlx_whisper.transcribe(req["audio"], path_or_hf_repo=WHISPER, language="en",
                               condition_on_previous_text=False)
    return {"ok": True, "text": r["text"].strip(), "wall": round(time.monotonic() - t0, 2)}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            reply = _transcribe(req) if req.get("op") == "transcribe" else _generate(req)
        except Exception as e:
            traceback.print_exc()
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        _proto.write(json.dumps(reply) + "\n")


if __name__ == "__main__":
    main()
