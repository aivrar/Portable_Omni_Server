"""ACE-Step gateway router — DiT song-generation orchestration.

Mirrors ``routers/audio_lab.py`` in structure but with two key differences:

1. **Cross-worker CLAP**: ``generate-ranked`` calls the ``audio_lab`` worker's
   ``/infer/audio_score`` for ranking. If audio_lab isn't running (or has no
   CLAP loaded), the gateway falls back to unranked output — files are
   returned in seed order, manifest records ``clap_available: false``.
2. **Wider per-mode surface**: 10 inference modes (T2M / A2A / Repaint /
   Edit / Extend / Cover / Vocal→BGM / Lyric→Vocal / Text→Samples / Analyze).

Worker dispatch: a single ``ace_step`` worker holds the DiT + LM + LoRA stack
+ optional VAE swap. Concurrent gateway requests are serialized by the
worker's internal ``state.lock`` plus the gateway's ``_INFLIGHT_LOCK``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import (
    ACE_STEP_EDIT_MODES,
    ACE_STEP_EXTEND_MODES,
    ACE_STEP_LMS,
    ACE_STEP_LORAS,
    ACE_STEP_MAX_BASE64_CHARS,
    ACE_STEP_MODEL_ID,
    ACE_STEP_MODELS,
    ACE_STEP_OUTPUT_KIND,
    ACE_STEP_SCHEDULERS,
    ACE_STEP_VAES,
    AUDIO_LAB_MODEL_ID,
    INFER_MAX_TEXT_CHARS,
    MODEL_INFER_TIMEOUT,
    CACHE_DIR,
    OUTPUT_DIR,
    ace_step_core_status,
    is_ace_step_lm_installed,
    is_ace_step_lora_installed,
    is_ace_step_model_installed,
)
from helpers import safe_child_path, safe_subtree_path
from state import worker_manager, worker_registry
from operation_gate import OperationGate
from omni_placement import ace_component_requirements

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_INFER_TIMEOUT = float(MODEL_INFER_TIMEOUT.get(ACE_STEP_MODEL_ID, 1800.0))
_AUTOSPAWN_BOOT_S = 300.0  # ACE-Step boot is slower than audio_lab (bigger checkpoints)
_SCORE_TIMEOUT_S = 120.0   # cross-worker CLAP call

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_ACE_STEP_OUTPUT_ROOT = OUTPUT_DIR / "omni" / ACE_STEP_OUTPUT_KIND

# Gateway-side locks. _SPAWN_LOCK serializes the autospawn critical section
# so two requests don't both call worker_manager.spawn_worker. _INFLIGHT_LOCK
# serializes /infer calls — the worker serializes via state.lock anyway, so
# parallel fan-out at the gateway is wasted thread-pool slots.
_OPERATION_GATE = OperationGate("ace_step")
_SPAWN_LOCK = asyncio.Lock()
_INFLIGHT_LOCK = asyncio.Lock()
_LOAD_LOCK = asyncio.Lock()

# In-memory progress slot (single, since _INFLIGHT_LOCK ensures one in-flight
# inference). UI polls /api/ace_step/progress.
_PROGRESS: dict = {
    "active": False, "mode": None, "stage": None,
    "current": 0, "total": 0, "label": "",
    "job_id": None, "started_at": None,
}


def _set_progress(**kw) -> None:
    _PROGRESS.update(kw)


def _clear_progress() -> None:
    _PROGRESS.update({"active": False, "mode": None, "stage": None,
                       "current": 0, "total": 0, "label": "",
                       "job_id": None, "started_at": None})


# ---------------------------------------------------------------------------
# Worker resolution
# ---------------------------------------------------------------------------
async def _wait_for_worker_ready(worker_id: str, timeout: float = _AUTOSPAWN_BOOT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        w = worker_registry.get(worker_id)
        if w is None or w.status == "dead":
            return False
        if w.status == "ready":
            return True
        await asyncio.sleep(1.5)
    return False


def _ace_worker_has_device(worker: object, device: str | None) -> bool:
    if not device:
        return True
    return getattr(worker, "device", None) == device


def _ace_split_placement(device: str, lm_device: str, variant: str | None = None,
                         precision: str = "bf16") -> dict:
    return worker_manager.analyze_placement(
        model=ACE_STEP_MODEL_ID,
        variant=variant,
        precision=precision,
        device=device,
        placement={
            "mode": "auto",
            "primary_device": device,
            "eligible_devices": [device, lm_device],
            "require_all": True,
        },
    )


async def _ensure_ace_step_worker(autospawn: bool = True,
                                  device: str | None = None,
                                  lm_device: str | None = None, allow_busy: bool = False,
                                  variant: str | None = None, precision: str = "bf16") -> object:
    """Return a ready ace_step worker, autospawning one if requested."""
    pending_id: str | None = None
    async with _SPAWN_LOCK:
        busy = [w for w in worker_registry.all_workers() if w.model == ACE_STEP_MODEL_ID and w.status == "busy" and _ace_worker_has_device(w, device)]
        if busy:
            if allow_busy:
                return busy[0]
            raise HTTPException(409, "ACE-Step worker is busy")
        ready = worker_registry.get_ready_workers(ACE_STEP_MODEL_ID)
        ready = [w for w in ready if _ace_worker_has_device(w, device)]
        if lm_device:
            ready = [w for w in ready if (not lm_device or lm_device == w.device or lm_device in list(getattr(w, "gpu_pool", None) or []))]
        if ready:
            return ready[0]
        starting = [
            w for w in worker_registry.all_workers()
            if w.model == ACE_STEP_MODEL_ID and w.status in ("starting", "loading")
            and _ace_worker_has_device(w, device)
            and (not lm_device or lm_device == w.device or lm_device in list(getattr(w, "gpu_pool", None) or []))
        ]
        if starting:
            pending_id = starting[0].worker_id
        elif autospawn:
            try:
                placement_plan = None
                if device and lm_device and lm_device != device:
                    placement_plan = _ace_split_placement(device, lm_device, variant, precision)
                    if not placement_plan.get("valid"):
                        raise RuntimeError(
                            "ACE-Step dual-GPU placement is invalid: "
                            + "; ".join(placement_plan.get("blockers") or ["unknown"])
                        )
                spawned = await worker_manager.spawn_worker(
                    model=ACE_STEP_MODEL_ID,
                    device=device,
                    placement_plan=placement_plan,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"ace_step spawn failed: {e}")
            pending_id = spawned.worker_id
        else:
            raise HTTPException(
                status_code=503,
                detail="No ACE-Step worker is running (pass autospawn=true or "
                       "POST /api/ace_step/load to start one)",
            )

    ok = await _wait_for_worker_ready(pending_id)
    if not ok:
        raise HTTPException(status_code=504,
                            detail="ACE-Step worker did not become ready in time")
    w = worker_registry.get(pending_id)
    if not w or w.status != "ready":
        raise HTTPException(status_code=504,
                            detail="ACE-Step worker died during startup")
    return w


def _worker_url(worker, path: str) -> str:
    return f"http://127.0.0.1:{worker.port}{path}"


async def _worker_post(client: httpx.AsyncClient, worker, path: str,
                       json_body: dict | None = None, timeout: float | None = None) -> dict:
    claimed = path.startswith("/infer/") or path.rsplit("/", 1)[-1] in {"load_model", "load_lm", "load_sa", "load_clap", "unload", "attach", "detach"}
    if claimed:
        if not worker_registry.claim_ready(worker.worker_id, f"ace:{uuid.uuid4().hex[:12]}"):
            raise HTTPException(409, "Selected worker is busy or unavailable")
    try:
        resp = await client.post(_worker_url(worker, path), json=json_body, timeout=timeout)
    except httpx.TimeoutException:
        worker_registry.mark_dead(worker.worker_id)
        try:
            await worker_manager.kill_worker(worker.worker_id)
        except Exception:
            logger.warning("Could not retire timed-out ACE-Step worker", exc_info=True)
        raise HTTPException(status_code=504, detail=f"Worker {path} timed out")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail=f"Worker died during {path}")
    except asyncio.CancelledError:
        worker_registry.mark_dead(worker.worker_id)
        import anyio
        with anyio.CancelScope(shield=True):
            await worker_manager.kill_worker(worker.worker_id)
        raise
    finally:
        current = worker_registry.get(worker.worker_id)
        if claimed and current and current.status == "busy":
            worker_registry.mark_ready(worker.worker_id)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text[:500]
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Worker {path} error: {detail}")
    return resp.json()


async def _worker_get(client: httpx.AsyncClient, worker, path: str) -> dict:
    try:
        resp = await client.get(_worker_url(worker, path))
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"Worker {path} timed out")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail=f"Worker died during {path}")
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code,
                            detail=f"Worker {path} error: {resp.text[:500]}")
    return resp.json()


async def _check_worker_cancel(client: httpx.AsyncClient, worker) -> bool:
    try:
        s = await _worker_get(client, worker, "/ace_step/state")
    except HTTPException:
        return False
    return bool(s.get("cancelled"))


async def _clear_worker_cancel(worker) -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(_worker_url(worker, "/ace_step/cancel/clear"))
    except Exception:
        pass  # best-effort; the inference will still surface cancel if it lingers


# ---------------------------------------------------------------------------
# Cross-worker CLAP — call out to the audio_lab worker for scoring
# ---------------------------------------------------------------------------
async def _try_get_audio_lab_clap_worker():
    """Return a ready audio_lab worker iff it exists AND has CLAP loaded.
    Returns None if either condition isn't met. Never spawns."""
    ready = worker_registry.get_ready_workers(AUDIO_LAB_MODEL_ID)
    if not ready:
        return None, None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for w in ready:
            try:
                state = await _worker_get(client, w, "/audio_lab/state")
            except HTTPException:
                continue
            clap_info = state.get("clap")
            if clap_info:
                return w, clap_info.get("variant_id") or clap_info.get("clap_variant") if isinstance(clap_info, dict) else None
    return None, None


async def _clap_score_one(client: httpx.AsyncClient, audio_lab_worker,
                           text: str, audio_b64: str, sample_rate: int,
                           expected_variant: str | None = None) -> float | None:
    """Score one wav against the prompt via the audio_lab worker. Returns
    None on failure (caller treats as 'unscored', sorts to bottom)."""
    from routers.audio_lab import _OPERATION_GATE as audio_gate

    @audio_gate
    async def score_current_variant():
        if expected_variant:
            state = await _worker_get(client, audio_lab_worker, "/audio_lab/state")
            if (state.get("clap") or {}).get("variant_id") != expected_variant:
                raise HTTPException(409, "CLAP variant changed during ranked generation")
        s = await _worker_post(
            client, audio_lab_worker, "/infer/audio_score",
            {"text": text, "audio_base64": audio_b64, "sample_rate": sample_rate},
            timeout=_SCORE_TIMEOUT_S,
        )
        score = s.get("score")
        if score is None:
            return None
        value = float(score)
        return value if math.isfinite(value) else None
    try:
        return await score_current_variant()
    except HTTPException as e:
        logger.warning("CLAP score failed (status=%s): %s", e.status_code, e.detail)
        return None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class _LoadRequest(BaseModel):
    model_variant: str | None = Field(default=None, max_length=128)
    lm_variant: str | None = Field(default=None, max_length=128)
    vae_variant: str | None = Field(default=None, max_length=128)
    bf16: bool = True
    cpu_offload: bool = False
    int8: bool = False
    torch_compile: bool = False
    device: str | None = Field(default=None, max_length=64)
    lm_device: str | None = Field(default=None, max_length=64)
    lm_backend: str | None = Field(default=None, pattern="^(pt|vllm)$")


class _UnloadRequest(BaseModel):
    component: str = Field(default="all", pattern="^(model|lm|vae|all)$")


class _GenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    lyrics: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 4)
    negative_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    duration_s: float = Field(default=60.0, ge=1.0, le=600.0)
    steps: int | None = Field(default=None, ge=2, le=200)
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    guidance_interval: list[float] | None = Field(default=None, min_length=2, max_length=2)
    scheduler: str = Field(default="euler", pattern="^(euler|heun|dpmpp)$")
    shift: float | None = Field(default=None, ge=1.0, le=5.0)
    infer_method: str = Field(default="ode", pattern="^(ode|sde)$")
    use_adg: bool = False
    dcw_enabled: bool | None = None
    thinking: bool | None = None
    use_cot_caption: bool = False
    use_cot_metas: bool = False
    use_cot_language: bool = False
    bpm: int | None = Field(default=None, ge=30, le=300)
    keyscale: str | None = Field(default=None, max_length=32)
    timesignature: str | None = Field(default=None, max_length=16)
    vocal_language: str | None = Field(default=None, max_length=16)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    output_name: str | None = Field(default=None, max_length=80)
    lm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    lm_cfg_scale: float | None = Field(default=None, ge=1.0, le=5.0)
    lm_top_k: int | None = Field(default=None, ge=0, le=200)
    lm_top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    use_constrained_decoding: bool | None = None
    use_cot_lyrics: bool = False
    dcw_mode: str | None = Field(default=None, max_length=16)
    dcw_scaler: float | None = Field(default=None, ge=0.0, le=1.0)
    dcw_high_scaler: float | None = Field(default=None, ge=0.0, le=1.0)
    dcw_wavelet: str | None = Field(default=None, max_length=16)
    enable_normalization: bool | None = None
    normalization_db: float | None = Field(default=None, ge=-12.0, le=0.0)
    fade_in_s: float | None = Field(default=None, ge=0.0, le=30.0)
    fade_out_s: float | None = Field(default=None, ge=0.0, le=30.0)
    audio_codes: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 8)
    timesteps: list[float] | None = Field(default=None, max_length=64)
    batch_size: int = Field(default=1, ge=1, le=8)
    reference_audio_base64: str | None = Field(default=None, max_length=ACE_STEP_MAX_BASE64_CHARS)
    reference_audio_url: str | None = Field(default=None, max_length=2048)
    reference_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    audio_cover_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    cover_noise_strength: float | None = Field(default=None, ge=0.0, le=1.0)
    velocity_norm_threshold: float | None = Field(default=None, ge=0.0, le=5.0)
    velocity_ema_factor: float | None = Field(default=None, ge=0.0, le=0.5)
    bf16: bool = True
    overlapped_decode: bool = True
    autospawn: bool = True


class _GenRankedRequest(_GenRequest):
    n: int = Field(default=4, ge=1, le=16)
    score_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    score_with_clap: bool = True  # set False to skip CLAP scoring even if available


class _InitAudioMixin(BaseModel):
    init_audio_base64: str | None = Field(default=None, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_audio_url: str | None = Field(default=None, max_length=2048)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)


class _A2ARequest(_GenRequest, _InitAudioMixin):
    init_noise_level: float = Field(default=0.6, ge=0.0, le=1.0)


class _RepaintRequest(_GenRequest, _InitAudioMixin):
    mask_start_s: float = Field(ge=0.0, le=600.0)
    mask_end_s: float = Field(ge=0.0, le=600.0)


class _EditRequest(_GenRequest, _InitAudioMixin):
    edit_mode: str = Field(pattern="^(only_lyrics|remix)$")
    source_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    source_lyrics: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 4)


class _ExtendRequest(_GenRequest, _InitAudioMixin):
    extend_mode: str = Field(pattern="^(prepend|append)$")
    extend_duration_s: float = Field(default=30.0, ge=1.0, le=300.0)


class _CoverRequest(_GenRequest, _InitAudioMixin):
    pass


class _ExtractRequest(_GenRequest, _InitAudioMixin):
    track_name: str = Field(min_length=2, max_length=32)


class _LegoRequest(_GenRequest, _InitAudioMixin):
    track_name: str = Field(min_length=2, max_length=32)


class _CompleteRequest(_GenRequest, _InitAudioMixin):
    track_names: list[str] = Field(min_length=1, max_length=12)


class _CreateSampleRequest(BaseModel):
    query: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    instrumental: bool = False
    vocal_language: str | None = Field(default=None, max_length=16)
    lm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    lm_top_k: int | None = Field(default=None, ge=0, le=200)
    lm_top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    autospawn: bool = True


class _FormatSampleRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    lyrics: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 4)
    bpm: int | None = Field(default=None, ge=30, le=300)
    duration_s: float | None = Field(default=None, ge=1.0, le=600.0)
    keyscale: str | None = Field(default=None, max_length=32)
    timesignature: str | None = Field(default=None, max_length=16)
    vocal_language: str | None = Field(default=None, max_length=16)
    lm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    autospawn: bool = True


class _UnderstandRequest(BaseModel):
    audio_codes: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS * 8)
    lm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    autospawn: bool = True


class _SimpleRequest(_GenRequest):
    prompt: str = Field(default="", max_length=INFER_MAX_TEXT_CHARS)
    query: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    instrumental: bool = False


class _Vocal2BGMRequest(_InitAudioMixin):
    prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    duration_s: float | None = Field(default=None, ge=1.0, le=600.0)
    steps: int | None = Field(default=None, ge=2, le=200)
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    scheduler: str = Field(default="euler", pattern="^(euler|heun|dpmpp)$")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    autospawn: bool = True


class _LoraAttachRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    multiplier: float = Field(default=1.0, ge=-2.0, le=2.0)
    adapter_file: str | None = Field(
        default=None,
        min_length=13,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.safetensors$",
    )


class _LoraDetachRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class _AnalyzeRequest(BaseModel):
    audio_base64: str | None = Field(default=None, max_length=ACE_STEP_MAX_BASE64_CHARS)
    audio_url: str | None = Field(default=None, max_length=2048)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    autospawn: bool = True


def _check_load_capacity(worker, req: _LoadRequest, current: dict,
                         lm_variant: str | None, load_model: bool, load_lm: bool) -> dict:
    """Check actual component destinations immediately before loading weights."""
    precision = "bf16" if req.bf16 else "fp32"
    try:
        sizes = ace_component_requirements(
            req.model_variant or current.get("model_variant"), lm_variant, precision,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    pool = list(getattr(worker, "gpu_pool", None) or [worker.device])
    lm_target = req.lm_device or (pool[1] if len(pool) > 1 else worker.device)
    if lm_target != "cpu" and lm_target not in pool:
        raise HTTPException(409, "Selected LM GPU is not visible to this ACE-Step worker")
    required = {}
    for active, target, amount in ((load_model, worker.device, sizes["dit"]),
                                   (load_lm, lm_target, sizes["lm"])):
        if active:
            required[target] = required.get(target, 0) + amount
    devices = {d["id"]: d for d in worker_manager.detect_devices(refresh=True)}
    memory = worker_manager.detect_app_memory_state()
    blockers, warnings = [], []
    owned = {w.process.pid for w in worker_registry.all_workers() if w.process}
    for target, amount in required.items():
        record = devices.get(target, {})
        available = (int(memory.get("available_worker_mb") or 0) if target == "cpu"
                     else max(0, int(record.get("vram_free_mb") or 0) - 1024))
        if amount > available:
            blockers.append(f"{target}: ACE-Step requires {amount} MB, current safe budget is {available} MB")
        foreign = sorted(set(record.get("compute_pids") or []) - owned)
        if foreign:
            warnings.append(f"{target} has non-Omni compute PIDs {foreign}; free memory can change")
    if load_model and req.cpu_offload and sizes["dit"] > int(memory.get("available_worker_mb") or 0):
        blockers.append("ACE-Step CPU offload exceeds the current safe host memory budget")
    result = {"valid": not blockers, "requirements_mb": required,
              "warnings": warnings, "blockers": blockers}
    if blockers:
        raise HTTPException(409, {"message": "ACE-Step component placement is invalid", "placement_plan": result})
    return result


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
@router.get("/api/ace_step/state")
async def ace_step_state(autospawn: bool = False):
    """Inspect the ACE-Step worker. autospawn=false makes this a cheap UI poll."""
    try:
        worker = await _ensure_ace_step_worker(autospawn=autospawn, allow_busy=True)
    except HTTPException as e:
        if e.status_code == 503 and not autospawn:
            return {"running": False}
        raise
    async with httpx.AsyncClient(timeout=10.0) as client:
        s = await _worker_get(client, worker, "/ace_step/state")
    # Augment with cross-worker CLAP availability so the UI can show whether
    # ranking is currently possible.
    al_worker, clap_variant = await _try_get_audio_lab_clap_worker()
    return {
        "running": True, "worker_id": worker.worker_id, "port": worker.port,
        "clap_available": al_worker is not None,
        "clap_via_worker": getattr(al_worker, "worker_id", None),
        "clap_variant": clap_variant,
        **s,
    }


@router.post("/api/ace_step/load")
@_OPERATION_GATE
async def ace_step_load(req: _LoadRequest):
    """Load model + optional LM + optional VAE. Pass only what you want to
    change; hot-swap is supported (worker frees prior before loading new)."""
    if not (req.model_variant or req.lm_variant or req.vae_variant):
        raise HTTPException(status_code=400,
                            detail="Provide at least one of model_variant / lm_variant / vae_variant")

    # Fail-fast registry / install checks (skip for 'custom:<name>' prefixes —
    # the worker introspects those at load time).
    resolved_lm_variant = req.lm_variant
    if req.model_variant and not req.model_variant.startswith("custom:"):
        if req.model_variant not in ACE_STEP_MODELS:
            raise HTTPException(status_code=400,
                                detail=f"Unknown ACE-Step model: {req.model_variant}")
        if not is_ace_step_model_installed(req.model_variant):
            raise HTTPException(status_code=409,
                                detail=f"Model '{req.model_variant}' is not installed yet.")
        model_info = ACE_STEP_MODELS[req.model_variant]
        if not resolved_lm_variant and model_info.get("default_lm"):
            resolved_lm_variant = model_info.get("default_lm")
        if model_info.get("format") == "native":
            core = ace_step_core_status()
            if not core.get("core_ready"):
                missing = ", ".join(core.get("core_missing") or [])
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "ACE-Step v1.5 shared core components are missing: "
                        f"{missing}. New native model installs include the "
                        "shared core automatically; for this partial install, "
                        "install the ACE-Step v1.5 Core Bundle (`ace-1.5`) "
                        "once from the ACE Step tab."
                    ),
                )
    if resolved_lm_variant and not resolved_lm_variant.startswith("custom:"):
        if resolved_lm_variant not in ACE_STEP_LMS:
            raise HTTPException(status_code=400, detail=f"Unknown LM: {resolved_lm_variant}")
        if not is_ace_step_lm_installed(resolved_lm_variant):
            raise HTTPException(status_code=409,
                                detail=f"LM '{resolved_lm_variant}' is not installed yet.")
    if req.vae_variant and req.vae_variant != "default":
        if req.vae_variant not in ACE_STEP_VAES:
            raise HTTPException(status_code=400, detail=f"Unknown VAE: {req.vae_variant}")

    worker = await _ensure_ace_step_worker(
        autospawn=True, device=req.device, lm_device=req.lm_device,
        variant=req.model_variant, precision="bf16" if req.bf16 else "fp32",
    )
    # A model load and its LM swap are one transaction. Without a gateway-wide
    # lock, a second timed-out/retried request can free the newly loaded DiT
    # between those two worker calls, forcing another multi-minute shard load
    # and making the first LM swap fail against an empty model state.
    async with _LOAD_LOCK:
        async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
            result: dict = {}
            current = await _worker_get(client, worker, "/ace_step/state")
            if req.vae_variant is not None and not req.model_variant:
                if not current.get("model_loaded") or not current.get("model_variant"):
                    raise HTTPException(409, "Load an ACE-Step model before changing its VAE")
                req = req.model_copy(update={
                    "model_variant": current["model_variant"],
                    **(current.get("model_load_kwargs") or {}),
                })
            requested_load_kwargs = {
                "bf16": req.bf16,
                "cpu_offload": req.cpu_offload,
                "int8": req.int8,
                "torch_compile": req.torch_compile,
            }
            requested_vae = None if req.vae_variant in (None, "default") else req.vae_variant
            model_matches = (
                bool(current.get("model_loaded"))
                and current.get("model_variant") == req.model_variant
                and current.get("model_load_kwargs") == requested_load_kwargs
                and current.get("vae_swap") == requested_vae
            )
            will_load_model = bool(req.model_variant and not model_matches)
            will_load_lm = bool(resolved_lm_variant and (
                will_load_model or not current.get("lm_loaded")
                or current.get("lm_variant") != resolved_lm_variant
                or (req.lm_device and current.get("lm_device") != dict(getattr(worker, "gpu_device_map", None) or {}).get(req.lm_device, req.lm_device))
                or (req.lm_backend and current.get("lm_backend") != req.lm_backend)
            ))
            if will_load_model or will_load_lm:
                result["placement_plan"] = _check_load_capacity(
                    worker, req, current, resolved_lm_variant, will_load_model, will_load_lm,
                )
            if req.model_variant and not model_matches:
                current = await _worker_post(
                    client, worker, "/ace_step/load_model",
                    {
                        "model_variant": req.model_variant,
                        "vae_variant": req.vae_variant,
                        **requested_load_kwargs,
                    },
                )
                result["model"] = current
            elif req.model_variant:
                result["model"] = {**current, "unchanged": True}

            lm_matches = (
                bool(current.get("lm_loaded"))
                and current.get("lm_variant") == resolved_lm_variant
                and (not req.lm_device or current.get("lm_device") == dict(getattr(worker, "gpu_device_map", None) or {}).get(req.lm_device, req.lm_device))
                and (not req.lm_backend or current.get("lm_backend") == req.lm_backend)
            )
            if resolved_lm_variant and not lm_matches:
                lm_body: dict = {"lm_variant": resolved_lm_variant}
                if req.lm_device:
                    mapped = dict(getattr(worker, "gpu_device_map", None) or {})
                    lm_body["lm_device"] = mapped.get(req.lm_device, req.lm_device)
                if req.lm_backend:
                    lm_body["backend"] = req.lm_backend
                current = await _worker_post(
                    client, worker, "/ace_step/load_lm",
                    lm_body,
                )
                result["lm"] = current
            elif resolved_lm_variant:
                result["lm"] = {**current, "unchanged": True}

            result["state"] = await _worker_get(client, worker, "/ace_step/state")
        return result


@router.post("/api/ace_step/unload")
@_OPERATION_GATE
async def ace_step_unload(req: _UnloadRequest):
    worker = await _ensure_ace_step_worker(autospawn=False)
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await _worker_post(client, worker, "/ace_step/unload",
                                   {"component": req.component})


@router.post("/api/ace_step/cancel")
async def ace_step_cancel_in_flight():
    try:
        worker = await _ensure_ace_step_worker(autospawn=False, allow_busy=True)
    except HTTPException as e:
        if e.status_code == 503:
            return {"cancelled": False, "reason": "no worker running"}
        raise
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _worker_post(client, worker, "/ace_step/cancel")


# ---------------------------------------------------------------------------
# LoRA management
# ---------------------------------------------------------------------------
@router.get("/api/ace_step/lora/list")
async def ace_step_lora_list():
    try:
        worker = await _ensure_ace_step_worker(autospawn=False, allow_busy=True)
    except HTTPException as e:
        if e.status_code == 503:
            return {"loras": [], "running": False}
        raise
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _worker_get(client, worker, "/ace_step/lora/list")


@router.post("/api/ace_step/lora/attach")
@_OPERATION_GATE
async def ace_step_lora_attach(req: _LoraAttachRequest):
    if not req.name.startswith("custom:"):
        info = ACE_STEP_LORAS.get(req.name)
        # An ad-hoc LoRA (not in registry) is OK as long as the dir exists,
        # which the worker checks. Don't fail-fast for those.
        if info and not is_ace_step_lora_installed(req.name):
            raise HTTPException(status_code=409,
                                detail=f"LoRA '{req.name}' is not installed.")
    worker = await _ensure_ace_step_worker(autospawn=True)
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _worker_post(client, worker, "/ace_step/lora/attach",
                                   {"name": req.name, "multiplier": req.multiplier,
                                    "adapter_file": req.adapter_file})


@router.post("/api/ace_step/lora/detach")
@_OPERATION_GATE
async def ace_step_lora_detach(req: _LoraDetachRequest):
    worker = await _ensure_ace_step_worker(autospawn=False)
    async with httpx.AsyncClient(timeout=30.0) as client:
        return await _worker_post(client, worker, "/ace_step/lora/detach",
                                   {"name": req.name})


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------
def _new_job_id() -> str:
    return uuid.uuid4().hex


def _new_job_dir(job_id: str) -> Path:
    job_dir = _ACE_STEP_OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _wav_url(job_id: str, filename: str) -> str:
    return f"/api/ace_step/outputs/{job_id}/{filename}"


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_str = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent),
    )
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_str, target)
    except OSError:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _atomic_write_json(target: Path, obj) -> None:
    _atomic_write_bytes(target, json.dumps(obj, indent=2).encode("utf-8"))


def _persist_candidate(target: Path, audio_b64: str) -> bytes:
    """Decode base64 audio and atomically write it to ``target``. Returns the
    decoded bytes so callers can reuse them (e.g. the ``best_`` copy) without
    decoding twice. CPU/IO-heavy — call via asyncio.to_thread off the loop."""
    audio_bytes = base64.b64decode(audio_b64)
    _atomic_write_bytes(target, audio_bytes)
    return audio_bytes


def _spool_ranked_candidate(job_dir: Path, candidate: dict, index: int) -> dict:
    data = dict(candidate)
    encoded = data.pop("audio_base64", "")
    data.pop("audios", None)
    if not encoded:
        raise ValueError("Worker returned no candidate audio")
    path = job_dir / f"candidate_{index:03d}.wav"
    _persist_candidate(path, encoded)
    data["_audio_path"] = str(path)
    return data


def _candidate_base64(candidate: dict) -> str:
    return base64.b64encode(Path(candidate["_audio_path"]).read_bytes()).decode("ascii")


def _finish_partial_manifest(job_dir: Path) -> None:
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") == "running":
        manifest["status"] = "incomplete"
        manifest["files"] = sorted(p.name for p in job_dir.glob("*.wav"))
        _atomic_write_json(manifest_path, manifest)


def _resolve_output_file(job_id: str, filename: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    if not _FILENAME_RE.fullmatch(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    job_dir = _ACE_STEP_OUTPUT_ROOT / job_id
    target = safe_child_path(job_dir, filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _resolve_init_audio(audio_base64: str | None, audio_url: str | None) -> str:
    """Resolve init_audio_base64 OR init_audio_url to base64. URLs must point
    at an ACE result or a guarded local /api/outputs library file. Empty
    strings are rejected so the worker doesn't receive a 0-byte payload and
    fail with a confusing pydantic min_length error."""
    if audio_base64 is not None and audio_base64.strip():
        return audio_base64
    if not audio_url or not audio_url.strip():
        raise HTTPException(status_code=400,
                            detail="Provide non-empty init_audio_base64 or init_audio_url")
    m = re.match(r"^/api/ace_step/outputs/([A-Za-z0-9_-]{6,64})/([A-Za-z0-9._-]{1,200})$",
                 audio_url)
    if m:
        job_id, filename = m.group(1), m.group(2)
        path = _resolve_output_file(job_id, filename)
    else:
        parsed = urlsplit(audio_url)
        prefix = "/api/outputs/"
        if parsed.scheme or parsed.netloc or not parsed.path.startswith(prefix):
            raise HTTPException(
                status_code=400,
                detail=(
                    "init_audio_url must be an ACE output URL or a local "
                    "/api/outputs/<path>?kind=<kind> URL"
                ),
            )
        query = parse_qs(parsed.query, keep_blank_values=True)
        kind = (query.get("kind") or ["output"])[0]
        roots = {
            "output": OUTPUT_DIR / "comfyui",
            "input": OUTPUT_DIR / "comfyui_input",
            "temp": CACHE_DIR / "comfyui_temp",
            "omni": OUTPUT_DIR / "omni",
        }
        root = roots.get(kind)
        if root is None:
            raise HTTPException(status_code=400, detail="Invalid outputs kind")
        relpath = unquote(parsed.path[len(prefix):])
        path = safe_subtree_path(root, relpath)
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="init_audio_url file not found")
        if path.suffix.lower() not in {".wav", ".mp3", ".flac", ".ogg", ".m4a"}:
            raise HTTPException(status_code=400, detail="init_audio_url is not an audio file")

    size = path.stat().st_size
    max_audio_bytes = (ACE_STEP_MAX_BASE64_CHARS * 3) // 4
    if size > max_audio_bytes:
        raise HTTPException(status_code=413, detail="init_audio_url file is too large")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _gen_payload(req) -> dict:
    """Strip gateway-only fields from a Pydantic request to pass to the worker."""
    payload = req.model_dump(exclude_none=True)
    payload.pop("autospawn", None)
    payload.pop("score_with_clap", None)
    payload.pop("score_prompt", None)
    payload.pop("n", None)
    payload.pop("init_audio_url", None)
    payload.pop("reference_audio_url", None)
    payload.pop("query", None)
    payload.pop("instrumental", None)
    return payload


# Manifest params that would bloat the JSON to 100+ MB — strip from the dump.
# The audio is on disk as a wav file; storing the base64 in manifest.json is
# pointless duplication and explodes disk usage for every job.
# Only the bulky base64 audio is stripped — small text fields like
# init_audio_url stay so the manifest preserves provenance.
_MANIFEST_PARAM_BLACKLIST = frozenset({
    "init_audio_base64", "audio_base64", "reference_audio_base64",
})


def _manifest_params(req) -> dict:
    """Pydantic dump with bulky audio fields stripped."""
    dump = req.model_dump(exclude_none=True)
    for k in list(dump.keys()):
        if k in _MANIFEST_PARAM_BLACKLIST:
            dump.pop(k, None)
    return dump


_SAFE_OUTPUT_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _result_filename(req, result: dict, mode: str) -> str:
    """Human-readable wav name for the media library tile."""
    explicit = str(getattr(req, "output_name", None) or "").strip()
    if explicit:
        stem = _SAFE_OUTPUT_NAME.sub("-", explicit).strip(".-_")[:80]
        return f"{stem or '01'}.wav"
    variant = str(result.get("model_variant") or mode or "ace").removeprefix("ace-")
    parts = [variant]
    duration = result.get("duration_s")
    if duration is not None:
        parts.append(f"{int(round(float(duration)))}s")
    seed = result.get("seed")
    if seed is not None:
        parts.append(f"s{seed}")
    return "-".join(parts) + ".wav"


def _save_single_result(audio_b64: str, mode: str, req, result: dict,
                         extra: dict | None = None) -> dict:
    """Persist a single-result inference and build a results-shaped manifest."""
    job_id = _new_job_id()
    job_dir = _new_job_dir(job_id)
    candidates = result.get("audios") or [{**result, "audio_base64": audio_b64}]
    entries = []
    for index, candidate in enumerate(candidates, 1):
        filename = _result_filename(req, candidate, mode)
        if len(candidates) > 1:
            filename = f"{index:02d}-{filename}"
        _persist_candidate(job_dir / filename, candidate["audio_base64"])
        entries.append({
            "rank": index, "filename": filename, "url": _wav_url(job_id, filename),
            "seed": candidate.get("seed"), "duration_s": candidate.get("duration_s"),
            "sample_rate": candidate.get("sample_rate"), "best": len(candidates) == 1,
            **(extra or {}),
        })
    result_entry = entries[0]
    manifest = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "model_variant": result.get("model_variant"),
        "lm_variant": result.get("lm_variant"),
        "vae_swap": result.get("vae_swap"),
        "loras": result.get("loras") or [],
        "params": _manifest_params(req),
        "clap_available": False,
        "results": entries,
    }
    _atomic_write_json(job_dir / "manifest.json", manifest)
    return {
        "job_id": job_id,
        "mode": mode,
        "results": entries,
        "url": result_entry["url"],
        **(extra or {}),
    }


# ---------------------------------------------------------------------------
# Single-mode generate
# ---------------------------------------------------------------------------
@router.post("/api/ace_step/generate")
@_OPERATION_GATE
async def ace_step_generate(req: _GenRequest):
    """Single T2M generation. Persists one wav, returns its URL + seed."""
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    _set_progress(active=True, mode="generate", stage="generate",
                   current=0, total=1, label="Generating",
                   job_id=None, started_at=time.time())
    try:
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/ace_step/state")
                if not state.get("model_loaded"):
                    raise HTTPException(
                        status_code=503,
                        detail="No ACE-Step model is loaded. POST /api/ace_step/load first.",
                    )
                payload = _gen_payload(req)
                if req.reference_audio_url and not payload.get("reference_audio_base64"):
                    payload["reference_audio_base64"] = await asyncio.to_thread(
                        _resolve_init_audio, None, req.reference_audio_url,
                    )
                result = await _worker_post(client, worker, "/infer/ace_generate", payload)
        audio_b64 = result.get("audio_base64") or ""
        if not audio_b64:
            raise HTTPException(status_code=502, detail="Worker returned no audio")
        return await asyncio.to_thread(
            _save_single_result, audio_b64, "generate", req, result,
            extra={"elapsed_s": result.get("elapsed_s")})
    finally:
        _clear_progress()


# ---------------------------------------------------------------------------
# Generate-ranked — fan-out + cross-worker CLAP + ranked persist
# ---------------------------------------------------------------------------
@router.post("/api/ace_step/generate-ranked")
@_OPERATION_GATE
async def ace_step_generate_ranked(req: _GenRankedRequest):
    """Fan out N candidates, optionally CLAP-rank them via the audio_lab worker,
    persist all with best_NN_<score>.wav on the winner. If audio_lab CLAP is
    not available, fall back to unranked (manifest records clap_available=false).
    """
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    audio_lab_worker, clap_variant = (None, None)
    if req.score_with_clap:
        audio_lab_worker, clap_variant = await _try_get_audio_lab_clap_worker()
    clap_available = audio_lab_worker is not None

    job_id = _new_job_id()
    job_dir = _new_job_dir(job_id)
    await asyncio.to_thread(_atomic_write_json, job_dir / "manifest.json", {"job_id": job_id, "mode": "generate-ranked", "status": "running", "results": []})
    score_prompt = (req.score_prompt or req.prompt).strip()
    candidates: list[dict] = []
    scores: list[float | None] = []
    failures: list[dict] = []
    cancelled = False

    _set_progress(active=True, mode="generate-ranked", stage="generate",
                   current=0, total=req.n,
                   label=f"Starting {req.n} candidates" + (
                       " (with CLAP rank)" if clap_available else " (unranked — no audio_lab CLAP)"
                   ),
                   job_id=job_id, started_at=time.time())

    try:
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/ace_step/state")
                if not state.get("model_loaded"):
                    raise HTTPException(
                        status_code=503,
                        detail="No ACE-Step model is loaded on the worker.",
                    )

                base_seed = req.seed
                gen_payload_base = _gen_payload(req)
                if req.reference_audio_url and not gen_payload_base.get("reference_audio_base64"):
                    gen_payload_base["reference_audio_base64"] = await asyncio.to_thread(
                        _resolve_init_audio, None, req.reference_audio_url,
                    )

                # Step 1: serial fan-out.
                for i in range(req.n):
                    _set_progress(active=True, mode="generate-ranked",
                                   stage="generate", current=i, total=req.n,
                                   label=f"Generating candidate {i+1}/{req.n}",
                                   job_id=job_id)
                    if await _check_worker_cancel(client, worker):
                        cancelled = True
                        break
                    payload = dict(gen_payload_base)
                    payload["seed"] = ((base_seed + i) & 0x7FFFFFFF) if base_seed is not None else None
                    payload["batch_size"] = 1
                    try:
                        g = await _worker_post(client, worker, "/infer/ace_generate", payload)
                        candidates.append(await asyncio.to_thread(_spool_ranked_candidate, job_dir, g, len(candidates) + 1))
                    except HTTPException as e:
                        failures.append({"candidate": i + 1, "stage": "generate",
                                          "status": e.status_code, "detail": str(e.detail)})
                        if e.status_code in (502, 504):
                            break

                # Step 2: cross-worker CLAP scoring (if available).
                if clap_available and candidates:
                    for i, c in enumerate(candidates):
                        _set_progress(active=True, mode="generate-ranked",
                                       stage="score", current=i, total=len(candidates),
                                       label=f"Scoring candidate {i+1}/{len(candidates)} via audio_lab CLAP",
                                       job_id=job_id)
                        if await _check_worker_cancel(client, worker):
                            cancelled = True
                            break
                        score = await _clap_score_one(
                            client, audio_lab_worker,
                            text=score_prompt,
                            audio_b64=await asyncio.to_thread(_candidate_base64, c),
                            sample_rate=c.get("sample_rate") or 48000,
                            expected_variant=clap_variant,
                        )
                        scores.append(score)
                        if score is None:
                            failures.append({"candidate": i + 1, "stage": "score",
                                              "reason": "clap_score_failed"})
                else:
                    # Unranked: scores all None — sort preserves generation order.
                    scores = [None] * len(candidates)

        if not candidates:
            if failures:
                raise HTTPException(
                    status_code=502,
                    detail=f"All candidates failed; first error: {failures[0].get('detail', failures[0])}",
                )
            if cancelled:
                raise HTTPException(status_code=499, detail="generation cancelled before any candidate completed")
            raise HTTPException(status_code=500, detail="no candidates produced")

        # Step 3: pair, sort, persist.
        while len(scores) < len(candidates):
            scores.append(None)
        # Defensive: if every score is None (audio_lab worker died / CLAP failed
        # mid-run), the manifest must reflect that — don't claim CLAP-ranked.
        any_score_succeeded = any(s is not None for s in scores)
        if clap_available and not any_score_succeeded:
            logger.warning(
                "generate-ranked job %s: clap_available was True at start but every "
                "score call failed. Falling back to unranked output.", job_id,
            )
            clap_available = False
        paired = [
            {
                "score": (None if scores[i] is None else float(scores[i])),
                "seed":  candidates[i].get("seed"),
                "_audio_path": candidates[i]["_audio_path"],
                "sample_rate": candidates[i].get("sample_rate"),
                "duration_s": candidates[i].get("duration_s"),
                "_orig_idx": i,
            }
            for i in range(len(candidates))
        ]
        if clap_available:
            # Sort by score desc; None sorts to the bottom.
            paired.sort(key=lambda p: (p["score"] is None, -p["score"] if p["score"] is not None else 0))
        # else: preserve generation order (already sorted by _orig_idx).

        results: list[dict] = []
        for rank, p in enumerate(paired, start=1):
            score_str = (f"{p['score']:.4f}" if p["score"] is not None else "unranked")
            base_name = f"{rank:02d}_{score_str}.wav"
            # Offload decode + atomic write off the event loop.
            await asyncio.to_thread(os.replace, p["_audio_path"], job_dir / base_name)
            result = {
                "rank": rank,
                "score": p["score"],
                "seed": p["seed"],
                "filename": base_name,
                "url": _wav_url(job_id, base_name),
                "duration_s": p["duration_s"],
                "best": False,
            }
            if clap_available and rank == 1:
                best_name = f"best_{base_name}"
                await asyncio.to_thread(shutil.copyfile, job_dir / base_name, job_dir / best_name)
                result["best"] = True
                result["best_filename"] = best_name
                result["best_url"] = _wav_url(job_id, best_name)
            results.append(result)

        manifest = {
            "job_id": job_id,
            "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "generate-ranked",
            "model_variant": (state.get("model_variant") if isinstance(state, dict) else None),
            "lm_variant": (state.get("lm_variant") if isinstance(state, dict) else None),
            "vae_swap": (state.get("vae_swap") if isinstance(state, dict) else None),
            "loras": (state.get("loras") if isinstance(state, dict) else []) or [],
            "prompt": req.prompt,
            "lyrics": req.lyrics,
            "negative_prompt": req.negative_prompt,
            "score_prompt": req.score_prompt or req.prompt,
            "clap_available": clap_available,
            "clap_variant": clap_variant if clap_available else None,
            "duration_s": req.duration_s,
            "steps": req.steps, "cfg_scale": req.cfg_scale,
            "guidance_interval": req.guidance_interval,
            "scheduler": req.scheduler,
            "n_requested": req.n,
            "n_completed": len(results),
            "cancelled": cancelled,
            "failures": failures,
            "sample_rate": (paired[0].get("sample_rate") if paired else None),
            "results": results,
        }
        await asyncio.to_thread(_atomic_write_json, job_dir / "manifest.json", manifest)
    finally:
        _clear_progress()
        await asyncio.to_thread(_finish_partial_manifest, job_dir)

    return {
        "job_id": job_id,
        "manifest_url": f"/api/ace_step/outputs/{job_id}",
        "results": results,
        "clap_available": clap_available,
        "cancelled": cancelled,
        "failures": failures,
        "n_completed": len(results),
        "n_requested": req.n,
    }


# ---------------------------------------------------------------------------
# Advanced modes — A2A / Repaint / Edit / Extend / Cover / Vocal→BGM /
# Lyric→Vocal / Text→Samples
# ---------------------------------------------------------------------------
async def _run_init_audio_mode(req, worker_path: str, mode_name: str,
                               extra: dict | None = None) -> dict:
    """Shared body for the 6 modes that take init_audio."""
    init_b64 = await asyncio.to_thread(
        _resolve_init_audio, getattr(req, "init_audio_base64", None),
        getattr(req, "init_audio_url", None))
    worker = await _ensure_ace_step_worker(autospawn=getattr(req, "autospawn", True))
    _set_progress(active=True, mode=mode_name, stage="generate",
                   current=0, total=1, label=f"Running {mode_name}",
                   started_at=time.time())
    try:
        payload = _gen_payload(req)
        payload["init_audio_base64"] = init_b64
        if getattr(req, "init_sample_rate", None):
            payload["init_sample_rate"] = req.init_sample_rate
        if getattr(req, "reference_audio_url", None) and not payload.get("reference_audio_base64"):
            payload["reference_audio_base64"] = await asyncio.to_thread(
                _resolve_init_audio, None, req.reference_audio_url,
            )
        if extra:
            payload.update(extra)
        async with _INFLIGHT_LOCK:
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/ace_step/state")
                if not state.get("model_loaded"):
                    raise HTTPException(
                        status_code=503,
                        detail="No ACE-Step model is loaded.",
                    )
                result = await _worker_post(client, worker, worker_path, payload)
        audio_b64 = result.get("audio_base64") or ""
        if not audio_b64:
            raise HTTPException(status_code=502, detail="Worker returned no audio")
        return await asyncio.to_thread(
            _save_single_result, audio_b64, mode_name, req, result,
            extra={"elapsed_s": result.get("elapsed_s")})
    finally:
        _clear_progress()


@router.post("/api/ace_step/a2a")
@_OPERATION_GATE
async def ace_step_a2a(req: _A2ARequest):
    return await _run_init_audio_mode(req, "/infer/ace_a2a", "a2a",
                                      extra={"init_noise_level": req.init_noise_level})


@router.post("/api/ace_step/repaint")
@_OPERATION_GATE
async def ace_step_repaint(req: _RepaintRequest):
    if req.mask_end_s <= req.mask_start_s:
        raise HTTPException(status_code=400,
                            detail="mask_end_s must be > mask_start_s")
    if req.mask_end_s > req.duration_s:
        raise HTTPException(status_code=400,
                            detail=f"mask_end_s ({req.mask_end_s}) exceeds duration_s ({req.duration_s})")
    return await _run_init_audio_mode(req, "/infer/ace_repaint", "repaint",
                                      extra={"mask_start_s": req.mask_start_s,
                                             "mask_end_s": req.mask_end_s})


@router.post("/api/ace_step/edit")
@_OPERATION_GATE
async def ace_step_edit(req: _EditRequest):
    return await _run_init_audio_mode(req, "/infer/ace_edit", "edit",
                                      extra={"edit_mode": req.edit_mode})


@router.post("/api/ace_step/extend")
@_OPERATION_GATE
async def ace_step_extend(req: _ExtendRequest):
    return await _run_init_audio_mode(req, "/infer/ace_extend", "extend",
                                      extra={"extend_mode": req.extend_mode,
                                             "extend_duration_s": req.extend_duration_s})


@router.post("/api/ace_step/cover")
@_OPERATION_GATE
async def ace_step_cover(req: _CoverRequest):
    return await _run_init_audio_mode(req, "/infer/ace_cover", "cover")


@router.post("/api/ace_step/vocal2bgm")
@_OPERATION_GATE
async def ace_step_vocal2bgm(req: _Vocal2BGMRequest):
    return await _run_init_audio_mode(req, "/infer/ace_vocal2bgm", "vocal2bgm")


@router.post("/api/ace_step/extract")
@_OPERATION_GATE
async def ace_step_extract(req: _ExtractRequest):
    return await _run_init_audio_mode(
        req, "/infer/ace_extract", "extract", extra={"track_name": req.track_name},
    )


@router.post("/api/ace_step/lego")
@_OPERATION_GATE
async def ace_step_lego(req: _LegoRequest):
    return await _run_init_audio_mode(
        req, "/infer/ace_lego", "lego", extra={"track_name": req.track_name},
    )


@router.post("/api/ace_step/complete")
@_OPERATION_GATE
async def ace_step_complete(req: _CompleteRequest):
    return await _run_init_audio_mode(
        req, "/infer/ace_complete", "complete", extra={"track_names": req.track_names},
    )


async def _run_lm_helper(req, worker_path: str, payload: dict) -> dict:
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    async with _INFLIGHT_LOCK:
        await _clear_worker_cancel(worker)
        async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
            state = await _worker_get(client, worker, "/ace_step/state")
            if not state.get("lm_loaded"):
                raise HTTPException(status_code=503, detail="No ACE-Step LM is loaded.")
            return await _worker_post(client, worker, worker_path, payload)


@router.post("/api/ace_step/create-sample")
@_OPERATION_GATE
async def ace_step_create_sample(req: _CreateSampleRequest):
    """Official Simple Mode planner: style query -> caption, lyrics, metadata."""
    return await _run_lm_helper(req, "/infer/ace_create_sample", {
        "query": req.query,
        "instrumental": req.instrumental,
        "vocal_language": req.vocal_language,
        "lm_temperature": req.lm_temperature,
        "lm_top_k": req.lm_top_k,
        "lm_top_p": req.lm_top_p,
    })


@router.post("/api/ace_step/format-sample")
@_OPERATION_GATE
async def ace_step_format_sample(req: _FormatSampleRequest):
    """Official Format: expand a caption/lyrics pair into structured metadata."""
    return await _run_lm_helper(req, "/infer/ace_format_sample", {
        "prompt": req.prompt,
        "lyrics": req.lyrics,
        "bpm": req.bpm,
        "duration_s": req.duration_s,
        "keyscale": req.keyscale,
        "timesignature": req.timesignature,
        "vocal_language": req.vocal_language,
        "lm_temperature": req.lm_temperature,
    })


@router.post("/api/ace_step/understand")
@_OPERATION_GATE
async def ace_step_understand(req: _UnderstandRequest):
    """Official understand_music: 5Hz codes -> caption/lyrics/metadata."""
    return await _run_lm_helper(req, "/infer/ace_understand", {
        "audio_codes": req.audio_codes,
        "lm_temperature": req.lm_temperature,
    })


@router.post("/api/ace_step/simple")
@_OPERATION_GATE
async def ace_step_simple(req: _SimpleRequest):
    """Official Simple Mode in one call: plan lyrics from a query, then generate."""
    sample = await ace_step_create_sample(_CreateSampleRequest(
        query=req.query,
        instrumental=req.instrumental,
        vocal_language=req.vocal_language,
        lm_temperature=req.lm_temperature,
        lm_top_k=req.lm_top_k,
        lm_top_p=req.lm_top_p,
        autospawn=req.autospawn,
    ))
    gen = req.model_copy(update={
        "prompt": sample.get("caption") or req.query,
        "lyrics": sample.get("lyrics") or ("[Instrumental]" if req.instrumental else req.lyrics),
        "bpm": req.bpm if req.bpm is not None else sample.get("bpm"),
        "duration_s": req.duration_s if req.duration_s != 60.0 or not sample.get("duration_s") else float(sample["duration_s"]),
        "keyscale": req.keyscale or sample.get("keyscale") or None,
        "timesignature": req.timesignature or sample.get("timesignature") or None,
        "vocal_language": req.vocal_language or sample.get("vocal_language") or None,
    })
    result = await ace_step_generate(gen)
    result["sample"] = sample
    return result


@router.post("/api/ace_step/lyric2vocal")
@_OPERATION_GATE
async def ace_step_lyric2vocal(req: _GenRequest):
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    _set_progress(active=True, mode="lyric2vocal", stage="generate",
                   current=0, total=1, label="Running lyric2vocal",
                   started_at=time.time())
    try:
        async with _INFLIGHT_LOCK:
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/ace_step/state")
                if not state.get("model_loaded"):
                    raise HTTPException(status_code=503,
                                        detail="No ACE-Step model is loaded.")
                result = await _worker_post(client, worker, "/infer/ace_lyric2vocal",
                                             _gen_payload(req))
        audio_b64 = result.get("audio_base64") or ""
        if not audio_b64:
            raise HTTPException(status_code=502, detail="Worker returned no audio")
        return await asyncio.to_thread(
            _save_single_result, audio_b64, "lyric2vocal", req, result,
            extra={"elapsed_s": result.get("elapsed_s")})
    finally:
        _clear_progress()


@router.post("/api/ace_step/text2samples")
@_OPERATION_GATE
async def ace_step_text2samples(req: _GenRequest):
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    _set_progress(active=True, mode="text2samples", stage="generate",
                   current=0, total=1, label="Running text2samples",
                   started_at=time.time())
    try:
        async with _INFLIGHT_LOCK:
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/ace_step/state")
                if not state.get("model_loaded"):
                    raise HTTPException(status_code=503,
                                        detail="No ACE-Step model is loaded.")
                result = await _worker_post(client, worker, "/infer/ace_text2samples",
                                             _gen_payload(req))
        audio_b64 = result.get("audio_base64") or ""
        if not audio_b64:
            raise HTTPException(status_code=502, detail="Worker returned no audio")
        return await asyncio.to_thread(
            _save_single_result, audio_b64, "text2samples", req, result,
            extra={"elapsed_s": result.get("elapsed_s")})
    finally:
        _clear_progress()


@router.post("/api/ace_step/analyze")
@_OPERATION_GATE
async def ace_step_analyze(req: _AnalyzeRequest):
    """BPM / key / loudness via librosa. Doesn't require model load."""
    if not req.audio_base64 and not req.audio_url:
        raise HTTPException(status_code=400, detail="Provide audio_base64 or audio_url")
    audio_b64 = req.audio_base64
    if not audio_b64:
        audio_b64 = await asyncio.to_thread(_resolve_init_audio, None, req.audio_url)
    worker = await _ensure_ace_step_worker(autospawn=req.autospawn)
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _worker_post(client, worker, "/infer/ace_analyze", {
            "audio_base64": audio_b64,
            "sample_rate": req.sample_rate,
        })


# ---------------------------------------------------------------------------
# Output gallery
# ---------------------------------------------------------------------------
@router.get("/api/ace_step/outputs/{job_id}")
async def ace_step_list_outputs(job_id: str):
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _ACE_STEP_OUTPUT_ROOT / job_id
    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job directory not found")

    manifest_path = job_dir / "manifest.json"
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None

    files = []
    for f in sorted(job_dir.iterdir()):
        if not f.is_file():
            continue
        files.append({
            "filename": f.name,
            "size_bytes": f.stat().st_size,
            "url": _wav_url(job_id, f.name) if f.suffix.lower() == ".wav" else None,
        })
    return {"job_id": job_id, "manifest": manifest, "files": files}


@router.get("/api/ace_step/outputs/{job_id}/{filename}")
async def ace_step_get_output(job_id: str, filename: str):
    target = _resolve_output_file(job_id, filename)
    ext_to_mime = {
        ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac",
        ".ogg": "audio/ogg", ".opus": "audio/opus",
        ".json": "application/json",
    }
    return FileResponse(
        path=str(target),
        media_type=ext_to_mime.get(target.suffix.lower(), "application/octet-stream"),
        filename=target.name,
    )


@router.get("/api/ace_step/jobs")
async def ace_step_list_jobs(mode: str | None = None,
                             model_variant: str | None = None,
                             limit: int = 100):
    """List ACE-Step inference jobs, newest-first. Optional filters by mode
    and model_variant."""
    if not _ACE_STEP_OUTPUT_ROOT.exists():
        return {"jobs": []}
    rows = []
    for entry in sorted(_ACE_STEP_OUTPUT_ROOT.iterdir(),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0,
                        reverse=True):
        if not entry.is_dir() or not _JOB_ID_RE.fullmatch(entry.name):
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if mode and manifest.get("mode") != mode:
            continue
        if model_variant and manifest.get("model_variant") != model_variant:
            continue
        results = manifest.get("results") or []
        best = next((r for r in results if r.get("best")), results[0] if results else None)
        rows.append({
            "job_id": entry.name,
            "mode": manifest.get("mode"),
            "created_at": manifest.get("created_at"),
            "model_variant": manifest.get("model_variant"),
            "lm_variant": manifest.get("lm_variant"),
            "vae_swap": manifest.get("vae_swap"),
            "loras": manifest.get("loras") or [],
            "prompt": (manifest.get("prompt")
                       or (manifest.get("params") or {}).get("prompt")),
            "clap_available": manifest.get("clap_available"),
            "n": manifest.get("n_completed") or len(results),
            "best_url": (_wav_url(entry.name, best["filename"])
                         if best and best.get("filename") else None),
            "best_score": (best.get("score") if best else None),
            "zip_url": f"/api/ace_step/zip/{entry.name}",
        })
        if len(rows) >= limit:
            break
    return {"jobs": rows}


@router.delete("/api/ace_step/jobs/{job_id}")
async def ace_step_delete_job(job_id: str):
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _ACE_STEP_OUTPUT_ROOT / job_id
    if not job_dir.exists():
        return {"status": "not_found", "job_id": job_id}
    shutil.rmtree(job_dir)
    return {"status": "deleted", "job_id": job_id}


@router.get("/api/ace_step/zip/{job_id}")
async def ace_step_zip_outputs(job_id: str):
    """Stream a ZIP of all files in a job directory."""
    import zipfile
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _ACE_STEP_OUTPUT_ROOT / job_id
    if not job_dir.exists() or not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Job directory not found")

    def _gen():
        with tempfile.TemporaryFile() as buf:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as zf:
                for file in sorted(job_dir.iterdir()):
                    if file.is_file() and not file.is_symlink():
                        zf.write(file, arcname=file.name)
            buf.seek(0)
            while chunk := buf.read(1024 * 1024):
                yield chunk

    return StreamingResponse(
        _gen(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.zip"'},
    )


@router.get("/api/ace_step/progress")
async def ace_step_progress():
    """Current in-flight inference progress (or {active: false} when idle)."""
    return dict(_PROGRESS)


@router.get("/api/ace_step/enums")
async def ace_step_enums():
    """Static UI helpers — sampler / edit_mode / extend_mode lists."""
    return {
        "schedulers": list(ACE_STEP_SCHEDULERS),
        "edit_modes": list(ACE_STEP_EDIT_MODES),
        "extend_modes": list(ACE_STEP_EXTEND_MODES),
    }
