"""Shared singletons for the Omni Studio gateway.

Routers import the registries, managers, API token getter, and the unified
``JobStore`` from here so they don't have to reach into ``omni_comfy_server``
(which would create a circular import once routers are wired in via
``app.include_router``).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from config import (
    API_TOKEN_FILE,
    COMFYUI_PORT_MAX, COMFYUI_PORT_MIN,
    OMNI_DOWNLOAD_WORKERS, OMNI_MAX_CONCURRENT_DOWNLOADS,
    WORKER_PORT_MAX, WORKER_PORT_MIN,
)
from security import get_or_create_token
from worker_registry import ComfyRegistry, WorkerRegistry
from worker_manager import WorkerManager
from comfy_manager import ComfyManager
from jobs import JobStore
from chat_sessions import chat_sessions  # noqa: F401 — re-exported for routers

SERVER_DIR = Path(__file__).parent.resolve()

worker_registry = WorkerRegistry(WORKER_PORT_MIN, WORKER_PORT_MAX)
comfy_registry = ComfyRegistry(COMFYUI_PORT_MIN, COMFYUI_PORT_MAX)
worker_manager = WorkerManager(worker_registry)
comfy_manager = ComfyManager(comfy_registry)

# Single unified job runner. Replaces the install-job dicts/sets that lived
# inline in omni_comfy_server.py before phase 2.
jobs = JobStore(
    download_cpu_count=OMNI_DOWNLOAD_WORKERS,
    max_concurrent_downloads=OMNI_MAX_CONCURRENT_DOWNLOADS,
)

_API_TOKEN: str | None = None
_API_TOKEN_LOCK = threading.Lock()


def get_api_token() -> str:
    """Read the per-instance session token, creating it on first call."""
    global _API_TOKEN
    if _API_TOKEN is None:
        # Double-checked locking: get_api_token() is called concurrently from
        # the async auth middleware (threadpool) and the lifespan startup, so
        # the check-then-set must be guarded to avoid two callers racing into
        # get_or_create_token().
        with _API_TOKEN_LOCK:
            if _API_TOKEN is None:
                _API_TOKEN = get_or_create_token(
                    API_TOKEN_FILE,
                    env_value=os.environ.get("OMNI_API_TOKEN"),
                )
    return _API_TOKEN
