"""Maintenance + system endpoints.

  * ``GET  /api/system/info``      -> versions, paths, uptime, PID
  * ``GET  /api/system/disk``      -> df for monitored roots
  * ``GET  /api/system/gpu``       -> nvidia-smi parsed
  * ``GET  /api/system/processes`` -> all managed PIDs (workers + comfy)
  * ``POST /api/system/refresh``   -> bust manager caches and re-scan
  * ``POST /api/system/restart``   -> graceful self-restart
  * ``POST /api/system/shutdown``  -> graceful self-shutdown

  * ``GET  /api/maintenance/policy``       -> persisted policy
  * ``PUT  /api/maintenance/policy``       -> overwrite the policy
  * ``GET  /api/maintenance/status``       -> last_run/next_run per task
  * ``POST /api/maintenance/run/{task}``   -> on-demand run, returns job_id
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import maintenance
from config import (
    APP_DIR,
    CACHE_DIR,
    MODELS_DIR,
    OUTPUT_DIR,
)
from jobs import DuplicateJobError
from scheduler import TICK_SECONDS
from state import comfy_registry, jobs, worker_registry

logger = logging.getLogger(__name__)

router = APIRouter()
_BOOT_TS = time.time()

_MONITORED_PATHS = [
    ("models", MODELS_DIR),
    ("output", OUTPUT_DIR),
    ("cache", CACHE_DIR),
    ("app", APP_DIR),
]


# ---------------------------------------------------------------------------
# /api/system/*
# ---------------------------------------------------------------------------
@router.get("/api/system/info")
async def system_info():
    info: dict = {
        "service": "omni_studio",
        "pid": os.getpid(),
        "uptime_seconds": int(time.time() - _BOOT_TS),
        "started_at": _BOOT_TS,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "app_dir": str(APP_DIR),
    }
    try:
        import torch
        info["torch"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
    except Exception as e:
        info["torch"] = f"not available: {e}"
        info["cuda_available"] = False
    try:
        import transformers
        info["transformers"] = transformers.__version__
    except Exception:
        info["transformers"] = None
    try:
        import fastapi
        info["fastapi"] = fastapi.__version__
    except Exception:
        info["fastapi"] = None
    info["workers"] = len(worker_registry.all_workers())
    info["comfy_instances"] = len(comfy_registry.all_instances())
    info["jobs_total"] = len(jobs._jobs)  # type: ignore[attr-defined]
    return info


@router.get("/api/system/disk")
async def system_disk():
    rows = []
    degraded = False
    for label, p in _MONITORED_PATHS:
        try:
            usage = shutil.disk_usage(str(p))
            free_pct = usage.free / max(usage.total, 1)
            row_degraded = free_pct < 0.10
            if row_degraded:
                degraded = True
            rows.append({
                "label": label,
                "path": str(p),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
                "free_pct": round(free_pct * 100, 2),
                "degraded": row_degraded,
            })
        except OSError as e:
            rows.append({"label": label, "path": str(p),
                         "error": repr(e)[:200]})
    return {"degraded": degraded, "paths": rows}


@router.get("/api/system/gpu")
async def system_gpu():
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,memory.free,"
             "utilization.gpu,temperature.gpu,power.draw,power.limit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return {"available": False, "error": repr(e)[:200], "gpus": []}
    if result.returncode != 0:
        return {"available": False, "error": result.stderr[:500], "gpus": []}

    gpus = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        try:
            gpu = {
                "index": int(parts[0]),
                "name": parts[1],
                "vram_total_mb": int(float(parts[2])),
                "vram_used_mb": int(float(parts[3])),
                "vram_free_mb": int(float(parts[4])),
                "util_pct": int(float(parts[5])),
                "temp_c": int(float(parts[6])),
                "power_w": float(parts[7]) if parts[7] != "[N/A]" else None,
                "power_limit_w": float(parts[8]) if parts[8] != "[N/A]" else None,
            }
            device_id = f"cuda:{gpu['index']}"
            gpu["workers"] = [
                w.worker_id for w in worker_registry.all_workers()
                if w.device == device_id
            ]
            gpu["comfy_instances"] = [
                i.instance_id for i in comfy_registry.all_instances()
                if i.device == device_id
            ]
            gpus.append(gpu)
        except (ValueError, KeyError):
            continue
    return {"available": True, "gpus": gpus}


@router.get("/api/system/processes")
async def system_processes():
    procs = []
    for w in worker_registry.all_workers():
        if w.process and w.process.poll() is None:
            procs.append({
                "kind": "worker", "id": w.worker_id, "model": w.model,
                "device": w.device, "port": w.port, "pid": w.process.pid,
                "status": w.status,
            })
    for i in comfy_registry.all_instances():
        if i.process and i.process.poll() is None:
            procs.append({
                "kind": "comfy", "id": i.instance_id,
                "device": i.device, "port": i.port, "pid": i.process.pid,
                "status": i.status,
            })
    for j in jobs.list():
        if j.process and j.process.returncode is None:
            procs.append({
                "kind": "job", "id": j.job_id, "job_kind": j.kind,
                "pid": j.process.pid, "status": j.status,
            })
    return {"processes": procs, "count": len(procs)}


@router.post("/api/system/refresh")
async def system_refresh():
    """Bust caches and re-scan filesystem-derived state."""
    from state import worker_manager, comfy_manager
    # Clear the device cache so next /api/devices re-runs nvidia-smi.
    worker_manager._device_cache = None  # type: ignore[attr-defined]
    worker_manager._device_cache_time = 0.0  # type: ignore[attr-defined]
    comfy_models = comfy_manager.get_installed_models()
    comfy_nodes = comfy_manager.get_custom_nodes()
    return {
        "status": "refreshed",
        "comfy_models_categories": len(comfy_models),
        "comfy_nodes": len(comfy_nodes),
    }


class ShutdownRequest(BaseModel):
    confirm: bool = False


@router.post("/api/system/shutdown")
async def system_shutdown(req: ShutdownRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to proceed")
    logger.warning("Shutdown requested via /api/system/shutdown")

    async def _exit_soon():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.get_running_loop().create_task(_exit_soon())
    return {"status": "shutting_down"}


@router.post("/api/shutdown")
async def shutdown_alias(req: ShutdownRequest):
    return await system_shutdown(req)


@router.post("/api/system/restart")
async def system_restart(req: ShutdownRequest):
    """Graceful restart relies on the bridge's auto-relaunch logic."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Set confirm=true to proceed")
    logger.warning("Restart requested via /api/system/restart")

    async def _exit_soon():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)
    asyncio.get_running_loop().create_task(_exit_soon())
    return {"status": "restarting"}


@router.post("/api/restart")
async def restart_alias(req: ShutdownRequest):
    return await system_restart(req)


# ---------------------------------------------------------------------------
# /api/maintenance/*
# ---------------------------------------------------------------------------
@router.get("/api/maintenance/policy")
async def get_policy():
    state = maintenance.load_policy()
    return {"policy": state["policy"], "tick_seconds": TICK_SECONDS,
            "policy_file": str(maintenance.POLICY_FILE)}


class PolicyUpdate(BaseModel):
    policy: dict


# Numeric keys per task; everything else is rejected.
_NUMERIC_POLICY_KEYS = {
    "cadence_s", "max_gb", "max_age_days", "keep_latest", "stale_days",
}


def _validate_policy_overrides(task: str, overrides: dict) -> dict:
    cleaned: dict = {}
    defaults = maintenance.DEFAULTS.get(task, {})
    for key, value in overrides.items():
        if key not in defaults:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown policy key for {task}: {key} "
                       f"(allowed: {sorted(defaults)})",
            )
        if key in _NUMERIC_POLICY_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(
                    status_code=400,
                    detail=f"Policy {task}.{key} must be a number, got "
                           f"{type(value).__name__}",
                )
            if value < 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"Policy {task}.{key} must be non-negative",
                )
            cleaned[key] = value
        else:
            cleaned[key] = value
    return cleaned


@router.put("/api/maintenance/policy")
async def put_policy(req: PolicyUpdate):
    state = maintenance.load_policy()
    for task, overrides in (req.policy or {}).items():
        if task not in maintenance.DEFAULTS:
            raise HTTPException(status_code=400,
                                detail=f"Unknown task: {task}")
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=400,
                                detail=f"Policy for {task} must be an object")
        cleaned = _validate_policy_overrides(task, overrides)
        state["policy"].setdefault(task, {})
        state["policy"][task].update(cleaned)
    # Persist only the override-diff vs DEFAULTS (not the full merged snapshot),
    # so a future change to a DEFAULT isn't permanently masked by a value the
    # operator never actually customized. Mirrors scheduler._tick_once (JOB-4).
    overrides_only: dict[str, dict] = {}
    for task, cfg in state["policy"].items():
        base = maintenance.DEFAULTS.get(task, {})
        diff = {k: v for k, v in cfg.items() if k not in base or base[k] != v}
        if diff:
            overrides_only[task] = diff
    maintenance.save_policy({"policy": overrides_only,
                             "last_run": state.get("last_run", {})})
    return {"status": "ok", "policy": state["policy"]}


@router.get("/api/maintenance/status")
async def maintenance_status():
    state = maintenance.load_policy()
    now = time.time()
    rows = []
    for task, cfg in state["policy"].items():
        last = float(state["last_run"].get(task, 0) or 0)
        cadence = float(cfg.get("cadence_s", 0))
        next_run = last + cadence if cadence > 0 else None
        rows.append({
            "task": task,
            "cadence_seconds": cadence,
            "last_run": last or None,
            "next_run": next_run,
            "due_in_seconds": (next_run - now) if next_run else None,
        })
    return {"tick_seconds": TICK_SECONDS, "tasks": rows}


@router.post("/api/maintenance/run/{task}")
async def run_maintenance(task: str):
    state = maintenance.load_policy()
    if task not in state["policy"]:
        raise HTTPException(status_code=400, detail=f"Unknown task: {task}")
    fn = maintenance.build_task_callable(task, state["policy"][task])
    try:
        job = jobs.enqueue_callable(
            kind="maintenance",
            fn=fn,
            meta={"task": task, "scheduled": False},
            active_key=f"maintenance:{task}",
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409,
                            detail=f"{task} already running")
    return {"job_id": job.job_id, "task": task, "status": job.status}
