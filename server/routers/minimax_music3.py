"""Standalone MiniMax Music 3 lifecycle, generation, and output routes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from config import (
    MINIMAX_MUSIC3_MAX_DURATION_S,
    MINIMAX_MUSIC3_MAX_TEXT_CHARS,
    MINIMAX_MUSIC3_MAX_TOKENS,
    MINIMAX_MUSIC3_MODEL_ID,
    MINIMAX_MUSIC3_MODELS,
    MINIMAX_MUSIC3_OUTPUT_KIND,
    MODEL_INFER_TIMEOUT,
    OUTPUT_DIR,
    is_minimax_music3_model_installed,
    is_minimax_music3_runtime_installed,
)
from helpers import safe_child_path
from state import worker_manager, worker_registry
from operation_gate import OperationGate

router = APIRouter()

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_OUTPUT_ROOT = OUTPUT_DIR / "omni" / MINIMAX_MUSIC3_OUTPUT_KIND
_OPERATION_GATE = OperationGate("MiniMax Music 3")
_SPAWN_LOCK = asyncio.Lock()
_LOAD_LOCK = asyncio.Lock()
_INFLIGHT_LOCK = asyncio.Lock()
_BOOT_TIMEOUT_S = 180.0
_INFER_TIMEOUT_S = float(MODEL_INFER_TIMEOUT[MINIMAX_MUSIC3_MODEL_ID])


class _LoadRequest(BaseModel):
    worker_id: str | None = Field(default=None, max_length=128)
    model_variant: str = Field(default="official-diffusers", max_length=128)
    device: str | None = Field(default=None, max_length=64)
    bf16: bool = True
    cpu_offload: bool = True
    cpu_memory_mb: int = Field(default=16000, ge=0, le=1048576,
                              description="Host-memory admission estimate; not a per-worker runtime limit. The shared workload cgroup remains the runtime ceiling.")
    reserve_mb: int = Field(default=1024, ge=0, le=65536)


class _GenerateRequest(BaseModel):
    worker_id: str | None = Field(default=None, max_length=128)
    prompt: str = Field(min_length=1, max_length=MINIMAX_MUSIC3_MAX_TEXT_CHARS)
    lyrics: str = Field(min_length=1, max_length=MINIMAX_MUSIC3_MAX_TEXT_CHARS)
    duration_s: float = Field(default=60.0, ge=1.0, le=MINIMAX_MUSIC3_MAX_DURATION_S)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)
    autospawn: bool = True


async def _wait_ready(worker_id: str) -> object:
    deadline = time.monotonic() + _BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        worker = worker_registry.get(worker_id)
        if worker is None or worker.status == "dead":
            break
        if worker.status == "ready":
            return worker
        await asyncio.sleep(1.0)
    raise HTTPException(status_code=504, detail="MiniMax Music 3 worker did not become ready")


async def _ensure_worker(*, autospawn: bool, device: str | None = None,
                         placement_plan: dict | None = None, worker_id: str | None = None, allow_busy: bool = False) -> object:
    if worker_id:
        chosen = worker_registry.get(worker_id)
        if chosen is None or chosen.model != MINIMAX_MUSIC3_MODEL_ID or (device and chosen.device != device):
            raise HTTPException(404, "Selected MiniMax Music 3 worker not found")
        if chosen.status == "ready" or (allow_busy and chosen.status == "busy"):
            return chosen
        if chosen.status == "busy":
            raise HTTPException(409, "Selected MiniMax Music 3 worker is busy")
        if chosen.status == "dead":
            raise HTTPException(410, "Selected MiniMax Music 3 worker is dead")
        return await _wait_ready(worker_id)
    pending_id: str | None = None
    async with _SPAWN_LOCK:
        workers = [
            w for w in worker_registry.all_workers()
            if w.model == MINIMAX_MUSIC3_MODEL_ID and w.status in ("ready", "busy")
            and (not device or w.device == device)
        ]
        if len(workers) > 1:
            raise HTTPException(409, "Select worker_id when multiple MiniMax Music 3 workers are running")
        if workers:
            if workers[0].status == "busy" and not allow_busy:
                raise HTTPException(409, "MiniMax Music 3 worker is busy")
            return workers[0]
        starting = [
            w for w in worker_registry.all_workers()
            if w.model == MINIMAX_MUSIC3_MODEL_ID and w.status in ("starting", "loading")
            and (not device or w.device == device)
        ]
        if starting:
            pending_id = starting[0].worker_id
        elif autospawn:
            try:
                pending_id = (
                    await worker_manager.spawn_worker(
                        model=MINIMAX_MUSIC3_MODEL_ID,
                        device=device,
                        placement_plan=placement_plan,
                    )
                ).worker_id
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Music 3 spawn failed: {exc}")
        else:
            raise HTTPException(status_code=503, detail="No MiniMax Music 3 worker is running")
    return await _wait_ready(pending_id)


def _url(worker: object, path: str) -> str:
    return f"http://127.0.0.1:{worker.port}{path}"


async def _get(worker: object, path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(_url(worker, path))
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail="MiniMax Music 3 worker is unreachable")
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=response.text[:500])
    return response.json()


async def _post(worker: object, path: str, body: dict | None, timeout: float) -> dict:
    claimed = path.startswith("/infer/") or path.endswith(("/load_model", "/unload"))
    if claimed:
        if not worker_registry.claim_ready(worker.worker_id, f"music3:{uuid.uuid4().hex[:12]}"):
            raise HTTPException(409, "Selected worker is busy or unavailable")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(_url(worker, path), json=body)
    except httpx.TimeoutException:
        worker_registry.mark_dead(worker.worker_id)
        await worker_manager.kill_worker(worker.worker_id)
        raise HTTPException(status_code=504, detail="MiniMax Music 3 worker timed out and was retired")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502, detail="MiniMax Music 3 worker died")
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
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text[:500]
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


def _write_manifest(job_id: str, manifest: dict) -> None:
    job_dir = _OUTPUT_ROOT / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError("Worker did not create the managed output directory")
    target = job_dir / "manifest.json"
    temporary = job_dir / ".manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temporary, target)


@router.get("/api/minimax_music3/status")
async def minimax_music3_status():
    variants = []
    for variant_id, meta in MINIMAX_MUSIC3_MODELS.items():
        variants.append({
            "variant_id": variant_id,
            "display": meta["display"],
            "repo": meta["repo"],
            "revision": meta["revision"],
            "size_gb": meta["size_gb"],
            "installed": is_minimax_music3_model_installed(variant_id),
            "license": meta["license"],
        })
    return {
        "engine": MINIMAX_MUSIC3_MODEL_ID,
        "variants": variants,
        "max_duration_s": MINIMAX_MUSIC3_MAX_DURATION_S,
        "max_text_tokens": MINIMAX_MUSIC3_MAX_TOKENS,
        "native_sample_rate": 44100,
        "requires_lyrics": True,
        "runtime_ready": is_minimax_music3_runtime_installed(),
        "lora_runtime_supported": False,
    }


@router.get("/api/minimax_music3/state")
async def minimax_music3_state(autospawn: bool = False, worker_id: str | None = None):
    try:
        worker = await _ensure_worker(autospawn=autospawn, worker_id=worker_id, allow_busy=True)
    except HTTPException as exc:
        if exc.status_code == 503 and not autospawn:
            return {"running": False}
        raise
    return {"running": True, "worker_id": worker.worker_id, **await _get(worker, "/minimax_music3/state")}


@router.post("/api/minimax_music3/load")
@_OPERATION_GATE
async def minimax_music3_load(req: _LoadRequest):
    if req.model_variant not in MINIMAX_MUSIC3_MODELS:
        raise HTTPException(status_code=400, detail="Unknown MiniMax Music 3 variant")
    if not is_minimax_music3_model_installed(req.model_variant):
        raise HTTPException(status_code=409, detail="MiniMax Music 3 is not installed")
    if not is_minimax_music3_runtime_installed():
        raise HTTPException(
            status_code=409,
            detail="MiniMax Music 3 runtime override is not installed; retry the model install",
        )
    if not req.device or not req.device.startswith("cuda:"):
        raise HTTPException(status_code=400, detail="Select an explicit CUDA device")
    placement = {
        "mode": "single",
        "eligible_devices": [req.device],
        "primary_device": req.device,
        "reserve_mb": req.reserve_mb,
        "require_all": False,
        "allow_cpu": req.cpu_offload,
        "cpu_memory_mb": req.cpu_memory_mb if req.cpu_offload else 0,
    }
    try:
        placement_plan = worker_manager.analyze_placement(
            model=MINIMAX_MUSIC3_MODEL_ID,
            variant=req.model_variant,
            precision="bf16" if req.bf16 else "fp32",
            device=req.device,
            placement=placement,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not placement_plan.get("valid"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "MiniMax Music 3 placement is invalid",
                "placement_plan": placement_plan,
            },
        )
    if req.cpu_offload:
        placement_plan.setdefault("warnings", []).append(
            "cpu_memory_mb is an admission estimate, not an enforced per-worker offload limit; runtime uses the shared workload cgroup"
        )
    worker = await _ensure_worker(
        autospawn=True,
        device=req.device,
        worker_id=req.worker_id,
        placement_plan=placement_plan,
    )
    async with _LOAD_LOCK:
        state = await _post(worker, "/minimax_music3/load_model", {
            "model_variant": req.model_variant,
            "bf16": req.bf16,
            "cpu_offload": req.cpu_offload,
        }, _INFER_TIMEOUT_S)
    return {
        "running": True,
        "worker_id": worker.worker_id,
        "placement_plan": placement_plan,
        **state,
    }


@router.post("/api/minimax_music3/generate")
@_OPERATION_GATE
async def minimax_music3_generate(req: _GenerateRequest):
    if len(req.prompt) + len(req.lyrics) > MINIMAX_MUSIC3_MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Combined prompt and lyrics are too long for the model's "
                "5,000-token source limit"
            ),
        )
    worker = await _ensure_worker(autospawn=req.autospawn, worker_id=req.worker_id)
    state = await _get(worker, "/minimax_music3/state")
    if not state.get("model_loaded"):
        raise HTTPException(status_code=503, detail="Load MiniMax Music 3 before generation")
    job_id = uuid.uuid4().hex
    try:
        async with _INFLIGHT_LOCK:
            result = await _post(worker, "/infer/minimax_music3_generate", {
                "job_id": job_id,
                "prompt": req.prompt,
                "lyrics": req.lyrics,
                "duration_s": req.duration_s,
                "seed": req.seed,
            }, _INFER_TIMEOUT_S)
        manifest = {
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "engine": MINIMAX_MUSIC3_MODEL_ID,
            "model_variant": state.get("model_variant"),
            "prompt": req.prompt,
            "lyrics": req.lyrics,
            "requested_duration_s": req.duration_s,
            "duration_s": result["duration_s"],
            "seed": req.seed,
            "sample_rate": result["sample_rate"],
            "bit_depth": 24,
            "elapsed_s": result["elapsed_s"],
            "results": [{
                "filename": result["filename"],
                "url": f"/api/minimax_music3/outputs/{job_id}/{result['filename']}",
            }],
        }
        await asyncio.to_thread(_write_manifest, job_id, manifest)
    except Exception:
        partial = _OUTPUT_ROOT / job_id
        if partial.exists():
            await asyncio.to_thread(shutil.rmtree, partial, True)
        raise
    return {"job_id": job_id, "manifest": manifest, "results": manifest["results"]}


@router.post("/api/minimax_music3/unload")
@_OPERATION_GATE
async def minimax_music3_unload(worker_id: str | None = None):
    workers = [
        w for w in worker_registry.all_workers()
        if w.model == MINIMAX_MUSIC3_MODEL_ID and w.status != "dead" and (not worker_id or w.worker_id == worker_id)
    ]
    if len(workers) > 1:
        raise HTTPException(409, "Select worker_id when multiple MiniMax Music 3 workers are running")
    if not workers:
        return {"running": False, "unloaded": True}
    if _INFLIGHT_LOCK.locked():
        raise HTTPException(status_code=409, detail="Generation is active; cancel it first")
    state = await _post(workers[0], "/minimax_music3/unload", None, 120.0)
    return {"running": True, "unloaded": True, **state}


@router.post("/api/minimax_music3/cancel")
async def minimax_music3_cancel(worker_id: str | None = None):
    workers = [
        w for w in worker_registry.all_workers()
        if w.model == MINIMAX_MUSIC3_MODEL_ID and w.status == "busy" and (not worker_id or w.worker_id == worker_id)
    ]
    if len(workers) > 1:
        raise HTTPException(409, "Select worker_id when multiple MiniMax Music 3 workers are running")
    if not workers:
        return {"cancelled": False, "reason": "no active generation"}
    worker_id = workers[0].worker_id
    await worker_manager.kill_worker(worker_id)
    return {"cancelled": True, "worker_id": worker_id, "model_unloaded": True}


@router.get("/api/minimax_music3/jobs")
async def minimax_music3_jobs(limit: int = 100):
    rows = []
    if _OUTPUT_ROOT.exists():
        for entry in sorted(_OUTPUT_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not entry.is_dir() or not _JOB_ID_RE.fullmatch(entry.name):
                continue
            manifest_path = entry / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append(manifest)
            if len(rows) >= max(1, min(limit, 500)):
                break
    return {"jobs": rows}


@router.get("/api/minimax_music3/outputs/{job_id}/{filename}")
async def minimax_music3_output(job_id: str, filename: str):
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id")
    job_dir = safe_child_path(_OUTPUT_ROOT, job_id)
    target = safe_child_path(job_dir, filename)
    if not target.is_file() or target.suffix.lower() not in (".wav", ".json"):
        raise HTTPException(status_code=404, detail="Output not found")
    return FileResponse(
        str(target),
        media_type="audio/wav" if target.suffix.lower() == ".wav" else "application/json",
        filename=target.name,
    )
