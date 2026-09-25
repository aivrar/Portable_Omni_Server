"""Guarded ComfyUI-Manager extension lifecycle.

This router deliberately exposes a small, stable surface instead of proxying
arbitrary Manager routes.  Each mutation bundles queue preflight, a Manager
snapshot, bounded queue execution, optional restart, and state verification.
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import COMFYUI_DIR, WORKER_LOG_DIR
from jobs import DuplicateJobError, JobCancelled
from state import comfy_manager, comfy_registry, jobs

router = APIRouter()

_MANAGER_DIR = COMFYUI_DIR / "custom_nodes" / "ComfyUI-Manager"
_MANAGER_DEFAULT_REF = "7955e638db7d4a4b8bf7a614e724a2013b83dfd7"
_MANAGER_PINNED_REF = str(
    os.environ.get("OMNI_COMFYUI_MANAGER_REF") or _MANAGER_DEFAULT_REF
).strip()
_ACTIONS = {
    "install", "update", "update_all", "enable", "disable",
    "reinstall", "fix", "uninstall",
}
_NODE_URL_RE = re.compile(
    r"^(?:https?://[A-Za-z0-9.\-/_:%?=&+~#@]+|"
    r"git@[A-Za-z0-9.\-]+:[A-Za-z0-9._\-/]+)$"
)


class ManageExtensionRequest(BaseModel):
    action: str
    instance_id: str | None = None
    node: str | None = None
    repo_url: str | None = None
    dry_run: bool = True
    auto_restart: bool = True
    timeout_s: int = 600
    expected_nodes: list[str] = Field(default_factory=list, max_length=500)


class InstallManagerModelRequest(BaseModel):
    instance_id: str | None = None
    name: str
    filename: str
    base: str
    save_path: str
    dry_run: bool = True
    timeout_s: int = 1800


class ManagerAPIError(RuntimeError):
    pass


def _normalise_expected_nodes(values: list[str]) -> list[str]:
    """Return bounded, unique Comfy node class names for live verification."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        node_class = str(value or "").strip()
        if not node_class:
            continue
        if len(node_class) > 240 or any(ord(char) < 32 for char in node_class):
            raise HTTPException(status_code=400, detail="Invalid expected node class")
        if node_class not in seen:
            seen.add(node_class)
            result.append(node_class)
    return result


def _manager_model_catalog() -> list[dict]:
    path = _MANAGER_DIR / "model-list.json"
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("models") if isinstance(payload, dict) else []
    return [dict(item) for item in rows or [] if isinstance(item, dict)]


def _find_manager_model(req: InstallManagerModelRequest) -> dict | None:
    wanted = (
        req.name.strip(), req.filename.strip(), req.base.strip(), req.save_path.strip(),
    )
    for item in _manager_model_catalog():
        current = tuple(str(item.get(key) or "").strip() for key in (
            "name", "filename", "base", "save_path",
        ))
        if current == wanted:
            return item
    return None


def _safe_manager_model_target(save_path: str, filename: str) -> Path:
    """Resolve an allowlisted Manager target below ComfyUI's models folder.

    Nested catalog paths are legitimate (for example ``checkpoints/SDXL``),
    so safety must be based on containment rather than requiring one path
    component.
    """
    raw_path = str(save_path or "").strip().replace("\\", "/")
    raw_name = str(filename or "").strip()
    relative = PurePosixPath(raw_path)
    if (
        not raw_path
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or not raw_name
        or Path(raw_name).name != raw_name
    ):
        raise ValueError("Manager model catalog entry has an unsafe path")

    models_root = (COMFYUI_DIR / "models").resolve()
    target = (models_root / Path(*relative.parts) / raw_name).resolve()
    if models_root not in target.parents:
        raise ValueError("Manager model catalog entry escapes the models directory")
    return target


def _git(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(_MANAGER_DIR), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr or "").strip()


def _manager_checkout_status() -> dict:
    installed = (_MANAGER_DIR / ".git").is_dir()
    current_commit = ""
    dirty = False
    remote_url = ""
    pinned_commit = ""
    errors: list[str] = []
    if installed:
        rc, value = _git("rev-parse", "HEAD")
        if rc == 0:
            current_commit = value
        else:
            errors.append(value[:300])
        rc, value = _git("status", "--porcelain", "--untracked-files=no")
        if rc == 0:
            dirty = bool(value)
        else:
            errors.append(value[:300])
        rc, value = _git("remote", "get-url", "origin")
        if rc == 0:
            remote_url = value
        else:
            errors.append(value[:300])
        rc, value = _git("rev-parse", f"{_MANAGER_PINNED_REF}^{{commit}}")
        if rc == 0:
            pinned_commit = value
        else:
            errors.append(f"pinned ref is unavailable locally: {value[:240]}")
    return {
        "installed": installed,
        "path": str(_MANAGER_DIR),
        "current_commit": current_commit,
        "dirty": dirty,
        "remote_url": remote_url,
        "pinned_ref": _MANAGER_PINNED_REF,
        "pinned_commit": pinned_commit,
        "matches_pinned_ref": bool(
            current_commit and pinned_commit and current_commit == pinned_commit
        ),
        "errors": [item for item in errors if item],
    }


def _ready_instance(instance_id: str | None = None):
    if instance_id:
        instance = comfy_registry.get(instance_id)
        if not instance:
            raise HTTPException(status_code=404, detail="ComfyUI instance not found")
        if instance.status != "ready":
            raise HTTPException(
                status_code=503,
                detail=f"ComfyUI instance is not ready (status={instance.status})",
            )
        return instance
    ready = [item for item in comfy_registry.all_instances() if item.status == "ready"]
    if not ready:
        return None
    return sorted(ready, key=lambda item: (item.port, item.instance_id))[0]


async def _manager_request(
    instance,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    url = f"http://127.0.0.1:{instance.port}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, url, json=body)
    except httpx.HTTPError as exc:
        raise ManagerAPIError(f"Manager request failed for {path}: {exc}") from exc
    if response.status_code < 200 or response.status_code >= 300:
        detail = response.text.strip()[:500]
        raise ManagerAPIError(
            f"Manager {method} {path} returned {response.status_code}"
            + (f": {detail}" if detail else "")
        )
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:1000]}


def _find_installed(installed: dict, selector: str) -> tuple[str, dict] | None:
    needle = selector.strip().lower()
    if not needle:
        return None
    for key, raw in installed.items():
        item = raw if isinstance(raw, dict) else {}
        aliases = {
            str(key), str(item.get("cnr_id") or ""), str(item.get("aux_id") or ""),
        }
        if needle in {alias.strip().lower() for alias in aliases if alias.strip()}:
            return str(key), item
    return None


def _catalog_aliases(key: str, item: dict) -> set[str]:
    aliases = {
        key,
        str(item.get("id") or ""),
        str(item.get("title") or ""),
        str(item.get("name") or ""),
        str(item.get("repository") or ""),
        str(item.get("reference") or ""),
    }
    for value in item.get("files") or []:
        aliases.add(str(value))
    return {value.strip().lower() for value in aliases if value.strip()}


def _find_catalog(catalog: dict, selectors: list[str]) -> tuple[str, dict] | None:
    needles = {item.strip().lower() for item in selectors if item and item.strip()}
    for key, raw in catalog.items():
        item = raw if isinstance(raw, dict) else {}
        if needles & _catalog_aliases(str(key), item):
            return str(key), dict(item)
    return None


def _fallback_body(
    key: str,
    installed: dict | None,
    repo_url: str,
) -> dict:
    item = installed or {}
    cnr_id = str(item.get("cnr_id") or "").strip()
    aux_id = str(item.get("aux_id") or "").strip()
    resolved_url = repo_url.strip()
    if not resolved_url and aux_id:
        resolved_url = aux_id if "://" in aux_id else f"https://github.com/{aux_id}"
    if cnr_id:
        version = "nightly" if aux_id else str(item.get("ver") or "latest")
        return {
            "id": cnr_id,
            "title": key,
            "version": version,
            "repository": resolved_url,
            "files": [resolved_url] if resolved_url else [],
        }
    if not resolved_url:
        raise ManagerAPIError(
            f"Manager metadata for '{key}' is incomplete; use its registry repo_url"
        )
    return {
        "id": key,
        "title": key,
        "version": "unknown",
        "repository": resolved_url,
        "files": [resolved_url],
        "pip": [],
    }


def _prepare_body(key: str, body: dict, repo_url: str = "") -> dict:
    prepared = dict(body)
    prepared.setdefault("id", key)
    prepared.setdefault("title", key)
    prepared.setdefault("version", "unknown")
    prepared.setdefault("files", [repo_url] if repo_url else [])
    prepared.setdefault("pip", [])
    prepared["selected_version"] = (
        "latest" if prepared.get("version") != "unknown" else "unknown"
    )
    prepared["channel"] = "default"
    prepared["mode"] = "cache"
    prepared["ui_id"] = str(prepared.get("id") or key)
    return prepared


async def _operation_context(instance, action: str, node: str, repo_url: str) -> dict:
    installed = await _manager_request(
        instance, "GET", "/customnode/installed", timeout=20.0,
    )
    if not isinstance(installed, dict):
        raise ManagerAPIError("Manager returned an invalid installed-node inventory")

    found = _find_installed(installed, node or repo_url)
    if action != "install" and action != "update_all" and found is None:
        raise ManagerAPIError(f"Extension '{node}' is not installed")
    if action == "install" and found is not None:
        raise ManagerAPIError(f"Extension '{found[0]}' is already installed")

    catalog_data = await _manager_request(
        instance,
        "GET",
        "/customnode/getlist?mode=cache&skip_update=true",
        timeout=60.0,
    )
    catalog = catalog_data.get("node_packs") if isinstance(catalog_data, dict) else {}
    if not isinstance(catalog, dict):
        catalog = {}

    selectors = [node, repo_url]
    if found:
        selectors.extend([found[0], str(found[1].get("cnr_id") or ""),
                          str(found[1].get("aux_id") or "")])
    catalog_match = _find_catalog(catalog, selectors)
    if catalog_match:
        key, body = catalog_match
    else:
        key = found[0] if found else (node or Path(repo_url.rstrip("/")).stem)
        body = _fallback_body(key, found[1] if found else None, repo_url)

    body = _prepare_body(key, body, repo_url)
    return {
        "key": key,
        "body": body,
        "installed_before": installed,
        "installed_match": found,
    }


async def _update_all_context(instance) -> dict:
    """Resolve only packages Manager itself reports as installed.

    Manager's native update-all also sweeps unknown source-managed folders,
    including Omni's ``omni_bridge``. That bridge is deployed by Omni setup,
    not by Manager, so allowing Manager to mutate it is both noisy and unsafe.
    """
    installed = await _manager_request(
        instance, "GET", "/customnode/installed", timeout=20.0,
    )
    if not isinstance(installed, dict):
        raise ManagerAPIError("Manager returned an invalid installed-node inventory")
    catalog_data = await _manager_request(
        instance,
        "GET",
        "/customnode/getlist?mode=cache&skip_update=true",
        timeout=60.0,
    )
    catalog = catalog_data.get("node_packs") if isinstance(catalog_data, dict) else {}
    if not isinstance(catalog, dict):
        catalog = {}

    items: list[dict] = []
    for installed_key, installed_item in installed.items():
        raw = installed_item if isinstance(installed_item, dict) else {}
        selectors = [
            str(installed_key),
            str(raw.get("cnr_id") or ""),
            str(raw.get("aux_id") or ""),
        ]
        match = _find_catalog(catalog, selectors)
        if match:
            key, body = match
        else:
            key = str(installed_key)
            body = _fallback_body(key, raw, "")
        items.append({"key": key, "body": _prepare_body(key, body)})
    if not items:
        raise ManagerAPIError("Manager reports no installed extensions to update")
    return {
        "key": "all",
        "body": {"items": items},
        "installed_before": installed,
        "installed_match": None,
    }


async def _save_snapshot(instance) -> str | None:
    before = await _manager_request(instance, "GET", "/snapshot/getlist", timeout=15.0)
    before_items = set(before.get("items") or []) if isinstance(before, dict) else set()
    await _manager_request(instance, "POST", "/snapshot/save", {}, timeout=90.0)
    after = await _manager_request(instance, "GET", "/snapshot/getlist", timeout=15.0)
    after_items = list(after.get("items") or []) if isinstance(after, dict) else []
    created = [item for item in after_items if item not in before_items]
    if len(created) != 1:
        raise ManagerAPIError("Manager did not create one identifiable recovery snapshot")
    return str(created[0])


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _manager_log_offset(instance) -> tuple[Path, int]:
    path = WORKER_LOG_DIR / f"comfy_{instance.port}.log"
    try:
        return path, path.stat().st_size
    except OSError:
        return path, 0


def _manager_log_evidence(path: Path, offset: int) -> dict:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, offset))
            raw = handle.read(512 * 1024).decode("utf-8", errors="replace")
    except OSError as exc:
        return {"stats": {}, "errors": [], "warning": str(exc)}
    clean = _ANSI_RE.sub("", raw).replace("\r", "")
    stats: dict = {}
    matches = re.findall(
        r"\[ComfyUI-Manager\] Queued works are completed\.\s*(\{[^\n]*\})",
        clean,
    )
    if matches:
        try:
            parsed = ast.literal_eval(matches[-1])
            if isinstance(parsed, dict):
                stats = parsed
        except (SyntaxError, ValueError):
            pass
    errors = []
    for line in clean.splitlines():
        stripped = line.strip()
        if (
            "ERROR:" in stripped
            or "[ERROR]" in stripped
            or stripped.lower().startswith("failed to ")
        ):
            errors.append(stripped[:500])
    return {"stats": stats, "errors": errors[-10:]}


async def _run_queue(instance, action: str, body: dict, timeout_s: int, cancel_event) -> dict:
    status = await _manager_request(instance, "GET", "/manager/queue/status", timeout=15.0)
    if not isinstance(status, dict) or not isinstance(status.get("is_processing"), bool):
        raise ManagerAPIError("Manager returned an invalid queue status before mutation")
    if status.get("is_processing"):
        raise ManagerAPIError("ComfyUI-Manager is already processing another queue")
    if cancel_event.is_set():
        raise JobCancelled()
    log_path, log_offset = _manager_log_offset(instance)
    await _manager_request(instance, "POST", "/manager/queue/reset", {}, timeout=15.0)
    if action == "update_all":
        for item in body.get("items") or []:
            await _manager_request(
                instance,
                "POST",
                "/manager/queue/update",
                dict(item["body"]),
                timeout=90.0,
            )
    else:
        endpoint_action = "install" if action == "enable" else action
        payload = dict(body)
        payload["skip_post_install"] = action == "enable"
        if action == "reinstall":
            # Manager 3.39's convenience route performs both calls but returns
            # no aiohttp response, producing HTTP 500 after queueing the work.
            # Queue the same two primitives explicitly so success is observable.
            await _manager_request(
                instance, "POST", "/manager/queue/uninstall", payload, timeout=90.0,
            )
            await _manager_request(
                instance, "POST", "/manager/queue/install", payload, timeout=90.0,
            )
        else:
            await _manager_request(
                instance, "POST", f"/manager/queue/{endpoint_action}", payload, timeout=90.0,
            )
    # Even a failed response to /start may have launched mutation. Stop the
    # owner on every failure before snapshot recovery or releasing the job key.
    try:
        await _manager_request(instance, "POST", "/manager/queue/start", {}, timeout=15.0)

        deadline = time.monotonic() + timeout_s
        last = {}
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                # Manager has no reliable abort for the running mutation. Retire
                # its owning Comfy process group before declaring cancellation.
                raise JobCancelled()
            last = await _manager_request(
                instance, "GET", "/manager/queue/status", timeout=15.0,
            )
            if not isinstance(last, dict) or not isinstance(last.get("is_processing"), bool):
                raise ManagerAPIError("Manager returned an invalid queue status")
            if not last["is_processing"]:
                evidence = {}
                for _ in range(6):
                    evidence = await asyncio.to_thread(
                        _manager_log_evidence, log_path, log_offset,
                    )
                    if evidence.get("stats") or action == "enable":
                        break
                    await asyncio.sleep(0.25)
                if evidence.get("errors"):
                    raise ManagerAPIError(
                        "Manager reported queued-work errors: "
                        + "; ".join(evidence["errors"][-3:])
                    )
                if action != "enable" and not evidence.get("stats"):
                    raise ManagerAPIError("Manager queue completed without durable log evidence")
                return {**last, "evidence": evidence}
            await asyncio.sleep(1.0)
        raise TimeoutError(f"Manager queue did not finish within {timeout_s} seconds; owning instance stopped")

    except BaseException:
        await asyncio.shield(comfy_manager.stop_instance(instance.instance_id))
        raise


async def _restart(instance):
    settings = {
        "device": instance.device,
        "vram_mode": instance.vram_mode,
        "precision": instance.precision,
        "preview_method": getattr(instance, "preview_method", "auto"),
        "disable_pinned_memory": instance.disable_pinned_memory,
        "startup_options": dict(getattr(instance, "startup_options", {}) or {}),
        "gpu_pool": list(getattr(instance, "gpu_pool", []) or []),
    }
    if comfy_registry.get(instance.instance_id) is not None and not await comfy_manager.stop_instance(instance.instance_id):
        raise ManagerAPIError(f"Could not stop {instance.instance_id} for restart")
    return await comfy_manager.start_instance(
        **settings,
    )


async def _verify(
    instance,
    action: str,
    key: str,
    body: dict,
    expected_nodes: list[str] | None = None,
    *,
    catalog_expected_nodes: bool = False,
) -> dict:
    installed = await _manager_request(
        instance, "GET", "/customnode/installed", timeout=30.0,
    )
    if not isinstance(installed, dict):
        raise ManagerAPIError("Manager returned an invalid installed-node inventory")
    if action == "update_all":
        verified = []
        for item in body.get("items") or []:
            item_body = item.get("body") or {}
            selectors = [
                str(item.get("key") or ""),
                str(item_body.get("id") or ""),
                str(item_body.get("repository") or ""),
            ]
            found = None
            for value in selectors:
                if value:
                    found = _find_installed(installed, value)
                    if found is not None:
                        break
            if found is None:
                raise ManagerAPIError(
                    f"Extension '{item.get('key')}' is absent after update-all"
                )
            verified.append(found[0])
        expected = body.get("expected_nodes_before") or []
        objects = await _manager_request(instance, "GET", "/object_info", timeout=60.0)
        if not isinstance(objects, dict):
            raise ManagerAPIError("ComfyUI returned invalid object_info after update_all")
        missing = [name for name in expected if name not in objects]
        if missing:
            raise ManagerAPIError("Previously working node classes are missing after update_all: " + ", ".join(missing[:20]))
        return {
            "mode": "manager_inventory_and_object_info",
            "installed_count": len(installed),
            "extensions": verified,
            "node_classes_checked": len(expected),
        }

    selectors = [key, str(body.get("id") or ""), str(body.get("repository") or "")]
    found = None
    for value in selectors:
        if value:
            found = _find_installed(installed, value)
            if found is not None:
                break
    if action == "uninstall":
        if found is not None:
            raise ManagerAPIError(f"Extension '{key}' is still installed after uninstall")
    else:
        if found is None:
            raise ManagerAPIError(f"Extension '{key}' is absent after {action}")
        enabled = bool(found[1].get("enabled", True))
        if action == "disable" and enabled:
            raise ManagerAPIError(f"Extension '{key}' did not become disabled")
        if action == "enable" and not enabled:
            raise ManagerAPIError(f"Extension '{key}' did not become enabled")
    result = {
        "mode": "manager_inventory",
        "installed_count": len(installed),
        "extension": found[0] if found else None,
        "node_classes_checked": 0,
    }
    expected = list(expected_nodes or [])
    if expected and action != "uninstall":
        object_info = await _manager_request(
            instance, "GET", "/object_info", timeout=60.0,
        )
        if not isinstance(object_info, dict):
            raise ManagerAPIError("ComfyUI returned invalid /object_info data")
        missing = [node_class for node_class in expected if node_class not in object_info]
        if missing:
            loaded = [node_class for node_class in expected if node_class in object_info]
            # Manager's registry mapping is updated independently from rolling
            # extension commits.  A catalog install should still fail when the
            # extension did not load, but one renamed/removed mapped class must
            # not roll back an otherwise healthy package.  Direct callers keep
            # strict all-class verification.
            minimum = max(1, int(len(expected) * 0.8))
            if catalog_expected_nodes and len(loaded) >= minimum:
                result.update({
                    "mode": "manager_inventory_and_object_info",
                    "node_classes_checked": len(expected),
                    "node_classes_loaded": loaded,
                    "catalog_classes_missing": missing,
                    "catalog_mapping_drift": True,
                })
                return result
            preview = ", ".join(missing[:20])
            suffix = f" (+{len(missing) - 20} more)" if len(missing) > 20 else ""
            raise ManagerAPIError(
                "Extension is present in Manager inventory but node classes failed "
                f"to load: {preview}{suffix}"
            )
        result.update({
            "mode": "manager_inventory_and_object_info",
            "node_classes_checked": len(expected),
            "node_classes_loaded": expected,
        })
    return result


async def _restore_snapshot(instance, snapshot_id: str | None) -> tuple[Any, dict]:
    if not snapshot_id:
        return instance, {"attempted": False, "reason": "snapshot id unavailable"}
    try:
        if comfy_registry.get(instance.instance_id) is None:
            instance = await _restart(instance)
        log_path, log_offset = _manager_log_offset(instance)
        await _manager_request(
            instance, "POST", "/snapshot/restore", {"target": snapshot_id}, timeout=30.0,
        )
        restarted = await _restart(instance)
        evidence = await asyncio.to_thread(_manager_log_evidence, log_path, log_offset)
        if evidence.get("errors"):
            return restarted, {
                "attempted": True,
                "restored": False,
                "snapshot": snapshot_id,
                "errors": evidence["errors"],
            }
        return restarted, {
            "attempted": True,
            "restored": True,
            "snapshot": snapshot_id,
            "evidence": evidence,
        }
    except Exception as exc:  # recovery details must survive the original failure
        return instance, {
            "attempted": True,
            "restored": False,
            "snapshot": snapshot_id,
            "error": str(exc),
        }


@router.post("/api/comfy/models/manager/install")
async def install_manager_model(req: InstallManagerModelRequest):
    """Install one exact model from Manager's current allowlisted catalog."""
    instance = _ready_instance(req.instance_id)
    if instance is None:
        raise HTTPException(status_code=409, detail="Start a ComfyUI instance first")
    model = _find_manager_model(req)
    if model is None:
        raise HTTPException(status_code=404, detail="Manager model selector was not found")
    filename = str(model.get("filename") or "").strip()
    save_path = str(model.get("save_path") or "").strip()
    try:
        target = _safe_manager_model_target(save_path, filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    plan = {
        "action": "install_manager_model",
        "instance_id": instance.instance_id,
        "name": str(model.get("name") or filename),
        "filename": filename,
        "save_path": save_path,
        "url": str(model.get("url") or ""),
        "target": str(target),
    }
    if target.is_file():
        return {"status": "already_installed", "job_id": None, **plan}
    if req.dry_run:
        return {"status": "planned", "job_id": None, **plan}

    timeout_s = max(60, min(int(req.timeout_s), 7200))

    async def run(progress, cancel_event):
        current = _ready_instance(instance.instance_id)
        if current is None:
            raise ManagerAPIError("ComfyUI instance disappeared before model installation")
        queue = await _manager_request(
            current, "GET", "/manager/queue/status", timeout=15.0,
        )
        if queue.get("is_processing"):
            raise ManagerAPIError("ComfyUI-Manager queue is busy")
        progress(1, 3, "catalog entry verified")
        body = dict(model)
        body["ui_id"] = f"model:{save_path}:{filename}"
        result = await _run_queue(
            current, "install_model", body, timeout_s, cancel_event,
        )
        progress(2, 3, "Manager download complete")
        if not target.is_file():
            raise ManagerAPIError(f"Manager completed but model is absent: {target}")
        progress(3, 3, "installed file verified")
        return {
            **plan,
            "status": "done",
            "size_bytes": target.stat().st_size,
            "queue": result,
        }

    from routers.assets import _guard_asset_mutation
    _guard_asset_mutation(target)
    try:
        job = jobs.enqueue_callable(
            "comfy_model_install",
            run,
            meta=plan,
            active_key="comfy:manager",
        )
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail="This Manager model install is already running") from exc
    return {
        "status": "running",
        "job_id": job.job_id,
        "job_status_path": f"/api/jobs/{job.job_id}",
        **plan,
    }


@router.get("/api/comfy/extensions/status")
async def extension_status(instance_id: str | None = None, refresh: bool = False):
    instance = _ready_instance(instance_id)
    result = {
        "manager": await asyncio.to_thread(_manager_checkout_status),
        "filesystem_nodes": comfy_manager.get_custom_nodes(),
        "instance": None,
        "manager_api": {"available": False},
    }
    if instance is None:
        return result
    if refresh:
        await _manager_request(instance, "GET", "/customnode/fetch_updates?mode=cache", timeout=90.0)
    installed, queue = await asyncio.gather(
        _manager_request(instance, "GET", "/customnode/installed", timeout=30.0),
        _manager_request(instance, "GET", "/manager/queue/status", timeout=15.0),
    )
    result["instance"] = {
        "instance_id": instance.instance_id,
        "port": instance.port,
        "device": instance.device,
        "status": instance.status,
    }
    result["manager_api"] = {
        "available": True,
        "installed": installed,
        "queue": queue,
        "refreshed": bool(refresh),
    }
    return result


@router.post("/api/comfy/extensions/manage")
async def manage_extension(
    req: ManageExtensionRequest,
    *,
    _catalog_expected_nodes: bool = False,
):
    if comfy_manager.maintenance_reason:
        raise HTTPException(
            status_code=409,
            detail=f"ComfyUI maintenance in progress: {comfy_manager.maintenance_reason}",
        )
    action = str(req.action or "").strip().lower().replace("-", "_")
    if action not in _ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action; valid: {sorted(_ACTIONS)}")
    node = str(req.node or "").strip()
    repo_url = str(req.repo_url or "").strip()
    expected_nodes = _normalise_expected_nodes(req.expected_nodes)
    if action not in {"update_all"} and not (node or repo_url):
        raise HTTPException(status_code=400, detail="node or repo_url is required")
    if repo_url and (len(repo_url) > 500 or not _NODE_URL_RE.fullmatch(repo_url)):
        raise HTTPException(status_code=400, detail="Invalid extension repo_url")
    if expected_nodes and not req.dry_run and not req.auto_restart and action != "uninstall":
        raise HTTPException(
            status_code=400,
            detail="auto_restart=true is required to verify expected node classes",
        )
    timeout_s = max(30, min(int(req.timeout_s), 1800))
    instance = _ready_instance(req.instance_id)
    if instance is None:
        raise HTTPException(status_code=409, detail="Start a ComfyUI instance first")

    try:
        context = (
            await _update_all_context(instance)
            if action == "update_all"
            else await _operation_context(instance, action, node, repo_url)
        )
        queue = await _manager_request(
            instance, "GET", "/manager/queue/status", timeout=15.0,
        )
    except ManagerAPIError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if queue.get("is_processing"):
        raise HTTPException(status_code=409, detail="ComfyUI-Manager queue is busy")

    if action == "update_all":
        if not req.dry_run and not req.auto_restart:
            raise HTTPException(400, "update_all requires auto_restart to verify loaded node classes")
        before_nodes = await _manager_request(instance, "GET", "/object_info", timeout=60.0)
        if not isinstance(before_nodes, dict) or not before_nodes:
            raise HTTPException(502, "Cannot capture working node classes before update_all")
        context["body"]["expected_nodes_before"] = sorted(before_nodes)

    plan = {
        "action": action,
        "extension": context["key"],
        "instance_id": instance.instance_id,
        "snapshot": True,
        "auto_restart": bool(req.auto_restart),
        "timeout_s": timeout_s,
        "verification_mode": (
            "manager_inventory_and_object_info"
            if expected_nodes and action != "uninstall"
            else "manager_inventory"
        ),
        "expected_node_count": len(expected_nodes),
    }
    if action == "update_all":
        plan["extension_count"] = len(context["body"].get("items") or [])
    if req.dry_run:
        return {"status": "planned", "job_id": None, **plan}

    async def run(progress, cancel_event):
        current = _ready_instance(instance.instance_id)
        if current is None:  # pragma: no cover - guarded above and registry raises first
            raise ManagerAPIError("ComfyUI instance disappeared before the job started")
        snapshot_id = None
        queued = False
        progress(1, 5, "preflight complete")
        try:
            snapshot_id = await _save_snapshot(current)
            progress(2, 5, "recovery snapshot saved")
            queue_result = await _run_queue(
                current, action, context["body"], timeout_s, cancel_event,
            )
            queued = True
            progress(3, 5, "Manager operation complete")
            restarted = current
            if req.auto_restart:
                restarted = await _restart(current)
                progress(4, 5, "ComfyUI restarted")
            verification = await _verify(
                restarted,
                action,
                context["key"],
                context["body"],
                expected_nodes,
                catalog_expected_nodes=_catalog_expected_nodes,
            )
            progress(5, 5, "verified")
            return {
                **plan,
                "status": "done",
                "snapshot": snapshot_id,
                "queue": queue_result,
                "restart_required": not req.auto_restart,
                "instance_id": restarted.instance_id,
                "verification": verification,
            }
        except JobCancelled:
            raise
        except Exception as exc:
            recovery = {"attempted": False, "reason": "operation was not queued"}
            if queued or snapshot_id:
                _, recovery = await _restore_snapshot(current, snapshot_id)
            raise RuntimeError(f"{exc}; recovery={recovery}") from exc

    # The Manager preflight above awaits live HTTP calls. Recheck the core
    # maintenance gate before enqueuing so a core update cannot race it.
    if comfy_manager.maintenance_reason:
        raise HTTPException(
            status_code=409,
            detail=f"ComfyUI maintenance in progress: {comfy_manager.maintenance_reason}",
        )
    from routers.assets import _guard_asset_mutation
    _guard_asset_mutation()
    try:
        job = jobs.enqueue_callable(
            "comfy_extension",
            run,
            meta=plan,
            active_key="comfy:manager",
        )
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail="An extension operation is already running") from exc
    return {
        "status": "running",
        "job_id": job.job_id,
        "job_status_path": f"/api/jobs/{job.job_id}",
        **plan,
    }
