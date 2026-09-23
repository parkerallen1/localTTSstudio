"""Checks for mlx_engine.py against a fake worker (no MLX needed):
the reply protocol, a crashed worker restarted once, and errors reported.

    ./venv/bin/python test_mlx_engine.py
"""
import os
import sys
import tempfile

import mlx_engine

PASS, FAIL = [], []


def check(label, got, want):
    (PASS if got == want else FAIL).append(label)
    print(f"[{'ok  ' if got == want else 'FAIL'}] {label}" + ("" if got == want else f"\n        got {got!r}, want {want!r}"))


FAKE = r'''
import json, os, sys
state = sys.argv[1]
for line in sys.stdin:
    req = json.loads(line)
    if req["text"] == "crash-once" and not os.path.exists(state):
        open(state, "w").close()
        os._exit(3)                                  # die mid-request, once
    if req["text"] == "boom":
        print(json.dumps({"ok": False, "error": "ValueError: boom"}), flush=True)
        continue
    open(req["out"], "wb").write(b"RIFF-fake-" + req["text"].encode())
    print(json.dumps({"ok": True, "sample_rate": 24000, "seconds": 1.0, "wall": 0.1,
                      "echo": {k: req.get(k) for k in ("lang_code", "top_p", "max_tokens", "ref_text")}}), flush=True)
'''
d = tempfile.mkdtemp()
fake = os.path.join(d, "fake_worker.py")
open(fake, "w").write(FAKE)
state = os.path.join(d, "crashed")
mlx_engine.MLX_PYTHON = sys.executable
mlx_engine.WORKER = fake
real_popen = mlx_engine.subprocess.Popen
mlx_engine.subprocess.Popen = lambda argv, **kw: real_popen(argv + [state], **kw)

check("enabled with a python and a worker", mlx_engine.enabled(), True)
check("Base 1.7B maps to the 8-bit build", mlx_engine.model_for("1.7B", "Base"),
      "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit")
check("0.6B VoiceDesign has no MLX build", mlx_engine.model_for("0.6B", "VoiceDesign"), None)

wav, reply = mlx_engine.generate("m", "hello", "English", 0.85, max_tokens=300, ref_text="ref")
check("audio comes back", wav, b"RIFF-fake-hello")
check("settings match the PyTorch path", reply["echo"],
      {"lang_code": "english", "top_p": 0.8, "max_tokens": 300, "ref_text": "ref"})
check("temp file removed", [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("tts-mlx-")], [])

wav, _ = mlx_engine.generate("m", "crash-once", "English", 0.85)
check("a crashed worker is restarted and the request retried", wav, b"RIFF-fake-crash-once")

try:
    mlx_engine.generate("m", "boom", "English", 0.85)
    got = None
except mlx_engine.MLXError as e:
    got = str(e)
check("a worker error is reported, not retried", got, "ValueError: boom")

mlx_engine.MLX_PYTHON = ""
check("off when QWEN_TTS_MLX_PYTHON isn't set", mlx_engine.model_for("1.7B", "Base"), None)
mlx_engine.worker.stop()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
