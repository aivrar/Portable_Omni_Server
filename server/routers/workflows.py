"""Workflow listing, fetch, and queue endpoints (phase 1) plus CRUD +
import + inline-run (phase 10).

Workflows live as ``*.json`` files in ``WORKFLOWS_DIR``. Every write goes
through ``safe_child_path`` (single-segment, ``.json`` suffix required) and
an atomic ``os.replace`` to avoid leaving a partially-written file behind.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Literal

import httpx
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from config import COMFYUI_DIR, COMFYUI_MODELS_DIR, COMFYUI_MODEL_CATEGORIES, WORKFLOWS_DIR
from comfy_placement import apply_placement_plan, build_placement_plan, normalize_policy
from helpers import safe_child_path
from state import comfy_manager, comfy_registry, worker_manager

logger = logging.getLogger(__name__)

router = APIRouter()

WORKFLOW_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB cap per /api/workflows plan
_FILENAME_BASE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_METADATA_DIR = WORKFLOWS_DIR / ".metadata"
_SENSITIVE_SEARCH_KEYS = {"token", "api_key", "apikey", "secret", "password", "auth"}
_SEARCH_TEXT_CAP = 64 * 1024
_OBJECT_INFO_TTL_SECONDS = 30.0
_object_info_cache: dict[str, tuple[float, int, dict]] = {}
_ANALYSIS_DEPENDENCY_TTL_SECONDS = 2.0
_analysis_dependency_cache: tuple[float, dict, list[dict]] | None = None
_analysis_dependency_lock = threading.Lock()
_workflow_write_lock = threading.RLock()
_workflow_search_cache: dict[str, tuple[int, int, dict, str, str, dict[str, list[str]]]] = {}
_workflow_search_cache_lock = threading.Lock()


class WorkflowPlacementPolicy(BaseModel):
    """Portable component-placement policy for a Comfy API-format graph."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{
            "mode": "auto",
            "eligible_devices": ["GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a"],
            "primary_device": "GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a",
            "reserve_mb": 1024,
            "overrides": {"12:model": "GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a"},
            "require_all": False,
        }]},
    )

    mode: Literal["single", "auto", "manual"] = Field(
        default="auto",
        description="single uses one GPU; auto plans all components; manual locks overrides and plans the rest.",
    )
    eligible_devices: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="Stable NVIDIA GPU UUIDs are preferred; cuda:N and primary/auxiliary aliases are accepted.",
    )
    primary_device: str | None = Field(
        default=None,
        max_length=160,
        description="Preferred device identifier. Single mode assigns every component here.",
    )
    reserve_mb: int = Field(
        default=1024,
        ge=0,
        le=262144,
        description="VRAM headroom retained on every eligible GPU.",
    )
    device_reserve_mb: dict[str, int] = Field(
        default_factory=dict,
        max_length=64,
        description="Optional per-device VRAM reserves keyed by UUID or cuda:N.",
    )
    overrides: dict[str, str] = Field(
        default_factory=dict,
        max_length=512,
        description="Component, node, or model-name locks mapped to a device UUID or alias.",
    )
    require_all: bool = Field(
        default=False,
        description="Block the plan unless every selected GPU receives a component.",
    )
    allow_cpu: bool = Field(
        default=False,
        description="Allow CPU as a placement target when the Comfy instance itself is CPU-only.",
    )


def _placement_payload(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value if isinstance(value, dict) else None


class WorkflowAnalyzeRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "workflow": {"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "model.safetensors"}}},
        "filename": "example-api-workflow.json",
        "instance_id": "comfy-cuda1-8188",
        "placement": {"mode": "auto", "reserve_mb": 1024},
    }]})

    workflow: dict
    filename: str | None = Field(default=None, max_length=180)
    instance_id: str | None = Field(default=None, max_length=160)
    placement: WorkflowPlacementPolicy | None = None


def _template_id(value: str) -> str:
    """Validate one opaque live-template ID without applying save-name rules.

    Comfy's versioned template catalogs legitimately use spaces, parentheses,
    commas, and other printable punctuation.  They are lookup selectors, not
    filesystem paths; discovery already supplies the exact ID.  Keep saved
    workflow writes on the stricter ``_FILENAME_BASE_RE`` boundary.
    """
    wanted = str(value or "").strip()
    if (
        not wanted
        or len(wanted) > 180
        or wanted in {".", ".."}
        or "/" in wanted
        or "\\" in wanted
        or any(ord(char) < 32 or ord(char) == 127 for char in wanted)
    ):
        raise HTTPException(status_code=400, detail="Invalid template id")
    return wanted


def _ensure_dir() -> None:
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(target_path, payload: bytes, *, overwrite: bool = True) -> None:
    """Write ``payload`` to ``target_path`` atomically via tempfile + rename."""
    _ensure_dir()
    tmp_fd, tmp_str = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=str(target_path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp_str, target_path)
        else:
            try:
                os.link(tmp_str, target_path)
            except FileExistsError as exc:
                raise HTTPException(status_code=409, detail="Workflow already exists") from exc
            os.unlink(tmp_str)
    except Exception:
        try:
            os.unlink(tmp_str)
        except OSError:
            pass
        raise


def _normalise_tags(tags) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        tag = re.sub(r"\s+", " ", str(raw or "").strip()).lower()
        if not tag or len(tag) > 48 or any(ord(char) < 32 for char in tag):
            raise HTTPException(status_code=400, detail="Tags must be 1-48 printable characters")
        if tag not in seen:
            seen.add(tag)
            out.append(tag)
    if len(out) > 32:
        raise HTTPException(status_code=400, detail="At most 32 workflow tags are allowed")
    return out


def _metadata_path(filename: str):
    safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    return _METADATA_DIR / f"{filename}.meta.json"


def _load_metadata(filename: str) -> dict:
    path = _metadata_path(filename)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"tags": [], "description": "", "placement_policy": None}
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail=f"Workflow metadata is unreadable for {filename}; repair it before running") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tags", []), list):
        raise HTTPException(status_code=409, detail=f"Invalid workflow metadata for {filename}")
    placement = data.get("placement_policy")
    if isinstance(placement, dict):
        try:
            placement = normalize_policy(placement)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"Invalid saved placement for {filename}: {exc}") from exc
    elif placement is not None:
        raise HTTPException(status_code=409, detail=f"Invalid saved placement for {filename}")
    return {
        "tags": [str(item) for item in data.get("tags") or [] if str(item).strip()][:32],
        "description": str(data.get("description") or "")[:2000],
        "placement_policy": placement,
    }


def _clean_metadata(tags, description: str, placement_policy=None) -> dict:
    try:
        placement_raw = _placement_payload(placement_policy)
        placement = normalize_policy(placement_raw) if placement_raw is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    clean = {
        "tags": _normalise_tags(tags),
        "description": str(description or "").strip()[:2000],
        "placement_policy": placement,
    }
    return clean


def _write_metadata(filename: str, tags, description: str, placement_policy=None) -> dict:
    clean = _clean_metadata(tags, description, placement_policy)
    target = _metadata_path(filename)
    _METADATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        target,
        json.dumps(clean, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )
    return clean


def _commit_workflow(path: Path, payload: bytes, metadata: dict, *, overwrite: bool):
    with _workflow_write_lock:
        prior = path.read_bytes() if path.exists() else None
        if prior is not None and not overwrite:
            raise HTTPException(status_code=409, detail="Workflow already exists")
        _atomic_write_json(path, payload, overwrite=overwrite)
        try:
            _write_metadata(path.name, metadata["tags"], metadata["description"], metadata["placement_policy"])
        except Exception:
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write_json(path, prior)
            raise



def _searchable_workflow_fields(data) -> dict[str, list[str]]:
    """Extract bounded human-readable workflow fields without returning graphs."""
    fields: dict[str, list[str]] = {
        "node_classes": [], "node_titles": [], "notes": [], "text": [],
    }
    seen = {key: set() for key in fields}
    total = 0

    def add(bucket: str, value) -> None:
        nonlocal total
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if not text or text in seen[bucket] or total >= _SEARCH_TEXT_CAP:
            return
        text = text[:4096]
        seen[bucket].add(text)
        fields[bucket].append(text)
        total += len(text)

    def walk(value, *, note_context: bool = False, depth: int = 0) -> None:
        if depth > 24 or total >= _SEARCH_TEXT_CAP:
            return
        if isinstance(value, dict):
            node_class = value.get("class_type") or value.get("type")
            if isinstance(node_class, str):
                add("node_classes", node_class)
            meta = value.get("_meta")
            title = meta.get("title") if isinstance(meta, dict) else value.get("title")
            if isinstance(title, str):
                add("node_titles", title)
            is_note = note_context or any(
                "note" in str(item or "").lower() for item in (node_class, title)
            )
            for key, child in value.items():
                if str(key).lower() in _SENSITIVE_SEARCH_KEYS:
                    continue
                walk(child, note_context=is_note, depth=depth + 1)
        elif isinstance(value, list):
            for child in value[:10000]:
                walk(child, note_context=note_context, depth=depth + 1)
        elif isinstance(value, str):
            add("notes" if note_context else "text", value)

    walk(data)
    return fields


def _match_workflow_document(query: str, base_fields: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    needle = query.casefold()
    matched: list[str] = []
    snippets: list[str] = []
    for field, values in base_fields.items():
        hits = [value for value in values if needle in value.casefold()]
        if hits:
            matched.append(field)
            snippets.extend(hits[:2])
    return matched, snippets[:6]


def _cached_search_document(path: Path, filename: str):
    """Parse and index one workflow, invalidating on size or mtime change."""
    stat = path.stat()
    cache_key = str(path.resolve())
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _workflow_search_cache_lock:
        cached = _workflow_search_cache.get(cache_key)
    if cached is not None and cached[:2] == stamp:
        data, graph_format, execution_mode, fields = cached[2:]
        return data, graph_format, execution_mode, {
            key: list(values) for key, values in fields.items()
        }

    if stat.st_size > WORKFLOW_MAX_BYTES:
        raise OverflowError("workflow exceeds size cap")
    data = json.loads(path.read_text(encoding="utf-8"))
    graph_format, execution_mode = _workflow_document_kind(data, filename)
    fields = _searchable_workflow_fields(data)
    with _workflow_search_cache_lock:
        _workflow_search_cache[cache_key] = (
            stamp[0], stamp[1], data, graph_format, execution_mode, fields,
        )
    return data, graph_format, execution_mode, {
        key: list(values) for key, values in fields.items()
    }


def _search_workflows_sync(
    query: str,
    wanted_tags: list[str],
    scope: str,
    limit: int,
) -> dict:
    """Filesystem/package scanning implementation run outside the event loop."""
    candidates: list[dict] = []
    if scope in {"all", "saved"}:
        _ensure_dir()
        for path in sorted(WORKFLOWS_DIR.glob("*.json")):
            candidates.append({
                "id": path.stem,
                "filename": path.name,
                "path": path,
                "source": "saved",
                "package": "",
                "package_version": "",
                "metadata": _load_metadata(path.name),
            })
    if scope in {"all", "templates"}:
        from routers.assets import _template_files
        for item in _template_files():
            candidates.append({
                "id": item["template_id"],
                "filename": item["path"].name,
                "path": item["path"],
                "source": item.get("source") or "template",
                "package": item.get("package") or "",
                "package_version": item.get("package_version") or "",
                "metadata": {"tags": [], "description": ""},
            })

    results: list[dict] = []
    seen_paths: set[str] = set()
    skipped = 0
    for item in candidates:
        try:
            resolved = str(item["path"].resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            _data, graph_format, execution_mode, fields = _cached_search_document(
                item["path"], item["filename"],
            )
        except (OSError, ValueError, OverflowError, json.JSONDecodeError):
            skipped += 1
            continue
        if graph_format == "metadata":
            skipped += 1
            continue
        metadata = item["metadata"]
        item_tags = _normalise_tags(metadata.get("tags") or [])
        if wanted_tags and not set(wanted_tags).issubset(item_tags):
            continue
        fields.update({
            "filename": [item["filename"], item["id"]],
            "tags": item_tags,
            "description": [metadata.get("description") or ""],
            "package": [item["package"]],
            "source": [item["source"]],
        })
        matched, snippets = _match_workflow_document(query, fields) if query else (["tags"], [])
        if query and not matched:
            continue
        results.append({
            "id": item["id"],
            "filename": item["filename"],
            "source": item["source"],
            "package": item["package"],
            "package_version": item["package_version"],
            "graph_format": graph_format,
            "execution_mode": execution_mode,
            "runnable": True,
            "api_runnable": graph_format == "api",
            "requires_api_export": graph_format != "api",
            "tags": item_tags,
            "description": metadata.get("description") or "",
            "matched_fields": matched,
            "snippets": snippets,
            "node_classes": fields["node_classes"][:30],
            "node_titles": fields["node_titles"][:30],
        })
        if len(results) >= limit:
            break
    return {
        "query": query,
        "tags": wanted_tags,
        "scope": scope,
        "results": results,
        "total": len(results),
        "scanned": len(seen_paths),
        "skipped": skipped,
    }


def _get_workflow_template_sync(wanted: str, source: str, package: str) -> dict:
    """Resolve and parse a package template without blocking the API loop."""
    from routers.assets import _template_files

    matches = [
        item for item in _template_files()
        if item["template_id"] == wanted
        and (not source or str(item.get("source") or "") == source)
        and (not package or str(item.get("package") or "") == package)
    ]
    if not matches:
        raise HTTPException(status_code=404, detail="Workflow template not found")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail={
            "message": "Workflow template selector is ambiguous",
            "candidates": [{
                "id": item["template_id"],
                "source": item.get("source") or "",
                "package": item.get("package") or "",
                "package_version": item.get("package_version") or "",
            } for item in matches[:20]],
        })
    item = matches[0]
    try:
        data, graph_format, execution_mode, fields = _cached_search_document(
            item["path"], item["path"].name,
        )
    except OverflowError as exc:
        raise HTTPException(status_code=413, detail="Workflow template exceeds size cap") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Workflow template is unreadable: {exc}") from exc
    if graph_format == "metadata":
        raise HTTPException(status_code=422, detail="Selected JSON is template metadata, not a workflow graph")
    return {
        "template": {
            "id": item["template_id"],
            "filename": item["path"].name,
            "source": item.get("source") or "template",
            "package": item.get("package") or "",
            "package_version": item.get("package_version") or "",
            "graph_format": graph_format,
            "execution_mode": execution_mode,
            "api_runnable": graph_format == "api",
            "requires_api_export": graph_format != "api",
            "node_classes": fields["node_classes"][:100],
            "node_titles": fields["node_titles"][:100],
        },
        "workflow": data,
    }


def _workflow_document_kind(data, filename: str = "") -> tuple[str, str]:
    """Classify executable graphs separately from package search metadata."""
    if isinstance(data, dict) and any(
        isinstance(node, dict) and isinstance(node.get("class_type"), str)
        for node in data.values()
    ):
        graph_format = "api"
    elif isinstance(data, dict) and isinstance(data.get("nodes"), list):
        graph_format = "ui"
    else:
        return "metadata", "not-runnable"
    # Current official partner/API templates use the api_* naming contract.
    # Keep them fully searchable and inspectable; the label prevents them from
    # being confused with a local open-weight workflow.
    execution_mode = "remote-api" if Path(filename).stem.lower().startswith("api_") else "local"
    return graph_format, execution_mode


def _workflow_node_classes(data) -> list[str]:
    classes: list[str] = []
    if isinstance(data, dict):
        for node in data.values():
            if isinstance(node, dict) and isinstance(node.get("class_type"), str):
                classes.append(node["class_type"])
        ui_nodes = data.get("nodes")
        if isinstance(ui_nodes, list):
            for node in ui_nodes:
                if isinstance(node, dict) and isinstance(node.get("type"), str):
                    classes.append(node["type"])
    return list(dict.fromkeys(classes))


def _api_graph_issues(workflow, object_info=None) -> list[str]:
    if not isinstance(workflow, dict) or isinstance(workflow.get("nodes"), list):
        return []
    issues = []
    for node_id, node in workflow.items():
        if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not node.get("class_type") or not isinstance(node.get("inputs"), dict)):
            issues.append(f"Node {node_id} must contain class_type and an inputs object")
            continue
        info = (object_info or {}).get(node["class_type"], {})
        required = (info.get("input") or {}).get("required") or {}
        for field in required:
            if field not in node["inputs"]:
                issues.append(f"Node {node_id} is missing required input {field}")
        for field, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in workflow or not isinstance(value[1], int) or value[1] < 0:
                    issues.append(f"Node {node_id} has an invalid link for {field}")
    return issues


def _krea_raw_semantic_issues(data) -> list[str]:
    """Catch runnable-looking Krea Raw graphs with Turbo sampling semantics."""
    if not isinstance(data, dict):
        return []
    nodes = {
        str(node_id): node for node_id, node in data.items()
        if isinstance(node, dict) and isinstance(node.get("class_type"), str)
    }
    raw_loaders = [
        node for node in nodes.values()
        if node.get("class_type") == "UNETLoader"
        and "krea2_raw" in str((node.get("inputs") or {}).get("unet_name") or "").casefold()
    ]
    if not raw_loaders:
        return []

    issues: list[str] = []
    dimensions = next((
        node.get("inputs") or {} for node in nodes.values()
        if node.get("class_type") == "EmptyLatentImage"
    ), {})
    try:
        width = int(dimensions.get("width") or 1024)
        height = int(dimensions.get("height") or 1024)
        sequence_length = width * height / 256.0
        expected_shift = 0.5 + ((sequence_length - 256.0) / (6400.0 - 256.0)) * 0.65
    except (TypeError, ValueError):
        expected_shift = None

    valid_shift = False
    for node in nodes.values():
        if node.get("class_type") != "ModelSamplingFlux":
            continue
        inputs = node.get("inputs") or {}
        try:
            base = float(inputs.get("base_shift"))
            maximum = float(inputs.get("max_shift"))
            patch_width = int(inputs.get("width"))
            patch_height = int(inputs.get("height"))
            patch_sequence = patch_width * patch_height / 256.0
            actual_shift = base + (
                (patch_sequence - 256.0) / (4096.0 - 256.0)
            ) * (maximum - base)
        except (TypeError, ValueError):
            continue
        if expected_shift is not None and abs(actual_shift - expected_shift) <= 0.02:
            valid_shift = True
            break
    if not valid_shift:
        issues.append(
            "Krea 2 Raw requires its resolution-derived flow shift; the current "
            "graph would use Turbo's fixed 1.15 schedule."
        )

    valid_unconditional = True
    samplers = [node for node in nodes.values() if node.get("class_type") == "KSampler"]
    for sampler in samplers:
        link = (sampler.get("inputs") or {}).get("negative")
        source = nodes.get(str(link[0])) if isinstance(link, list) and link else None
        source_inputs = source.get("inputs") if isinstance(source, dict) else None
        if (
            not isinstance(source_inputs, dict)
            or source.get("class_type") != "CLIPTextEncode"
            or str(source_inputs.get("text") or "").strip()
        ):
            valid_unconditional = False
            break
    if not samplers or not valid_unconditional:
        issues.append(
            "Krea 2 Raw CFG requires an independently encoded empty-string "
            "unconditional branch; ConditioningZeroOut is not equivalent."
        )
    return issues


def _ui_non_runtime_node_statuses(data) -> dict[str, str]:
    """Classify UI-only decorations and embedded subgraph instance IDs."""
    statuses: dict[str, str] = {}
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        return statuses
    for node in data["nodes"]:
        if not isinstance(node, dict) or not isinstance(node.get("type"), str):
            continue
        node_type = node["type"]
        if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", node_type, re.I):
            statuses[node_type] = "embedded-subgraph"
        elif not (node.get("inputs") or []) and not (node.get("outputs") or []):
            statuses[node_type] = "ui-only"
    return statuses


def _manager_candidates(model: dict, manager_rows: list[dict]) -> list[dict]:
    """Return exact filename/category matches from the local Manager catalog."""
    from routers.assets import _parse_hf_url

    wanted_name = Path(str(model.get("name") or "")).name.casefold()
    wanted_category = str(model.get("category") or "")
    matches: list[dict] = []
    for raw in manager_rows:
        filename = str(raw.get("filename") or "").strip()
        category = str(raw.get("save_path") or "").strip()
        if Path(filename).name.casefold() != wanted_name or category != wanted_category:
            continue
        raw_url = raw.get("url") or ""
        if isinstance(raw_url, list):
            raw_url = next((str(item) for item in raw_url if str(item).strip()), "")
        url = str(raw_url).strip()
        matches.append({
            "name": str(raw.get("name") or filename),
            "filename": filename,
            "category": category,
            "url": url,
            "downloadable": bool(_parse_hf_url(url)),
            "source": "comfyui-manager",
        })
    return matches[:10]


def _unclassified_model_search(
    item: dict,
    inventory: dict,
    manager_rows: list[dict],
) -> dict:
    """Attach exact cross-category lookup results to an unknown model field.

    A third-party node may use an arbitrary widget name, so its folder cannot
    always be inferred safely.  Exact basename matches are still useful to API
    clients and the UI without guessing a category or allowing a workflow to
    run with an ambiguous dependency.
    """
    from routers.assets import _parse_hf_url

    enriched = dict(item)
    wanted = Path(str(item.get("name") or "")).name.casefold()
    installed_matches: list[dict] = []
    for category in COMFYUI_MODEL_CATEGORIES:
        names = inventory.get(category) or {}
        if wanted and wanted in (names.get("basenames") or set()):
            installed_matches.append({
                "name": Path(str(item.get("name") or "")).name,
                "category": category,
                "installed": True,
                "source": "installed-filesystem",
            })

    manager_candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    allowed = set(COMFYUI_MODEL_CATEGORIES)
    for raw in manager_rows:
        filename = str(raw.get("filename") or "").strip()
        if not filename or Path(filename).name.casefold() != wanted:
            continue
        save_path = str(raw.get("save_path") or "").strip().replace("\\", "/")
        parts = [part for part in save_path.split("/") if part]
        category = parts[0] if parts and parts[0] in allowed else ""
        if not category:
            continue
        raw_url = raw.get("url") or ""
        if isinstance(raw_url, list):
            raw_url = next((str(value) for value in raw_url if str(value).strip()), "")
        url = str(raw_url).strip()
        key = (category, filename.casefold(), url)
        if key in seen:
            continue
        seen.add(key)
        manager_candidates.append({
            "name": str(raw.get("name") or filename),
            "filename": filename,
            "category": category,
            "save_path": save_path,
            "url": url,
            "downloadable": bool(_parse_hf_url(url)),
            "source": "comfyui-manager",
        })
        if len(manager_candidates) >= 20:
            break

    enriched["installed_matches"] = installed_matches
    enriched["manager_candidates"] = manager_candidates
    enriched["search"] = {
        "query": Path(str(item.get("name") or "")).stem,
        "installed_path": "/api/registry/comfy/installed-models/search",
        "manager_path": "/api/registry/comfy/manager-models/search",
        "huggingface_path": "/api/registry/comfy/models/search",
    }
    return enriched


def _analysis_dependencies() -> tuple[dict, list[dict]]:
    """Reuse short-lived filesystem/catalog snapshots across rapid UI checks."""
    global _analysis_dependency_cache
    now = time.monotonic()
    with _analysis_dependency_lock:
        cached = _analysis_dependency_cache
        if cached and now - cached[0] < _ANALYSIS_DEPENDENCY_TTL_SECONDS:
            return cached[1], cached[2]
        from routers.assets import _installed_asset_inventory
        from routers.registry import _manager_model_rows
        inventory = _installed_asset_inventory()
        manager_rows = _manager_model_rows()
        _analysis_dependency_cache = (now, inventory, manager_rows)
        return inventory, manager_rows


def _analyze_workflow_sync(
    workflow: dict,
    filename: str,
    object_info: dict | None = None,
    *,
    inventory: dict | None = None,
    manager_rows: list[dict] | None = None,
) -> dict:
    """Analyze one workflow locally; never loads a model or contacts a registry."""
    from routers.assets import (
        _asset_installed,
        _decorate_model_api,
        _extract_blueprint_models,
        _extract_hf_links,
        _extract_workflow_model_refs,
        _installed_asset_inventory,
        _merge_template_model,
        _normalise_blueprint_model,
        _parse_hf_url,
        _workflow_weight_inputs,
    )
    from routers.registry import _manager_model_rows

    graph_format, execution_mode = _workflow_document_kind(workflow, filename)
    inventory = inventory if inventory is not None else _installed_asset_inventory()
    manager_rows = manager_rows if manager_rows is not None else _manager_model_rows()
    candidates: list[dict] = []
    candidates.extend(
        model for model in (
            _normalise_blueprint_model(raw, filename)
            for raw in _extract_blueprint_models(workflow)
        ) if model is not None
    )
    candidates.extend(_extract_workflow_model_refs(
        workflow,
        filename,
        object_info=object_info,
        inventory=inventory,
    ))

    by_key: dict[tuple[str, str], dict] = {}
    for model in candidates:
        key = (str(model["category"]), str(model["name"]).casefold())
        if key not in by_key:
            by_key[key] = {
                "name": model["name"],
                "category": model["category"],
                "url": "",
                "repo": "",
                "file": "",
                "source": "",
                "sources": [],
                "downloadable": False,
                "installed": False,
                "reason": "No verified download source is attached to this workflow",
                "references": [],
                "candidates": [],
            }
        _merge_template_model(by_key[key], model)

    # Attach embedded HF links by exact basename, even when the URL's folder
    # does not use one of ComfyUI's canonical category names.
    for url in _extract_hf_links(workflow):
        parsed = _parse_hf_url(url)
        if not parsed:
            continue
        repo, _revision, hub_file = parsed
        basename = Path(hub_file).name.casefold()
        matches = [item for item in by_key.values()
                   if Path(str(item["name"])).name.casefold() == basename]
        if len(matches) == 1:
            item = matches[0]
            item.update({
                "url": url,
                "repo": repo,
                "file": hub_file,
                "downloadable": True,
                "reason": "",
            })
            if "link" not in item["sources"]:
                item["sources"].append("link")

    models = sorted(by_key.values(), key=lambda item: (
        str(item["category"]), str(item["name"]).casefold(),
    ))
    for model in models:
        model["installed"] = _asset_installed(
            model["category"], model["name"], inventory=inventory,
        )
        if not model["installed"] and not model.get("downloadable"):
            matches = _manager_candidates(model, manager_rows)
            model["candidates"] = matches
            verified = [item for item in matches if item.get("downloadable")]
            if len(verified) == 1:
                chosen = verified[0]
                model.update({
                    "url": chosen["url"],
                    "file": chosen["filename"],
                    "downloadable": True,
                    "reason": "",
                })
                model["sources"].append("comfyui-manager")
        model["source"] = ",".join(dict.fromkeys(model.get("sources") or []))
        _decorate_model_api(model)
        model["search"] = {
            "query": Path(str(model["name"])).stem,
            "category": model["category"],
        }

    recognized = {
        (str(ref.get("node_type") or ""), str(ref.get("field") or ""),
         str(model["name"]).casefold())
        for model in models for ref in model.get("references") or []
    }
    unclassified: list[dict] = []
    seen_unknown: set[tuple[str, str, str]] = set()
    for item in _workflow_weight_inputs(workflow):
        signature = (item["node_type"], item["field"], item["name"].casefold())
        if signature in recognized or signature in seen_unknown:
            continue
        seen_unknown.add(signature)
        unclassified.append(_unclassified_model_search(item, inventory, manager_rows))

    node_classes = _workflow_node_classes(workflow)
    non_runtime_statuses = _ui_non_runtime_node_statuses(workflow)
    if object_info is None:
        node_items = [{
            "class_type": name,
            "status": non_runtime_statuses.get(name, "unknown"),
        } for name in node_classes]
        node_verification = "requires-running-comfyui"
    else:
        node_items = [{
            "class_type": name,
            "status": non_runtime_statuses.get(
                name, "installed" if name in object_info else "missing",
            ),
        } for name in node_classes]
        node_verification = "live-object-info"

    missing = [item for item in models if not item["installed"]]
    downloadable = [item for item in missing if item.get("downloadable")]
    unresolved = [item for item in missing if not item.get("downloadable")]
    missing_nodes = [item for item in node_items if item["status"] == "missing"]
    runtime_nodes = [
        item for item in node_items
        if item["status"] not in {"embedded-subgraph", "ui-only"}
    ]
    semantic_issues = _krea_raw_semantic_issues(workflow) + _api_graph_issues(workflow, object_info)
    warnings: list[str] = []
    if unclassified:
        warnings.append(
            f"{len(unclassified)} weight-looking input(s) use an unknown custom-node field; review them manually."
        )
    if object_info is None and runtime_nodes:
        warnings.append("Start ComfyUI to verify custom-node availability against its live schema.")
    if graph_format == "ui":
        warnings.append("This is a UI-format graph; export API format from ComfyUI before running it through the API.")
    warnings.extend(semantic_issues)

    if graph_format != "api":
        readiness = "needs-api-export"
    elif missing:
        readiness = "needs-models"
    elif unclassified:
        readiness = "needs-review"
    elif semantic_issues:
        readiness = "needs-review"
    elif missing_nodes:
        readiness = "needs-nodes"
    elif object_info is None:
        readiness = "check-nodes"
    else:
        readiness = "ready"

    return {
        "filename": filename,
        "readiness": readiness,
        "ready_to_run": readiness == "ready",
        "graph": {
            "format": graph_format,
            "execution_mode": execution_mode,
            "api_runnable": graph_format == "api",
            "node_count": len(node_classes),
        },
        "storage": {
            "mode": "comfyui-native",
            "model_root": str(COMFYUI_DIR / "models"),
            "shared_model_roots_scanned": False,
        },
        "models": {
            "items": models,
            "installed": [item for item in models if item["installed"]],
            "missing": missing,
            "downloadable": downloadable,
            "unresolved": unresolved,
            "unclassified_inputs": unclassified,
            "total": len(models),
            "missing_count": len(missing),
            "downloadable_count": len(downloadable),
            "unresolved_count": len(unresolved),
        },
        "nodes": {
            "items": node_items,
            "verification": node_verification,
            "missing": missing_nodes,
            "missing_count": len(missing_nodes),
            "total": len(node_items),
            "runtime_total": len(runtime_nodes),
            "non_runtime": [
                item for item in node_items
                if item["status"] in {"embedded-subgraph", "ui-only"}
            ],
        },
        "warnings": warnings,
    }


def _validate_workflow_payload(workflow: dict) -> bytes:
    """Serialize, size-check, and shape-check a workflow JSON dict."""
    if not isinstance(workflow, dict):
        raise HTTPException(status_code=400, detail="Workflow must be a JSON object")
    try:
        serialized = json.dumps(workflow, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Workflow not JSON-serializable: {e}")
    if len(serialized) > WORKFLOW_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workflow exceeds {WORKFLOW_MAX_BYTES // (1024 * 1024)} MiB cap",
        )
    return serialized


def _resolve_target_instance(instance_id: str | None):
    instances = comfy_registry.all_instances()
    ready = [i for i in instances if i.status == "ready"]
    if not ready:
        raise HTTPException(status_code=503, detail="No ComfyUI instances running")
    if not instance_id:
        return ready[0]
    target = comfy_registry.get(instance_id)
    if not target or target.status != "ready":
        raise HTTPException(status_code=404, detail="Instance not ready")
    return target


def _optional_target_instance(instance_id: str | None):
    if instance_id:
        return _resolve_target_instance(instance_id)
    return next((item for item in comfy_registry.all_instances()
                 if item.status == "ready"), None)


async def _object_info_for_instance(target) -> dict | None:
    if target is None:
        return None
    generation = (target.port, getattr(target.process, "pid", None), id(target.process or target))
    cached = _object_info_cache.get(target.instance_id)
    now = time.monotonic()
    if cached and cached[1] == generation and now - cached[0] < _OBJECT_INFO_TTL_SECONDS:
        return cached[2]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"http://127.0.0.1:{target.port}/object_info")
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    _object_info_cache[target.instance_id] = (now, generation, payload)
    return payload


async def _attach_node_catalog_actions(report: dict) -> None:
    """Resolve workflow node classes to exact Manager catalog installers."""
    node_section = report.get("nodes") if isinstance(report, dict) else None
    rows = node_section.get("items") if isinstance(node_section, dict) else None
    if not isinstance(rows, list) or not rows:
        return
    try:
        from routers.registry import _manager_catalog_snapshot
        snapshot = await _manager_catalog_snapshot(refresh=False)
    except Exception as exc:
        node_section["catalog_warning"] = f"Manager catalog resolution unavailable: {exc}"
        return

    by_class: dict[str, list[dict]] = {}
    for record in snapshot.get("records") or []:
        for provided in record.get("provided_nodes") or []:
            key = str(provided or "").casefold()
            if key:
                by_class.setdefault(key, []).append(record)

    resolvable = 0
    for row in rows:
        if row.get("status") in {"installed", "embedded-subgraph", "ui-only"}:
            continue
        candidates = []
        for record in by_class.get(str(row.get("class_type") or "").casefold(), [])[:10]:
            install = record.get("install")
            if isinstance(install, dict):
                install = {
                    **install,
                    "body": {
                        **(install.get("body") or {}),
                        "dry_run": False,
                        "auto_restart": True,
                    },
                }
            candidates.append({
                "catalog_key": record.get("catalog_key"),
                "id": record.get("id"),
                "title": record.get("title"),
                "repo_url": record.get("repo_url"),
                "install": install,
            })
        row["catalog_candidates"] = candidates
        if candidates:
            resolvable += 1
        if len(candidates) == 1:
            row["install"] = candidates[0]["install"]

    node_section.update({
        "catalog_revision": snapshot.get("revision"),
        "catalog_source": snapshot.get("source"),
        "catalog_warning": snapshot.get("warning"),
        "resolvable_count": resolvable,
    })


async def _analyze_with_live_context(
    workflow: dict,
    filename: str,
    instance_id: str | None = None,
    placement: dict | None = None,
) -> dict:
    target = _optional_target_instance(instance_id)
    object_info = await _object_info_for_instance(target)
    inventory, manager_rows = await asyncio.to_thread(_analysis_dependencies)
    report = await asyncio.to_thread(
        _analyze_workflow_sync,
        workflow,
        filename,
        object_info,
        inventory=inventory,
        manager_rows=manager_rows,
    )
    report["instance_id"] = target.instance_id if target else None
    if placement is not None:
        try:
            devices = await worker_manager.detect_devices_async()
            report["placement_plan"] = await asyncio.to_thread(
                build_placement_plan,
                workflow,
                placement,
                target,
                devices,
                COMFYUI_MODELS_DIR,
                object_info,
            )
            _apply_placement_readiness(report)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _attach_node_catalog_actions(report)
    return report


def _apply_placement_readiness(report: dict) -> dict:
    """Make an invalid placement plan a top-level workflow hard stop."""
    plan = report.get("placement_plan")
    if isinstance(plan, dict) and not plan.get("valid"):
        # Requirements can be complete while the selected GPU/host placement
        # is impossible. Align analyze with the queue gate so agents never
        # interpret an invalid plan as runnable.
        report["readiness"] = "needs-placement"
        report["ready_to_run"] = False
    return report


async def _queue_on_instance(workflow: dict, instance_id: str | None,
                             client_id: str | None = None,
                             *, preflight: bool = True,
                             placement: dict | None = None):
    target = _resolve_target_instance(instance_id)
    placement_plan = None
    placement_changes = None
    if placement is not None:
        try:
            object_info = await _object_info_for_instance(target)
            devices = await worker_manager.detect_devices_async()
            placement_plan = await asyncio.to_thread(
                build_placement_plan,
                workflow,
                placement,
                target,
                devices,
                COMFYUI_MODELS_DIR,
                object_info,
            )
            if not placement_plan.get("valid"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "GPU placement plan is not runnable",
                        "placement_plan": placement_plan,
                    },
                )
            workflow, placement_changes = await asyncio.to_thread(
                apply_placement_plan, workflow, placement_plan,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if preflight:
        object_info = await _object_info_for_instance(target)
        inventory, manager_rows = await asyncio.to_thread(_analysis_dependencies)
        report = await asyncio.to_thread(
            _analyze_workflow_sync,
            workflow,
            "inline-workflow.json",
            object_info,
            inventory=inventory,
            manager_rows=manager_rows,
        )
        blockers = not report["ready_to_run"]
        if blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Workflow requirements are not ready",
                    "requirements": report,
                },
            )
    payload: dict = {"prompt": workflow}
    if client_id:
        payload["client_id"] = client_id
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{target.port}/prompt",
                json=payload,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"ComfyUI error: {resp.text[:500]}")
            data = resp.json()
            data["instance_id"] = target.instance_id
            if placement_plan is not None:
                data["placement_plan"] = placement_plan
                data["placement_changes"] = placement_changes
            return data
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="ComfyUI instance unreachable")


@router.get("/api/workflows")
async def list_workflows():
    rows = comfy_manager.get_workflows()
    for row in rows:
        row.update(_load_metadata(str(row.get("filename") or "")))
    return {"workflows": rows}


@router.get("/api/workflows/search")
async def search_workflows(
    q: str = Query(default="", max_length=200),
    tags: str = Query(default="", max_length=500),
    scope: str = Query(default="all", pattern="^(all|saved|templates)$"),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Search saved workflows and live ComfyUI template packages by content."""
    query = str(q or "").strip()
    wanted_tags = _normalise_tags(item for item in str(tags or "").split(",") if item.strip())
    if not query and not wanted_tags:
        raise HTTPException(status_code=400, detail="q or tags is required")

    return await asyncio.to_thread(
        _search_workflows_sync, query, wanted_tags, scope, limit,
    )

@router.get("/api/workflow-templates/{template_id}")
async def get_workflow_template(
    template_id: str,
    source: str = Query(default="", max_length=80),
    package: str = Query(default="", max_length=200),
):
    """Return one live package/blueprint workflow with provenance."""
    raw_template_id = str(template_id or "")
    if raw_template_id.lower().endswith(".json"):
        raw_template_id = raw_template_id[:-5]
    wanted = _template_id(raw_template_id)
    return await asyncio.to_thread(
        _get_workflow_template_sync, wanted, source, package,
    )


@router.get("/api/workflows/requirements")
async def workflow_requirements(instance_id: str | None = None):
    """Analyze every saved workflow with one shared filesystem/catalog pass."""
    target = _optional_target_instance(instance_id)
    object_info = await _object_info_for_instance(target)
    inventory, manager_rows = await asyncio.to_thread(_analysis_dependencies)
    reports: list[dict] = []
    _ensure_dir()
    for path in sorted(WORKFLOWS_DIR.glob("*.json")):
        try:
            if path.stat().st_size > WORKFLOW_MAX_BYTES:
                continue
            workflow = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        reports.append(await asyncio.to_thread(
            _analyze_workflow_sync,
            workflow,
            path.name,
            object_info,
            inventory=inventory,
            manager_rows=manager_rows,
        ))
    return {
        "workflows": reports,
        "instance_id": target.instance_id if target else None,
        "count": len(reports),
    }


@router.post("/api/workflows/analyze")
async def analyze_workflow(req: WorkflowAnalyzeRequest):
    _validate_workflow_payload(req.workflow)
    filename = req.filename or "inline-workflow.json"
    return await _analyze_with_live_context(
        req.workflow, filename, req.instance_id, _placement_payload(req.placement),
    )


@router.post("/api/workflows/probe")
async def probe_workflow(req: WorkflowAnalyzeRequest):
    """Alias with explicit discovery semantics for arbitrary inline workflows."""
    return await analyze_workflow(req)


@router.get("/api/workflows/{filename}/requirements")
async def saved_workflow_requirements(filename: str, instance_id: str | None = None):
    path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Workflow not found")
    if path.stat().st_size > WORKFLOW_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Workflow exceeds size cap")
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Workflow is unreadable: {exc}")
    metadata = _load_metadata(path.name)
    return await _analyze_with_live_context(
        workflow, path.name, instance_id, metadata.get("placement_policy"),
    )


@router.get("/api/workflows/{filename}")
async def get_workflow(filename: str):
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not wf_path.exists() or not wf_path.is_file():
        raise HTTPException(status_code=404, detail="Workflow not found")
    return FileResponse(str(wf_path), media_type="application/json")


# ---------------------------------------------------------------------------
# Phase 10 — write endpoints (PUT/DELETE/import/inline-run) followed by the
# original /queue route.
# ---------------------------------------------------------------------------
class WorkflowSaveRequest(BaseModel):
    workflow: dict
    overwrite: bool = False
    tags: list[str] | None = None
    description: str | None = Field(default=None, max_length=2000)
    placement_policy: WorkflowPlacementPolicy | None = None


class WorkflowMetadataRequest(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=32)
    description: str = Field(default="", max_length=2000)
    placement_policy: WorkflowPlacementPolicy | None = None


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "workflow": {"1": {"class_type": "EmptyImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}}},
        "instance_id": "comfy-cuda1-8188",
        "client_id": "omni-api-client",
        "placement": {"mode": "auto", "reserve_mb": 1024},
    }]})

    workflow: dict
    instance_id: str | None = None
    client_id: str | None = Field(default=None, max_length=128)
    placement: WorkflowPlacementPolicy | None = None


class WorkflowParameterizedRunRequest(BaseModel):
    """Run a saved workflow with patched node inputs.

    ``params`` is a flat dict of overrides. Each key is matched against
    nodes in this priority order:

    1. Exact node id (``"6"`` -> nodes["6"].inputs.text)
    2. ``<node_id>.<field>`` (``"6.seed"`` -> nodes["6"].inputs.seed)
    3. ``_meta.title`` of any node (``"Positive prompt"`` -> the matching
       node's primary input field)
    4. Convenience aliases: ``prompt`` -> first CLIPTextEncode node's text,
       ``seed`` -> any KSampler.seed, ``steps`` -> KSampler.steps,
       ``negative_prompt`` -> last CLIPTextEncode (heuristic).
    """

    params: dict
    instance_id: str | None = None
    client_id: str | None = Field(default=None, max_length=128)
    placement: WorkflowPlacementPolicy | None = None


# Heuristic for the "convenience alias" path.
_PRIMARY_INPUT_FOR_CLASS = {
    "CLIPTextEncode": "text",
    "CLIPTextEncodeSDXL": "text",
    "KSampler": "seed",
    "KSamplerAdvanced": "noise_seed",
    "EmptyLatentImage": "width",
    "LoraLoader": "strength_model",
    "CheckpointLoaderSimple": "ckpt_name",
}

_ALIASES_TO_CLASS = {
    "prompt": ("CLIPTextEncode", "text", "first"),
    "positive_prompt": ("CLIPTextEncode", "text", "first"),
    "negative_prompt": ("CLIPTextEncode", "text", "last"),
    "seed": ("KSampler", "seed", "first"),
    "steps": ("KSampler", "steps", "first"),
    "cfg": ("KSampler", "cfg", "first"),
    "sampler_name": ("KSampler", "sampler_name", "first"),
    "scheduler": ("KSampler", "scheduler", "first"),
    "width": ("EmptyLatentImage", "width", "first"),
    "height": ("EmptyLatentImage", "height", "first"),
    "batch_size": ("EmptyLatentImage", "batch_size", "first"),
}


def _apply_param_to_node(workflow: dict, key: str, value) -> bool:
    """Apply one ``key`` -> ``value`` override. Returns True if matched."""
    # Strategy 1: exact node id (and optional .field)
    if key in workflow and isinstance(workflow[key], dict):
        node = workflow[key]
        # No field given - assume the class's "primary" input
        cls = node.get("class_type", "")
        primary = _PRIMARY_INPUT_FOR_CLASS.get(cls)
        if primary and primary in node.get("inputs", {}):
            node.setdefault("inputs", {})[primary] = value
            return True
        return False
    if "." in key:
        node_id, field = key.split(".", 1)
        if node_id in workflow and isinstance(workflow[node_id], dict):
            workflow[node_id].setdefault("inputs", {})[field] = value
            return True

    # Strategy 2: match _meta.title
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta = node.get("_meta") or {}
        if meta.get("title") == key:
            cls = node.get("class_type", "")
            primary = _PRIMARY_INPUT_FOR_CLASS.get(cls)
            if primary:
                node.setdefault("inputs", {})[primary] = value
                return True
            # No known primary - take the first existing input field
            inputs = node.setdefault("inputs", {})
            if inputs:
                first_field = next(iter(inputs))
                inputs[first_field] = value
                return True

    # Strategy 3: convenience aliases
    if key in _ALIASES_TO_CLASS:
        cls, field, position = _ALIASES_TO_CLASS[key]
        matches = [(nid, n) for nid, n in workflow.items()
                   if isinstance(n, dict) and n.get("class_type") == cls]
        if not matches:
            return False
        target_id, target_node = matches[0] if position == "first" else matches[-1]
        target_node.setdefault("inputs", {})[field] = value
        return True

    return False


def patch_workflow(workflow: dict, params: dict) -> tuple[dict, dict]:
    """Apply ``params`` to a deep-copied workflow.

    Returns ``(patched_workflow, report)`` where ``report`` includes
    ``applied`` (list of keys that landed) and ``unmatched`` (keys that
    didn't resolve to a node).
    """
    import copy
    patched = copy.deepcopy(workflow)
    applied: list[str] = []
    unmatched: list[str] = []
    for k, v in (params or {}).items():
        if _apply_param_to_node(patched, k, v):
            applied.append(k)
        else:
            unmatched.append(k)
    return patched, {"applied": applied, "unmatched": unmatched}


@router.put("/api/workflows/{filename}")
async def save_workflow(filename: str, req: WorkflowSaveRequest):
    """Atomic write of a workflow JSON. ``overwrite=false`` blocks clobbering."""
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not _FILENAME_BASE_RE.fullmatch(wf_path.stem):
        raise HTTPException(status_code=400, detail="Invalid workflow filename")
    if wf_path.exists() and not req.overwrite:
        raise HTTPException(status_code=409,
                            detail=f"{filename} exists (use overwrite=true)")
    payload = _validate_workflow_payload(req.workflow)
    with _workflow_write_lock:
        metadata = _load_metadata(wf_path.name)
        metadata = _clean_metadata(
            req.tags if req.tags is not None else metadata["tags"],
            req.description if req.description is not None else metadata["description"],
            req.placement_policy if "placement_policy" in req.model_fields_set else metadata.get("placement_policy"),
        )
        _commit_workflow(wf_path, payload, metadata, overwrite=req.overwrite)
    return {
        "status": "saved",
        "name": wf_path.name,
        "size_bytes": len(payload),
        "path": str(wf_path),
        **metadata,
    }


@router.put("/api/workflows/{filename}/metadata")
async def set_workflow_metadata(filename: str, req: WorkflowMetadataRequest):
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not wf_path.exists() or not wf_path.is_file():
        raise HTTPException(status_code=404, detail="Workflow not found")
    current = _load_metadata(wf_path.name) if "placement_policy" not in req.model_fields_set else {}
    return {"filename": wf_path.name, **_write_metadata(
        wf_path.name,
        req.tags,
        req.description,
        req.placement_policy if "placement_policy" in req.model_fields_set
        else current.get("placement_policy"),
    )}


@router.delete("/api/workflows/{filename}")
async def delete_workflow(filename: str):
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not wf_path.exists() or not wf_path.is_file():
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf_path.is_symlink():
        raise HTTPException(status_code=400, detail="Refusing to delete a symlink")
    try:
        wf_path.unlink()
        _metadata_path(wf_path.name).unlink(missing_ok=True)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"unlink failed: {e}")
    return {"status": "deleted", "name": wf_path.name}


@router.post("/api/workflows/import")
async def import_workflow(file: UploadFile = File(...), overwrite: bool = False):
    """Multipart .json upload. Sanitises filename, validates JSON, atomic-writes."""
    if file.filename is None:
        raise HTTPException(status_code=400, detail="Filename required")
    base = file.filename.split("/")[-1].split("\\")[-1]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if not safe_name.endswith(".json"):
        safe_name = safe_name + ".json"
    wf_path = safe_child_path(WORKFLOWS_DIR, safe_name, suffix=".json")
    if not _FILENAME_BASE_RE.fullmatch(wf_path.stem):
        raise HTTPException(status_code=400, detail="Invalid workflow filename")
    if wf_path.exists() and not overwrite:
        raise HTTPException(status_code=409,
                            detail=f"{safe_name} exists (use overwrite=true)")
    raw = await file.read(WORKFLOW_MAX_BYTES + 1)
    if len(raw) > WORKFLOW_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Workflow exceeds {WORKFLOW_MAX_BYTES // (1024 * 1024)} MiB cap",
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {e}")
    payload = _validate_workflow_payload(parsed)
    _commit_workflow(wf_path, payload, _clean_metadata([], "", None), overwrite=overwrite)
    return {"status": "imported", "name": wf_path.name,
            "size_bytes": len(payload), "path": str(wf_path)}


@router.post("/api/workflows/run")
async def run_workflow_inline(req: WorkflowRunRequest):
    """Queue a workflow without persisting it - useful for one-shot CLI runs."""
    _validate_workflow_payload(req.workflow)
    return await _queue_on_instance(
        req.workflow,
        req.instance_id,
        req.client_id,
        placement=_placement_payload(req.placement),
    )


@router.post("/api/workflows/{filename}/run")
async def run_workflow_parameterized(filename: str, req: WorkflowParameterizedRunRequest):
    """Run a saved workflow with parameter overrides.

    Loads the workflow JSON from disk, applies ``params`` via ``patch_workflow``
    (matching by node id, ``_meta.title``, or convenience alias), then
    queues it on the chosen instance. The response includes the patch
    report so callers can see which params didn't land.
    """
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not wf_path.exists():
        raise HTTPException(status_code=404, detail="Workflow not found")
    base_workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    if not isinstance(base_workflow, dict):
        raise HTTPException(status_code=400, detail="Stored workflow is not a JSON object")
    patched, report = patch_workflow(base_workflow, req.params or {})
    _validate_workflow_payload(patched)
    saved_policy = _load_metadata(wf_path.name).get("placement_policy")
    queue_resp = await _queue_on_instance(
        patched,
        req.instance_id,
        req.client_id,
        placement=_placement_payload(req.placement) if req.placement is not None else saved_policy,
    )
    return {**queue_resp, "patch_report": report}


@router.post("/api/workflows/{filename}/queue")
async def queue_workflow(filename: str, instance_id: str | None = None,
                          client_id: str | None = None):
    """Load a workflow JSON and queue it on a ComfyUI instance."""
    wf_path = safe_child_path(WORKFLOWS_DIR, filename, suffix=".json")
    if not wf_path.exists():
        raise HTTPException(status_code=404, detail="Workflow not found")
    workflow = json.loads(wf_path.read_text(encoding="utf-8"))
    saved_policy = _load_metadata(wf_path.name).get("placement_policy")
    return await _queue_on_instance(
        workflow, instance_id, client_id, placement=saved_policy,
    )
