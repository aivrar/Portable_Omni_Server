"""MOSS-specific speech and sound-effect routes."""

from __future__ import annotations

import asyncio
import base64
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import (
    DEFAULT_INFER_TIMEOUT,
    INFER_MAX_TEXT_CHARS,
    MODEL_INFER_TIMEOUT,
    MOSS_SFX_MODEL_ID,
)
from omni_outputs import omni_output_headers, persist_omni_bytes
from state import worker_registry
from routers.workers import (
    _release_worker,
    _resolve_busy_worker,
    _retire_timed_out_worker,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class MossSfxRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    seconds: float = Field(default=10.0, ge=0.1, le=30.0)
    steps: int = Field(default=100, ge=1, le=200)
    cfg_scale: float = Field(default=4.0, ge=0.0, le=20.0)
    sigma_shift: float = Field(default=5.0, ge=0.0, le=20.0)
    seed: int = 0
    negative_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    autospawn: bool = False
    device: str | None = Field(default=None, pattern=r"^(cuda:[0-9]+|cpu)$")


@router.post("/api/moss/sfx")
async def moss_sfx(req: MossSfxRequest):
    worker, _job_id = await _resolve_busy_worker(
        MOSS_SFX_MODEL_ID, autospawn=req.autospawn, device=req.device,
    )
    timeout = MODEL_INFER_TIMEOUT.get(MOSS_SFX_MODEL_ID, DEFAULT_INFER_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/infer",
                json={
                    "text": req.prompt,
                    "model_params": {
                        "seconds": req.seconds,
                        "steps": req.steps,
                        "cfg_scale": req.cfg_scale,
                        "sigma_shift": req.sigma_shift,
                        "seed": req.seed,
                        "negative_prompt": req.negative_prompt or "",
                    },
                },
            )
            if resp.status_code != 200:
                if resp.status_code >= 500:
                    await _retire_timed_out_worker(worker.worker_id)
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Worker error: {resp.text[:500]}",
                )
            data = resp.json()
            audio_b64 = data.get("audio_base64") or data.get("audio")
            if not audio_b64:
                raise HTTPException(status_code=502, detail="Worker returned no audio")
            try:
                audio_bytes = base64.b64decode(audio_b64)
            except (ValueError, TypeError):
                raise HTTPException(status_code=502, detail="Worker returned invalid audio base64")
            persisted = persist_omni_bytes(
                audio_bytes, model=MOSS_SFX_MODEL_ID, kind="sfx", ext="wav",
            )
            return Response(
                content=audio_bytes,
                media_type="audio/wav",
                headers=omni_output_headers(persisted),
            )
    except httpx.TimeoutException:
        await _retire_timed_out_worker(worker.worker_id)
        raise HTTPException(status_code=504, detail="MOSS sound-effect generation timed out")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail="MOSS sound-effect worker died")
    except asyncio.CancelledError:
        import anyio
        with anyio.CancelScope(shield=True):
            await _retire_timed_out_worker(worker.worker_id)
        raise
    finally:
        _release_worker(worker.worker_id)
