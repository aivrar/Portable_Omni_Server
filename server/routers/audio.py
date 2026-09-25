"""TTS / STT endpoints + OpenAI-compat audio routes.

* ``POST /api/stt/{model}`` (multipart) and
  ``POST /v1/audio/transcriptions`` accept an audio file, base64 it, and
  forward to the worker's ``/infer`` endpoint with the ``audio`` field set.
  The worker's existing image+audio inference path returns the transcribed
  text under ``text``, which we surface unchanged.

* ``POST /api/tts/{model}`` (JSON) and ``POST /v1/audio/speech`` send a
  text prompt to the worker's ``/infer/tts`` endpoint. The worker must
  implement TTS for the model in question - for models that don't support
  audio output (e.g. AnyGPT) the worker returns 501 and the gateway
  surfaces that.

The gateway is mostly plumbing here; the heavy lifting belongs in
``omni_worker.py``. Models with native audio output (Qwen2.5-Omni,
MiniCPM-o 2.6) are wired in worker-side ``_INFER_TTS`` handlers.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import (
    DEFAULT_INFER_TIMEOUT,
    INFER_MAX_NEW_TOKENS,
    INFER_MAX_IMAGE_BASE64_CHARS,
    INFER_MAX_TEXT_CHARS,
    MODEL_INFER_TIMEOUT,
    MOSS_TTS_MODEL_ID,
    OMNI_MODEL_SETUP,
    OUTPUT_DIR,
)
from omni_outputs import persist_omni_bytes
from state import worker_registry
from routers.workers import (
    _release_worker,
    _resolve_busy_worker,
    _resolve_openai_model,
    _retire_timed_out_worker,
)

logger = logging.getLogger(__name__)

router = APIRouter()

import os as _os

try:
    _AUDIO_INPUT_MAX_BYTES = max(1 * 1024 * 1024,
                                  int(_os.environ.get("OMNI_AUDIO_INPUT_MAX_MB", "50")) * 1024 * 1024)
except (TypeError, ValueError):
    _AUDIO_INPUT_MAX_BYTES = 50 * 1024 * 1024
_AUDIO_INPUT_MAX_BYTES = min(_AUDIO_INPUT_MAX_BYTES, (INFER_MAX_IMAGE_BASE64_CHARS // 4) * 3)
_AUDIO_INPUT_MAX_MB = _AUDIO_INPUT_MAX_BYTES // (1024 * 1024)
try:
    _STT_DEFAULT_MAX_TOKENS = max(64, min(INFER_MAX_NEW_TOKENS,
                                           int(_os.environ.get("OMNI_STT_MAX_TOKENS", "1024"))))
except (TypeError, ValueError):
    _STT_DEFAULT_MAX_TOKENS = min(1024, INFER_MAX_NEW_TOKENS)

_TTS_OUTPUT_FORMATS = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "opus": "audio/opus",
}


def _atempo_filter(speed: float) -> str:
    """Build an FFmpeg atempo chain for the full public 0.25-4.0 range."""
    remaining = float(speed)
    factors: list[float] = []
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    if not factors or abs(remaining - 1.0) > 1e-9:
        factors.append(remaining)
    return ",".join(f"atempo={factor:.8g}" for factor in factors)


def _retime_moss_wav(audio_bytes: bytes, speed: float) -> bytes:
    """Apply MOSS' otherwise unsupported speed setting without changing pitch."""
    if abs(float(speed) - 1.0) <= 1e-9:
        return audio_bytes
    configured = _os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    ffmpeg = configured if configured and Path(configured).is_file() else shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to apply the MOSS-TTS speed setting")
    with tempfile.TemporaryDirectory(prefix="omni-moss-tts-speed-") as tmp:
        source = Path(tmp) / "source.wav"
        target = Path(tmp) / "retimed.wav"
        source.write_bytes(audio_bytes)
        result = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", str(source),
                "-filter:a", _atempo_filter(speed),
                str(target),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or not target.is_file():
            detail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise RuntimeError(f"FFmpeg could not apply MOSS-TTS speed: {detail}")
        return target.read_bytes()


def _convert_audio_format(data: bytes, source_format: str, target_format: str) -> bytes:
    configured = _os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip()
    ffmpeg = configured if configured and Path(configured).is_file() else shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for the requested audio format")
    with tempfile.TemporaryDirectory(prefix="omni-audio-convert-") as tmp:
        source, target = Path(tmp) / f"input.{source_format}", Path(tmp) / f"output.{target_format}"
        source.write_bytes(data)
        result = subprocess.run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        if result.returncode or not target.is_file():
            raise RuntimeError("Audio format conversion failed: " + result.stderr.decode("utf-8", errors="replace")[-500:])
        return target.read_bytes()


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------
class _STTHelpers:
    """Shared between the native and OpenAI-compat STT routes."""

    @staticmethod
    async def transcribe(model: str, audio_b64: str,
                          prompt: str = "Transcribe this audio.",
                          autospawn: bool = False) -> dict:
        worker, _job_id = await _resolve_busy_worker(model, autospawn=autospawn)
        timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{worker.port}/infer",
                    json={
                        "text": prompt,
                        "audio": audio_b64,
                        "max_new_tokens": _STT_DEFAULT_MAX_TOKENS,
                        "temperature": 0.0,
                    },
                )
                if resp.status_code != 200:
                    raise HTTPException(
                        status_code=resp.status_code,
                        detail=f"Worker error: {resp.text[:500]}",
                    )
                return resp.json()
        except httpx.TimeoutException:
            await _retire_timed_out_worker(worker.worker_id)
            raise HTTPException(status_code=504, detail="Transcription timed out")
        except httpx.HTTPError:
            worker_registry.mark_dead(worker.worker_id)
            raise HTTPException(status_code=502, detail="Worker died during transcription")
        except asyncio.CancelledError:
            import anyio
            with anyio.CancelScope(shield=True):
                await _retire_timed_out_worker(worker.worker_id)
            raise
        finally:
            _release_worker(worker.worker_id)


async def _read_audio_to_b64(file: UploadFile, *, model: str | None = None) -> str:
    raw = await file.read(_AUDIO_INPUT_MAX_BYTES + 1)
    if len(raw) > _AUDIO_INPUT_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"Audio file exceeds {_AUDIO_INPUT_MAX_MB} MiB cap")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio file")
    if model:
        # Best-effort archive of the input clip so it lands in Media.
        ext = "bin"
        fname = (file.filename or "").lower()
        for candidate in ("wav", "mp3", "flac", "ogg", "opus", "m4a", "webm"):
            if fname.endswith("." + candidate):
                ext = candidate
                break
        persist_omni_bytes(raw, model=model, kind="stt", ext=ext)
    return base64.b64encode(raw).decode("ascii")


@router.post("/api/stt/{model}")
async def stt_native(
    model: str,
    file: UploadFile = File(...),
    prompt: str = Form(default="Transcribe this audio."),
    autospawn: bool = Form(default=False),
):
    if model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    if len(prompt) > INFER_MAX_TEXT_CHARS:
        raise HTTPException(status_code=400, detail="prompt too long")
    audio_b64 = await _read_audio_to_b64(file, model=model)
    return await _STTHelpers.transcribe(model, audio_b64, prompt=prompt,
                                         autospawn=autospawn)


@router.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(...),
    model: str = Form(...),
    prompt: str = Form(default="Transcribe this audio."),
    response_format: str = Form(default="json"),
):
    """OpenAI Whisper-compat. ``response_format`` honored: ``json`` or ``text``."""
    if len(prompt) > INFER_MAX_TEXT_CHARS or response_format not in {"json", "text"}:
        raise HTTPException(422, "Use a bounded prompt and response_format json or text")
    native = _resolve_openai_model(model)
    audio_b64 = await _read_audio_to_b64(file, model=native)
    result = await _STTHelpers.transcribe(native, audio_b64, prompt=prompt)
    text = (result or {}).get("text", "")
    if response_format == "text":
        return Response(content=text, media_type="text/plain")
    return {"text": text}


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------
class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    voice: str | None = None
    response_format: str = Field(default="wav")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    autospawn: bool = False
    device: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^(cpu|cuda(?::\d+)?)$",
    )
    model_params: dict | None = None


class _OpenAISpeechRequest(BaseModel):
    model: str
    input: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    voice: str | None = None
    response_format: str = Field(default="mp3")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


async def _do_tts(*, model: str, text: str, voice: str | None,
                  response_format: str, speed: float,
                  autospawn: bool = False,
                  device: str | None = None,
                  model_params: dict | None = None) -> tuple[bytes, str, str, str | None]:
    if response_format not in _TTS_OUTPUT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported response_format: {response_format}. "
                   f"Use one of {sorted(_TTS_OUTPUT_FORMATS)}",
        )
    worker, _job_id = await _resolve_busy_worker(
        model, autospawn=autospawn, device=device,
    )
    timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/infer/tts",
                json={
                    "text": text,
                    "voice": voice,
                    "response_format": "wav" if model == MOSS_TTS_MODEL_ID else response_format,
                    "speed": speed,
                    "model_params": model_params or {},
                },
            )
            if resp.status_code == 501:
                raise HTTPException(status_code=501,
                                    detail=f"{model} does not support TTS output")
            if resp.status_code != 200:
                if resp.status_code >= 500:
                    # A worker-side inference failure can leave CUDA contexts
                    # or device descriptors poisoned (observed with MOSS-TTS
                    # exhausting dxgresource descriptors after a driver
                    # error). Never return such a worker to the ready pool.
                    await _retire_timed_out_worker(worker.worker_id)
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"Worker error: {resp.text[:500]}")
            data = resp.json()
            audio_b64 = data.get("audio_base64") or data.get("audio")
            if not audio_b64:
                raise HTTPException(status_code=502,
                                    detail="Worker returned no audio")
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except (ValueError, TypeError):
                raise HTTPException(status_code=502,
                                    detail="Worker returned invalid audio base64")
            actual_format = str(data.get("format") or response_format).lower()
            if actual_format not in _TTS_OUTPUT_FORMATS:
                actual_format = response_format
            if model == MOSS_TTS_MODEL_ID and abs(float(speed) - 1.0) > 1e-9:
                if actual_format != "wav":
                    raise HTTPException(
                        status_code=502,
                        detail="MOSS-TTS speed processing requires WAV worker output",
                    )
                try:
                    audio_bytes = await asyncio.to_thread(
                        _retime_moss_wav, audio_bytes, speed,
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
            if actual_format != response_format:
                try:
                    audio_bytes = await asyncio.to_thread(_convert_audio_format, audio_bytes, actual_format, response_format)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    raise HTTPException(500, str(exc)) from exc
                actual_format = response_format
            # Best-effort archive into OUTPUT_DIR/omni/tts/<model>/ so the
            # Media tab can list/replay/export the result. Failure to write
            # never blocks the response.
            persisted = persist_omni_bytes(
                audio_bytes,
                model=model,
                kind="tts",
                ext=actual_format,
            )
            output_rel = None
            if persisted is not None:
                try:
                    output_rel = persisted.relative_to(OUTPUT_DIR / "omni").as_posix()
                except ValueError:
                    output_rel = None
            return (
                audio_bytes,
                _TTS_OUTPUT_FORMATS[actual_format],
                actual_format,
                output_rel,
            )
    except httpx.TimeoutException:
        await _retire_timed_out_worker(worker.worker_id)
        raise HTTPException(status_code=504, detail="TTS timed out")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail="Worker died during TTS")
    except asyncio.CancelledError:
        import anyio
        with anyio.CancelScope(shield=True):
            await _retire_timed_out_worker(worker.worker_id)
        raise
    finally:
        _release_worker(worker.worker_id)


@router.post("/api/tts/{model}")
async def tts_native(model: str, req: TTSRequest):
    if model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    audio_bytes, media_type, _actual_format, output_rel = await _do_tts(
        model=model, text=req.text, voice=req.voice,
        response_format=req.response_format, speed=req.speed,
        autospawn=req.autospawn, device=req.device,
        model_params=req.model_params,
    )
    headers = {}
    if output_rel:
        headers = {
            "X-Omni-Output-Path": output_rel,
            "X-Omni-Output-Ref": f"omni://outputs/{output_rel}",
            "X-Omni-Output-URL": f"/api/outputs/{output_rel}?kind=omni",
        }
    return Response(content=audio_bytes, media_type=media_type, headers=headers)


@router.post("/v1/audio/speech")
async def openai_speech(req: _OpenAISpeechRequest):
    native = _resolve_openai_model(req.model)
    audio_bytes, media_type, _actual_format, output_rel = await _do_tts(
        model=native, text=req.input, voice=req.voice,
        response_format=req.response_format, speed=req.speed,
    )
    headers = {}
    if output_rel:
        headers = {
            "X-Omni-Output-Path": output_rel,
            "X-Omni-Output-Ref": f"omni://outputs/{output_rel}",
            "X-Omni-Output-URL": f"/api/outputs/{output_rel}?kind=omni",
        }
    return Response(content=audio_bytes, media_type=media_type, headers=headers)
