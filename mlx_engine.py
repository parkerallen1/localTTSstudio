"""
MLX engine — the app's side of mlx_worker.py.

Qwen3-TTS runs faster on Apple Silicon through MLX than through PyTorch/MPS,
and without PyTorch's per-generation memory growth; see mlx_worker.py for the
measurements and for why it has to be a separate process. This module starts
that process, keeps it alive, and turns one generation into one request.

Enabled by pointing QWEN_TTS_MLX_PYTHON at the Python of the MLX venv (built
from requirements-mlx.txt). When it's unset or missing — any machine that
isn't set up for it, Intel Macs, Windows — the app uses PyTorch as before.
Only modes with a published 8-bit MLX build use it (MLX_MODELS); others fall
back to PyTorch too.
"""
import json
import os
import select
import subprocess
import tempfile
import threading

MLX_PYTHON = (os.environ.get("QWEN_TTS_MLX_PYTHON") or "").strip()
WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mlx_worker.py")

# (size, mode) -> MLX repo. 8-bit: in a blind A/B against the live PyTorch
# narration it was judged just as good, at 3.5x the speed.
MLX_MODELS = {
    ("0.6B", "Base"): "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
    ("1.7B", "Base"): "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    ("0.6B", "CustomVoice"): "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit",
    ("1.7B", "CustomVoice"): "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
    ("1.7B", "VoiceDesign"): "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
}

# A paragraph is at most a few minutes of audio; this only bounds a hang.
REPLY_TIMEOUT = 1800


class MLXError(RuntimeError):
    pass


class _WorkerExited(MLXError):
    """The worker process died — worth one fresh start (unlike a hang)."""


def enabled():
    return bool(MLX_PYTHON) and os.path.exists(MLX_PYTHON) and os.path.exists(WORKER)


def model_for(size, mode):
    """The MLX repo for this size/mode, or None when it should use PyTorch."""
    return MLX_MODELS.get((size, mode)) if enabled() else None


class _Worker:
    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._proc and self._proc.poll() is None:
            return
        # stderr is inherited, so the worker's own logs land in the app's log.
        self._proc = subprocess.Popen(
            [MLX_PYTHON, WORKER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)

    def _ask(self, request):
        self._ensure()
        self._proc.stdin.write(json.dumps(request) + "\n")
        self._proc.stdin.flush()
        ready, _, _ = select.select([self._proc.stdout], [], [], REPLY_TIMEOUT)
        if not ready:
            self.stop()
            raise MLXError(f"no reply from the MLX worker within {REPLY_TIMEOUT}s")
        line = self._proc.stdout.readline()
        if not line:
            code = self._proc.poll()
            self._proc = None
            raise _WorkerExited(f"the MLX worker exited (code {code})")
        return json.loads(line)

    def generate(self, request):
        """Send one request; if the worker has died, start it again and retry
        once. Returns the WAV bytes and the reply."""
        with self._lock:
            fd, out = tempfile.mkstemp(prefix="tts-mlx-", suffix=".wav")
            os.close(fd)
            try:
                request = dict(request, out=out)
                try:
                    reply = self._ask(request)
                except _WorkerExited:
                    reply = self._ask(request)       # it died: one fresh start
                if not reply.get("ok"):
                    raise MLXError(reply.get("error") or "MLX generation failed")
                with open(out, "rb") as f:
                    return f.read(), reply
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        self._proc = None


worker = _Worker()


def generate(model_id, text, language, temperature, *, max_tokens=None, ref_audio=None,
             ref_text=None, voice=None, instruct=None):
    """One paragraph through MLX, with the same sampling settings the PyTorch
    path uses. Returns (wav_bytes, reply)."""
    return worker.generate({
        "model": model_id, "text": text,
        "lang_code": (language or "auto").lower(),
        "temperature": temperature, "top_p": 0.8, "top_k": 50, "repetition_penalty": 1.1,
        "max_tokens": max_tokens,
        "ref_audio": ref_audio, "ref_text": ref_text, "voice": voice, "instruct": instruct,
    })
