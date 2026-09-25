"""Root, health, session, and config endpoints.

Behavior is identical to the pre-split versions in omni_comfy_server.py.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import webbrowser
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import (
    BASE_DIR,
    CACHE_DIR,
    COMFYUI_MODELS_DIR,
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    LORA_COMPATIBLE_MODELS,
    MODELS_DIR,
    OMNI_DOWNLOAD_WORKERS,
    OMNI_MAX_CONCURRENT_DOWNLOADS,
    OMNI_MODEL_SETUP,
    OMNI_MODEL_VARIANTS,
    OMNI_OPENAI_ALIASES,
    OUTPUT_DIR,
    WORKFLOWS_DIR,
)
from state import comfy_manager, comfy_registry, get_api_token, worker_registry

router = APIRouter()

STATIC_DIR = BASE_DIR / "static"


class OpenUrlRequest(BaseModel):
    url: str
    browser: str | None = None


def _is_wsl() -> bool:
    try:
        return (
            os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
            or "microsoft" in platform.release().lower()
        )
    except Exception:
        return False


def _validate_external_url(raw: str) -> str:
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Only http(s) URLs can be opened")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Credentialed URLs are not allowed")
    return url


def _run_open(method: str, argv: list[str], failures: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        failures.append(f"{method}: {exc}")
        return False, method
    if result.returncode == 0:
        return True, method
    failures.append(f"{method}: exit {result.returncode}")
    return False, method


def _wsl_browser_commands(browser: str, url: str) -> list[tuple[str, list[str]]]:
    if browser == "chrome":
        return [
            ("chrome", ["/mnt/c/Program Files/Google/Chrome/Application/chrome.exe", url]),
            ("chrome-x86", ["/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe", url]),
            ("chrome-start", ["cmd.exe", "/C", "start", "", "chrome", url]),
        ]
    if browser == "edge":
        return [
            ("edge", ["/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", url]),
            ("edge-x64", ["/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe", url]),
            ("edge-start", ["cmd.exe", "/C", "start", "", "msedge", url]),
            ("edge-protocol", ["cmd.exe", "/C", "start", "", f"microsoft-edge:{url}"]),
        ]
    if browser == "firefox":
        return [
            ("firefox", ["/mnt/c/Program Files/Mozilla Firefox/firefox.exe", url]),
            ("firefox-x86", ["/mnt/c/Program Files (x86)/Mozilla Firefox/firefox.exe", url]),
            ("firefox-start", ["cmd.exe", "/C", "start", "", "firefox", url]),
        ]
    return []


def _open_system_browser(url: str, browser: str | None = None) -> tuple[bool, str]:
    browser = (browser or "default").strip().lower()
    if browser not in {"default", "chrome", "edge", "firefox"}:
        raise HTTPException(status_code=400, detail="Unknown browser choice")
    failures: list[str] = []

    if _is_wsl():
        if browser != "default":
            for method, argv in _wsl_browser_commands(browser, url):
                opened, used = _run_open(f"windows-{method}", argv, failures)
                if opened:
                    return opened, used
            return False, "; ".join(failures)[:200] if failures else "browser-not-found"

        # WSL interop: hand URLs to the user's Windows desktop session.
        # explorer.exe avoids cmd shell quoting; keep cmd/PowerShell fallbacks.
        if shutil.which("explorer.exe"):
            opened, method = _run_open("windows-explorer", ["explorer.exe", url], failures)
            if opened:
                return opened, method
        if shutil.which("cmd.exe"):
            opened, method = _run_open("windows-cmd-start", ["cmd.exe", "/C", "start", "", url], failures)
            if opened:
                return opened, method
        if shutil.which("powershell.exe"):
            opened, method = _run_open(
                "windows-powershell-start",
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Start-Process -FilePath (([uri]$args[0]).AbsoluteUri)",
                    url,
                ],
                failures,
            )
            if opened:
                return opened, method
    if browser == "default" and webbrowser.open_new_tab(url):
        return True, "system-default"
    if browser != "default":
        try:
            ctl = webbrowser.get(browser)
            if ctl.open_new_tab(url):
                return True, f"system-{browser}"
        except Exception as exc:  # noqa: BLE001
            failures.append(f"system-{browser}: {exc}")
    if failures:
        return False, "; ".join(failures)[:200]
    return False, "no-handler"


@router.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@router.get("/health")
async def health():
    return {"status": "ok", "service": "omni_studio"}


@router.post("/api/open-url")
async def open_url(req: OpenUrlRequest):
    """Open a trusted http(s) URL in the OS default browser when possible."""
    url = _validate_external_url(req.url)
    try:
        # WSL/Windows browser hand-off can wait on several 8-second process
        # fallbacks. Keep that OS integration off the gateway event loop so
        # health, cancellation, and state reads remain responsive.
        opened, method = await asyncio.to_thread(
            _open_system_browser, url, req.browser,
        )
    except Exception as e:
        return {"opened": False, "method": "fallback", "error": str(e)[:200]}
    return {"opened": opened, "method": method}


@router.get("/api/live")
async def live():
    return {"ok": True, "status": "ok", "app": "omni_studio"}


@router.get("/api/session")
async def session(request: Request):
    auth_mode = os.environ.get("OMNI_AUTH_MODE", "loopback-token").lower()
    # Prefer the actual request URL the browser hit (so the API access
    # card shows the right base when the gateway was started with a
    # non-default --port). Falls back to config defaults if unavailable.
    try:
        base = f"{request.url.scheme}://{request.url.netloc}"
    except (AttributeError, ValueError):
        base = f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}"
    payload = {
        "token": get_api_token(),
        "header": "X-Omni-Token",
        "auth_mode": auth_mode,
        "api_base_url": base,
        "openai_aliases": OMNI_OPENAI_ALIASES,
    }
    response = JSONResponse(payload)
    response.set_cookie(
        key="omni_session",
        value=get_api_token(),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@router.get("/api/config")
async def get_config():
    return {
        "models": OMNI_MODEL_SETUP,
        "variants": OMNI_MODEL_VARIANTS,
        "lora_compatible": sorted(LORA_COMPATIBLE_MODELS),
        "comfyui_installed": comfy_manager.is_installed(),
        "download_policy": {
            "cpu_workers": OMNI_DOWNLOAD_WORKERS,
            "cpu_basis": "one-third-of-available-cpus",
            "max_concurrent_downloads": OMNI_MAX_CONCURRENT_DOWNLOADS,
        },
    }


@router.get("/api/capabilities")
async def capabilities():
    """Lightweight machine-readable contract for peer apps and agents."""
    workers = worker_registry.to_dict_list()
    comfy_instances = comfy_registry.to_dict_list()
    try:
        workflows = comfy_manager.get_workflows()
    except Exception:
        workflows = []
    try:
        comfy_models = comfy_manager.get_installed_models()
    except Exception:
        comfy_models = {}
    comfy_model_counts = {
        str(category): len(items) if isinstance(items, list) else 0
        for category, items in (comfy_models or {}).items()
    }
    comfy_model_total = sum(comfy_model_counts.values())
    comfy_generation_ready = (
        comfy_manager.is_installed()
        and len(workflows) > 0
        and comfy_model_total > 0
    )
    readiness_blockers = []
    if not comfy_manager.is_installed():
        readiness_blockers.append("ComfyUI is not installed")
    if not workflows:
        readiness_blockers.append("No ComfyUI workflow files are available")
    if comfy_model_total <= 0:
        readiness_blockers.append("No ComfyUI model assets are installed")
    return {
        "app": "omni_studio",
        "title": "Omni Studio",
        "version": "1.0.0",
        "capabilities": [
            "image_generation",
            "video_generation",
            "audio_generation",
            "comfyui_workflows",
            "openai_compatible_chat",
            "model_worker_management",
        ],
        "status_endpoints": [
            "GET /health",
            "GET /api/live",
            "GET /api/capabilities",
            "GET /api/comfy/instances",
            "GET /api/workers",
            "GET /api/jobs/{job_id}",
        ],
        "model_endpoints": [
            "GET /api/config",
            "GET /api/setup/status",
            "POST /api/comfy/start",
            "POST /api/comfy/{instance_id}/stop",
            "POST /api/comfy/stop-all",
            "POST /api/comfy/installation/update",
            "POST /api/comfy/extensions/manage",
            "POST /api/registry/comfy/nodes/{node_id}/install",
            "POST /api/workers/spawn",
            "DELETE /api/workers/{worker_id}",
        ],
        "discovery_endpoints": [
            "GET /api/workflows/search",
            "POST /api/workflows/analyze",
            "POST /api/workflows/probe",
            "GET /api/workflows/{filename}/requirements",
            "GET /api/registry/comfy/installed-models/search",
            "GET /api/registry/comfy/manager-models/search",
            "GET /api/registry/comfy/models/search",
            "GET /api/registry/comfy/nodes/search",
            "GET /api/registry/comfy/nodes/{node_id}",
            "GET /api/assets/comfy/templates/requirements",
            "GET /api/assets/comfy/storage",
        ],
        "generation_endpoints": [
            "POST /api/workflows/run",
            "POST /api/workflows/{filename}/run",
            "GET /api/outputs",
            "POST /v1/chat/completions",
        ],
        "destructive_endpoints": [
            "POST /api/app/shutdown",
            "DELETE /api/workers/{worker_id}",
            "POST /api/workers/kill-all",
            "POST /api/comfy/{instance_id}/stop",
            "POST /api/comfy/stop-all",
            "POST /api/comfy/installation/update",
            "POST /api/comfy/extensions/manage",
            "POST /api/registry/comfy/nodes/{node_id}/install",
            "DELETE /api/assets/comfy/{category}/{filename}",
            "POST /api/shutdown",
            "POST /api/restart",
            "POST /api/system/shutdown",
            "POST /api/system/restart",
        ],
        "artifact_roots": {
            "cache": str(CACHE_DIR),
            "models": str(MODELS_DIR),
            "comfy_models": str(COMFYUI_MODELS_DIR),
            "output": str(OUTPUT_DIR),
            "workflows": str(WORKFLOWS_DIR),
        },
        "models": {
            "registered": sorted(OMNI_MODEL_SETUP.keys()),
            "openai_aliases": OMNI_OPENAI_ALIASES,
        },
        "readiness": {
            "comfyui_installed": comfy_manager.is_installed(),
            "workflow_count": len(workflows),
            "comfy_model_count": comfy_model_total,
            "comfy_model_counts": comfy_model_counts,
            "comfy_generation_ready": comfy_generation_ready,
            "blockers": readiness_blockers,
        },
        "loaded_resources": {
            "workers": workers,
            "comfy_instances": comfy_instances,
        },
        "policy": {
            "start_comfy_only_when_needed": True,
            "stop_comfy_after_artifact_capture": True,
            "use_workflows_for_repeatable_media_jobs": True,
            "downloads": {
                "cpu_workers": OMNI_DOWNLOAD_WORKERS,
                "cpu_basis": "one-third-of-available-cpus",
                "max_concurrent": OMNI_MAX_CONCURRENT_DOWNLOADS,
            },
        },
    }
