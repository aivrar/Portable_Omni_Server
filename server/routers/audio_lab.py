"""Audio Lab gateway router — Stable Audio + CLAP orchestration.

Surface (Phase 2):

* ``GET  /api/audio_lab/state``           — worker introspection (which SA/CLAP loaded)
* ``POST /api/audio_lab/load``            — load SA + (optional) VAE + (optional) CLAP
* ``POST /api/audio_lab/unload``          — free a single component or all
* ``POST /api/audio_lab/generate``        — single text→audio
* ``POST /api/audio_lab/generate-ranked`` — fan out N candidates, CLAP-score, rank, persist
* ``POST /api/audio_lab/score``           — score one existing wav against a prompt
* ``GET  /api/audio_lab/outputs/{job_id}`` — list files + manifest for a job
* ``GET  /api/audio_lab/outputs/{job_id}/{filename}`` — fetch a single wav

Worker dispatch: a single ``audio_lab`` worker holds the live state (SA, CLAP,
optional VAE, future LoRA stack). Multiple concurrent gateway requests are
serialized by the worker's internal ``state.lock`` — the gateway does *not*
atomic-pick-and-mark-busy because audio_lab is fundamentally one consumer
sharing one GPU. Concurrency above the worker is fine; concurrency below the
worker would fight for VRAM.
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

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import (
    AUDIO_LAB_MAX_BASE64_CHARS,
    AUDIO_LAB_MODEL_ID,
    AUDIO_LAB_OUTPUT_KIND,
    AUDIO_LAB_RF_SAMPLERS,
    AUDIO_LAB_SAMPLERS,
    AUDIO_LAB_V_SAMPLERS,
    INFER_MAX_TEXT_CHARS,
    MODEL_INFER_TIMEOUT,
    OUTPUT_DIR,
    STABLE_AUDIO_MODELS,
    STABLE_AUDIO_VAES,
    CLAP_MODELS,
    is_audio_lab_variant_installed,
    is_audio_lab_vae_installed,
    is_clap_installed,
)
from helpers import safe_child_path
from state import worker_manager, worker_registry
from operation_gate import OperationGate

logger = logging.getLogger(__name__)

router = APIRouter()

# Long timeout — N=8 candidates at 100 steps can take several minutes on a
# mid-tier GPU. The worker timeout is the upper bound for one /infer call.
_DEFAULT_INFER_TIMEOUT = float(MODEL_INFER_TIMEOUT.get(AUDIO_LAB_MODEL_ID, 600.0))
_AUTOSPAWN_BOOT_S = 240.0   # how long to wait for the worker process to become "ready"

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_JOB_ID_RE   = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_AUDIO_LAB_OUTPUT_ROOT = OUTPUT_DIR / "omni" / AUDIO_LAB_OUTPUT_KIND

# Module-level locks. The spawn lock serializes the "check for ready worker /
# spawn one" critical section so two concurrent requests don't both call
# worker_manager.spawn_worker. The inflight lock serializes worker /infer
# calls from inside this router so a single GPU-bound worker isn't pummeled
# by gather() fan-out (the worker already serializes via state.lock, but
# fanning out at the gateway just wastes thread-pool slots).
_OPERATION_GATE = OperationGate("audio_lab")
_SPAWN_LOCK = asyncio.Lock()
_INFLIGHT_LOCK = asyncio.Lock()

# In-memory progress tracker for poll-based UI updates. There's at most one
# active audio_lab inference at a time (gated by _INFLIGHT_LOCK), so a single
# slot is sufficient. Reset between runs by _set_progress / _clear_progress.
_PROGRESS: dict = {
    "active": False,
    "mode": None,        # "generate" | "generate-ranked" | "a2a" | "inpaint" | "uncond" | "vae" | "score"
    "stage": None,       # "generate" | "score" | "encode" | "decode"
    "current": 0,
    "total": 0,
    "label": "",
    "job_id": None,
    "started_at": None,
}


def _set_progress(**kw) -> None:
    """Update the in-memory progress slot; fills missing keys with defaults."""
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


async def _ensure_audio_lab_worker(autospawn: bool = True,
                                   worker_id: str | None = None,
                                   device: str | None = None, allow_busy: bool = False) -> object:
    """Return a ready audio_lab worker, autospawning one if requested.

    The spawn/wait critical section is serialized via _SPAWN_LOCK so two
    concurrent callers don't both call worker_manager.spawn_worker. The
    lock is released while polling for "ready" so other handlers can also
    check the registry.
    """
    if worker_id:
        w = worker_registry.get(worker_id)
        if not w or w.model != AUDIO_LAB_MODEL_ID:
            raise HTTPException(status_code=404, detail="Audio Lab worker not found")
        if w.status == "ready" or (allow_busy and w.status == "busy"):
            return w
        if w.status in ("starting", "loading"):
            ok = await _wait_for_worker_ready(worker_id)
            if ok:
                w = worker_registry.get(worker_id)
                if w and w.status == "ready":
                    return w
            raise HTTPException(status_code=504,
                                detail="Selected Audio Lab worker did not become ready in time")
        if w.status == "busy":
            raise HTTPException(status_code=409, detail="Selected Audio Lab worker is busy")
        raise HTTPException(status_code=410, detail="Selected Audio Lab worker is not running")

    pending_id: str | None = None

    async with _SPAWN_LOCK:
        busy = [w for w in worker_registry.all_workers() if w.model == AUDIO_LAB_MODEL_ID and w.status == "busy" and (not device or w.device == device)]
        if busy:
            if allow_busy:
                return busy[0]
            raise HTTPException(409, "Audio Lab worker is busy")
        ready = worker_registry.get_ready_workers(AUDIO_LAB_MODEL_ID)
        if device:
            ready = [w for w in ready if w.device == device]
        if ready:
            return ready[0]
        starting = [
            w for w in worker_registry.all_workers()
            if w.model == AUDIO_LAB_MODEL_ID and w.status in ("starting", "loading")
            and (not device or w.device == device)
        ]
        if starting:
            pending_id = starting[0].worker_id
        elif autospawn:
            try:
                spawned = await worker_manager.spawn_worker(model=AUDIO_LAB_MODEL_ID, device=device)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"audio_lab spawn failed: {e}")
            pending_id = spawned.worker_id
        else:
            raise HTTPException(
                status_code=503,
                detail="No Audio Lab worker is running (pass autospawn=true or "
                       "POST /api/audio_lab/load to start one)",
            )

    # Lock released — poll for ready while other handlers also wait.
    ok = await _wait_for_worker_ready(pending_id)
    if not ok:
        raise HTTPException(status_code=504,
                            detail="Audio Lab worker did not become ready in time")
    w = worker_registry.get(pending_id)
    if not w or w.status != "ready":
        raise HTTPException(status_code=504,
                            detail="Audio Lab worker died during startup")
    return w


def _worker_url(worker, path: str) -> str:
    return f"http://127.0.0.1:{worker.port}{path}"


async def _worker_post(client: httpx.AsyncClient, worker, path: str,
                       json_body: dict | None = None, timeout: float | None = None) -> dict:
    claimed = path.startswith("/infer/") or path.rsplit("/", 1)[-1] in {"load_model", "load_lm", "load_sa", "load_clap", "unload", "attach", "detach"}
    if claimed:
        if not worker_registry.claim_ready(worker.worker_id, f"audio-lab:{uuid.uuid4().hex[:12]}"):
            raise HTTPException(409, "Selected worker is busy or unavailable")
    try:
        resp = await client.post(_worker_url(worker, path), json=json_body, timeout=timeout)
    except httpx.TimeoutException:
        worker_registry.mark_dead(worker.worker_id)
        try:
            await worker_manager.kill_worker(worker.worker_id)
        except Exception:
            logger.warning("Could not retire timed-out Audio Lab worker", exc_info=True)
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


async def _audio_lab_worker_record(client: httpx.AsyncClient, worker) -> dict:
    record = {
        "worker_id": worker.worker_id,
        "port": worker.port,
        "device": worker.device,
        "status": worker.status,
        "variant": worker.variant,
        "precision": worker.precision,
        "vram_used_mb": worker.vram_used_mb,
        "vram_total_mb": worker.vram_total_mb,
        "running": worker.status in ("starting", "loading", "ready", "busy"),
    }
    if worker.status == "ready":
        try:
            record["state"] = await _worker_get(client, worker, "/audio_lab/state")
        except HTTPException as e:
            record["state_error"] = e.detail
    return record


async def _check_worker_cancel(client: httpx.AsyncClient, worker) -> bool:
    """Read the worker's cancel flag (cheap GET). Returns True if cancelled."""
    try:
        s = await _worker_get(client, worker, "/audio_lab/state")
    except HTTPException:
        return False
    return bool(s.get("cancelled"))


async def _clear_worker_cancel(worker) -> None:
    """Best-effort clear of a stale cancel flag from a prior request.
    Called at the start of every gateway-initiated inference so a leftover
    flag from an earlier cancelled run doesn't abort the new one."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await _worker_post(client, worker, "/audio_lab/cancel/clear")
    except HTTPException:
        pass


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class _LoadRequest(BaseModel):
    sa_variant: str | None = Field(default=None, max_length=128)
    vae_variant: str | None = Field(default=None, max_length=128)
    clap_variant: str | None = Field(default=None, max_length=128)
    worker_id: str | None = Field(default=None, max_length=128)
    device: str | None = Field(default=None, max_length=64)


class _UnloadRequest(BaseModel):
    component: str = Field(default="all", pattern="^(sa|clap|vae|all)$")
    worker_id: str | None = Field(default=None, max_length=128)


class _GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    negative_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    duration_s: float = Field(default=10.0, ge=1.0, le=120.0)
    steps: int = Field(default=100, ge=1, le=500)
    cfg_scale: float = Field(default=7.0, ge=0.0, le=20.0)
    sigma_min: float | None = Field(default=None, ge=0.0)
    sigma_max: float | None = Field(default=None, ge=0.0)
    sampler: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    autospawn: bool = True
    worker_id: str | None = Field(default=None, max_length=128)


class _GenerateRankedRequest(_GenerateRequest):
    n: int = Field(default=4, ge=1, le=16)
    clap_variant: str | None = Field(default=None, max_length=128)
    score_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)


class _ScoreRequest(BaseModel):
    text: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    audio_base64: str | None = Field(default=None, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    audio_url: str | None = Field(default=None, max_length=2048)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    clap_variant: str | None = Field(default=None, max_length=128)
    autospawn: bool = True
    worker_id: str | None = Field(default=None, max_length=128)


def _is_custom_ref(value: str | None) -> bool:
    return bool(value and value.startswith("custom:"))


def _validate_sampling_payload(payload: dict) -> dict:
    sampler = payload.get("sampler")
    if sampler and sampler not in AUDIO_LAB_SAMPLERS:
        raise HTTPException(status_code=400, detail=f"Unknown Audio Lab sampler: {sampler}")
    sigma_min = payload.get("sigma_min")
    sigma_max = payload.get("sigma_max")
    if sigma_min is not None and sigma_max is not None and float(sigma_min) >= float(sigma_max):
        raise HTTPException(status_code=400, detail="sigma_min must be less than sigma_max")
    return payload


# ---------------------------------------------------------------------------
# Lifecycle endpoints
# ---------------------------------------------------------------------------
@router.get("/api/audio_lab/workers")
async def audio_lab_workers():
    workers = [
        w for w in worker_registry.all_workers()
        if w.model == AUDIO_LAB_MODEL_ID and w.status != "dead"
    ]
    async with httpx.AsyncClient(timeout=5.0) as client:
        records = [await _audio_lab_worker_record(client, w) for w in workers]
    return {"workers": records}


@router.get("/api/audio_lab/state")
async def audio_lab_state(autospawn: bool = False, worker_id: str | None = None):
    """Inspect the running audio_lab worker.

    By default this does NOT autospawn — useful as a low-cost UI poll. Pass
    autospawn=true to force a worker boot."""
    try:
        worker = await _ensure_audio_lab_worker(autospawn=autospawn, worker_id=worker_id, allow_busy=True)
    except HTTPException as e:
        if e.status_code == 503 and not autospawn:
            return {"running": False}
        raise
    async with httpx.AsyncClient(timeout=10.0) as client:
        state = await _worker_get(client, worker, "/audio_lab/state")
    return {"running": True, "worker_id": worker.worker_id, "port": worker.port, **state}


@router.post("/api/audio_lab/load")
@_OPERATION_GATE
async def audio_lab_load(req: _LoadRequest):
    """Load SA + (optional) VAE + (optional) CLAP onto the worker.

    All three are independent — pass only what you want to change. Hot-swaps
    are supported (worker frees the previous instance before loading the new
    one)."""
    if not (req.sa_variant or req.clap_variant or req.vae_variant):
        raise HTTPException(status_code=400,
                            detail="Provide at least one of sa_variant / clap_variant / vae_variant")

    # Pre-validate against the registries so we fail fast with a clear message.
    # Custom installs are referenced with the 'custom:<name>' prefix and bypass
    # the registry check; the worker validates them at load time by inspecting
    # the on-disk format sentinels (model_index.json vs model_config.json).
    if req.sa_variant and not _is_custom_ref(req.sa_variant):
        if req.sa_variant not in STABLE_AUDIO_MODELS:
            raise HTTPException(status_code=400,
                                detail=f"Unknown Stable Audio variant: {req.sa_variant}")
        if not is_audio_lab_variant_installed(req.sa_variant):
            raise HTTPException(status_code=409,
                                detail=f"Stable Audio variant '{req.sa_variant}' not installed yet. "
                                       f"Install it from the Audio Lab tab first.")
    if req.vae_variant and req.vae_variant != "default":
        if not _is_custom_ref(req.vae_variant) and req.vae_variant not in STABLE_AUDIO_VAES:
            raise HTTPException(status_code=400, detail=f"Unknown VAE: {req.vae_variant}")
        if not _is_custom_ref(req.vae_variant) and not is_audio_lab_vae_installed(req.vae_variant):
            raise HTTPException(status_code=409,
                                detail=f"VAE '{req.vae_variant}' not installed yet.")
    if req.clap_variant:
        if not _is_custom_ref(req.clap_variant) and req.clap_variant not in CLAP_MODELS:
            raise HTTPException(status_code=400, detail=f"Unknown CLAP: {req.clap_variant}")
        if not _is_custom_ref(req.clap_variant) and not is_clap_installed(req.clap_variant):
            raise HTTPException(status_code=409,
                                detail=f"CLAP '{req.clap_variant}' not installed yet.")

    worker = await _ensure_audio_lab_worker(autospawn=True, worker_id=req.worker_id, device=req.device)
    async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
        result: dict = {}
        sa_variant = req.sa_variant
        if req.vae_variant and not sa_variant:
            current = await _worker_get(client, worker, "/audio_lab/state")
            loaded_sa = current.get("sa") if isinstance(current, dict) else None
            if not loaded_sa or not loaded_sa.get("variant_id"):
                raise HTTPException(
                    status_code=409,
                    detail="Load a Stable Audio model before applying a VAE swap.",
                )
            sa_variant = loaded_sa["variant_id"]

        if sa_variant:
            result["sa"] = await _worker_post(
                client, worker, "/audio_lab/load_sa",
                {"sa_variant": sa_variant, "vae_variant": req.vae_variant},
            )
            worker.variant = sa_variant
        if req.clap_variant:
            result["clap"] = await _worker_post(
                client, worker, "/audio_lab/load_clap",
                {"clap_variant": req.clap_variant},
            )
        result["state"] = await _worker_get(client, worker, "/audio_lab/state")
    return result


@router.post("/api/audio_lab/unload")
@_OPERATION_GATE
async def audio_lab_unload(req: _UnloadRequest):
    worker = await _ensure_audio_lab_worker(autospawn=False, worker_id=req.worker_id)
    async with httpx.AsyncClient(timeout=30.0) as client:
        if req.component == "vae":
            state = await _worker_get(client, worker, "/audio_lab/state")
            sa = state.get("sa") if isinstance(state, dict) else None
            if not sa or not sa.get("variant_id"):
                return state
            if not sa.get("vae_swap"):
                return state
            reset = await _worker_post(
                client, worker, "/audio_lab/load_sa",
                {"sa_variant": sa["variant_id"], "vae_variant": "default"},
                timeout=_DEFAULT_INFER_TIMEOUT,
            )
            return {"vae_reset": reset, "state": await _worker_get(client, worker, "/audio_lab/state")}
        result = await _worker_post(client, worker, "/audio_lab/unload",
                                    {"component": req.component})
        if req.component in ("sa", "all"):
            worker.variant = None
        return result


@router.post("/api/audio_lab/cancel")
async def audio_lab_cancel_in_flight(worker_id: str | None = None):
    """Set the worker's cancel flag. Best-effort: mid-step diffusion can't
    be interrupted, but pending candidates in generate-ranked are skipped
    and CLAP scoring windows abort between windows."""
    try:
        worker = await _ensure_audio_lab_worker(autospawn=False, worker_id=worker_id, allow_busy=True)
    except HTTPException as e:
        if e.status_code == 503:
            return {"cancelled": False, "reason": "no worker running"}
        raise
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _worker_post(client, worker, "/audio_lab/cancel")


# ---------------------------------------------------------------------------
# Inference — single generate
# ---------------------------------------------------------------------------
def _new_job_id() -> str:
    # 32-char URL-safe id; collision-free for sane traffic.
    return uuid.uuid4().hex


def _new_job_dir(job_id: str) -> Path:
    job_dir = _AUDIO_LAB_OUTPUT_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def _gen_payload(req: _GenerateRequest) -> dict:
    payload = req.model_dump(exclude_none=True)
    payload.pop("autospawn", None)
    payload.pop("worker_id", None)
    return _validate_sampling_payload(payload)


def _wav_url(job_id: str, filename: str) -> str:
    return f"/api/audio_lab/outputs/{job_id}/{filename}"


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


def _safe_float(value, default=0.0):
    """Coerce a worker-returned score to float, degrading to ``default`` for
    missing/None/non-numeric values instead of raising (which would discard
    every completed candidate during ranking)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _persist_candidate(target: Path, audio_b64: str) -> bytes:
    """Decode base64 audio and atomically write it to ``target``. Returns the
    decoded bytes so callers can reuse them (e.g. the ``best_`` copy) without
    decoding twice. CPU/IO-heavy — call via asyncio.to_thread off the loop."""
    audio_bytes = base64.b64decode(audio_b64)
    _atomic_write_bytes(target, audio_bytes)
    return audio_bytes


@router.post("/api/audio_lab/generate")
@_OPERATION_GATE
async def audio_lab_generate(req: _GenerateRequest):
    """Single text→audio generation. Persists one wav, returns its URL."""
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    _set_progress(active=True, mode="generate", stage="generate",
                   current=0, total=1, label="Generating",
                   job_id=None, started_at=time.time())
    try:
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                # M6: pre-check SA loaded so the failure message is informative.
                state = await _worker_get(client, worker, "/audio_lab/state")
                if not state.get("sa"):
                    raise HTTPException(
                        status_code=503,
                        detail="No Stable Audio model is loaded. POST /api/audio_lab/load first.",
                    )
                result = await _worker_post(client, worker, "/infer/audio_gen", _gen_payload(req))

        audio_b64 = result.get("audio_base64") or ""
        if not audio_b64:
            raise HTTPException(status_code=502, detail="Worker returned no audio")
        return await asyncio.to_thread(_save_audio_result, audio_b64, "generate", req, extra={
            "seed": result.get("seed"),
            "duration_s": result.get("duration_s"),
            "sample_rate": result.get("sample_rate"),
            "score": None,
        })
    finally:
        _clear_progress()


# ---------------------------------------------------------------------------
# Inference — generate-ranked (the killer feature)
# ---------------------------------------------------------------------------
@router.post("/api/audio_lab/generate-ranked")
@_OPERATION_GATE
async def audio_lab_generate_ranked(req: _GenerateRankedRequest):
    """Fan out N candidates, CLAP-score each, rank, persist all with the
    winner double-prefixed ``best_NN_<score>.wav``. Returns the full manifest.

    Sequential fan-out (M5 fix): the GPU serializes anyway via the worker's
    state.lock, and concurrent HTTP fan-out just ties up FastAPI threadpool
    slots. Sequential is identical in wall-clock and cleaner to cancel.

    Cancel is checked between candidates and between scoring rounds. Partial
    results are persisted even if a later candidate fails.
    """
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)

    job_id = _new_job_id()
    job_dir = _new_job_dir(job_id)
    await asyncio.to_thread(_atomic_write_json, job_dir / "manifest.json", {"job_id": job_id, "mode": "generate-ranked", "status": "running", "results": []})
    score_prompt = (req.score_prompt or req.prompt).strip()
    candidates: list[dict] = []
    scores: list[dict] = []
    failures: list[dict] = []
    cancelled = False

    _set_progress(active=True, mode="generate-ranked", stage="generate",
                   current=0, total=req.n,
                   label=f"Starting {req.n} candidates",
                   job_id=job_id, started_at=time.time())

    try:
        # Step 1: confirm both SA and CLAP are loaded.
        async with _INFLIGHT_LOCK:
            # Clear any stale cancel flag inside the lock so a concurrent
            # request can't erase a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                state = await _worker_get(client, worker, "/audio_lab/state")
                if not state.get("sa"):
                    raise HTTPException(
                        status_code=503,
                        detail="No Stable Audio model is loaded on the worker. "
                               "Load one via POST /api/audio_lab/load {\"sa_variant\": ...} first.",
                    )
                if not state.get("clap"):
                    raise HTTPException(
                        status_code=503,
                        detail="No CLAP model is loaded on the worker. "
                               "Load one via POST /api/audio_lab/load {\"clap_variant\": ...} first.",
                    )

                if req.clap_variant and state["clap"].get("variant_id") != req.clap_variant:
                    raise HTTPException(409, "Requested CLAP variant is not loaded on the selected worker")

                gen_payload_base = _gen_payload(req)
                gen_payload_base.pop("n", None)
                gen_payload_base.pop("clap_variant", None)
                gen_payload_base.pop("score_prompt", None)
                gen_payload_base["num_waveforms_per_prompt"] = 1

                # Step 2: generate candidates sequentially. Stable Audio's
                # batched-waveform mode scales peak VAE decode memory with N;
                # a batch of four exhausts a 12 GB GPU even when one waveform
                # fits comfortably. Sequential calls keep peak memory bounded
                # and also let cancellation/failures preserve partial results.
                for i in range(req.n):
                    _set_progress(active=True, mode="generate-ranked",
                                   stage="generate", current=i, total=req.n,
                                   label=f"Generating candidate {i+1}/{req.n}",
                                   job_id=job_id)
                    cancel_state = await _check_worker_cancel(client, worker)
                    if cancel_state:
                        cancelled = True
                        break
                    payload = dict(gen_payload_base)
                    if req.seed is not None:
                        payload["seed"] = (req.seed + i) & 0x7FFFFFFF
                    try:
                        g = await _worker_post(client, worker, "/infer/audio_gen", payload)
                        batch = g.get("audios") if isinstance(g, dict) else None
                        if isinstance(batch, list):
                            for candidate in batch[:1]:
                                candidate.setdefault("candidate_index", i + 1)
                                candidates.append(await asyncio.to_thread(_spool_ranked_candidate, job_dir, candidate, len(candidates) + 1))
                        elif isinstance(g, dict) and g.get("audio_base64"):
                            g.setdefault("candidate_index", i + 1)
                            candidates.append(await asyncio.to_thread(_spool_ranked_candidate, job_dir, g, len(candidates) + 1))
                    except HTTPException as e:
                        failures.append({"candidate": i + 1, "stage": "generate",
                                          "status": e.status_code, "detail": str(e.detail)})

                # Step 3: serial CLAP scoring for each successful candidate.
                for i, c in enumerate(candidates):
                    _set_progress(active=True, mode="generate-ranked",
                                   stage="score", current=i, total=len(candidates),
                                   label=f"Scoring candidate {i+1}/{len(candidates)}",
                                   job_id=job_id)
                    cancel_state = await _check_worker_cancel(client, worker)
                    if cancel_state:
                        cancelled = True
                        break
                    try:
                        s = await _worker_post(client, worker, "/infer/audio_score", {
                            "text": score_prompt,
                            "audio_base64": await asyncio.to_thread(_candidate_base64, c),
                            "sample_rate": c.get("sample_rate") or 44100,
                        })
                        try:
                            valid_score = math.isfinite(float(s.get("score")))
                        except (TypeError, ValueError):
                            valid_score = False
                        if not valid_score:
                            raise HTTPException(502, "CLAP returned no finite score")
                        scores.append(s)
                    except HTTPException as e:
                        failures.append({"candidate": i + 1, "stage": "score",
                                          "status": e.status_code, "detail": str(e.detail)})
                        scores.append({"score": None, "_failed": True, "_error": str(e.detail)})

        if not candidates:
            # Nothing to persist — surface the first failure if there is one.
            if failures:
                raise HTTPException(
                    status_code=502,
                    detail=f"All candidates failed; first error: {failures[0]['detail']}",
                )
            if cancelled:
                raise HTTPException(status_code=499, detail="generation cancelled before any candidate completed")
            raise HTTPException(status_code=500, detail="no candidates produced")

        # Step 4: pair, sort by score desc, write to disk with rank-prefixed names.
        # We accept fewer scores than candidates (partial scoring failure) by
        # marking them unscored. Scored entries sort above unscored entries.
        while len(scores) < len(candidates):
            scores.append({"score": None, "_failed": True, "_error": "CLAP scoring did not return a result"})

        paired = [
            {"score": None if scores[i].get("_failed") else _safe_float(scores[i].get("score")),
             "seed":  candidates[i].get("seed"),
             "_audio_path": candidates[i]["_audio_path"],
             "sample_rate": candidates[i].get("sample_rate"),
             "duration_s": candidates[i].get("duration_s"),
             "waveform_index": candidates[i].get("waveform_index", i + 1),
             "candidate_index": candidates[i].get("candidate_index", i + 1),
             "_orig_idx": i,
             "_score_failed": bool(scores[i].get("_failed")),
             "_score_error": scores[i].get("_error")}
            for i in range(len(candidates))
        ]
        paired.sort(key=lambda p: p["score"] if p["score"] is not None else float("-inf"),
                    reverse=True)

        results: list[dict] = []
        for rank, p in enumerate(paired, start=1):
            score_str = f"{p['score']:.4f}" if p["score"] is not None else "unscored"
            base_name = f"{rank:02d}_{score_str}.wav"
            # Offload decode + atomic write off the event loop.
            await asyncio.to_thread(os.replace, p["_audio_path"], job_dir / base_name)
            result = {
                "rank": rank,
                "score": p["score"],
                "score_failed": p["_score_failed"],
                "score_error": p["_score_error"],
                "seed": p["seed"],
                "candidate_index": p["candidate_index"],
                "waveform_index": p["waveform_index"],
                "filename": base_name,
                "url": _wav_url(job_id, base_name),
                "duration_s": p["duration_s"],
                "best": False,
            }
            if rank == 1 and not p["_score_failed"]:
                best_name = f"best_{base_name}"
                await asyncio.to_thread(shutil.copyfile, job_dir / base_name, job_dir / best_name)
                result["best"] = True
                result["best_filename"] = best_name
                result["best_url"] = _wav_url(job_id, best_name)
            results.append(result)

        sa_info = state.get("sa", {}) if isinstance(state, dict) else {}
        clap_info = state.get("clap", {}) if isinstance(state, dict) else {}

        manifest = {
            "job_id": job_id,
            "status": "completed",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "generate-ranked",
            "sa_variant": sa_info.get("variant_id"),
            "vae_swap": sa_info.get("vae_swap"),
            "clap_variant": clap_info.get("variant_id"),
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt,
            "score_prompt": req.score_prompt or req.prompt,
            "duration_s": req.duration_s,
            "steps": req.steps, "cfg_scale": req.cfg_scale,
            "sigma_min": req.sigma_min, "sigma_max": req.sigma_max,
            "sampler": req.sampler,
            "seed": (candidates[0].get("seed") if candidates else req.seed),
            "requested_seed": req.seed,
            "seed_strategy": ("sequential_incrementing_seed" if req.seed is not None
                              else "sequential_worker_random_seeds"),
            "n_requested": req.n,
            "n_completed": len(results),
            "cancelled": cancelled,
            "scoring_failed": bool(results) and all(r.get("score_failed") for r in results),
            "failures": failures,
            "sample_rate": (paired[0].get("sample_rate") if paired else None),
            "results": results,
        }
        await asyncio.to_thread(_atomic_write_json, job_dir / "manifest.json", manifest)

        return {
            "job_id": job_id,
            "manifest_url": f"/api/audio_lab/outputs/{job_id}",
            "results": results,
            "cancelled": cancelled,
            "scoring_failed": bool(results) and all(r.get("score_failed") for r in results),
            "seed": (candidates[0].get("seed") if candidates else req.seed),
            "requested_seed": req.seed,
            "seed_strategy": ("sequential_incrementing_seed" if req.seed is not None
                              else "sequential_worker_random_seeds"),
            "failures": failures,
            "n_completed": len(results),
            "n_requested": req.n,
        }
    finally:
        _clear_progress()
        await asyncio.to_thread(_finish_partial_manifest, job_dir)


# ---------------------------------------------------------------------------
# Score one existing wav (by URL or base64)
# ---------------------------------------------------------------------------
@router.post("/api/audio_lab/score")
@_OPERATION_GATE
async def audio_lab_score(req: _ScoreRequest):
    if not req.audio_base64 and not req.audio_url:
        raise HTTPException(status_code=400, detail="Provide audio_base64 or audio_url")

    audio_b64 = req.audio_base64
    sample_rate = req.sample_rate

    if not audio_b64:
        # Resolve audio_url against our own /api/audio_lab/outputs path.
        m = re.match(r"^/api/audio_lab/outputs/([A-Za-z0-9_-]{6,64})/([A-Za-z0-9._-]{1,200})$",
                     req.audio_url or "")
        if not m:
            raise HTTPException(status_code=400,
                                detail="audio_url must be a /api/audio_lab/outputs/<job>/<file> path")
        job_id, filename = m.group(1), m.group(2)
        path = _resolve_output_file(job_id, filename)
        audio_b64 = base64.b64encode(path.read_bytes()).decode("ascii")

    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    _set_progress(active=True, mode="score", stage="score", current=0,
                   total=1, label="CLAP scoring", started_at=time.time())
    try:
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                if req.clap_variant:
                    current = await _worker_get(client, worker, "/audio_lab/state")
                    if (current.get("clap") or {}).get("variant_id") != req.clap_variant:
                        raise HTTPException(409, "Requested CLAP variant is not loaded on the selected worker")
                return await _worker_post(client, worker, "/infer/audio_score", {
                    "text": req.text,
                    "audio_base64": audio_b64,
                    "sample_rate": sample_rate,
                })
    finally:
        _clear_progress()


# ---------------------------------------------------------------------------
# Output gallery
# ---------------------------------------------------------------------------
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
    job_dir = _AUDIO_LAB_OUTPUT_ROOT / job_id
    target = safe_child_path(job_dir, filename)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


@router.get("/api/audio_lab/outputs/{job_id}")
async def audio_lab_list_outputs(job_id: str):
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _AUDIO_LAB_OUTPUT_ROOT / job_id
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


@router.get("/api/audio_lab/outputs/{job_id}/{filename}")
async def audio_lab_get_output(job_id: str, filename: str):
    target = _resolve_output_file(job_id, filename)
    # Map by extension; default to application/octet-stream.
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


# ---------------------------------------------------------------------------
# Phase 3 — A2A, Inpaint, Unconditional, VAE Lab
# ---------------------------------------------------------------------------
class _A2ARequest(_GenerateRequest):
    init_audio_base64: str | None = Field(default=None, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    init_audio_url: str | None = Field(default=None, max_length=2048)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    init_noise_level: float | None = Field(default=None, ge=0.0, le=1.0)


class _InpaintRequest(_GenerateRequest):
    init_audio_base64: str | None = Field(default=None, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    init_audio_url: str | None = Field(default=None, max_length=2048)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    mask_start_s: float = Field(ge=0.0, le=180.0)
    mask_end_s: float = Field(ge=0.0, le=180.0)


class _UncondRequest(BaseModel):
    duration_s: float = Field(default=10.0, ge=1.0, le=120.0)
    steps: int = Field(default=100, ge=1, le=500)
    sigma_min: float | None = Field(default=None, ge=0.0)
    sigma_max: float | None = Field(default=None, ge=0.0)
    sampler: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    autospawn: bool = True
    worker_id: str | None = Field(default=None, max_length=128)


class _VAEEncodeRequest(BaseModel):
    audio_base64: str | None = Field(default=None, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    audio_url: str | None = Field(default=None, max_length=2048)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    autospawn: bool = True
    worker_id: str | None = Field(default=None, max_length=128)


class _VAEDecodeRequest(BaseModel):
    latent_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    shape: list[int] | None = Field(default=None, min_length=3, max_length=3)
    autospawn: bool = True
    worker_id: str | None = Field(default=None, max_length=128)


def _resolve_init_audio(audio_base64: str | None, audio_url: str | None) -> str:
    """Return base64 audio data, fetching from /api/audio_lab/outputs/... if a
    URL was passed instead of inline data."""
    if audio_base64:
        return audio_base64
    if not audio_url:
        raise HTTPException(status_code=400,
                            detail="Provide audio_base64 or audio_url")
    m = re.match(r"^/api/audio_lab/outputs/([A-Za-z0-9_-]{6,64})/([A-Za-z0-9._-]{1,200})$",
                 audio_url)
    if not m:
        raise HTTPException(status_code=400,
                            detail="audio_url must be /api/audio_lab/outputs/<job>/<file>")
    job_id, filename = m.group(1), m.group(2)
    path = _resolve_output_file(job_id, filename)
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _save_audio_result(audio_b64: str, mode: str, request: BaseModel,
                        extra: dict | None = None) -> dict:
    """Persist a single-result inference output and build the manifest.
    Returns a shape compatible with the generate-ranked UI: ``results`` is
    always a list, even for single-output modes."""
    audio_bytes = base64.b64decode(audio_b64)
    job_id = _new_job_id()
    job_dir = _new_job_dir(job_id)
    filename = "01.wav"
    _atomic_write_bytes(job_dir / filename, audio_bytes)
    result_entry = {
        "rank": 1,
        "filename": filename,
        "url": _wav_url(job_id, filename),
        "best": True,
        **(extra or {}),
    }
    manifest = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "params": {key: value for key, value in request.model_dump(exclude_none=True).items() if not key.endswith("base64")},
        "results": [result_entry],
    }
    _atomic_write_json(job_dir / "manifest.json", manifest)
    return {
        "job_id": job_id,
        "mode": mode,
        "results": [result_entry],
        "url": result_entry["url"],   # keep top-level url for back-compat
        **(extra or {}),
    }


@router.post("/api/audio_lab/a2a")
@_OPERATION_GATE
async def audio_lab_a2a(req: _A2ARequest):
    """Audio-to-audio variation: feed init_audio + prompt → variation."""
    init_b64 = await asyncio.to_thread(
        _resolve_init_audio, req.init_audio_base64, req.init_audio_url)
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    _set_progress(active=True, mode="a2a", stage="generate", current=0,
                   total=1, label="Running A2A", started_at=time.time())
    try:
        payload = _gen_payload(req)
        payload["init_audio_base64"] = init_b64
        if req.init_sample_rate:
            payload["init_sample_rate"] = req.init_sample_rate
        payload["init_noise_level"] = req.init_noise_level
        payload.pop("init_audio_url", None)
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                result = await _worker_post(client, worker, "/infer/audio_a2a", payload)
        return await asyncio.to_thread(
            _save_audio_result,
            result["audio_base64"], "a2a", req,
            extra={"seed": result.get("seed"),
                   "init_noise_level": req.init_noise_level,
                   "sample_rate": result.get("sample_rate"),
                   "duration_s": result.get("duration_s")},
        )
    finally:
        _clear_progress()


@router.post("/api/audio_lab/inpaint")
@_OPERATION_GATE
async def audio_lab_inpaint(req: _InpaintRequest):
    """Inpaint a time range in an existing clip."""
    if req.mask_end_s <= req.mask_start_s:
        raise HTTPException(status_code=400,
                            detail="mask_end_s must be greater than mask_start_s")
    # L2: keep the mask inside the declared output duration. We don't know
    # the actual init audio length here without decoding it, so this is a
    # best-effort upper bound — the worker still clamps further if needed.
    if req.mask_end_s > req.duration_s:
        raise HTTPException(status_code=400,
                            detail=f"mask_end_s ({req.mask_end_s}) exceeds duration_s ({req.duration_s})")
    init_b64 = await asyncio.to_thread(
        _resolve_init_audio, req.init_audio_base64, req.init_audio_url)
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    _set_progress(active=True, mode="inpaint", stage="generate", current=0,
                   total=1, label="Running inpaint", started_at=time.time())
    try:
        payload = _gen_payload(req)
        payload["init_audio_base64"] = init_b64
        payload["mask_start_s"] = req.mask_start_s
        payload["mask_end_s"]   = req.mask_end_s
        if req.init_sample_rate:
            payload["init_sample_rate"] = req.init_sample_rate
        payload.pop("init_audio_url", None)
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                result = await _worker_post(client, worker, "/infer/audio_inpaint", payload)
        return await asyncio.to_thread(
            _save_audio_result,
            result["audio_base64"], "inpaint", req,
            extra={"seed": result.get("seed"),
                   "method": result.get("method"),
                   "mask_start_s": req.mask_start_s, "mask_end_s": req.mask_end_s,
                   "sample_rate": result.get("sample_rate"),
                   "duration_s": result.get("duration_s")},
        )
    finally:
        _clear_progress()


@router.post("/api/audio_lab/uncond")
@_OPERATION_GATE
async def audio_lab_uncond(req: _UncondRequest):
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    _set_progress(active=True, mode="uncond", stage="generate", current=0,
                   total=1, label="Unconditional generation", started_at=time.time())
    try:
        payload = req.model_dump(exclude_none=True)
        payload.pop("autospawn", None)
        payload.pop("worker_id", None)
        payload = _validate_sampling_payload(payload)
        async with _INFLIGHT_LOCK:
            # Cancel-clear inside the lock so a concurrent request can't erase
            # a flag set by the currently-running job.
            await _clear_worker_cancel(worker)
            async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
                result = await _worker_post(client, worker, "/infer/audio_uncond", payload)
        return await asyncio.to_thread(
            _save_audio_result,
            result["audio_base64"], "uncond", req,
            extra={"seed": result.get("seed"),
                   "sample_rate": result.get("sample_rate"),
                   "duration_s": result.get("duration_s")},
        )
    finally:
        _clear_progress()


@router.post("/api/audio_lab/vae/encode")
@_OPERATION_GATE
async def audio_lab_vae_encode(req: _VAEEncodeRequest):
    """Encode audio to a latent. Returns latent inline (base64-encoded .pt
    bytes) — not persisted to disk."""
    audio_b64 = await asyncio.to_thread(
        _resolve_init_audio, req.audio_base64, req.audio_url)
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    payload: dict = {"audio_base64": audio_b64}
    if req.sample_rate:
        payload["sample_rate"] = req.sample_rate
    async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
        return await _worker_post(client, worker, "/infer/vae_encode", payload)


@router.post("/api/audio_lab/vae/decode")
@_OPERATION_GATE
async def audio_lab_vae_decode(req: _VAEDecodeRequest):
    """Decode a latent back to a wav. Persists the result."""
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    payload = {"latent_base64": req.latent_base64}
    if req.shape:
        payload["shape"] = req.shape
    async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
        result = await _worker_post(client, worker, "/infer/vae_decode", payload)
    return await asyncio.to_thread(
        _save_audio_result,
        result["audio_base64"], "vae_decode", req,
        extra={"sample_rate": result.get("sample_rate"),
               "duration_s": result.get("duration_s")},
    )


@router.post("/api/audio_lab/vae/reconstruct")
@_OPERATION_GATE
async def audio_lab_vae_reconstruct(req: _VAEEncodeRequest):
    """Encode + decode in-process — quality sanity check for VAE swaps."""
    audio_b64 = await asyncio.to_thread(
        _resolve_init_audio, req.audio_base64, req.audio_url)
    worker = await _ensure_audio_lab_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    payload: dict = {"audio_base64": audio_b64}
    if req.sample_rate:
        payload["sample_rate"] = req.sample_rate
    async with httpx.AsyncClient(timeout=_DEFAULT_INFER_TIMEOUT) as client:
        result = await _worker_post(client, worker, "/infer/vae_reconstruct", payload)
    return await asyncio.to_thread(
        _save_audio_result,
        result["audio_base64"], "vae_reconstruct", req,
        extra={"diff_rms": result.get("diff_rms"),
               "sample_rate": result.get("sample_rate"),
               "duration_s": result.get("duration_s")},
    )


# ---------------------------------------------------------------------------
# Phase 5 polish — jobs listing, zip download
# ---------------------------------------------------------------------------
@router.get("/api/audio_lab/jobs")
async def audio_lab_list_jobs(mode: str | None = None, limit: int = 100):
    """List Audio Lab inference jobs (manifest.json under OUTPUT_DIR/omni/audio_lab/).

    Sorted newest-first by directory mtime. Optional `mode` filter narrows by
    manifest mode ("generate", "generate-ranked", "a2a", "inpaint", "uncond",
    "vae_decode", "vae_reconstruct"). `limit` caps the returned list."""
    if not _AUDIO_LAB_OUTPUT_ROOT.exists():
        return {"jobs": []}
    rows = []
    for entry in sorted(_AUDIO_LAB_OUTPUT_ROOT.iterdir(),
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
        results = manifest.get("results") or []
        best = next((r for r in results if r.get("best")), results[0] if results else None)
        rows.append({
            "job_id": entry.name,
            "mode": manifest.get("mode"),
            "created_at": manifest.get("created_at"),
            "sa_variant": manifest.get("sa_variant"),
            "vae_swap": manifest.get("vae_swap"),
            "clap_variant": manifest.get("clap_variant"),
            "prompt": manifest.get("prompt"),
            "n": manifest.get("n") or len(results),
            "best_url": (_wav_url(entry.name, best["filename"])
                         if best and best.get("filename") else None),
            "best_score": (best.get("score") if best else None),
            "zip_url": f"/api/audio_lab/zip/{entry.name}",
        })
        if len(rows) >= limit:
            break
    return {"jobs": rows}


@router.delete("/api/audio_lab/jobs/{job_id}")
async def audio_lab_delete_job(job_id: str):
    """Remove all files for a job_id from disk."""
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _AUDIO_LAB_OUTPUT_ROOT / job_id
    if not job_dir.exists():
        return {"status": "not_found", "job_id": job_id}
    import shutil
    shutil.rmtree(job_dir)
    return {"status": "deleted", "job_id": job_id}


@router.get("/api/audio_lab/zip/{job_id}")
async def audio_lab_zip_outputs(job_id: str):
    """Stream a ZIP of all files in a job directory."""
    import zipfile
    from fastapi.responses import StreamingResponse
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = _AUDIO_LAB_OUTPUT_ROOT / job_id
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


# ---------------------------------------------------------------------------
# Progress (poll endpoint for the UI during long-running inference)
# ---------------------------------------------------------------------------
@router.get("/api/audio_lab/progress")
async def audio_lab_progress():
    """Return the current inference progress (or {active: false} if idle).
    There is at most one in-flight Audio Lab inference, gated by _INFLIGHT_LOCK."""
    return dict(_PROGRESS)


# ---------------------------------------------------------------------------
# Static convenience — list of samplers, used by the UI dropdown
# ---------------------------------------------------------------------------
@router.get("/api/audio_lab/samplers")
async def audio_lab_samplers():
    response = {
        "samplers": list(AUDIO_LAB_SAMPLERS),
        "all_samplers": list(AUDIO_LAB_SAMPLERS),
        "families": {
            "v": list(AUDIO_LAB_V_SAMPLERS),
            "rectified_flow": list(AUDIO_LAB_RF_SAMPLERS),
            "rf_denoiser": list(AUDIO_LAB_RF_SAMPLERS),
        },
        "loaded_objective": None,
        "default": None,
    }
    try:
        worker = await _ensure_audio_lab_worker(autospawn=False)
        async with httpx.AsyncClient(timeout=10.0) as client:
            state = await _worker_get(client, worker, "/audio_lab/state")
        objective = ((state.get("sa") or {}).get("diffusion_objective")
                     if isinstance(state, dict) else None)
        valid = ((state.get("sa") or {}).get("valid_samplers")
                 if isinstance(state, dict) else None)
        if objective and valid:
            response["loaded_objective"] = objective
            response["samplers"] = list(valid)
            response["default"] = "euler" if objective in (
                "rectified_flow", "rf_denoiser"
            ) else "dpmpp-2m-sde"
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
    return response
