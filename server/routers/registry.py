"""Registries: ComfyUI Manager node search + curated checkpoint catalog.

Two endpoints fed by external + built-in registries:

* ``GET /api/registry/comfy/nodes/search?q=...`` proxies the
  ComfyUI-Manager ``custom-node-list.json`` so callers can find a node
  without leaving the API. The upstream JSON is cached for 6 hours.

* ``GET /api/registry/checkpoints[?family=sdxl]`` returns a curated
  list of popular checkpoints with one-click install hints.

Both endpoints emit the exact ``repo`` / ``file`` strings the existing
``POST /api/assets/comfy/{category}/install`` and
``POST /api/assets/comfy/nodes/install`` routes accept, so callers can
chain ``search -> install`` without fishing for the right syntax.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path, PurePosixPath
import re
import threading
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from config import ASSET_WEIGHT_EXTS, COMFYUI_DIR, COMFYUI_MODEL_CATEGORIES
from state import comfy_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_MANAGER_LIST_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/"
    "main/custom-node-list.json"
)
_MANAGER_MAP_URL = (
    "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/"
    "main/extension-node-map.json"
)
_MANAGER_CACHE_TTL_S = 6 * 3600

_cache_lock = threading.Lock()
_cache: dict = {"fetched_at": 0.0, "data": None, "error": None, "source": None}
_map_cache: dict = {"fetched_at": 0.0, "data": None, "error": None, "source": None}
_MANAGER_LOCAL_DIR = COMFYUI_DIR / "custom_nodes" / "ComfyUI-Manager"
_COMFY_CATEGORIES = frozenset(COMFYUI_MODEL_CATEGORIES)
_WEIGHT_EXTS = frozenset(ext.lower() for ext in ASSET_WEIGHT_EXTS)
_HF_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


# ---------------------------------------------------------------------------
# Built-in checkpoint catalog. Trimmed to entries that work out-of-the-box
# with the comfy-asset arm of install_model.sh — i.e. single-file downloads
# from public HF repos that don't need a license click-through.
# ---------------------------------------------------------------------------
CHECKPOINT_CATALOG: list[dict] = [
    # --- SDXL ---
    {
        "id": "sdxl-base-1.0", "family": "sdxl", "category": "checkpoints",
        "display": "Stable Diffusion XL Base 1.0",
        "repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "file": "sd_xl_base_1.0.safetensors",
        "size_gb": 6.9, "license": "openrail++",
    },
    {
        "id": "sdxl-refiner-1.0", "family": "sdxl", "category": "checkpoints",
        "display": "SDXL Refiner 1.0",
        "repo": "stabilityai/stable-diffusion-xl-refiner-1.0",
        "file": "sd_xl_refiner_1.0.safetensors",
        "size_gb": 6.0, "license": "openrail++",
    },
    {
        "id": "sdxl-turbo", "family": "sdxl", "category": "checkpoints",
        "display": "SDXL Turbo (1-4 step distill)",
        "repo": "stabilityai/sdxl-turbo",
        "file": "sd_xl_turbo_1.0_fp16.safetensors",
        "size_gb": 6.9, "license": "stabilityai-non-commercial",
    },
    # --- SD 1.5 ---
    {
        "id": "sd-1.5", "family": "sd15", "category": "checkpoints",
        "display": "Stable Diffusion 1.5 (RunwayML)",
        "repo": "runwayml/stable-diffusion-v1-5",
        "file": "v1-5-pruned-emaonly.safetensors",
        "size_gb": 4.0, "license": "openrail",
    },
    # --- SD 3 ---
    {
        "id": "sd3-medium", "family": "sd3", "category": "checkpoints",
        "display": "Stable Diffusion 3 Medium",
        "repo": "stabilityai/stable-diffusion-3-medium",
        "file": "sd3_medium.safetensors",
        "size_gb": 4.3, "license": "stabilityai-non-commercial",
    },
    # --- Flux ---
    {
        "id": "flux1-schnell", "family": "flux", "category": "checkpoints",
        "display": "FLUX.1 schnell (4-step)",
        "repo": "black-forest-labs/FLUX.1-schnell",
        "file": "flux1-schnell.safetensors",
        "size_gb": 23.8, "license": "apache-2.0",
    },
    {
        "id": "flux1-dev", "family": "flux", "category": "checkpoints",
        "display": "FLUX.1 dev",
        "repo": "black-forest-labs/FLUX.1-dev",
        "file": "flux1-dev.safetensors",
        "size_gb": 23.8, "license": "non-commercial",
    },
    # --- VAEs ---
    {
        "id": "sdxl-vae", "family": "sdxl", "category": "vae",
        "display": "SDXL VAE (fp16)",
        "repo": "madebyollin/sdxl-vae-fp16-fix",
        "file": "sdxl_vae.safetensors",
        "size_gb": 0.32, "license": "openrail",
    },
    # --- Upscalers ---
    {
        "id": "ultrasharp", "family": "upscaler", "category": "upscale_models",
        "display": "4x-UltraSharp",
        "repo": "Kim2091/UltraSharp",
        "file": "4x-UltraSharp.pth",
        "size_gb": 0.067, "license": "mit",
    },
    {
        "id": "realesrgan-x4plus", "family": "upscaler", "category": "upscale_models",
        "display": "RealESRGAN x4plus",
        "repo": "ai-forever/Real-ESRGAN",
        "file": "RealESRGAN_x4plus.pth",
        "size_gb": 0.067, "license": "bsd-3",
    },
]


@router.get("/api/registry/checkpoints")
async def list_checkpoint_catalog(
    family: str | None = Query(default=None,
                                description="Filter: sdxl | sd15 | sd3 | flux | upscaler"),
    category: str | None = Query(default=None,
                                  description="Filter: checkpoints | vae | upscale_models | ..."),
):
    """Curated checkpoint catalog. Filter by family or category."""
    items = [c for c in CHECKPOINT_CATALOG
             if (family is None or c["family"] == family)
             and (category is None or c["category"] == category)]
    return {
        "checkpoints": items,
        "total": len(items),
        "families": sorted({c["family"] for c in CHECKPOINT_CATALOG}),
        "categories": sorted({c["category"] for c in CHECKPOINT_CATALOG}),
    }


def _infer_model_category(
    filename: str,
    tags: list[str],
    repo_id: str = "",
) -> tuple[str, str]:
    path = filename.replace("\\", "/")
    parts = [part.lower() for part in path.split("/") if part]
    for part in reversed(parts[:-1]):
        if part in _COMFY_CATEGORIES:
            return part, "repository path"
    path_aliases = {
        "checkpoint": "checkpoints",
        "checkpoints": "checkpoints",
        "control_net": "controlnet",
        "controlnet": "controlnet",
        "lora": "loras",
        "loras": "loras",
        "text_encoder": "text_encoders",
        "text_encoders": "text_encoders",
        "clip": "text_encoders",
        "clip_vision": "clip_vision",
        "unet": "diffusion_models",
        "transformer": "diffusion_models",
        "vae": "vae",
    }
    for part in reversed(parts[:-1]):
        normalized_part = re.sub(r"[_-]?\d+$", "", re.sub(r"[\s.-]+", "_", part))
        if normalized_part in path_aliases:
            return path_aliases[normalized_part], "repository component path"
    name = Path(path).name.lower()
    joined_tags = " ".join(str(tag).lower() for tag in tags)
    hints = (
        ("controlnet", ("controlnet", "control_net")),
        ("loras", ("lora", "lycoris")),
        ("vae", ("vae",)),
        ("text_encoders", ("text_encoder", "text-encoder", "t5", "clip_l", "clip_g")),
        ("clip_vision", ("clip_vision", "clip-vision")),
        ("upscale_models", ("upscal", "realesrgan", "esrgan")),
        ("embeddings", ("embedding", "textual_inversion")),
        ("diffusion_models", ("diffusion_model", "transformer", "unet", "krea2", "krea-2")),
    )
    combined = f"{name} {joined_tags} {str(repo_id or '').lower()}"
    for category, needles in hints:
        if any(needle in combined for needle in needles):
            return category, "filename/tags"
    if name.endswith(".gguf"):
        return "gguf", "file extension"
    if len(parts) > 1:
        return "unknown", "unmapped repository component path"
    return "checkpoints", "fallback"


def _search_comfy_models_sync(
    query: str,
    category: str | None,
    repo_limit: int,
    file_limit: int,
) -> dict:
    from huggingface_hub import HfApi

    api = HfApi()
    repositories = list(api.list_models(
        search=query,
        limit=repo_limit,
        sort="downloads",
        direction=-1,
    ))
    candidates: list[dict] = []
    warnings: list[str] = []
    for model in repositories:
        repo_id = str(getattr(model, "id", None) or getattr(model, "modelId", None) or "")
        if not repo_id:
            continue
        try:
            info = api.model_info(repo_id, files_metadata=True)
        except Exception as exc:
            warnings.append(f"{repo_id}: {str(exc)[:160]}")
            continue
        tags = [str(tag) for tag in (getattr(info, "tags", None) or getattr(model, "tags", None) or [])]
        revision = str(getattr(info, "sha", None) or "main")
        downloads = int(getattr(model, "downloads", 0) or 0)
        likes = int(getattr(model, "likes", 0) or 0)
        for sibling in getattr(info, "siblings", None) or []:
            filename = str(getattr(sibling, "rfilename", None) or "")
            if Path(filename).suffix.lower() not in _WEIGHT_EXTS:
                continue
            inferred, reason = _infer_model_category(filename, tags, repo_id)
            if category and inferred != category:
                continue
            size = getattr(sibling, "size", None)
            lfs = getattr(sibling, "lfs", None)
            if size is None and isinstance(lfs, dict):
                size = lfs.get("size")
            candidates.append({
                "repo": repo_id,
                "file": filename,
                "name": Path(filename).name,
                "category": inferred,
                "category_reason": reason,
                "category_confident": reason != "fallback",
                "revision": revision,
                "size_bytes": int(size) if isinstance(size, (int, float)) else None,
                "downloads": downloads,
                "likes": likes,
                "tags": tags[:20],
                "url": f"https://huggingface.co/{repo_id}/resolve/{revision}/{filename}",
            })
    query_lower = query.lower()
    candidates.sort(key=lambda item: (
        0 if item["name"].lower() == query_lower else 1,
        0 if query_lower in item["name"].lower() else 1,
        0 if item["category_confident"] else 1,
        -item["downloads"],
        item["repo"].lower(),
        item["file"].lower(),
    ))
    return {
        "query": query,
        "category": category,
        "repositories_scanned": len(repositories),
        "candidates": candidates[:file_limit],
        "total_candidates": len(candidates),
        "warnings": warnings[:20],
        "source": "live-huggingface",
    }


@router.get("/api/registry/comfy/models/search")
async def search_comfy_models(
    q: str = Query(min_length=2, max_length=160),
    category: str | None = Query(default=None),
    repo_limit: int = Query(default=8, ge=1, le=20),
    file_limit: int = Query(default=50, ge=1, le=100),
):
    """Discover actionable ComfyUI weight files from live Hugging Face data."""
    query = q.strip()
    if category and category not in _COMFY_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI category: {category}")
    try:
        return await asyncio.to_thread(
            _search_comfy_models_sync, query, category, repo_limit, file_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Hugging Face model discovery failed: {exc}")


def _inspect_comfy_model_repo_sync(repo_id: str, category: str | None, file_limit: int) -> dict:
    """List actionable weight files from one exact Hugging Face repository."""
    from huggingface_hub import HfApi

    info = HfApi().model_info(repo_id, files_metadata=True)
    tags = [str(tag) for tag in (getattr(info, "tags", None) or [])]
    revision = str(getattr(info, "sha", None) or "main")
    candidates: list[dict] = []
    for sibling in getattr(info, "siblings", None) or []:
        filename = str(getattr(sibling, "rfilename", None) or "")
        if Path(filename).suffix.lower() not in _WEIGHT_EXTS:
            continue
        inferred, reason = _infer_model_category(filename, tags, repo_id)
        if category and inferred != category:
            continue
        size = getattr(sibling, "size", None)
        lfs = getattr(sibling, "lfs", None)
        if size is None and isinstance(lfs, dict):
            size = lfs.get("size")
        candidates.append({
            "repo": repo_id,
            "file": filename,
            "name": Path(filename).name,
            "category": inferred,
            "category_reason": reason,
            "category_confident": reason != "fallback",
            "revision": revision,
            "size_bytes": int(size) if isinstance(size, (int, float)) else None,
            "tags": tags[:20],
            "url": f"https://huggingface.co/{repo_id}/resolve/{revision}/{filename}",
        })
    candidates.sort(key=lambda item: (item["category"], item["file"].lower()))
    return {
        "repo": repo_id,
        "category": category,
        "revision": revision,
        "candidates": candidates[:file_limit],
        "total_candidates": len(candidates),
        "source": "exact-huggingface-repository",
    }


@router.get("/api/registry/comfy/models/repository")
async def inspect_comfy_model_repository(
    repo: str = Query(min_length=3, max_length=193),
    category: str | None = Query(default=None),
    file_limit: int = Query(default=100, ge=1, le=500),
):
    """Inspect an exact repository when Hub free-text search is insufficient."""
    repo_id = repo.strip()
    if not _HF_REPO_RE.fullmatch(repo_id):
        raise HTTPException(status_code=400, detail="Invalid Hugging Face repository id")
    if category and category not in _COMFY_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI category: {category}")
    try:
        return await asyncio.to_thread(
            _inspect_comfy_model_repo_sync, repo_id, category, file_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Hugging Face repository inspection failed: {exc}")


def _manager_model_rows() -> list[dict]:
    path = COMFYUI_DIR / "custom_nodes" / "ComfyUI-Manager" / "model-list.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = payload.get("models") if isinstance(payload, dict) else []
    return [dict(item) for item in rows or [] if isinstance(item, dict)]


def _installed_model_names() -> dict[str, set[str]]:
    return {
        category: {str(item.get("name") or "").casefold() for item in items}
        for category, items in comfy_manager.get_installed_models().items()
    }


def _manager_save_path_parts(value: str) -> tuple[str, ...]:
    """Return a safe Manager model path relative to ``models/``.

    Manager intentionally uses nested destinations such as
    ``checkpoints/SDXL``.  Keep those paths intact while rejecting absolute
    paths and traversal segments before they are used for inventory matching.
    """
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ()
    return tuple(path.parts)


def _manager_model_is_installed(
    save_path: str,
    filename: str,
    installed: dict[str, set[str]],
) -> bool:
    """Match Manager's nested catalog destinations to the recursive inventory.

    A model already installed at ``models/checkpoints/foo.safetensors`` should
    not be offered again merely because Manager recommends
    ``models/checkpoints/SDXL/foo.safetensors``.  Exact relative paths win, and
    a basename match inside the same top-level Comfy category prevents a large
    duplicate download when users organise files differently.
    """
    parts = _manager_save_path_parts(save_path)
    name = Path(str(filename or "")).name
    if not parts or not name or name != str(filename or ""):
        return False

    top_level = parts[0]
    wanted_relative = PurePosixPath(*parts[1:], name).as_posix().casefold()
    wanted_name = name.casefold()
    for current in installed.get(top_level, set()):
        normalized = str(current or "").replace("\\", "/").strip("/").casefold()
        if normalized == wanted_relative or PurePosixPath(normalized).name == wanted_name:
            return True

    # Some Manager destinations belong to custom-node model families that are
    # not part of ComfyUI's standard category catalog.  Exact filesystem
    # detection still prevents duplicates for those allowlisted entries.
    target = COMFYUI_DIR / "models" / Path(*parts) / name
    return target.is_file()


@router.get("/api/registry/comfy/manager-models/search")
async def search_manager_models(
    q: str = Query(default="", max_length=160),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """Search ComfyUI-Manager's current downloadable model catalog."""
    if category and category not in _COMFY_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI category: {category}")
    needle = str(q or "").strip().casefold()
    installed = _installed_model_names()
    results: list[dict] = []
    for item in _manager_model_rows():
        save_path = str(item.get("save_path") or "").strip()
        filename = str(item.get("filename") or "").strip()
        save_parts = _manager_save_path_parts(save_path)
        category_root = save_parts[0] if save_parts else ""
        if category and category_root != category:
            continue
        searchable = " ".join(str(item.get(key) or "") for key in (
            "name", "filename", "description", "type", "base", "save_path",
        )).casefold()
        if needle and needle not in searchable:
            continue
        results.append({
            "name": str(item.get("name") or filename),
            "filename": filename,
            "category": save_path,
            "category_root": category_root,
            "type": str(item.get("type") or ""),
            "base": str(item.get("base") or ""),
            "description": str(item.get("description") or "")[:800],
            "reference": str(item.get("reference") or ""),
            "url": str(item.get("url") or ""),
            "size": str(item.get("size") or ""),
            "installed": _manager_model_is_installed(save_path, filename, installed),
            "source": "comfyui-manager",
            "install_selector": {
                "name": str(item.get("name") or filename),
                "filename": filename,
                "base": str(item.get("base") or ""),
                "save_path": save_path,
            },
        })
        if len(results) >= limit:
            break
    return {
        "query": str(q or "").strip(),
        "category": category,
        "models": results,
        "total": len(results),
        "source": "comfyui-manager",
        "catalog_available": bool(_manager_model_rows()),
    }


@router.get("/api/registry/comfy/installed-models/search")
async def search_installed_models(
    q: str = Query(default="", max_length=160),
    category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
):
    """Search the live filesystem-backed ComfyUI model inventory."""
    if category and category not in _COMFY_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI category: {category}")
    needle = str(q or "").strip().casefold()
    results = []
    for current_category, items in comfy_manager.get_installed_models().items():
        if category and current_category != category:
            continue
        for item in items:
            name = str(item.get("name") or "")
            if needle and needle not in f"{current_category} {name}".casefold():
                continue
            results.append({
                "name": name,
                "category": current_category,
                "size_mb": item.get("size_mb", 0),
                "installed": True,
                "source": "installed-filesystem",
            })
            if len(results) >= limit:
                return {"query": str(q or "").strip(), "models": results, "total": len(results)}
    return {"query": str(q or "").strip(), "models": results, "total": len(results)}


# ---------------------------------------------------------------------------
# ComfyUI Manager node-list passthrough
# ---------------------------------------------------------------------------
def _read_local_manager_catalog(filename: str) -> dict:
    path = _MANAGER_LOCAL_DIR / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{filename} must contain a JSON object")
    return data


async def _fetch_manager_list_async() -> dict:
    """Fetch upstream once per cache window.

    On error we keep returning the previously-fetched data (if any) and
    surface the error to the caller separately so a transient GitHub
    outage doesn't break ``search``.
    """
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["fetched_at"]) < _MANAGER_CACHE_TTL_S:
            return _cache
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(_MANAGER_LIST_URL,
                             headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        error = str(e)
        with _cache_lock:
            _cache["error"] = error
            has_stale_data = _cache["data"] is not None
        if not has_stale_data:
            try:
                local_data = await asyncio.to_thread(
                    _read_local_manager_catalog, "custom-node-list.json",
                )
            except (OSError, ValueError) as local_exc:
                with _cache_lock:
                    _cache["error"] = f"{error}; local fallback failed: {local_exc}"
            else:
                with _cache_lock:
                    _cache.update({
                        "fetched_at": now,
                        "data": local_data,
                        "error": f"live registry unavailable; using Manager local catalog: {error}",
                        "source": "local-manager-fallback",
                    })
        return _cache
    with _cache_lock:
        _cache["fetched_at"] = now
        _cache["data"] = data
        _cache["error"] = None
        _cache["source"] = "live-manager-registry"
    return _cache


def _normalise_repo_url(value: str) -> str:
    return str(value or "").strip().lower().rstrip("/").removesuffix(".git")


def _mapping_index(mappings: dict) -> dict[str, tuple[list[str], dict]]:
    """Index Manager's repository map once instead of rescanning it per row."""
    index: dict[str, tuple[list[str], dict]] = {}
    for key, raw in mappings.items():
        normalized = _normalise_repo_url(key)
        if not normalized or not isinstance(raw, list) or not raw:
            continue
        nodes = raw[0] if isinstance(raw[0], list) else []
        meta = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
        index[normalized] = (
            [str(item) for item in nodes if str(item).strip()],
            meta,
        )
    return index


def _mapping_for_entry(
    entry: dict,
    mappings: dict,
    index: dict[str, tuple[list[str], dict]] | None = None,
) -> tuple[list[str], dict]:
    aliases = {
        _normalise_repo_url(entry.get("reference")),
        *(_normalise_repo_url(item) for item in (entry.get("files") or [])),
    }
    aliases.discard("")
    lookup = index if index is not None else _mapping_index(mappings)
    for alias in aliases:
        if alias in lookup:
            return lookup[alias]
    return [], {}


def _node_record(
    entry: dict,
    mappings: dict | None = None,
    mapping_index: dict[str, tuple[list[str], dict]] | None = None,
) -> dict:
    """Normalise one upstream entry to a stable shape."""
    files = entry.get("files") or []
    repo_url = str(files[0] if files else entry.get("reference") or "").strip()
    node_id = str(entry.get("id") or "").strip()
    normalized_repo = _normalise_repo_url(repo_url or entry.get("reference"))
    catalog_key = node_id or (
        "repo-" + hashlib.sha256(normalized_repo.encode("utf-8")).hexdigest()[:16]
        if normalized_repo else ""
    )
    provided_nodes, mapping_meta = _mapping_for_entry(
        entry, mappings or {}, mapping_index,
    )
    installable = bool(catalog_key and (node_id or repo_url))
    record = {
        "id": node_id,
        "catalog_key": catalog_key,
        "title": entry.get("title", ""),
        "author": entry.get("author", ""),
        "description": entry.get("description", "")[:600],
        "repo_url": repo_url,
        "reference": entry.get("reference", ""),
        "tags": entry.get("tags") or [],
        "install_type": entry.get("install_type", "git-clone"),
        "provided_nodes": provided_nodes[:500],
        "provided_node_count": len(provided_nodes),
        "provided_nodes_truncated": len(provided_nodes) > 500,
        "node_name_pattern": entry.get("nodename_pattern", ""),
        "mapping_title": mapping_meta.get("title_aux", ""),
        "installable": installable,
    }
    if installable:
        record["install"] = {
            "method": "POST",
            "path": f"/api/registry/comfy/nodes/{quote(catalog_key, safe='')}/install",
            "body": {
                "dry_run": True,
                "auto_restart": True,
                "timeout_s": 600,
            },
        }
        manager_body = {
            "action": "install",
            "node": node_id or catalog_key,
            "dry_run": True,
            "auto_restart": True,
            "timeout_s": 600,
        }
        if repo_url:
            manager_body["repo_url"] = repo_url
        record["manager_install_request"] = {
            "method": "POST",
            "path": "/api/comfy/extensions/manage",
            "body": manager_body,
        }
    return record


async def _fetch_manager_map_async() -> dict:
    now = time.time()
    with _cache_lock:
        if _map_cache["data"] is not None and (now - _map_cache["fetched_at"]) < _MANAGER_CACHE_TTL_S:
            return _map_cache
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(_MANAGER_MAP_URL, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("extension-node-map must be an object")
    except (httpx.HTTPError, ValueError) as exc:
        error = str(exc)
        with _cache_lock:
            _map_cache["error"] = error
            has_stale_data = _map_cache["data"] is not None
        if not has_stale_data:
            try:
                local_data = await asyncio.to_thread(
                    _read_local_manager_catalog, "extension-node-map.json",
                )
            except (OSError, ValueError) as local_exc:
                with _cache_lock:
                    _map_cache["error"] = f"{error}; local fallback failed: {local_exc}"
            else:
                with _cache_lock:
                    _map_cache.update({
                        "fetched_at": now,
                        "data": local_data,
                        "error": f"live node map unavailable; using Manager local map: {error}",
                        "source": "local-manager-fallback",
                    })
        return _map_cache
    with _cache_lock:
        _map_cache.update({
            "fetched_at": now,
            "data": data,
            "error": None,
            "source": "live-manager-registry",
        })
    return _map_cache


def _catalog_revision(records: list[dict]) -> str:
    identity = [
        {
            "key": row.get("catalog_key") or "",
            "id": row.get("id") or "",
            "repo": _normalise_repo_url(row.get("repo_url") or row.get("reference")),
            "title": row.get("title") or "",
        }
        for row in records
    ]
    payload = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


async def _manager_catalog_snapshot(*, refresh: bool = False) -> dict:
    if refresh:
        with _cache_lock:
            _cache["fetched_at"] = 0.0
            _map_cache["fetched_at"] = 0.0
    cache, map_cache = await asyncio.gather(
        _fetch_manager_list_async(), _fetch_manager_map_async(),
    )
    data = cache["data"]
    err = cache["error"]
    if data is None:
        raise HTTPException(
            status_code=502,
            detail=f"ComfyUI Manager registry unreachable: {err}",
        )
    raw_items = data.get("custom_nodes") or data.get("nodes") or []
    items = [item for item in raw_items if isinstance(item, dict)]
    mappings = map_cache.get("data") if isinstance(map_cache.get("data"), dict) else {}
    mapping_index = _mapping_index(mappings)
    records = [_node_record(item, mappings, mapping_index) for item in items]
    records.sort(key=lambda row: (
        str(row.get("title") or "").casefold(),
        str(row.get("id") or "").casefold(),
        _normalise_repo_url(row.get("repo_url") or row.get("reference")),
    ))
    revision = _catalog_revision(records)
    for row in records:
        install = row.get("install")
        if isinstance(install, dict) and isinstance(install.get("body"), dict):
            install["body"]["catalog_revision"] = revision
    return {
        "records": records,
        "revision": revision,
        "fetched_at": cache["fetched_at"],
        "warning": err or map_cache.get("error"),
        "source": cache.get("source") or "memory-cache",
        "mapping_source": map_cache.get("source"),
    }


def _record_matches_query(row: dict, query: str) -> bool:
    needle = query.casefold()
    if not needle:
        return True
    fields = (
        row.get("title"), row.get("description"), row.get("author"),
        row.get("id"), row.get("catalog_key"), row.get("repo_url"),
        row.get("reference"), row.get("mapping_title"),
        row.get("node_name_pattern"),
    )
    return (
        any(needle in str(value or "").casefold() for value in fields)
        or any(needle in str(tag or "").casefold() for tag in row.get("tags") or [])
        or any(needle in str(node or "").casefold()
               for node in row.get("provided_nodes") or [])
    )


def _require_catalog_revision(snapshot: dict, requested: str | None) -> None:
    if requested and requested != snapshot["revision"]:
        raise HTTPException(status_code=409, detail={
            "message": "ComfyUI-Manager catalog changed; restart pagination from offset 0",
            "requested_revision": requested,
            "current_revision": snapshot["revision"],
        })


def _find_catalog_record(snapshot: dict, selector: str) -> dict:
    wanted = str(selector or "").strip().casefold()
    matches = [
        row for row in snapshot["records"]
        if wanted in {
            str(row.get("catalog_key") or "").casefold(),
            str(row.get("id") or "").casefold(),
        }
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Manager catalog node not found")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail={
            "message": "Manager catalog selector is ambiguous",
            "candidates": [
                {
                    "catalog_key": row.get("catalog_key"),
                    "id": row.get("id"),
                    "repo_url": row.get("repo_url"),
                }
                for row in matches[:20]
            ],
        })
    return matches[0]


class InstallCatalogNodeRequest(BaseModel):
    instance_id: str | None = Field(default=None, max_length=160)
    dry_run: bool = True
    auto_restart: bool = True
    timeout_s: int = Field(default=600, ge=30, le=1800)
    catalog_revision: str | None = Field(default=None, max_length=64)


@router.get("/api/registry/comfy/nodes/search")
async def search_comfy_nodes(
    q: str = Query(
        default="",
        max_length=160,
        description="Substring match across metadata and advertised node classes",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    catalog_revision: str | None = Query(
        default=None,
        max_length=64,
        description="Revision returned by the first page; rejects changed catalogs",
    ),
    refresh: bool = Query(default=False, description="Bypass the 6h cache"),
):
    """Search ComfyUI-Manager with stable, gap-free offset pagination.

    The full list is cached server-side; ``q`` is matched in-memory so a
    typo doesn't cost a round-trip. Pass the first page's ``catalog_revision``
    on later pages to detect a catalog refresh instead of silently skipping or
    duplicating entries. ``refresh=true`` forces a fetch and a new revision.
    """
    snapshot = await _manager_catalog_snapshot(refresh=refresh)
    _require_catalog_revision(snapshot, catalog_revision)
    query = str(q or "").strip()
    matched = [
        row for row in snapshot["records"]
        if _record_matches_query(row, query)
    ]
    out = matched[offset:offset + limit]
    next_offset = offset + len(out)
    has_more = next_offset < len(matched)
    return {
        "nodes": out,
        "query": query,
        "catalog_total": len(snapshot["records"]),
        "catalog_installable_total": sum(
            1 for row in snapshot["records"] if row.get("installable")
        ),
        "total": len(matched),
        "returned": len(out),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": next_offset if has_more else None,
        "catalog_revision": snapshot["revision"],
        "fetched_at": snapshot["fetched_at"],
        "stale_seconds": time.time() - snapshot["fetched_at"],
        "warning": snapshot["warning"],
        "source": snapshot.get("source"),
        "mapping_source": snapshot.get("mapping_source"),
    }


@router.get("/api/registry/comfy/nodes/{node_id}")
async def get_comfy_catalog_node(
    node_id: str,
    catalog_revision: str | None = Query(default=None, max_length=64),
    refresh: bool = Query(default=False),
):
    snapshot = await _manager_catalog_snapshot(refresh=refresh)
    _require_catalog_revision(snapshot, catalog_revision)
    return {
        "node": _find_catalog_record(snapshot, node_id),
        "catalog_revision": snapshot["revision"],
        "fetched_at": snapshot["fetched_at"],
        "warning": snapshot["warning"],
        "source": snapshot.get("source"),
        "mapping_source": snapshot.get("mapping_source"),
    }


@router.post("/api/registry/comfy/nodes/{node_id}/install")
async def install_comfy_catalog_node(node_id: str, req: InstallCatalogNodeRequest):
    """Resolve one cached catalog entry, then use the guarded Manager installer.

    The delegated lifecycle creates a snapshot, queues the install, restarts
    ComfyUI, checks Manager's installed inventory, and verifies mapped node
    classes against live ``/object_info`` before the job can succeed.
    """
    snapshot = await _manager_catalog_snapshot(refresh=False)
    _require_catalog_revision(snapshot, req.catalog_revision)
    record = _find_catalog_record(snapshot, node_id)
    if not record.get("installable"):
        raise HTTPException(status_code=409, detail="Catalog entry is not installable")
    if not req.auto_restart and not req.dry_run:
        raise HTTPException(
            status_code=400,
            detail="Catalog installation requires auto_restart=true for live verification",
        )

    # Lazy import avoids a router import cycle at process startup.
    from routers.extensions import ManageExtensionRequest, manage_extension

    result = await manage_extension(ManageExtensionRequest(
        action="install",
        instance_id=req.instance_id,
        node=str(record.get("id") or record.get("catalog_key") or ""),
        repo_url=str(record.get("repo_url") or ""),
        dry_run=req.dry_run,
        auto_restart=req.auto_restart,
        timeout_s=req.timeout_s,
        expected_nodes=list(record.get("provided_nodes") or []),
    ), _catalog_expected_nodes=True)
    return {
        **result,
        "catalog_id": record.get("id") or record.get("catalog_key"),
        "catalog_revision": snapshot["revision"],
        "expected_node_count": len(record.get("provided_nodes") or []),
        "job_status_path": (
            f"/api/jobs/{result['job_id']}" if result.get("job_id") else None
        ),
    }
