"""
Omni Studio API Server - Gateway for ComfyUI + Omni Models

The gateway runs on port 8200 and orchestrates:
- ComfyUI instance management (start/stop on different GPUs/ports)
- Omni model worker lifecycle (spawn/kill/health)
- Model installation and downloads
- Workflow management
- Static file serving for the GUI
- Multi-GPU support and load balancing

Run with: python omni_comfy_server.py --host 127.0.0.1 --port 8200

Phase 1 of the overhaul: this module is now a thin app factory. Concrete
endpoints live in ``routers/``; shared singletons live in ``state``; helpers
live in ``helpers``. Lifespan, signal handlers, and the auth middleware stay
here because they touch all routers.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from process_identity import process_alive

_BASE_DIR = Path(__file__).parent.resolve()
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import (
    BASE_DIR, CACHE_DIR, MODELS_DIR, OUTPUT_DIR, PID_DIR,
    WORKFLOWS_DIR, WORKER_LOG_DIR,
    DEFAULT_API_HOST, DEFAULT_API_PORT,
    setup_environment,
)
from helpers import prune_runtime_files
from key_store import api_keys
from metrics import metrics, render_prometheus
from security import is_loopback_peer, origin_matches_host, token_matches, required_scope
from state import (
    comfy_manager,
    comfy_registry,
    get_api_token,
    jobs,
    worker_manager,
    worker_registry,
)
from jobs import TERMINAL_STATES
from scheduler import get_scheduler
from routers import (
    ace_step as router_ace_step,
    assets as router_assets,
    audio as router_audio,
    audio_lab as router_audio_lab,
    chat_sessions_routes as router_chat_sessions,
    comfy as router_comfy,
    devices as router_devices,
    extensions as router_extensions,
    jobs_routes as router_jobs,
    keys as router_keys,
    logs as router_logs,
    maintenance_routes as router_maintenance,
    moss as router_moss,
    minimax_music3 as router_minimax_music3,
    outputs as router_outputs,
    previews as router_previews,
    registry as router_registry,
    sessions as router_sessions,
    setup as router_setup,
    workers as router_workers,
    workflows as router_workflows,
)


_REGISTRY_NAME = "omni_studio"


def _local_network_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
    except OSError:
        return ""
    if not host or host.startswith("127."):
        return ""
    return host


def _windows_localappdata_from_wsl() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local)
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "echo %LOCALAPPDATA%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            win_path = result.stdout.strip().rstrip("\r\n")
            if win_path and win_path != "%LOCALAPPDATA%" and len(win_path) >= 2 and win_path[1] == ":":
                drive = win_path[0].lower()
                rest = win_path[2:].replace("\\", "/").lstrip("/")
                path = Path(f"/mnt/{drive}/{rest}")
                if path.exists():
                    return path
    except Exception:
        pass
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username:
        path = Path("/mnt/c/Users") / username / "AppData" / "Local"
        if path.exists():
            return path
    return None


def _registry_dir() -> Path:
    local = _windows_localappdata_from_wsl()
    if local is not None:
        path = local / "AppHub" / "registry"
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(xdg) / "AppHub" / "registry"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _safe_load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _loopback_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def _pid_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    return process_alive(value)


def _endpoint_uses_port(value: object, port: int) -> bool:
    text = str(value or "")
    return text.endswith(f":{port}") or text.endswith(f":{port}/")


def _publish_discovery() -> None:
    host = os.environ.get("OMNI_API_HOST") or DEFAULT_API_HOST
    port = int(os.environ.get("OMNI_API_PORT") or DEFAULT_API_PORT)
    path = _registry_dir() / f"{_REGISTRY_NAME}.json"
    existing = _safe_load_json(path)
    existing_endpoints = dict(existing.get("endpoints") or {})
    endpoints = dict(existing_endpoints)
    gateway_url = f"http://127.0.0.1:{port}"
    gateway_web = f"http://127.0.0.1:{port}/"
    endpoints["gateway"] = gateway_url
    endpoints["gateway_web"] = gateway_web
    if not endpoints.get("api"):
        endpoints["api"] = gateway_url
    if not endpoints.get("web"):
        endpoints["web"] = gateway_web
    bridge_port = int(os.environ.get("OMNI_BRIDGE_PORT") or os.environ.get("BRIDGE_PORT") or 9200)
    bridge_alive = bridge_port != port and _loopback_port_open(bridge_port)
    if bridge_alive:
        endpoints["api"] = f"http://127.0.0.1:{bridge_port}"
        endpoints["web"] = f"http://127.0.0.1:{bridge_port}/"
        endpoints["bridge"] = f"http://127.0.0.1:{bridge_port}"
    wsl_host = _local_network_host()
    if not _ALLOW_REMOTE:
        for key in ("wsl_api", "wsl_web", "wsl_bridge", "wsl_gateway", "wsl_gateway_web"):
            endpoints.pop(key, None)
    if wsl_host and _ALLOW_REMOTE:
        endpoints["wsl_gateway"] = f"http://{wsl_host}:{port}"
        endpoints["wsl_gateway_web"] = f"http://{wsl_host}:{port}/"
        if not endpoints.get("wsl_api"):
            endpoints["wsl_api"] = f"http://{wsl_host}:{port}"
        if not endpoints.get("wsl_web"):
            endpoints["wsl_web"] = f"http://{wsl_host}:{port}/"
        if bridge_alive:
            endpoints["wsl_api"] = f"http://{wsl_host}:{bridge_port}"
            endpoints["wsl_web"] = f"http://{wsl_host}:{bridge_port}/"
            endpoints["wsl_bridge"] = f"http://{wsl_host}:{bridge_port}"
    extra = dict(existing.get("extra") or {})
    existing_pid = existing.get("pid")
    existing_primary_is_bridge = any(
        _endpoint_uses_port(existing_endpoints.get(key), bridge_port)
        for key in ("api", "web", "bridge", "wsl_api", "wsl_web", "wsl_bridge")
    )
    bridge_pid = extra.get("bridge_pid")
    if bridge_alive and not _pid_alive(bridge_pid) and existing_pid != os.getpid():
        if existing_primary_is_bridge and _pid_alive(existing_pid):
            bridge_pid = existing_pid
    if bridge_alive and _pid_alive(bridge_pid):
        extra["bridge_pid"] = int(bridge_pid)
    else:
        extra.pop("bridge_pid", None)
    extra.update({
        "wsl_distro": os.environ.get("WSL_DISTRO_NAME"),
        "host": host,
        "gateway_pid": os.getpid(),
        "api_port": port,
        "bridge_port": bridge_port if bridge_alive else None,
    })
    registry_pid = int(extra["bridge_pid"]) if extra.get("bridge_pid") else os.getpid()
    payload = {
        "name": _REGISTRY_NAME,
        "version": "1.0.0",
        "pid": registry_pid,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_dir": existing.get("app_dir") or str(BASE_DIR.parent.resolve()),
        "endpoints": endpoints,
        "auth": {
            "header": "X-Omni-Token",
            "session_endpoint": endpoints["api"].rstrip("/") + "/api/session",
        },
        "extra": extra,
    }
    _atomic_write_json(path, payload)
    logger.info("discovery registry published at %s", path)


def _unpublish_discovery() -> None:
    try:
        path = _registry_dir() / f"{_REGISTRY_NAME}.json"
        data = _safe_load_json(path)
        if not data:
            return
        extra = dict(data.get("extra") or {})
        if data.get("pid") != os.getpid() and extra.get("gateway_pid") != os.getpid():
            return
        endpoints = dict(data.get("endpoints") or {})
        port = int(os.environ.get("OMNI_API_PORT") or DEFAULT_API_PORT)
        gateway_urls = {
            f"http://127.0.0.1:{port}",
            f"http://127.0.0.1:{port}/",
        }
        for key in ("gateway", "gateway_web", "wsl_gateway", "wsl_gateway_web"):
            endpoints.pop(key, None)
        for key in ("api", "web", "wsl_api", "wsl_web"):
            value = str(endpoints.get(key) or "")
            if value in gateway_urls or _endpoint_uses_port(value, port):
                endpoints.pop(key, None)
        if endpoints:
            data["endpoints"] = endpoints
            extra.pop("gateway_pid", None)
            if extra.get("bridge_pid") and _pid_alive(extra.get("bridge_pid")):
                data["pid"] = extra["bridge_pid"]
            data["extra"] = extra
            _atomic_write_json(path, data)
        elif path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("could not unpublish discovery registry: %s", exc)

setup_environment()
MODELS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PID_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
_server_log_path = WORKER_LOG_DIR / "server_gateway.log"
_file_handler = RotatingFileHandler(
    str(_server_log_path),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger().addHandler(_file_handler)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def _kill_all_sync():
    """Hard-stop only processes tracked by this gateway.

    The bridge/full-app shutdown path owns the broad orphan and compatibility
    sweeps.  Repeating those scans here made an otherwise normal gateway
    restart wait through several per-process grace periods and could leave the
    bridge with a live-but-unreachable gateway process.  Registry entries and
    durable PID records are the authoritative in-gateway fallback.
    """
    for w in worker_registry.all_workers():
        try:
            if w.process and w.process.poll() is None:
                try:
                    pgid = os.getpgid(w.process.pid)
                    os.killpg(pgid, 9)
                except (OSError, ProcessLookupError):
                    w.process.kill()
                try:
                    w.process.wait(timeout=3)
                except Exception:
                    pass
        except Exception:
            pass

    for i in comfy_registry.all_instances():
        try:
            if i.process and i.process.poll() is None:
                try:
                    pgid = os.getpgid(i.process.pid)
                    os.killpg(pgid, 9)
                except (OSError, ProcessLookupError):
                    i.process.kill()
                try:
                    i.process.wait(timeout=3)
                except Exception:
                    pass
        except Exception:
            pass

    # Clean up or kill exact durable records left behind by a registry race.
    # This deliberately avoids manager construction and pgrep fallbacks; those
    # remain available to the bridge watchdog and omni_shutdown CLI.
    try:
        from omni_shutdown import _kill_pid_records  # noqa: WPS433

        _kill_pid_records("worker_*.json", unload_workers=False)
        _kill_pid_records("comfy_*.json")
    except Exception as exc:
        logger.warning("Exact PID-record cleanup failed during sync kill: %s", exc)
    for j in jobs.list():
        if j.status in TERMINAL_STATES:
            continue
        proc = j.process
        if not proc or proc.returncode is not None:
            continue
        try:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
        except Exception:
            pass


def _signal_handler(signum, frame):
    logger.info("Signal %d received - killing all processes...", signum)
    _kill_all_sync()
    sys.exit(0)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
async def _webhook_terminal_listener(job):
    """If the job carries ``meta.webhook_url``, POST a job summary on terminal.

    Best-effort: 5s timeout, exceptions swallowed by the JobStore listener
    runner. The body is the same shape as ``GET /api/jobs/{job_id}``.
    """
    url = (job.meta or {}).get("webhook_url")
    if not url:
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        return
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=5.0) as c:
            await c.post(url, json=job.to_dict())
    except _httpx.HTTPError as e:
        logger.warning("Job %s webhook %s failed: %s", job.job_id, url, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    prune_runtime_files()
    worker_manager.kill_orphan_workers()
    await comfy_manager.recover_instances()
    worker_manager.start_health_checks()
    comfy_manager.start_health_checks()
    jobs.add_terminal_listener(_webhook_terminal_listener)
    get_scheduler().start()
    try:
        _publish_discovery()
    except Exception as exc:
        logger.warning("could not publish discovery registry: %s", exc)
    yield
    _unpublish_discovery()
    await get_scheduler().stop()
    worker_manager.stop_health_checks()
    comfy_manager.stop_health_checks()
    await jobs.shutdown(timeout=5.0)
    await worker_manager.kill_all_workers()
    await comfy_manager.stop_all()
    _kill_all_sync()
    try:
        from omni_shutdown import GATEWAY_PID_FILE  # noqa: WPS433

        GATEWAY_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# Docs/schema endpoints are disabled by default: this is a single-user local
# appliance, not a public API, and an unauthenticated OpenAPI schema needlessly
# advertises the entire admin surface (GAT-3). Re-enable with
# OMNI_ENABLE_DOCS=1 for interactive development.
_ENABLE_DOCS = os.environ.get("OMNI_ENABLE_DOCS", "").strip().lower() in ("1", "true", "yes", "on")
app = FastAPI(
    title="Omni Studio",
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    """Time every request and record into the in-memory exporter."""
    import time as _time
    started = _time.monotonic()
    response = await call_next(request)
    elapsed = _time.monotonic() - started
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    try:
        metrics.record_request(
            path=request.url.path,
            method=request.method,
            status=response.status_code,
            elapsed=elapsed,
        )
    except Exception:  # noqa: BLE001
        pass
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static UI
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_AUTH_MODE = os.environ.get("OMNI_AUTH_MODE", "loopback-token").lower()
_ALLOW_REMOTE = os.environ.get("OMNI_API_ALLOW_REMOTE", "").strip().lower() in (
    "1", "true", "yes", "on")


def _extract_credential(request: Request) -> str | None:
    """Pull the candidate token/key from any of the supported headers/qparams."""
    provided = request.headers.get("x-omni-token") or request.cookies.get("omni_session")
    path = request.url.path
    # SSE and binary media GETs can't set custom headers from <img>/<video>/<audio>
    # tags, so allow ?token= on those specific paths. Loopback-only service.
    if path == "/api/logs/stream" or (
        request.method in ("GET", "HEAD")
        and (
            path.startswith("/api/outputs/")
            or path == "/api/outputs"
            # Audio Lab serves generated wavs that <audio> / <a download> need
            # to fetch without setting custom headers — same exception.
            or path.startswith("/api/audio_lab/outputs/")
            or path.startswith("/api/audio_lab/zip/")
            # ACE-Step outputs follow the same pattern.
            or path.startswith("/api/ace_step/outputs/")
            or path.startswith("/api/ace_step/zip/")
        )
    ):
        provided = provided or request.query_params.get("token")
    if path.startswith("/v1/") or _AUTH_MODE == "bearer":
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = provided or auth[7:].strip()
    return provided


@app.middleware("http")
async def require_local_session_token(request: Request, call_next):
    """Auth gate for /api/* (except /api/session) and /v1/*.

    Two modes:

    * ``loopback-token`` (default) -- the per-instance token.
    * ``bearer`` -- any registered key in ``api_keys`` works AND the
      loopback token still works (so the static UI keeps loading without
      operator action).
    """
    path = request.url.path
    origin = request.headers.get("origin")
    if origin and not origin_matches_host(origin, request.headers.get("host")):
        return JSONResponse({"detail": "Forbidden origin"}, status_code=403)

    # /api/session dispenses the per-instance token and is therefore exempt from
    # the token gate (the UI bootstraps with it). Restrict it to loopback peers
    # so a missing Origin header can't be used to lift the token from a
    # non-loopback caller. When the gateway is loopback-bound (the default) every
    # arriving peer is necessarily 127.0.0.1 — the kernel won't route a remote
    # source to a loopback socket — so this only bites when the operator has
    # deliberately exposed the gateway without opting into remote access.
    if path == "/api/session":
        peer = request.client.host if request.client else None
        if not is_loopback_peer(peer):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

    # Token-gate /api/* (except /api/session), /v1/*, and /metrics. /metrics
    # exposes operationally sensitive registry/worker/job data, so it is no
    # longer served unauthenticated.
    needs_auth = (
        (path.startswith("/api/") and path != "/api/session")
        or path.startswith("/v1/")
        or path == "/metrics"
    )
    if needs_auth:
        provided = _extract_credential(request)
        token_ok = token_matches(provided, get_api_token())
        key_ok = False
        if not token_ok and _AUTH_MODE == "bearer":
            matched = api_keys.authenticate(provided)
            if matched is not None:
                api_keys.touch_last_used(matched.id)
                request.state.api_key = matched
                request.state.scopes = list(matched.scopes)
                key_ok = True
        if token_ok:
            request.state.api_key = None
            request.state.scopes = ["admin"]
        if not (token_ok or key_ok):
            return JSONResponse({"detail": "Invalid or missing session token"},
                                status_code=401)
        scopes = request.state.scopes
        needed = required_scope(request.method, path, autospawn=request.query_params.get("autospawn", "").lower() in {"true", "1", "yes", "on", "t", "y"})
        if "admin" not in scopes and needed not in scopes:
            return JSONResponse({"detail": f"{needed} scope required"}, status_code=403)

    return await call_next(request)


# Wire routers (URLs and shapes are identical to the pre-split version).
app.include_router(router_sessions.router)
app.include_router(router_devices.router)
app.include_router(router_extensions.router)
app.include_router(router_comfy.router)
app.include_router(router_workflows.router)
app.include_router(router_chat_sessions.router)
app.include_router(router_workers.router)
app.include_router(router_audio.router)
app.include_router(router_moss.router)
app.include_router(router_audio_lab.router)
app.include_router(router_ace_step.router)
app.include_router(router_minimax_music3.router)
app.include_router(router_setup.router)
app.include_router(router_jobs.router)
app.include_router(router_keys.router)
app.include_router(router_outputs.router)
app.include_router(router_previews.router)
app.include_router(router_assets.router)
app.include_router(router_registry.router)
app.include_router(router_maintenance.router)
app.include_router(router_logs.router)


# ---------------------------------------------------------------------------
# Prometheus exposition. Token-gated like /api/* by the auth middleware (the
# registry/worker/job data it exposes is operationally sensitive); a local
# Prometheus scraper presents X-Omni-Token. The gateway also binds loopback by
# default, so /metrics is unreachable off-box without an explicit opt-in.
# ---------------------------------------------------------------------------
@app.get("/metrics")
async def prometheus_metrics():
    body = render_prometheus(
        jobs_store=jobs,
        worker_registry=worker_registry,
        comfy_registry=comfy_registry,
    )
    return PlainTextResponse(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_API_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    args = parser.parse_args()

    # Default to loopback (DEFAULT_API_HOST). Widen to all interfaces only when
    # the operator explicitly opts in, so the documented loopback trust boundary
    # (app_intents #13/#24) holds even though app.json no longer pins --host.
    host = args.host
    if _ALLOW_REMOTE and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"

    os.environ["OMNI_API_HOST"] = host
    os.environ["OMNI_API_PORT"] = str(args.port)
    logger.info("Omni Studio gateway starting on %s:%d", host, args.port)
    # Router and schema tests import this module. Only the actual gateway
    # process may replace the host's signal handlers and run shutdown cleanup.
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    uvicorn.run(app, host=host, port=args.port, log_level="info")
