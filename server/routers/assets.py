"""ComfyUI asset management API.

Wires the new ``install_model.sh`` arms (``comfy-asset``, ``comfy-node``)
behind HTTP routes so ComfyUI checkpoints, VAEs, CLIPs, LoRAs, custom nodes,
etc. can be installed, uploaded, listed, scanned, and deleted from the API
or CLI - no SSH-into-WSL required.

Storage:

* HF/Xet-driven downloads land as real files in
  ``COMFYUI_DIR/models/<category>/``.
* Uploaded files land in the same directory after streaming through a
  ``.part`` tempfile and an atomic rename.
* Custom nodes live under ``COMFYUI_DIR/custom_nodes/<name>/``.

Path safety: every category is matched against ``COMFYUI_MODEL_CATEGORIES``
(allowlist); every filename is single-segment-validated via
``safe_child_path`` and required to carry a known weight extension.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from config import (
    ASSET_WEIGHT_EXTS,
    COMFYUI_CACHE_DIR,
    COMFYUI_DIR,
    COMFYUI_MODEL_CATEGORIES,
    OMNI_ASSET_UPLOAD_MAX_BYTES,
    OMNI_ASSET_UPLOAD_MAX_GB,
    WORKFLOWS_DIR,
    WORKER_LOG_DIR,
)

from helpers import safe_child_path, safe_subtree_path
from jobs import DuplicateJobError, hf_tqdm_parser
from state import SERVER_DIR, comfy_manager, comfy_registry, jobs

logger = logging.getLogger(__name__)
_UPLOADING: set[Path] = set()

router = APIRouter()

_INSTALLED_TEMPLATE_CACHE_TTL_SECONDS = 30.0
_installed_template_cache: tuple[float, list[dict]] | None = None
_installed_template_cache_lock = threading.Lock()

_CATEGORY_SET = frozenset(COMFYUI_MODEL_CATEGORIES)
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_NODE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
# Matches the URL forms `install_model.sh comfy-node` accepts.
_NODE_URL_RE = re.compile(r"^(?:https?://[A-Za-z0-9.\-/_:%?=&+~#@]+|git@[A-Za-z0-9.\-]+:[A-Za-z0-9._\-/]+)$")
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,127}$")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_WEIGHT_EXT_SET = set(ASSET_WEIGHT_EXTS)

_MODEL_INPUT_CATEGORIES = {
    "ckpt_name": "checkpoints",
    "checkpoint_name": "checkpoints",
    "unet_name": "diffusion_models",
    "diffusion_model_name": "diffusion_models",
    "vae_name": "vae",
    "clip_name": "text_encoders",
    "text_encoder": "text_encoders",
    "text_encoder_name": "text_encoders",
    "lora_name": "loras",
    "control_net_name": "controlnet",
    "controlnet_name": "controlnet",
    "style_model_name": "style_models",
    "gligen_name": "gligen",
    "hypernetwork_name": "hypernetworks",
    "upscale_model_name": "upscale_models",
    "audio_encoder_name": "audio_encoders",
    "projection": "clip_projections",
    "bg_removal_name": "background_removal",
    "detector_variant": "detection",
}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class InstallAssetRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    file: str | None = None
    name: str | None = None


class InstallAssetUrlRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2048)
    name: str | None = None


class InstallNodeRequest(BaseModel):
    repo_url: str = Field(min_length=4, max_length=512)
    ref: str | None = None


class InstallBlueprintAssetsRequest(BaseModel):
    filename: str | None = Field(default=None, max_length=180)
    missing_only: bool = True
    limit: int = Field(default=500, ge=1, le=2000)
    categories: list[str] = Field(default_factory=list)
    templates: list[str] = Field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def _guard_asset_mutation(target: Path | None = None, *, deletion=False) -> None:
    if comfy_manager.maintenance_reason:
        raise HTTPException(409, "ComfyUI maintenance is active")
    if deletion and any(instance.status != "dead" for instance in comfy_registry.all_instances()):
        raise HTTPException(409, "Stop ComfyUI instances before deleting installed assets")
    candidates = list(_UPLOADING)
    for job in jobs.list(kinds={"comfy_asset_install", "comfy_model_install", "comfy_extension", "comfy_node_install", "default_install"}):
        if job.status not in {"queued", "running", "cancelling"}:
            continue
        path = (job.meta or {}).get("target_path") or (job.meta or {}).get("target")
        if not path or target is None:
            raise HTTPException(409, "A shared Comfy asset mutation is in progress")
        candidates.append(Path(path))
    if target is None and candidates:
        raise HTTPException(409, "A Comfy asset upload is in progress")
    if target is not None:
        target = target.resolve()
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate == target or target in candidate.parents or candidate in target.parents:
                raise HTTPException(409, "This asset destination is already being modified")


def _require_category(category: str) -> str:
    if category not in _CATEGORY_SET:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown ComfyUI category: {category}. Valid: {sorted(_CATEGORY_SET)}",
        )
    return category


def _category_dirs(category: str) -> list[Path]:
    """Return only ComfyUI's canonical, self-contained model category."""
    return [COMFYUI_DIR / "models" / category]


def _model_root() -> Path:
    return COMFYUI_DIR / "models"


def _model_relpath(name: str) -> str:
    """Validate a model path relative to its allowlisted category."""
    value = str(name or "").replace("\\", "/").strip()
    if not value or len(value) > 512:
        raise HTTPException(status_code=400, detail="Invalid model path")
    suffix = Path(value).suffix.lower()
    if suffix not in ASSET_WEIGHT_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Model path must end with one of: {', '.join(ASSET_WEIGHT_EXTS)}",
        )
    # The helper enforces relative path segments and containment, including
    # existing symlinks that try to escape the canonical category directory.
    safe_subtree_path(_model_root(), value)
    return value


def _model_target(category: str, name: str) -> Path:
    category_root = _model_root() / _require_category(category)
    return safe_subtree_path(category_root, _model_relpath(name))


def _validate_filename(name: str) -> str:
    """Filename must be single-segment, weight-extension, no traversal."""
    if not name or len(name) > 255:
        raise HTTPException(status_code=400, detail="Invalid filename")
    suffix = Path(name).suffix.lower()
    if suffix not in ASSET_WEIGHT_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Filename must end with one of: {', '.join(ASSET_WEIGHT_EXTS)}",
        )
    return name


def _validate_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if not _REPO_RE.fullmatch(repo):
        raise HTTPException(status_code=400,
                            detail="Invalid HF repo (expect <org>/<name>)")
    return repo


def _validate_local_name(name: str | None) -> str | None:
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None
    return _model_relpath(name)


def _validate_hub_file(name: str | None) -> str | None:
    if name is None:
        return None
    name = name.strip()
    if not name:
        return None
    # Hub paths can contain slashes (subfolders inside the repo) but must not
    # try to escape, contain NUL, or be absolute.
    if "\x00" in name or name.startswith("/") or ".." in name.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file path inside repo")
    if len(name) > 512:
        raise HTTPException(status_code=400, detail="File path too long")
    return name


def _validate_hf_asset_url(url: str) -> tuple[str, str, str, str]:
    url = (url or "").strip()
    parsed = _parse_hf_url(url)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="Direct ComfyUI downloads require a HuggingFace /resolve/ or /blob/ URL so the installer can use hf_xet",
        )
    repo, revision, hub_file = parsed
    default_name = Path(hub_file).name
    _validate_filename(default_name)
    return url, repo, revision, hub_file


def _validate_direct_filename(name: str | None, default_name: str) -> str:
    chosen = (name or default_name or "").strip()
    return _model_relpath(chosen)


def _blueprint_dir() -> Path:
    return COMFYUI_DIR / "blueprints"


def _template_roots() -> list[tuple[str, Path]]:
    """Mutable ComfyUI template roots that can change without a restart."""
    roots = [("blueprint", _blueprint_dir())]
    if WORKFLOWS_DIR.exists():
        roots.append(("saved-workflow", WORKFLOWS_DIR))
    user_workflows = COMFYUI_DIR / "user" / "default" / "workflows"
    if user_workflows.exists():
        roots.append(("workflow", user_workflows))
    return roots


def invalidate_template_cache() -> None:
    global _installed_template_cache
    with _installed_template_cache_lock:
        _installed_template_cache = None


def _installed_template_files() -> list[dict]:
    """Discover templates shipped by the currently installed ComfyUI wheels.

    Modern ComfyUI no longer keeps its default workflows in the checkout's
    ``blueprints`` directory.  They are split across versioned
    ``comfyui-workflow-templates-*`` distributions.  Inspect distribution
    file manifests with a short cache; core-update qualification explicitly
    invalidates it so newly installed template packages are visible.
    """
    global _installed_template_cache
    now = time.monotonic()
    with _installed_template_cache_lock:
        cached = _installed_template_cache
    if cached is not None and now - cached[0] < _INSTALLED_TEMPLATE_CACHE_TTL_SECONDS:
        return [dict(item) for item in cached[1]]

    found: list[dict] = []
    try:
        distributions = list(importlib.metadata.distributions())
    except Exception as exc:  # pragma: no cover - interpreter packaging fault
        logger.warning("Could not enumerate ComfyUI template packages: %s", exc)
        return found

    for dist in distributions:
        raw_name = str(dist.metadata.get("Name") or "")
        normalized = re.sub(r"[-_.]+", "-", raw_name).lower()
        if not (
            normalized.startswith("comfyui-workflow-templates")
            or normalized.startswith("comfyui-subgraph-blueprints")
        ):
            continue
        version = str(getattr(dist, "version", "") or "")
        for relative in dist.files or ():
            parts = tuple(str(part).lower() for part in Path(str(relative)).parts)
            if Path(str(relative)).suffix.lower() != ".json":
                continue
            if "templates" not in parts and "blueprints" not in parts:
                continue
            try:
                path = Path(dist.locate_file(relative)).resolve()
            except (OSError, TypeError, ValueError):
                continue
            if not path.is_file():
                continue
            source = "package-blueprint" if "blueprints" in parts else "package-template"
            found.append({
                "source": source,
                "path": path,
                "template_id": path.stem,
                "package": raw_name,
                "package_version": version,
            })
    result = sorted(
        found,
        key=lambda item: (
            item["source"], item["package"].lower(), item["template_id"].lower(),
        ),
    )
    with _installed_template_cache_lock:
        _installed_template_cache = (time.monotonic(), result)
    return [dict(item) for item in result]


def _template_files(filename: str | None = None) -> list[dict]:
    files: list[dict] = []
    for source, root in _template_roots():
        if not root.exists():
            continue
        try:
            files.extend({
                "source": source,
                "path": p,
                "template_id": p.stem,
                "package": "",
                "package_version": "",
            } for p in sorted(root.rglob("*.json")) if p.is_file())
        except OSError:
            continue
    files.extend(_installed_template_files())
    if not filename:
        return files
    selector = str(filename).strip()
    if not selector or Path(selector).name != selector or "/" in selector or "\\" in selector:
        raise HTTPException(status_code=400, detail="Invalid template selector")
    selector_stem = Path(selector).stem
    return [
        item for item in files
        if item["path"].name == selector or item["template_id"] == selector_stem
    ]


def _extract_blueprint_models(value) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        models = value.get("models")
        if isinstance(models, list):
            found.extend(m for m in models if isinstance(m, dict))
        for child in value.values():
            found.extend(_extract_blueprint_models(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_extract_blueprint_models(child))
    return found


def _clean_url(raw: str) -> str:
    return raw.rstrip(").,;]")


def _parse_hf_url(url: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "huggingface.co":
        return None
    parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
    marker = None
    for candidate in ("resolve", "blob"):
        if candidate in parts:
            marker = parts.index(candidate)
            break
    if marker is None or marker < 1 or len(parts) <= marker + 2:
        return None
    repo = "/".join(parts[:marker])
    revision = parts[marker + 1]
    hub_file = "/".join(parts[marker + 2:])
    if not repo or not revision or not hub_file:
        return None
    return repo, revision, hub_file


def _category_from_hub_file(hub_file: str) -> str | None:
    parts = [part for part in hub_file.split("/") if part]
    for part in reversed(parts[:-1]):
        if part in _CATEGORY_SET:
            return part
    return None


def _extract_hf_links(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        for match in _URL_RE.findall(value):
            url = _clean_url(match)
            if _parse_hf_url(url):
                found.append(url)
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_extract_hf_links(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_extract_hf_links(child))
    return found


def _looks_like_weight_name(value: str) -> bool:
    if not value or len(value) > 512:
        return False
    if value in {"None", "none", "default"}:
        return False
    return Path(value).suffix.lower() in _WEIGHT_EXT_SET


def _normalise_input_name(name: str) -> str:
    lowered = (name or "").strip().lower()
    lowered = re.sub(r"_(\d+)$", "", lowered)
    lowered = re.sub(r"(?<=name)\d+$", "", lowered)
    return lowered


def _category_for_workflow_input(field: str, class_type: str | None = None) -> str | None:
    norm = _normalise_input_name(field)
    cls = (class_type or "").lower()
    if "clipvision" in cls or "clip_vision" in cls:
        return "clip_vision"
    if norm == "upscale_model_name" and (
        "latent" in cls or "spatialrefine" in cls
    ):
        return "latent_upscale_models"
    if norm == "duration_head_name":
        return "model_patches"
    if norm in _MODEL_INPUT_CATEGORIES:
        return _MODEL_INPUT_CATEGORIES[norm]
    if norm == "model_name":
        if "latentupscale" in cls or "latent_upscale" in cls:
            return "latent_upscale_models"
        if "upscale" in cls:
            return "upscale_models"
        if "frame" in cls and "interpolation" in cls:
            return "frame_interpolation"
    if norm == "name":
        if "control" in cls:
            return "controlnet"
        if "patch" in cls:
            return "model_patches"
    return None


def _installed_asset_inventory() -> dict[str, dict[str, set[str]]]:
    """Build one recursive, filesystem-backed inventory for workflow checks."""
    inventory: dict[str, dict[str, set[str]]] = {}
    for category in COMFYUI_MODEL_CATEGORIES:
        relative: set[str] = set()
        basenames: set[str] = set()
        for root in _category_dirs(category):
            if not root.exists():
                continue
            try:
                paths = root.rglob("*")
                for path in paths:
                    if not path.is_file() or path.stat().st_size <= 0 or path.suffix.lower() not in _WEIGHT_EXT_SET:
                        continue
                    try:
                        rel = path.relative_to(root).as_posix().casefold()
                    except ValueError:
                        continue
                    relative.add(rel)
                    basenames.add(path.name.casefold())
            except OSError:
                continue
        inventory[category] = {"relative": relative, "basenames": basenames}
    return inventory


def _workflow_weight_inputs(value) -> list[dict]:
    """Return every weight-looking widget/input, including unknown custom fields."""
    found: list[dict] = []

    def add(raw, field: str, node_type: str) -> None:
        if not isinstance(raw, str) or not _looks_like_weight_name(raw):
            return
        clean = raw.replace("\\", "/").strip("/")
        if not clean or ".." in Path(clean).parts:
            clean = Path(raw).name
        found.append({"name": clean, "field": field, "node_type": node_type})

    def walk(node) -> None:
        if isinstance(node, dict):
            class_type = str(node.get("class_type") or "")
            raw_type = str(node.get("type") or "")
            node_type = str(class_type or raw_type or node.get("title") or "")
            infer_node = bool(class_type) or not _UUID_RE.fullmatch(raw_type)
            inputs = node.get("inputs")
            if infer_node and isinstance(inputs, dict):
                for field, raw in inputs.items():
                    add(raw, str(field), node_type)
            elif infer_node and isinstance(inputs, list):
                widgets = node.get("widgets_values")
                if isinstance(widgets, list):
                    widget_index = 0
                    for item in inputs:
                        if not isinstance(item, dict) or item.get("widget") is None:
                            continue
                        if widget_index >= len(widgets):
                            break
                        raw = widgets[widget_index]
                        widget_index += 1
                        field = str(item.get("name") or (item.get("widget") or {}).get("name") or "")
                        add(raw, field, node_type)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def _category_from_object_info(
    field: str,
    class_type: str,
    object_info: dict | None,
    inventory: dict[str, dict[str, set[str]]] | None = None,
) -> str | None:
    """Infer a custom loader's model folder from its live Comfy input schema."""
    known = _category_for_workflow_input(field, class_type)
    if known:
        return known

    combined = f"{field} {class_type}".casefold().replace("-", "_")
    keyword_categories = (
        (("clip_vision", "clipvision"), "clip_vision"),
        (("text_encoder", "textencoder"), "text_encoders"),
        (("controlnet", "control_net"), "controlnet"),
        (("diffusion", "unet"), "diffusion_models"),
        (("checkpoint", "ckpt"), "checkpoints"),
        (("lora",), "loras"),
        (("vae",), "vae"),
        (("upscale",), "upscale_models"),
    )
    for needles, category in keyword_categories:
        if any(needle in combined for needle in needles):
            return category

    info = (object_info or {}).get(class_type)
    if not isinstance(info, dict):
        return None
    input_groups = info.get("input") if isinstance(info.get("input"), dict) else {}
    spec = None
    for group_name in ("required", "optional", "hidden"):
        group = input_groups.get(group_name)
        if isinstance(group, dict) and field in group:
            spec = group[field]
            break
    options = spec[0] if isinstance(spec, (list, tuple)) and spec else None
    # V3/dynamic Comfy inputs advertise a symbolic type first and place the
    # actual choices in the metadata object, e.g. ["COMBO", {"options": [...]}].
    # Treat those choices the same as the legacy [[...], {...}] schema so
    # custom artifact loaders can be resolved against the installed inventory.
    if (
        isinstance(spec, (list, tuple))
        and len(spec) > 1
        and isinstance(spec[1], dict)
        and isinstance(spec[1].get("options"), (list, tuple))
    ):
        options = spec[1]["options"]
    if not isinstance(options, (list, tuple)):
        return None
    option_names = {
        str(item).replace("\\", "/").strip("/").casefold()
        for item in options
        if isinstance(item, str) and _looks_like_weight_name(item)
    }
    if not option_names or not inventory:
        return None
    scores: list[tuple[int, str]] = []
    for category, names in inventory.items():
        score = sum(
            1 for option in option_names
            if option in names["relative"] or Path(option).name in names["basenames"]
        )
        if score:
            scores.append((score, category))
    scores.sort(reverse=True)
    if not scores or (len(scores) > 1 and scores[0][0] == scores[1][0]):
        return None
    return scores[0][1]


def _extract_workflow_model_refs(
    value,
    template: str,
    *,
    object_info: dict | None = None,
    inventory: dict[str, dict[str, set[str]]] | None = None,
) -> list[dict]:
    refs: list[dict] = []

    def add_ref(category: str | None, name: str, node_type: str | None, field: str) -> None:
        if not category or category not in _CATEGORY_SET:
            return
        if not _looks_like_weight_name(name):
            return
        clean_name = name.replace("\\", "/").strip("/")
        refs.append({
            "blueprint": template,
            "template": template,
            "name": clean_name,
            "category": category,
            "url": "",
            "source": "workflow",
            "sources": ["workflow"],
            "downloadable": False,
            "installed": _asset_installed(category, clean_name, inventory=inventory),
            "references": [{
                "node_type": node_type or "",
                "field": field,
            }],
        })

    for item in _workflow_weight_inputs(value):
        category = _category_from_object_info(
            item["field"], item["node_type"], object_info, inventory,
        )
        add_ref(category, item["name"], item["node_type"], item["field"])
    return refs


def _asset_installed(
    category: str,
    name: str,
    *,
    inventory: dict[str, dict[str, set[str]]] | None = None,
) -> bool:
    relative_name = str(name or "").replace("\\", "/").strip("/")
    normalized = relative_name.casefold()
    if inventory is not None:
        names = inventory.get(category) or {"relative": set(), "basenames": set()}
        return (
            normalized in names["relative"]
        )
    for root in _category_dirs(category):
        candidate = root / relative_name
        if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
            return True
    return False


def _normalise_blueprint_model(raw: dict, blueprint: str) -> dict | None:
    category = str(raw.get("directory") or raw.get("save_path") or "").strip()
    if category not in _CATEGORY_SET:
        return None
    name = str(raw.get("filename") or raw.get("name") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not name or not url:
        return None
    parsed = _parse_hf_url(url)
    reason = "" if parsed else "Only HuggingFace /resolve/ or /blob/ URLs are installable via Xet"
    clean_name = name.replace("\\", "/").strip("/")
    if not clean_name or ".." in Path(clean_name).parts:
        clean_name = Path(name).name
    return {
        "blueprint": blueprint,
        "template": blueprint,
        "name": clean_name,
        "category": category,
        "url": url,
        "repo": parsed[0] if parsed else "",
        "file": parsed[2] if parsed else "",
        "source": "declared",
        "sources": ["declared"],
        "downloadable": bool(parsed),
        "xet": bool(parsed),
        "reason": reason,
        "installed": _asset_installed(category, clean_name),
    }


def _normalise_link_model(url: str, blueprint: str) -> dict | None:
    parsed = _parse_hf_url(url)
    if not parsed:
        return None
    repo, _revision, hub_file = parsed
    name = Path(hub_file).name
    if not _looks_like_weight_name(name):
        return None
    category = _category_from_hub_file(hub_file)
    if category not in _CATEGORY_SET:
        return None
    return {
        "blueprint": blueprint,
        "template": blueprint,
        "name": name,
        "category": category,
        "url": url,
        "repo": repo,
        "file": hub_file,
        "source": "link",
        "sources": ["link"],
        "downloadable": True,
        "xet": True,
        "reason": "",
        "installed": _asset_installed(category, name),
    }


def _merge_template_model(target: dict, incoming: dict) -> None:
    template = incoming.get("template") or incoming.get("blueprint")
    if template:
        templates = target.setdefault("templates", [])
        if template not in templates:
            templates.append(template)
    for source in incoming.get("sources") or [incoming.get("source") or "unknown"]:
        if source and source not in target.setdefault("sources", []):
            target["sources"].append(source)
    if incoming.get("url") and (not target.get("url") or (
        incoming.get("downloadable") and not target.get("downloadable")
    )):
        target["url"] = incoming["url"]
        target["repo"] = incoming.get("repo", "")
        target["file"] = incoming.get("file", "")
        target["downloadable"] = bool(incoming.get("downloadable"))
        target["xet"] = bool(incoming.get("xet"))
        target["reason"] = incoming.get("reason", "")
    if incoming.get("downloadable"):
        target["downloadable"] = True
        target["xet"] = True
        if not target.get("reason"):
            target["reason"] = ""
    if incoming.get("references"):
        target.setdefault("references", []).extend(incoming["references"])
    target["installed"] = _asset_installed(target["category"], target["name"])
    target["source"] = ",".join(target.get("sources") or [])


def _decorate_model_api(model: dict) -> dict:
    """Attach deterministic storage and lifecycle actions to one model row."""
    try:
        target = _model_target(str(model.get("category") or ""), str(model.get("name") or ""))
    except HTTPException:
        return model
    category = str(model["category"])
    name = _model_relpath(str(model["name"]))
    encoded_name = quote(name, safe="/")
    model.update({
        "storage": "comfyui-native",
        "model_root": str(_model_root()),
        "target_directory": str(_model_root() / category),
        "target_path": str(target),
        "delete": {
            "method": "DELETE",
            "path": f"/api/assets/comfy/{category}/{encoded_name}",
            "available": bool(model.get("installed")),
        },
    })
    if model.get("downloadable") and model.get("url"):
        action = {
            "method": "POST",
            "path": f"/api/assets/comfy/{category}/install-url",
            "body": {"url": model["url"], "name": name},
            "transport": "huggingface-hub+xet",
            "target_path": str(target),
        }
        model["install"] = action
        model["download"] = action
    return model


def _scan_blueprint_requirements(filename: str | None = None) -> dict:
    by_key: dict[tuple[str, str], dict] = {}
    per_blueprint: dict[str, dict] = {}
    package_versions: dict[str, str] = {}
    source_counts: dict[str, int] = {}
    for template_file in _template_files(filename):
        source = template_file["source"]
        path = template_file["path"]
        template_id = template_file["template_id"]
        package = template_file.get("package") or ""
        package_version = template_file.get("package_version") or ""
        source_counts[source] = source_counts.get(source, 0) + 1
        if package:
            package_versions[package] = package_version
        entry_key = template_id
        if entry_key in per_blueprint:
            qualifier = package or source
            entry_key = f"{qualifier}:{template_id}"
        entry = {
            "id": entry_key,
            "filename": path.name,
            "template": entry_key,
            "source": source,
            "package": package,
            "package_version": package_version,
            "model_count": 0,
            "missing_count": 0,
            "downloadable_missing_count": 0,
            "models": [],
        }
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            entry["error"] = str(e)[:300]
            per_blueprint[entry_key] = entry
            continue
        candidates: list[dict] = []
        candidates.extend(
            model for model in
            (_normalise_blueprint_model(raw, entry_key)
             for raw in _extract_blueprint_models(data))
            if model is not None
        )
        candidates.extend(
            model for model in
            (_normalise_link_model(url, entry_key)
             for url in _extract_hf_links(data))
            if model is not None
        )
        candidates.extend(_extract_workflow_model_refs(data, entry_key))

        entry_seen: set[tuple[str, str]] = set()
        for model in candidates:
            key = (model["category"], model["name"])
            if key not in by_key:
                by_key[key] = {
                    "blueprint": model.get("blueprint") or entry_key,
                    "template": model.get("template") or entry_key,
                    "templates": [],
                    "name": model["name"],
                    "category": model["category"],
                    "url": "",
                    "repo": "",
                    "file": "",
                    "source": "",
                    "sources": [],
                    "downloadable": False,
                    "xet": False,
                    "reason": "No installable HuggingFace/Xet URL was found in the template",
                    "installed": False,
                    "references": [],
                }
            _merge_template_model(by_key[key], model)
            if key not in entry_seen:
                entry_seen.add(key)
                entry["models"].append(by_key[key])
        per_blueprint[entry_key] = entry
    items = sorted(by_key.values(), key=lambda m: (m["category"], m["name"]))
    for model in items:
        _decorate_model_api(model)
    for entry in per_blueprint.values():
        entry["model_count"] = len(entry["models"])
        entry["missing_count"] = sum(1 for m in entry["models"] if not m["installed"])
        entry["downloadable_missing_count"] = sum(
            1 for m in entry["models"]
            if not m["installed"] and m.get("downloadable")
        )
    missing = [m for m in items if not m["installed"]]
    downloadable_missing = [m for m in missing if m.get("downloadable")]
    undownloadable = [m for m in missing if not m.get("downloadable")]
    return {
        "blueprints": list(per_blueprint.values()),
        "templates": list(per_blueprint.values()),
        "models": items,
        "missing": missing,
        "downloadable_missing": downloadable_missing,
        "undownloadable": undownloadable,
        "total_blueprints": len(per_blueprint),
        "total_templates": len(per_blueprint),
        "total_models": len(items),
        "total_missing": len(missing),
        "total_downloadable_missing": len(downloadable_missing),
        "total_undownloadable": len(undownloadable),
        "source_counts": source_counts,
        "template_packages": [
            {"name": name, "version": version}
            for name, version in sorted(package_versions.items())
        ],
        "storage": {
            "mode": "comfyui-native",
            "model_root": str(_model_root()),
            "shared_model_roots_scanned": False,
        },
    }


def _template_inventory_summary(
    filename: str | None = None,
    *,
    include_templates: bool = False,
) -> dict:
    """Return live template/package inventory without parsing workflows.

    Requirement extraction recursively walks every current workflow and can
    be intentionally expensive.  Counts and package-version discovery only
    need distribution manifests, so expose a small fast path for assistants
    and status screens that do not need model-level requirements.
    """
    files = _template_files(filename)
    source_counts: dict[str, int] = {}
    package_versions: dict[str, str] = {}
    rows: list[dict] = []
    for item in files:
        source = str(item.get("source") or "unknown")
        package = str(item.get("package") or "")
        package_version = str(item.get("package_version") or "")
        source_counts[source] = source_counts.get(source, 0) + 1
        if package:
            package_versions[package] = package_version
        if include_templates:
            rows.append({
                "id": item["template_id"],
                "filename": item["path"].name,
                "source": source,
                "package": package,
                "package_version": package_version,
            })
    return {
        "summary_only": True,
        "total_blueprints": len(files),
        "total_templates": len(files),
        "source_counts": source_counts,
        "template_packages": [
            {"name": name, "version": version}
            for name, version in sorted(package_versions.items())
        ],
        "templates": rows,
    }


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
@router.get("/api/assets/comfy")
async def list_all_assets():
    """Alias of /api/comfy/models - one shot of every category."""
    return {
        "models": comfy_manager.get_installed_models(),
        "storage": {
            "mode": "comfyui-native",
            "model_root": str(_model_root()),
            "shared_model_roots_scanned": False,
        },
    }


@router.get("/api/assets/comfy/storage")
async def comfy_asset_storage():
    """Describe the one canonical model tree used by ComfyUI and this API."""
    root = _model_root()
    root.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(str(root))
    return {
        "mode": "comfyui-native",
        "model_root": str(root),
        "inside_comfyui_tree": True,
        "shared_model_roots_scanned": False,
        "xet_downloads": True,
        "xet_cache": str(COMFYUI_CACHE_DIR / "huggingface" / "xet"),
        "free_bytes": usage.free,
        "categories": {
            category: str(root / category)
            for category in COMFYUI_MODEL_CATEGORIES
        },
    }


@router.get("/api/assets/comfy/scan")
async def scan_assets():
    """Walk every category dir, summarise total size + extension breakdown.

    Defined before the ``{category}`` route so FastAPI matches the literal
    ``scan`` segment first.
    """
    by_category: dict[str, dict] = {}
    by_extension: dict[str, dict] = {}
    grand_total = 0
    grand_count = 0
    for category in COMFYUI_MODEL_CATEGORIES:
        cat_total = 0
        cat_count = 0
        for root in _category_dirs(category):
            if not root.exists():
                continue
            try:
                for entry in root.rglob("*"):
                    if not entry.is_file():
                        continue
                    suffix = entry.suffix.lower()
                    if suffix not in ASSET_WEIGHT_EXTS:
                        continue
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        continue
                    cat_total += size
                    cat_count += 1
                    ext_entry = by_extension.setdefault(suffix, {"count": 0, "size_bytes": 0})
                    ext_entry["count"] += 1
                    ext_entry["size_bytes"] += size
            except OSError:
                continue
        by_category[category] = {
            "count": cat_count,
            "size_bytes": cat_total,
            "size_gb": round(cat_total / (1024**3), 3),
        }
        grand_total += cat_total
        grand_count += cat_count
    for ext_entry in by_extension.values():
        ext_entry["size_gb"] = round(ext_entry["size_bytes"] / (1024**3), 3)
    return {
        "total_bytes": grand_total,
        "total_gb": round(grand_total / (1024**3), 3),
        "total_files": grand_count,
        "by_category": by_category,
        "by_extension": by_extension,
    }


# ---------------------------------------------------------------------------
# Blueprint model requirements
# ---------------------------------------------------------------------------
@router.get("/api/assets/comfy/blueprints/requirements")
async def blueprint_requirements(filename: str | None = None,
                                 missing_only: bool = False,
                                 summary_only: bool = False,
                                 include_templates: bool = False):
    """Scan current ComfyUI templates for declared model requirements.

    Sources are rediscovered on every call: packaged default templates,
    subgraph blueprints, and mutable user workflows.  This intentionally
    avoids a hard-coded template catalog and survives ComfyUI package updates.
    """
    if summary_only:
        report = await asyncio.to_thread(
            _template_inventory_summary,
            filename,
            include_templates=include_templates,
        )
    else:
        # Full model-reference extraction is CPU/filesystem work.  Keep it off
        # FastAPI's event loop so health/status/stop routes remain responsive
        # while a comprehensive scan is running.
        report = await asyncio.to_thread(_scan_blueprint_requirements, filename)
    if filename and report["total_templates"] == 0:
        raise HTTPException(status_code=404, detail=f"Template not found: {filename}")
    if missing_only and not summary_only:
        report["models"] = list(report["missing"])
    return report


def _filter_blueprint_pending(base_pending: list[dict], req: InstallBlueprintAssetsRequest) -> list[dict]:
    categories = {c for c in (req.categories or []) if c}
    invalid_categories = sorted(c for c in categories if c not in _CATEGORY_SET)
    if invalid_categories:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI categories: {invalid_categories}")
    templates = {Path(t).name for t in (req.templates or []) if t}
    pending = [
        item for item in base_pending
        if item.get("downloadable") and item.get("url")
    ]
    if categories:
        pending = [item for item in pending if item.get("category") in categories]
    if templates:
        pending = [
            item for item in pending
            if templates.intersection(set(item.get("templates") or [item.get("template") or item.get("blueprint") or ""]))
        ]
    return pending


@router.get("/api/assets/comfy/templates/requirements")
async def template_requirements(filename: str | None = None,
                                missing_only: bool = False,
                                summary_only: bool = False,
                                include_templates: bool = False):
    """Alias using ComfyUI's current template terminology."""
    return await blueprint_requirements(
        filename=filename,
        missing_only=missing_only,
        summary_only=summary_only,
        include_templates=include_templates,
    )


@router.post("/api/assets/comfy/blueprints/install")
async def install_blueprint_assets(req: InstallBlueprintAssetsRequest):
    """Install missing assets referenced by one or all current blueprints."""
    scan = _scan_blueprint_requirements(req.filename)
    if req.filename and scan["total_templates"] == 0:
        raise HTTPException(status_code=404, detail=f"Template not found: {req.filename}")
    base_pending = scan["missing"] if req.missing_only else scan["models"]
    pending = _filter_blueprint_pending(base_pending, req)
    if not pending:
        return {
            "status": "ready",
            "job_id": None,
            "missing": len(scan["missing"]),
            "downloadable_missing": len(scan["downloadable_missing"]),
            "undownloadable": len(scan["undownloadable"]),
            "message": "No missing template assets with HuggingFace/Xet URLs",
        }
    if req.dry_run:
        return {
            "status": "planned",
            "job_id": None,
            "install_count": min(len(pending), req.limit),
            "items": pending[:req.limit],
            "total_pending": len(pending),
            "policy": {
                "categories": req.categories or [],
                "templates": req.templates or [],
                "missing_only": req.missing_only,
                "limit": req.limit,
            },
        }

    _guard_asset_mutation()

    log_uniq = str(uuid.uuid4())[:8]
    stem = "blueprints"
    if req.filename:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(req.filename).stem)
    log_path = WORKER_LOG_DIR / f"install_blueprint_assets_{stem}_{log_uniq}.log"
    script = SERVER_DIR / "install_model.sh"
    argv = [
        "bash", str(script), "comfy-blueprints",
        req.filename or "",
        str(req.limit),
        "1" if req.missing_only else "0",
        ",".join(req.categories or []),
        ",".join(req.templates or []),
    ]

    policy_hash = hashlib.sha1(
        json.dumps({
            "filename": req.filename or "",
            "missing_only": req.missing_only,
            "categories": req.categories or [],
            "templates": req.templates or [],
        }, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    active_key = f"blueprints:{req.filename or '*'}:{policy_hash}"
    try:
        job = jobs.enqueue_subprocess(
            kind="comfy_asset_install",
            argv=argv,
            cwd=str(SERVER_DIR),
            meta={
                "source": "blueprints",
                "name": "ComfyUI template models",
                "filename": req.filename,
                "missing_only": req.missing_only,
                "limit": req.limit,
                "categories": req.categories or [],
                "templates": req.templates or [],
                "policy_hash": policy_hash,
                "missing_at_start": len(scan["missing"]),
                "downloadable_missing_at_start": len(scan["downloadable_missing"]),
                "undownloadable_at_start": len(scan["undownloadable"]),
                "total_at_start": len(scan["models"]),
                "log_path": str(log_path),
            },
            active_key=active_key,
            log_path=str(log_path),
            progress_parser=hf_tqdm_parser,
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409,
                            detail=f"Blueprint asset install already running for {req.filename or 'all'}")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "missing_at_start": len(scan["missing"]),
        "downloadable_missing_at_start": len(scan["downloadable_missing"]),
        "undownloadable_at_start": len(scan["undownloadable"]),
        "total_at_start": len(scan["models"]),
        "install_count": min(len(pending), req.limit),
        "log_path": str(log_path),
    }


@router.post("/api/assets/comfy/templates/install")
async def install_template_assets(req: InstallBlueprintAssetsRequest):
    """Alias of the blueprint installer using current UI wording."""
    return await install_blueprint_assets(req)


# ---------------------------------------------------------------------------
# Custom nodes (literal "nodes" segment - keep above ``{category}`` routes)
# ---------------------------------------------------------------------------
@router.get("/api/assets/comfy/nodes")
async def list_assets_nodes():
    return {"nodes": comfy_manager.get_custom_nodes()}


@router.post("/api/assets/comfy/nodes/install")
async def install_assets_node(req: InstallNodeRequest):
    _guard_asset_mutation()
    repo_url = (req.repo_url or "").strip()
    if not _NODE_URL_RE.fullmatch(repo_url):
        raise HTTPException(status_code=400,
                            detail="Invalid repo_url (http(s):// or git@host:org/repo)")
    ref = (req.ref or "").strip() or None
    if ref is not None and not _GIT_REF_RE.fullmatch(ref):
        raise HTTPException(status_code=400, detail="Invalid git ref")

    derived = Path(repo_url.split("/")[-1]).name
    derived = re.sub(r"\.git$", "", derived)
    if not _NODE_NAME_RE.fullmatch(derived):
        raise HTTPException(status_code=400,
                            detail="Could not derive a safe node directory name from repo_url")

    log_uniq = str(uuid.uuid4())[:8]
    log_path = WORKER_LOG_DIR / f"install_node_{derived}_{log_uniq}.log"
    script = SERVER_DIR / "install_model.sh"
    argv = ["bash", str(script), "comfy-node", repo_url]
    if ref:
        argv.append(ref)

    try:
        job = jobs.enqueue_subprocess(
            kind="comfy_node_install",
            argv=argv,
            cwd=str(SERVER_DIR),
            meta={"repo_url": repo_url, "ref": ref, "name": derived,
                  "log_path": str(log_path)},
            active_key=f"node:{derived}",
            log_path=str(log_path),
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409,
                            detail=f"Custom node {derived} is already installing")
    return {"job_id": job.job_id, "name": derived, "ref": ref, "status": "running"}


@router.delete("/api/assets/comfy/nodes/{name}")
async def delete_assets_node(name: str):
    _guard_asset_mutation(deletion=True)
    nodes_dir = COMFYUI_DIR / "custom_nodes"
    if not nodes_dir.exists():
        raise HTTPException(status_code=404, detail="custom_nodes directory missing")
    if not _NODE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid node name")
    target = safe_child_path(nodes_dir, name)
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Node not found")
    if target.is_symlink():
        raise HTTPException(status_code=400, detail="Refusing to delete a symlink")
    try:
        shutil.rmtree(target)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"rmtree failed: {e}")
    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Per-category routes (literal "upload" before ``{category}`` so FastAPI
# routes /api/assets/comfy/upload/<cat> here, not into a category lookup
# of the literal string "upload").
# ---------------------------------------------------------------------------
@router.post("/api/assets/comfy/upload/{category}")
async def upload_asset(
    request: Request,
    category: str,
    file: UploadFile = File(...),
    sha256: str | None = Form(default=None),
    overwrite: bool = Form(default=False),
):
    _require_category(category)
    if file.filename is None:
        raise HTTPException(status_code=400, detail="filename required")
    fname = _validate_filename(Path(file.filename).name)

    target_dir = _model_root() / category
    target_dir.mkdir(parents=True, exist_ok=True)
    final_path = safe_child_path(target_dir, fname)
    _guard_asset_mutation(final_path)
    if final_path.exists() and not overwrite:
        raise HTTPException(status_code=409,
                            detail=f"{fname} already exists (use overwrite=true)")

    declared_size = None
    cl = request.headers.get("content-length")
    if cl and cl.isdigit():
        declared_size = int(cl)
        if declared_size > OMNI_ASSET_UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds cap of {OMNI_ASSET_UPLOAD_MAX_GB} GiB",
            )

    expected_sha = None
    if sha256:
        expected_sha = sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise HTTPException(status_code=400, detail="Invalid sha256 hex")

    free_bytes = shutil.disk_usage(str(target_dir)).free
    headroom = (declared_size or 1 * 1024 * 1024 * 1024) + 512 * 1024 * 1024
    if free_bytes < headroom:
        raise HTTPException(
            status_code=507,
            detail=f"Insufficient disk space ({free_bytes // (1024**3)} GiB free)",
        )

    tmp_dir = target_dir
    tmp_fd, tmp_str = tempfile.mkstemp(prefix=f".{fname}.", suffix=".part", dir=str(tmp_dir))
    tmp_path = Path(tmp_str)
    _UPLOADING.add(final_path.resolve())
    hasher = hashlib.sha256() if expected_sha else None
    written = 0
    # When Content-Length is absent (chunked transfer) the up-front headroom
    # check above could only guess, so re-check free space periodically against
    # what we've actually written to avoid filling the disk mid-stream.
    headroom_recheck = declared_size is None
    next_space_check = 64 * 1024 * 1024  # bytes
    space_margin = 512 * 1024 * 1024
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > OMNI_ASSET_UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds cap of {OMNI_ASSET_UPLOAD_MAX_GB} GiB",
                    )
                if headroom_recheck and written >= next_space_check:
                    free_now = shutil.disk_usage(str(tmp_dir)).free
                    if free_now < space_margin:
                        raise HTTPException(
                            status_code=507,
                            detail="Insufficient disk space during upload "
                                   f"({free_now // (1024**2)} MiB free)",
                        )
                    next_space_check = written + 64 * 1024 * 1024
                if hasher is not None:
                    hasher.update(chunk)
                fh.write(chunk)
        if hasher is not None and hasher.hexdigest() != expected_sha:
            raise HTTPException(status_code=422, detail="sha256 mismatch")
        if overwrite:
            os.replace(tmp_path, final_path)
        else:
            try:
                os.link(tmp_path, final_path)
            except FileExistsError:
                raise HTTPException(409, "Asset appeared during upload; overwrite=false")
            tmp_path.unlink()
        tmp_path = None  # ownership transferred
    finally:
        _UPLOADING.discard(final_path.resolve())
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
    logger.info("Uploaded %s to comfyui/%s (%d bytes)", fname, category, written)
    return {
        "status": "uploaded",
        "category": category,
        "name": fname,
        "size_bytes": written,
        "sha256": expected_sha if expected_sha else (hasher.hexdigest() if hasher else None),
        "path": str(final_path),
    }


@router.post("/api/assets/comfy/{category}/install-url")
async def install_asset_url(category: str, req: InstallAssetUrlRequest):
    _require_category(category)
    url, repo, revision, hub_file = _validate_hf_asset_url(req.url)
    local_name = _validate_direct_filename(req.name, Path(hub_file).name)
    target_path = _model_target(category, local_name)
    _guard_asset_mutation(target_path)

    log_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{category}_{repo.replace('/', '__')}_{local_name}")
    log_uniq = str(uuid.uuid4())[:8]
    log_path = WORKER_LOG_DIR / f"install_asset_url_{log_stem}_{log_uniq}.log"

    script = SERVER_DIR / "install_model.sh"
    argv = ["bash", str(script), "comfy-url", category, url, local_name]

    try:
        job = jobs.enqueue_subprocess(
            kind="comfy_asset_install",
            argv=argv,
            cwd=str(SERVER_DIR),
            meta={
                "category": category,
                "repo": repo,
                "revision": revision,
                "file": hub_file,
                "name": local_name,
                "url": url,
                "source": "direct-url",
                "target_path": str(target_path),
                "log_path": str(log_path),
            },
            active_key=f"asset-target:{target_path.resolve()}",
            log_path=str(log_path),
            progress_parser=hf_tqdm_parser,
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409,
                            detail=f"Asset install for {local_name} is already running")
    return {
        "job_id": job.job_id,
        "category": category,
        "repo": repo,
        "file": hub_file,
        "name": local_name,
        "target_path": str(target_path),
        "job_status_path": f"/api/jobs/{job.job_id}",
        "status": job.status,
    }


@router.post("/api/assets/comfy/{category}/install")
async def install_asset(category: str, req: InstallAssetRequest):
    _require_category(category)
    repo = _validate_repo(req.repo)
    hub_file = _validate_hub_file(req.file)
    local_name = _validate_local_name(req.name)
    if not hub_file:
        local_name = local_name or repo.replace("/", "_")
    target_path = _model_target(category, local_name or Path(hub_file).name)
    _guard_asset_mutation(target_path)

    log_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{category}_{repo.replace('/', '__')}")
    log_uniq = str(uuid.uuid4())[:8]
    log_path = WORKER_LOG_DIR / f"install_asset_{log_stem}_{log_uniq}.log"

    script = SERVER_DIR / "install_model.sh"
    argv = ["bash", str(script), "comfy-asset", category, repo]
    if hub_file:
        argv.append(hub_file)
    if local_name:
        if not hub_file:
            argv.append("")  # preserve positional ordering for shell
        argv.append(local_name)

    try:
        job = jobs.enqueue_subprocess(
            kind="comfy_asset_install",
            argv=argv,
            cwd=str(SERVER_DIR),
            meta={
                "category": category,
                "repo": repo,
                "file": hub_file,
                "name": local_name,
                "target_path": str(target_path) if target_path else None,
                "log_path": str(log_path),
            },
            active_key=f"asset-target:{target_path.resolve()}",
            log_path=str(log_path),
            progress_parser=hf_tqdm_parser,
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409,
                            detail=f"Asset install for {repo} is already running")
    return {
        "job_id": job.job_id,
        "category": category,
        "repo": repo,
        "file": hub_file,
        "name": local_name,
        "target_path": str(target_path) if target_path else None,
        "model_root": str(_model_root()),
        "job_status_path": f"/api/jobs/{job.job_id}",
        "status": job.status,
    }


@router.delete("/api/assets/comfy/{category}/{filename:path}")
async def delete_asset(category: str, filename: str):
    _require_category(category)
    fname = _model_relpath(filename)
    candidate = _model_target(category, fname)
    _guard_asset_mutation(candidate, deletion=True)
    if not candidate.exists() and not candidate.is_symlink():
        raise HTTPException(status_code=404, detail="Asset not found")
    if not candidate.is_file() and not candidate.is_symlink():
        raise HTTPException(status_code=409, detail="Asset path is not a file")
    try:
        size_bytes = candidate.stat().st_size if not candidate.is_symlink() else 0
        was_symlink = candidate.is_symlink()
        candidate.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"unlink failed: {e}")
    pruned_directories: list[str] = []
    category_root = (_model_root() / category).resolve()
    parent = candidate.parent
    while parent != category_root:
        try:
            parent.rmdir()
        except OSError:
            break
        pruned_directories.append(str(parent))
        parent = parent.parent
    return {
        "status": "deleted",
        "category": category,
        "name": fname,
        "path": str(candidate),
        "size_bytes": size_bytes,
        "was_symlink": was_symlink,
        "recoverable": False,
        "pruned_directories": pruned_directories,
    }


@router.get("/api/assets/comfy/{category}")
async def list_one_category(category: str):
    """Single category list - one of the COMFYUI_MODEL_CATEGORIES allowlist."""
    _require_category(category)
    all_models = comfy_manager.get_installed_models()
    return {"category": category, "files": all_models.get(category, [])}
