"""ComfyUI instance lifecycle, model/node listings, per-instance logs, and
HTTP+WebSocket passthrough proxy to each running instance.

* ``/api/comfy/{instance_id}/proxy/{subpath}`` (GET/POST/PUT/DELETE/PATCH)
  forwards HTTP requests to ComfyUI after validating the subpath against an
  explicit allowlist (see ``proxy.is_allowed_subpath``).
* ``/api/comfy/{instance_id}/ws`` bridges the WebSocket progress channel to
  ComfyUI's ``/ws?clientId=...``. Browsers can't set headers on the WS
  handshake, so auth comes via ``?token=...`` query param or the
  ``omni.token.<TOKEN>`` subprotocol.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
import re
import signal
import subprocess
import urllib.parse
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config import COMFYUI_DIR, COMFYUI_PINNED_REF, COMFYUI_VRAM_MODES, WORKER_LOG_DIR
from helpers import bounded_lines
from jobs import DuplicateJobError, JobCancelled, TERMINAL_STATES
from proxy import forward_http, is_allowed_subpath
from security import origin_matches_host, token_matches
from state import SERVER_DIR, comfy_manager, comfy_registry, get_api_token, jobs
from comfy_startup import startup_catalog

logger = logging.getLogger(__name__)

router = APIRouter()

_WS_TOKEN_SUBPROTOCOL_PREFIX = "omni.token."
_WS_MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # 64 MiB - matches preview frames
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,159}$")
_COMFY_UPDATE_TIMEOUT_S = 1800.0


def _resolve_ready_instance(instance_id: str):
    inst = comfy_registry.get(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
    if inst.status != "ready":
        raise HTTPException(
            status_code=503,
            detail=f"Instance {instance_id} not ready (status={inst.status})",
        )
    return inst


class StartComfyRequest(BaseModel):
    device: str | None = None
    gpu_pool: list[str] = Field(default_factory=list, max_length=64)
    vram_mode: str = "normal"
    precision: str | None = None  # 'fp16' | 'bf16' | 'fp32' | None (auto)
    preview_method: str = "auto"
    disable_pinned_memory: bool = False
    startup_options: dict = Field(default_factory=dict)
    extra_args: list[str] | None = None


class UpdateComfyRequest(BaseModel):
    ref: str | None = None
    dry_run: bool = True
    restart_instances: bool = False


def _git(*args: str, timeout: float = 15.0) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(COMFYUI_DIR), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    text = (result.stdout or result.stderr or "").strip()
    return result.returncode, text


def _validate_update_ref(value: str | None) -> str:
    ref = str(value or COMFYUI_PINNED_REF).strip()
    if not _GIT_REF_RE.fullmatch(ref) or ref.startswith("-") or ".." in ref.split("/"):
        raise HTTPException(status_code=400, detail="Invalid ComfyUI git ref")
    return ref


def _template_package_versions() -> list[dict]:
    packages: dict[str, str] = {}
    try:
        distributions = importlib.metadata.distributions()
        for dist in distributions:
            name = str(dist.metadata.get("Name") or "")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized.startswith("comfyui-workflow-templates"):
                packages[name] = str(getattr(dist, "version", "") or "")
    except Exception as exc:  # pragma: no cover - interpreter packaging fault
        logger.warning("Could not enumerate ComfyUI template package versions: %s", exc)
    return [
        {"name": name, "version": version}
        for name, version in sorted(packages.items())
    ]


_OMNI_MANAGED_COMFY_PATHS = frozenset({"input", "models", "user"})


def _tracked_comfy_source_changes(porcelain: str) -> list[str]:
    """Exclude only expected changes beneath Omni's persistent layout links.

    Setup replaces Comfy's tracked placeholder files under input/models/user
    with guarded persistent directories. Those deletions are not source edits
    and must not permanently disable the guarded core-update API. Any change
    outside those exact roots remains a hard update blocker.
    """
    changes: list[str] = []
    for raw in str(porcelain or "").splitlines():
        line = raw.rstrip()
        if len(line) < 3:
            continue
        # _git() strips the whole stdout string, which removes the first
        # record's leading index-status space (" D path" -> "D path").
        # Accept both canonical porcelain and that first-record form.
        if len(line) >= 2 and line[1] == " ":
            path_text = line[2:]
        elif len(line) >= 3 and line[2] == " ":
            path_text = line[3:]
        else:
            changes.append(line)
            continue
        path = path_text.strip().strip('"').replace("\\", "/")
        if " -> " in path:
            path = path.split(" -> ", 1)[-1]
        root = path.split("/", 1)[0]
        if root not in _OMNI_MANAGED_COMFY_PATHS:
            changes.append(line)
    return changes


def _installation_status(ref: str | None = None, *, check_remote: bool = False) -> dict:
    target_ref = _validate_update_ref(ref or ("latest" if check_remote else None))
    installed = (COMFYUI_DIR / ".git").is_dir()
    current_commit = ""
    dirty = False
    remote_url = ""
    errors: list[str] = []
    if installed:
        rc, value = _git("rev-parse", "HEAD")
        current_commit = value if rc == 0 else ""
        if rc != 0:
            errors.append(value[:300])
        rc, value = _git("status", "--porcelain", "--untracked-files=no")
        dirty = bool(_tracked_comfy_source_changes(value)) if rc == 0 else False
        if rc != 0:
            errors.append(value[:300])
        rc, value = _git("remote", "get-url", "origin")
        remote_url = value if rc == 0 else ""
    remote_commit = ""
    if installed and check_remote:
        query_ref = "HEAD" if target_ref == "latest" else target_ref
        rc, value = _git("ls-remote", "--exit-code", "origin", query_ref, timeout=30.0)
        if rc == 0 and value:
            remote_commit = value.split()[0]
        else:
            errors.append(f"remote ref check failed: {value[:240]}")
    remote_checked = bool(check_remote)
    if not remote_checked:
        update_status = "not-checked"
    elif not remote_commit:
        update_status = "unknown"
    elif current_commit == remote_commit:
        update_status = "current"
    else:
        update_status = "available"
    return {
        "installed": installed,
        "path": str(COMFYUI_DIR),
        "current_commit": current_commit,
        "dirty": dirty,
        "remote_url": remote_url,
        "pinned_ref": COMFYUI_PINNED_REF,
        "target_ref": target_ref,
        "remote_commit": remote_commit,
        "remote_checked": remote_checked,
        "update_status": update_status,
        "update_available": bool(remote_commit and current_commit and remote_commit != current_commit),
        "running_instances": comfy_registry.to_dict_list(),
        "maintenance": comfy_manager.maintenance_reason,
        "template_packages": _template_package_versions(),
        "errors": [item for item in errors if item],
    }


def _restart_spec(instance) -> dict:
    """Capture every supported start option before an instance is stopped."""
    return {
        "previous_instance_id": instance.instance_id,
        "previous_port": instance.port,
        "device": instance.device,
        "vram_mode": instance.vram_mode,
        "precision": instance.precision,
        "preview_method": getattr(instance, "preview_method", "auto"),
        "disable_pinned_memory": bool(instance.disable_pinned_memory),
        "startup_options": dict(getattr(instance, "startup_options", {}) or {}),
        "gpu_pool": list(getattr(instance, "gpu_pool", []) or []),
    }


async def _terminate_update_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        logger.error("ComfyUI update process %s did not exit after SIGKILL", process.pid)


async def _run_update_script(
    target_ref: str,
    log_path: Path,
    progress,
    cancel_event: asyncio.Event,
) -> dict:
    """Run the rollback-aware shell updater with bounded cancellation."""
    script = Path(SERVER_DIR) / "install_model.sh"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    process: asyncio.subprocess.Process | None = None
    output_tail: list[str] = []
    try:
        with log_path.open("w", encoding="utf-8", buffering=1) as log_fh:
            process = await asyncio.create_subprocess_exec(
                "bash", str(script), "comfyui-update", target_ref,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(SERVER_DIR),
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + _COMFY_UPDATE_TIMEOUT_S
            while True:
                if cancel_event.is_set():
                    await _terminate_update_process(process)
                    raise JobCancelled()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await _terminate_update_process(process)
                    raise TimeoutError(
                        f"ComfyUI update timed out after {_COMFY_UPDATE_TIMEOUT_S:.0f}s"
                    )
                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(), timeout=min(1.0, remaining),
                    )
                except asyncio.TimeoutError:
                    if process.returncode is not None:
                        break
                    continue
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace")
                log_fh.write(decoded)
                clean = decoded.strip()
                if clean:
                    output_tail.append(clean)
                    output_tail = output_tail[-30:]
                    progress(2, 4, clean[:200])
            returncode = await process.wait()
    except asyncio.CancelledError:
        if process is not None:
            await _terminate_update_process(process)
        raise

    if returncode != 0:
        detail = " | ".join(output_tail[-5:])
        raise RuntimeError(
            f"ComfyUI update exited with code {returncode}"
            + (f": {detail}" if detail else "")
        )
    return {
        "returncode": returncode,
        "output_tail": output_tail[-10:],
        "log_path": str(log_path),
    }


async def _restart_instances(specs: list[dict], progress) -> tuple[list[dict], list[dict]]:
    restarted: list[dict] = []
    failures: list[dict] = []
    total = len(specs)
    for index, spec in enumerate(specs, start=1):
        progress(3, 5, f"restarting ComfyUI instance {index}/{total}")
        try:
            instance = await comfy_manager.start_instance(
                device=spec["device"],
                vram_mode=spec["vram_mode"],
                precision=spec["precision"],
                preview_method=spec["preview_method"],
                disable_pinned_memory=spec["disable_pinned_memory"],
                startup_options=spec["startup_options"],
                gpu_pool=spec.get("gpu_pool") or [],
                _maintenance_override=True,
            )
            restarted.append({
                "previous_instance_id": spec["previous_instance_id"],
                "instance_id": instance.instance_id,
                "port": instance.port,
                "device": instance.device,
                "status": instance.status,
            })
        except Exception as exc:  # continue restoring the remaining instances
            failures.append({
                "previous_instance_id": spec["previous_instance_id"],
                "device": spec["device"],
                "error": str(exc)[:500],
            })
    return restarted, failures


async def _qualify_comfy_update(target_commit: str, restarted: list[dict]) -> dict:
    """Verify checkout/templates and, when available, the restarted runtime."""
    blockers: list[str] = []
    checkout = await asyncio.to_thread(_installation_status, check_remote=False)
    current_commit = str(checkout.get("current_commit") or "")
    if target_commit and current_commit != target_commit:
        blockers.append(
            f"checkout commit {current_commit or 'unknown'} does not match target {target_commit}"
        )
    if checkout.get("dirty"):
        blockers.append("ComfyUI checkout is dirty after update")

    template_count = 0
    template_error = None
    try:
        from routers.assets import _template_files, invalidate_template_cache  # noqa: WPS433

        invalidate_template_cache()

        template_count = len(await asyncio.to_thread(_template_files))
    except Exception as exc:  # a package regression must be visible in the job
        template_error = str(exc)[:500]
        blockers.append(f"workflow template discovery failed: {template_error}")

    instance_checks: list[dict] = []
    required_core_nodes = ("CheckpointLoaderSimple", "KSampler", "OmniRouteModel", "OmniRouteCLIP", "OmniRouteVAE", "OmniStageVAEDecode")
    async with httpx.AsyncClient(timeout=60.0) as client:
        for record in restarted:
            instance_id = str(record.get("instance_id") or "")
            instance = comfy_registry.get(instance_id)
            check = {
                "instance_id": instance_id,
                "ready": bool(instance and instance.status == "ready"),
                "system_stats": False,
                "object_info_count": 0,
                "missing_core_nodes": [],
            }
            if not instance or instance.status != "ready":
                blockers.append(f"restarted instance is not ready: {instance_id or 'unknown'}")
                instance_checks.append(check)
                continue
            try:
                stats, objects = await asyncio.gather(
                    client.get(f"http://127.0.0.1:{instance.port}/system_stats"),
                    client.get(f"http://127.0.0.1:{instance.port}/object_info"),
                )
                stats.raise_for_status()
                objects.raise_for_status()
                object_info = objects.json()
                check["system_stats"] = isinstance(stats.json(), dict)
                check["object_info_count"] = (
                    len(object_info) if isinstance(object_info, dict) else 0
                )
                check["missing_core_nodes"] = [
                    node for node in required_core_nodes
                    if not isinstance(object_info, dict) or node not in object_info
                ]
                if not check["system_stats"]:
                    blockers.append(f"invalid system_stats from {instance_id}")
                if check["missing_core_nodes"]:
                    blockers.append(
                        f"{instance_id} is missing core nodes: "
                        + ", ".join(check["missing_core_nodes"])
                    )
            except Exception as exc:
                blockers.append(f"runtime qualification failed for {instance_id}: {exc}")
                check["error"] = str(exc)[:500]
            instance_checks.append(check)

    return {
        "valid": not blockers,
        "level": "live-runtime" if restarted else "checkout-and-templates",
        "checkout_commit": current_commit,
        "target_commit": target_commit,
        "dirty": bool(checkout.get("dirty")),
        "template_packages": checkout.get("template_packages") or [],
        "template_count": template_count,
        "template_error": template_error,
        "instances": instance_checks,
        "blockers": blockers,
    }


@router.get("/api/comfy/instances")
async def list_comfy_instances():
    return {"instances": comfy_registry.to_dict_list()}


@router.get("/api/comfy/{instance_id}/multigpu")
async def comfy_multigpu_capabilities(instance_id: str):
    """Probe the live runtime instead of assuming a Comfy version contract."""
    instance = _resolve_ready_instance(instance_id)
    node_ids = [
        "OmniRouteModel", "OmniRouteCLIP", "OmniRouteVAE",
        "OmniMultiGPUWorkUnits", "SelectModelDevice", "SelectCLIPDevice",
        "SelectVAEDevice", "MultiGPU_WorkUnits",
    ]
    async with httpx.AsyncClient(timeout=10.0) as client:
        stats_response = await client.get(
            f"http://127.0.0.1:{instance.port}/system_stats"
        )
        stats_response.raise_for_status()
        responses = await asyncio.gather(*(
            client.get(f"http://127.0.0.1:{instance.port}/object_info/{node_id}")
            for node_id in node_ids
        ), return_exceptions=True)

    available: dict[str, bool] = {}
    for node_id, response in zip(node_ids, responses):
        if isinstance(response, Exception) or response.status_code != 200:
            available[node_id] = False
            continue
        try:
            available[node_id] = node_id in response.json()
        except (TypeError, ValueError):
            available[node_id] = False
    omni_component = all(available.get(node_id, False) for node_id in (
        "OmniRouteModel", "OmniRouteCLIP", "OmniRouteVAE",
    ))
    native_component = all(available.get(node_id, False) for node_id in (
        "SelectModelDevice", "SelectCLIPDevice", "SelectVAEDevice",
    ))
    stats = stats_response.json()
    return {
        "instance_id": instance.instance_id,
        "primary_device": instance.device,
        "gpu_pool": list(getattr(instance, "gpu_pool", []) or []),
        "gpu_device_map": dict(getattr(instance, "gpu_device_map", {}) or {}),
        "logical_devices": stats.get("devices", []),
        "nodes": available,
        "capabilities": {
            "component_placement": omni_component or native_component,
            "cfg_parallel": bool(
                available.get("OmniMultiGPUWorkUnits")
                or available.get("MultiGPU_WorkUnits")
            ),
            "single_model_layer_sharding": False,
        },
    }


@router.get("/api/comfy/start-options")
async def comfy_start_options():
    """Return the safe startup flag catalog consumed by the ComfyUI tab."""
    return {
        "groups": startup_catalog(),
        "managed_flags": [
            "--listen", "--port", "--cuda-device", "--base-directory",
            "--output-directory", "--input-directory", "--temp-directory",
            "--extra-model-paths-config", "--tls-keyfile", "--tls-certfile",
            "--database-url", "--user-directory", "--front-end-root",
        ],
    }


@router.get("/api/comfy/installation/status")
async def comfy_installation_status(ref: str | None = None, check_remote: bool = False):
    """Report the live checkout and versioned template packages."""
    return await asyncio.to_thread(_installation_status, ref, check_remote=check_remote)


@router.post("/api/comfy/installation/update")
async def update_comfy_installation(req: UpdateComfyRequest):
    """Plan or enqueue a guarded, optionally self-restarting core update.

    With ``restart_instances=true``, all registered instances are captured,
    stopped, and recreated after the rollback-aware checkout/dependency update.
    External starts are blocked for the whole maintenance window.  Without
    that explicit opt-in, the historical stop-first contract remains intact.
    """
    if not str(req.ref or "").strip():
        raise HTTPException(
            status_code=400,
            detail="An explicit ref is required for updates (use 'latest' for origin HEAD)",
        )
    target_ref = _validate_update_ref(req.ref)
    status = await asyncio.to_thread(_installation_status, target_ref, check_remote=True)
    if not status["installed"]:
        raise HTTPException(status_code=409, detail="ComfyUI is not installed")
    if status["dirty"]:
        raise HTTPException(status_code=409, detail="ComfyUI checkout has tracked changes; refusing to overwrite them")
    if status["errors"] and not status["remote_commit"]:
        raise HTTPException(status_code=502, detail=status["errors"][-1])
    if status["running_instances"] and not req.restart_instances:
        raise HTTPException(
            status_code=409,
            detail=(
                "Stop all ComfyUI instances before updating, or set "
                "restart_instances=true for a guarded stop/update/restart job"
            ),
        )
    plan = {
        "status": "planned" if req.dry_run else "running",
        "target_ref": target_ref,
        "target_commit": status["remote_commit"],
        "current_commit": status["current_commit"],
        "update_available": status["update_available"],
        "restart_instances": bool(req.restart_instances),
        "instances_before": status["running_instances"],
        "template_packages_before": status["template_packages"],
    }
    if req.dry_run:
        return {**plan, "job_id": None}

    log_path = WORKER_LOG_DIR / f"comfy_update_{str(uuid.uuid4())[:8]}.log"
    reservation = f"core-update:{uuid.uuid4().hex[:12]}"
    try:
        comfy_manager.begin_maintenance(reservation)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        active_extensions = [
            job for job in jobs.list(kinds={"comfy_extension", "comfy_model_install", "comfy_asset_install", "comfy_node_install"})
            if job.status not in TERMINAL_STATES
        ]
        if active_extensions:
            raise HTTPException(
                status_code=409,
                detail="A ComfyUI extension operation is already running",
            )

        # Re-read after reserving maintenance so a concurrent start cannot race
        # between the remote-ref check above and the captured restart plan.
        instances = comfy_registry.all_instances()
        if instances and not req.restart_instances:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Stop all ComfyUI instances before updating, or set "
                    "restart_instances=true for a guarded stop/update/restart job"
                ),
            )
        specs = [_restart_spec(instance) for instance in instances]
        plan["instances_before"] = specs

        async def run(progress, cancel_event):
            update_result = None
            primary_error: Exception | None = None
            restarted: list[dict] = []
            restart_failures: list[dict] = []
            qualification: dict | None = None
            stopped_count = 0
            try:
                try:
                    progress(1, 5, "stopping ComfyUI instances")
                    stopped_count = await comfy_manager.stop_all()
                    remaining = comfy_registry.all_instances()
                    if remaining:
                        raise RuntimeError(
                            "Could not stop all ComfyUI instances: "
                            + ", ".join(item.instance_id for item in remaining)
                        )
                    if cancel_event.is_set():
                        raise JobCancelled()
                    progress(2, 5, "updating ComfyUI core and dependencies")
                    update_result = await _run_update_script(
                        target_ref, log_path, progress, cancel_event,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # restart the stopped service before reporting
                    primary_error = exc

                if req.restart_instances and specs:
                    # On a partial stop failure, only recreate instances that
                    # are no longer registered; never duplicate a survivor.
                    missing = [
                        spec for spec in specs
                        if comfy_registry.get(spec["previous_instance_id"]) is None
                    ]
                    restarted, restart_failures = await _restart_instances(
                        missing, progress,
                    )

                if primary_error is not None:
                    if restart_failures:
                        raise RuntimeError(
                            f"{primary_error}; restart failures: {restart_failures}"
                        ) from primary_error
                    raise primary_error
                if restart_failures:
                    raise RuntimeError(
                        "ComfyUI updated, but one or more instances failed to restart: "
                        f"{restart_failures}"
                    )
                progress(4, 5, "qualifying updated ComfyUI checkout and runtime")
                qualification = await _qualify_comfy_update(
                    str(status.get("remote_commit") or ""), restarted,
                )
                if not qualification["valid"]:
                    raise RuntimeError(
                        "ComfyUI post-update qualification failed: "
                        + "; ".join(qualification["blockers"])
                    )
                progress(5, 5, "ComfyUI update complete")
                return {
                    "status": "done",
                    "target_ref": target_ref,
                    "target_commit": status["remote_commit"],
                    "stopped": stopped_count,
                    "restarted": restarted,
                    "restart_failures": restart_failures,
                    "update": update_result,
                    "qualification": qualification,
                }
            finally:
                comfy_manager.end_maintenance(reservation)

        job = jobs.enqueue_callable(
            kind="comfy_update",
            fn=run,
            meta={**plan, "log_path": str(log_path)},
            active_key="comfy:update",
        )
    except DuplicateJobError as exc:
        comfy_manager.end_maintenance(reservation)
        raise HTTPException(
            status_code=409, detail="A ComfyUI update is already running",
        ) from exc
    except Exception:
        comfy_manager.end_maintenance(reservation)
        raise
    return {**plan, "job_id": job.job_id, "log_path": str(log_path)}


@router.post("/api/comfy/start")
async def start_comfy_instance(req: StartComfyRequest):
    if comfy_manager.maintenance_reason:
        raise HTTPException(
            status_code=409,
            detail=f"ComfyUI maintenance in progress: {comfy_manager.maintenance_reason}",
        )
    if req.vram_mode not in COMFYUI_VRAM_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid VRAM mode: {req.vram_mode}. Valid: {list(COMFYUI_VRAM_MODES)}",
        )
    if req.extra_args:
        raise HTTPException(status_code=400,
                            detail="Custom ComfyUI startup args are disabled")
    if req.precision and req.precision not in ("fp16", "bf16", "fp32"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid precision: {req.precision}. Valid: fp16, bf16, fp32",
        )
    if req.preview_method not in ("none", "auto", "latent2rgb", "taesd"):
        raise HTTPException(
            status_code=400,
            detail="Invalid preview method. Valid: none, auto, latent2rgb, taesd",
        )
    try:
        instance = await comfy_manager.start_instance(
            device=req.device,
            gpu_pool=req.gpu_pool,
            vram_mode=req.vram_mode,
            precision=req.precision,
            preview_method=req.preview_method,
            disable_pinned_memory=req.disable_pinned_memory,
            startup_options=req.startup_options,
        )
        return {
            "instance_id": instance.instance_id,
            "port": instance.port,
            "device": instance.device,
            "gpu_pool": list(getattr(instance, "gpu_pool", []) or []),
            "gpu_device_map": dict(getattr(instance, "gpu_device_map", {}) or {}),
            "status": instance.status,
            "disable_pinned_memory": instance.disable_pinned_memory,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/comfy/{instance_id}/stop")
async def stop_comfy_instance(instance_id: str):
    ok = await comfy_manager.stop_instance(instance_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Instance not found")
    return {"status": "stopped"}


@router.post("/api/comfy/stop-all")
async def stop_all_comfy():
    count = await comfy_manager.stop_all()
    return {"stopped": count}


@router.get("/api/comfy/models")
async def list_comfy_models():
    return {"models": comfy_manager.get_installed_models()}


@router.get("/api/comfy/nodes")
async def list_comfy_nodes():
    return {"nodes": comfy_manager.get_custom_nodes()}


@router.get("/api/comfy/{instance_id}/nodes/search")
async def search_live_comfy_nodes(
    instance_id: str,
    q: str = Query(min_length=1, max_length=160),
    exact: bool = False,
    include_schema: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Search exact live node classes and optionally return callable schemas."""
    instance = _resolve_ready_instance(instance_id)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"http://127.0.0.1:{instance.port}/object_info")
            response.raise_for_status()
            inventory = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"ComfyUI node inventory failed: {exc}") from exc
    if not isinstance(inventory, dict):
        raise HTTPException(status_code=502, detail="ComfyUI returned an invalid node inventory")

    needle = q.strip().casefold()
    results = []
    for class_type, raw in inventory.items():
        info = raw if isinstance(raw, dict) else {}
        display_name = str(info.get("display_name") or class_type)
        category = str(info.get("category") or "")
        description = str(info.get("description") or "")
        aliases = [str(item) for item in info.get("search_aliases") or []]
        if exact:
            matched = needle in {str(class_type).casefold(), display_name.casefold()}
        else:
            matched = any(needle in value.casefold() for value in (
                str(class_type), display_name, category, description, *aliases,
            ))
        if not matched:
            continue
        input_info = info.get("input") if isinstance(info.get("input"), dict) else {}
        row = {
            "class_type": str(class_type),
            "display_name": display_name,
            "category": category,
            "description": description[:1000],
            "search_aliases": aliases,
            "output_node": bool(info.get("output_node")),
            "input_names": {
                group: list(values.keys()) if isinstance(values, dict) else []
                for group, values in input_info.items()
                if group in {"required", "optional", "hidden"}
            },
            "output_types": info.get("output") or [],
            "output_names": info.get("output_name") or [],
        }
        if include_schema or exact:
            row["input_schema"] = input_info
            row["output_is_list"] = info.get("output_is_list") or []
            row["input_is_list"] = info.get("input_is_list") or {}
        results.append(row)
        if len(results) >= limit:
            break
    return {
        "query": q.strip(),
        "exact": bool(exact),
        "instance_id": instance.instance_id,
        "nodes": results,
        "total": len(results),
    }


@router.get("/api/comfy/{instance_id}/logs")
async def get_comfy_logs(instance_id: str, lines: int = 100):
    instance = comfy_registry.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    log_file = WORKER_LOG_DIR / f"comfy_{instance.port}.log"
    if not log_file.exists():
        return {"lines": []}
    def read_tail():
        with log_file.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            return fh.read(256 * 1024).decode("utf-8", errors="replace")
    try:
        text = await asyncio.to_thread(read_tail)
    except OSError:
        return {"lines": []}
    return {"lines": text.strip().splitlines()[-bounded_lines(lines):]}


# ---------------------------------------------------------------------------
# HTTP passthrough — phase 7
# ---------------------------------------------------------------------------
@router.post("/api/comfy/{instance_id}/upload/image")
async def upload_image(instance_id: str, request: Request) -> StreamingResponse:
    """Forward a multipart image upload to ComfyUI's /upload/image."""
    inst = _resolve_ready_instance(instance_id)
    return await forward_http(request, f"http://127.0.0.1:{inst.port}", "upload/image")


@router.post("/api/comfy/{instance_id}/upload/mask")
async def upload_mask(instance_id: str, request: Request) -> StreamingResponse:
    """Forward a multipart mask upload to ComfyUI's /upload/mask."""
    inst = _resolve_ready_instance(instance_id)
    return await forward_http(request, f"http://127.0.0.1:{inst.port}", "upload/mask")


async def comfy_http_proxy(
    instance_id: str, subpath: str, request: Request,
) -> StreamingResponse:
    """Generic ComfyUI HTTP passthrough.

    Subpath is matched against the proxy allowlist before forwarding. The
    bridge auth middleware already ran at the gateway boundary, but we keep a
    second allowlist here so a future regression elsewhere can't expose every
    internal ComfyUI endpoint.
    """
    if not is_allowed_subpath(subpath):
        raise HTTPException(status_code=404, detail=f"Subpath not allowed: {subpath}")
    inst = _resolve_ready_instance(instance_id)
    return await forward_http(request, f"http://127.0.0.1:{inst.port}", subpath)


# The existing five HTTP operations each need a unique OpenAPI operation ID.
for _proxy_method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
    router.add_api_route(
        "/api/comfy/{instance_id}/proxy/{subpath:path}", comfy_http_proxy,
        methods=[_proxy_method], operation_id=f"comfy_proxy_{_proxy_method.lower()}",
    )


# ---------------------------------------------------------------------------
# WebSocket passthrough — phase 9
# ---------------------------------------------------------------------------
def _resolve_ws_token(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Pull the API token from the WS handshake.

    Returns ``(token, accepted_subprotocol_or_none)``. The HTTP middleware
    doesn't see WebSocket upgrades, so we authenticate inline.
    """
    token = websocket.query_params.get("token")
    chosen_subprotocol: str | None = None
    if not token:
        for sp in websocket.scope.get("subprotocols") or []:
            if sp.startswith(_WS_TOKEN_SUBPROTOCOL_PREFIX):
                token = sp[len(_WS_TOKEN_SUBPROTOCOL_PREFIX):]
                chosen_subprotocol = sp
                break
    return token, chosen_subprotocol


@router.websocket("/api/comfy/{instance_id}/ws")
async def comfy_ws_proxy(websocket: WebSocket, instance_id: str):
    """Bridge the gateway-side WebSocket to ComfyUI's /ws."""
    # Lazy import keeps the optional dep cost off the cold start path.
    import websockets as _ws
    from websockets.exceptions import ConnectionClosed

    # COM-2: the HTTP auth middleware (and its Origin check) is bypassed for WS
    # upgrades because browsers can't set X-Omni-Token on a handshake, so token
    # auth is done inline below. Replicate the middleware's Origin guard here so
    # a forbidden-origin browser page can't open this socket with a stolen
    # token. Mirror the middleware exactly: only block when an Origin header is
    # present and doesn't match the host (a missing Origin — non-browser
    # clients — is still allowed). Reject before accept() so the upgrade fails.
    origin = websocket.headers.get("origin")
    if origin and not origin_matches_host(origin, websocket.headers.get("host")):
        await websocket.close(code=1008, reason="Forbidden origin")
        return

    token, chosen_subprotocol = _resolve_ws_token(websocket)
    if not token_matches(token, get_api_token()):
        await websocket.close(code=1008, reason="Invalid or missing token")
        return

    inst = comfy_registry.get(instance_id)
    if not inst or inst.status != "ready":
        await websocket.close(code=1011, reason="Instance not ready")
        return

    client_id = (websocket.query_params.get("client_id")
                 or websocket.query_params.get("clientId")
                 or "")
    upstream_url = f"ws://127.0.0.1:{inst.port}/ws"
    if client_id:
        # urlencode the value so a hostile client_id with `&` or `?` cannot
        # inject extra query params into the upstream ComfyUI URL.
        upstream_url = f"{upstream_url}?clientId={urllib.parse.quote(client_id, safe='')}"

    accept_kwargs = {}
    if chosen_subprotocol:
        accept_kwargs["subprotocol"] = chosen_subprotocol
    await websocket.accept(**accept_kwargs)

    try:
        async with _ws.connect(
            upstream_url,
            max_size=_WS_MAX_MESSAGE_BYTES,
            open_timeout=10,
            ping_interval=20,
        ) as upstream:
            async def _down():
                try:
                    async for msg in upstream:
                        if isinstance(msg, (bytes, bytearray)):
                            await websocket.send_bytes(bytes(msg))
                        else:
                            await websocket.send_text(msg)
                except ConnectionClosed:
                    return

            async def _up():
                try:
                    while True:
                        event = await websocket.receive()
                        etype = event.get("type")
                        if etype == "websocket.disconnect":
                            return
                        if "text" in event and event["text"] is not None:
                            await upstream.send(event["text"])
                        elif "bytes" in event and event["bytes"] is not None:
                            await upstream.send(event["bytes"])
                except WebSocketDisconnect:
                    return

            down_task = asyncio.create_task(_down())
            up_task = asyncio.create_task(_up())
            done, pending = await asyncio.wait(
                {down_task, up_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
    except (OSError, ConnectionRefusedError) as e:
        logger.warning("ws upstream connect failed for %s: %s", instance_id, e)
        try:
            await websocket.close(code=1011, reason="Upstream connect failed")
        except Exception:
            pass
        return
    except Exception as e:
        logger.exception("ws proxy error for %s: %s", instance_id, e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
