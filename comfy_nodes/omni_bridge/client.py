"""Shared HTTP client + token discovery + media conversion helpers."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import wave
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = os.environ.get("OMNI_GATEWAY_URL", "http://127.0.0.1:8200")
DEFAULT_TOKEN_FILE = Path(
    os.environ.get("OMNI_TOKEN_FILE", "/opt/omni_studio/cache/runtime/api_token")
)
DEFAULT_TIMEOUT_S = float(os.environ.get("OMNI_GATEWAY_TIMEOUT_S", "300"))

_TOKEN_CACHE: str | None = None
_TOKEN_LOCK = threading.Lock()


def _read_token(*, force_refresh: bool = False) -> str:
    """Resolve the per-instance API token. Cached for the process lifetime
    unless ``force_refresh=True`` (used by the request layer when a 401/403
    suggests the cached token has gone stale across a gateway restart).

    Order of attempts:
      1. ``OMNI_API_TOKEN`` env (operator override).
      2. The runtime token file written by the gateway at boot.
      3. ``GET /api/session`` (loopback-exempt from auth).
    Raises ``RuntimeError`` if none works.
    """
    global _TOKEN_CACHE
    with _TOKEN_LOCK:
        if _TOKEN_CACHE and not force_refresh:
            return _TOKEN_CACHE
        env_tok = os.environ.get("OMNI_API_TOKEN", "").strip()
        if env_tok:
            _TOKEN_CACHE = env_tok
            return _TOKEN_CACHE
        try:
            if DEFAULT_TOKEN_FILE.exists():
                tok = DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
                if tok:
                    _TOKEN_CACHE = tok
                    return _TOKEN_CACHE
        except OSError as e:
            logger.warning("OmniBridge: token file read failed: %s", e)
        try:
            resp = requests.get(f"{DEFAULT_BASE_URL}/api/session", timeout=5)
            resp.raise_for_status()
            tok = (resp.json() or {}).get("token")
            if tok:
                _TOKEN_CACHE = tok
                return _TOKEN_CACHE
        except requests.RequestException as e:
            raise RuntimeError(
                f"OmniBridge: cannot reach Omni gateway at {DEFAULT_BASE_URL}/api/session: {e}"
            )
        raise RuntimeError("OmniBridge: gateway returned no token in /api/session response")


def _invalidate_token_cache() -> None:
    global _TOKEN_CACHE
    with _TOKEN_LOCK:
        _TOKEN_CACHE = None


def _headers(extra: dict | None = None, *, force_refresh: bool = False) -> dict:
    h = {"X-Omni-Token": _read_token(force_refresh=force_refresh),
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


# ---------------------------------------------------------------------------
# Image / audio conversion
# ---------------------------------------------------------------------------
def comfy_image_to_b64(image_tensor) -> str:
    """ComfyUI ``IMAGE`` tensor (NHWC, 0..1 float) → base64 PNG.

    ComfyUI's IMAGE type is ``torch.Tensor`` with shape ``[B, H, W, C]`` and
    values in [0, 1]. We render the first frame of the batch only.
    """
    try:
        from PIL import Image
        import numpy as np  # noqa: F401 - verifies Comfy's numpy bridge is importable
    except ImportError as e:
        raise RuntimeError(f"OmniBridge: PIL/numpy required for image conversion: {e}")
    arr = image_tensor[0].cpu().numpy() if hasattr(image_tensor, "cpu") else image_tensor[0]
    arr = (arr * 255.0).clip(0, 255).astype("uint8")
    img = Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def comfy_audio_to_b64_wav(audio_dict) -> str:
    """ComfyUI ``AUDIO`` dict → base64 WAV.

    ComfyUI represents audio as ``{"waveform": tensor [B, C, N], "sample_rate": int}``
    with float samples in [-1, 1]. We mix to mono and write 16-bit PCM WAV.
    """
    waveform = audio_dict.get("waveform")
    sr = int(audio_dict.get("sample_rate", 16000))
    if waveform is None:
        raise RuntimeError("OmniBridge: AUDIO input missing 'waveform'")
    try:
        import numpy as np  # noqa: F401 - verifies Comfy's numpy bridge is importable
    except ImportError as e:
        raise RuntimeError(f"OmniBridge: numpy required for audio conversion: {e}")
    arr = waveform[0].cpu().numpy() if hasattr(waveform, "cpu") else waveform[0]
    if arr.ndim > 1:
        arr = arr.mean(axis=0)  # mix to mono
    pcm = (arr * 32767.0).clip(-32768, 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def b64_wav_to_comfy_audio(b64_data: str) -> dict:
    """Base64-encoded WAV → ComfyUI AUDIO dict.

    The gateway returns whatever ``response_format`` we asked for; we ask
    for WAV so the parser is simple stdlib. If the bytes turn out to not
    be a WAV we raise — caller should handle the error in the workflow.
    """
    try:
        import numpy as np
        import torch
    except ImportError as e:
        raise RuntimeError(f"OmniBridge: numpy/torch required for audio output: {e}")
    raw = base64.b64decode(b64_data)
    with wave.open(io.BytesIO(raw), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(n)
    if sw != 2:
        raise RuntimeError(f"OmniBridge: expected 16-bit PCM WAV, got sampwidth={sw}")
    if n <= 0 or not frames:
        raise RuntimeError(
            f"OmniBridge: gateway returned an empty WAV (0 frames); "
            f"the TTS worker produced no audio."
        )
    arr = np.frombuffer(frames, dtype="<i2").astype("float32") / 32767.0
    if ch > 1:
        arr = arr.reshape(-1, ch).T
    else:
        arr = arr.reshape(1, -1)
    waveform = torch.from_numpy(arr).unsqueeze(0)  # [B=1, C, N]
    return {"waveform": waveform, "sample_rate": sr}


# ---------------------------------------------------------------------------
# Gateway calls
# ---------------------------------------------------------------------------
def _post_json(path: str, body: dict, *, timeout: float | None = None) -> dict:
    """POST JSON to the gateway. Auto-retries once on 401/403 with a
    refreshed token, in case the gateway restarted and our cache is stale."""
    url = f"{DEFAULT_BASE_URL}{path}"
    payload = json.dumps(body)
    eff_timeout = timeout or DEFAULT_TIMEOUT_S

    def _do(force_refresh: bool):
        try:
            return requests.post(url, headers=_headers(force_refresh=force_refresh),
                                 data=payload, timeout=eff_timeout)
        except requests.Timeout:
            raise RuntimeError(f"OmniBridge: {path} timed out after {eff_timeout}s")
        except requests.RequestException as e:
            raise RuntimeError(f"OmniBridge: {path} connection failed: {e}")

    resp = _do(force_refresh=False)
    if resp.status_code in (401, 403):
        _invalidate_token_cache()
        resp = _do(force_refresh=True)

    if resp.status_code == 504:
        raise RuntimeError(
            f"OmniBridge: gateway timed out spawning a worker for this model. "
            f"Spawn one in Omni Studio's Server tab, or wait and re-queue."
        )
    if resp.status_code == 503:
        raise RuntimeError(
            f"OmniBridge: no workers loaded for this model and autospawn was rejected. "
            f"Spawn one in Omni Studio's Server tab."
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"OmniBridge: gateway rejected our token even after refresh "
            f"(HTTP {resp.status_code}). Restart ComfyUI to clear the worker cache."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"OmniBridge: {path} returned {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except json.JSONDecodeError:
        raise RuntimeError(f"OmniBridge: {path} returned non-JSON body")


def chat_text(model: str, text: str, *, image_b64: str | None = None,
              audio_b64: str | None = None, max_new_tokens: int = 512,
              temperature: float = 0.7, top_p: float = 0.9) -> str:
    """Single-turn chat through the stateless ``/api/chat/{model}`` endpoint
    (with ?autospawn=true so worker comes up if needed). Returns the text.
    """
    body = {
        # ChatRequest.model is required by the route — even though the path
        # also names the model, Pydantic validates the body field. Omitting
        # it 422s.
        "model": model,
        "text": text,
        "max_new_tokens": int(max_new_tokens),
        "temperature": float(temperature),
        "top_p": float(top_p),
    }
    if image_b64: body["image"] = image_b64
    if audio_b64: body["audio"] = audio_b64
    data = _post_json(f"/api/chat/{model}?autospawn=true", body) or {}
    if "text" not in data:
        keys = ", ".join(sorted(data.keys())) or "<none>"
        raise RuntimeError(
            f"OmniBridge: chat response from '{model}' has no 'text' field "
            f"(response keys: {keys}). The gateway returned a 200 but the "
            f"result is not a normal chat completion."
        )
    return data["text"]


def stt_audio(model: str, audio_b64: str, *,
              prompt: str = "Transcribe this audio.") -> str:
    """STT via ``/api/chat/{model}`` (audio + transcribe prompt)."""
    return chat_text(model, prompt, audio_b64=audio_b64,
                     max_new_tokens=1024, temperature=0.0)


def tts_speak(model: str, text: str, *, voice: str | None = None,
              speed: float = 1.0) -> dict:
    """TTS via ``/api/tts/{model}``. Asks for WAV so the receiving node can
    wrap it as a ComfyUI AUDIO dict. Auto-retries once on 401/403 in case
    the gateway restarted and our token cache is stale."""
    body = {
        "text": text,
        "voice": voice,
        "response_format": "wav",
        "speed": float(speed),
        "autospawn": True,
    }
    url = f"{DEFAULT_BASE_URL}/api/tts/{model}"
    payload = json.dumps(body)

    def _do(force_refresh: bool):
        try:
            return requests.post(url, headers=_headers(force_refresh=force_refresh),
                                 data=payload, timeout=DEFAULT_TIMEOUT_S)
        except requests.Timeout:
            raise RuntimeError("OmniBridge: TTS timed out")
        except requests.RequestException as e:
            raise RuntimeError(f"OmniBridge: TTS connection failed: {e}")

    resp = _do(force_refresh=False)
    if resp.status_code in (401, 403):
        _invalidate_token_cache()
        resp = _do(force_refresh=True)
    if resp.status_code == 501:
        raise RuntimeError(
            f"OmniBridge: model '{model}' has no TTS handler wired in omni_worker.py. "
            f"Try a model with native audio output (Qwen-Omni / MiniCPM-o once handlers ship)."
        )
    if resp.status_code == 504:
        raise RuntimeError(
            f"OmniBridge: gateway timed out spawning a worker for this model. "
            f"Spawn one in Omni Studio's Server tab, or wait and re-queue."
        )
    if resp.status_code == 503:
        raise RuntimeError(
            f"OmniBridge: no workers loaded for this model and autospawn was rejected. "
            f"Spawn one in Omni Studio's Server tab."
        )
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"OmniBridge: gateway rejected our token even after refresh "
            f"(HTTP {resp.status_code}). Restart ComfyUI to clear the worker cache."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"OmniBridge: TTS returned {resp.status_code}: {resp.text[:300]}")
    return b64_wav_to_comfy_audio(base64.b64encode(resp.content).decode("ascii"))


# Models the nodes expose. Filtered to those that can plausibly serve the
# associated modality. Lives here so node files stay tiny.
TEXT_MODELS  = ["qwen_omni_3b", "qwen_omni_7b", "minicpm_o", "anygpt"]
VISION_MODELS = ["qwen_omni_3b", "qwen_omni_7b", "minicpm_o", "anygpt"]
AUDIO_IN_MODELS = ["qwen_omni_3b", "qwen_omni_7b", "minicpm_o"]
TTS_MODELS = ["qwen_omni_3b", "qwen_omni_7b", "minicpm_o"]
