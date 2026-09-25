"""Model setup, install jobs, LoRA management, HF search, and HF token storage.

After phase 2 the install routes hand off to the unified ``JobStore`` (see
``server/jobs.py``). The legacy ``/api/setup/jobs*`` shape is preserved by an
adapter that translates ``Job`` records back into the old dict shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import logging
import math
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import (
    ACE_STEP_LMS,
    ACE_STEP_LORAS,
    ACE_STEP_MODEL_ID,
    ACE_STEP_MODELS,
    ACE_STEP_ROOT,
    ACE_STEP_VAES,
    AUDIO_LAB_MODEL_ID,
    AUDIO_LAB_ROOT,
    CLAP_MODELS,
    COMFYUI_DIR,
    HF_TOKEN_FILE,
    LORA_DIR,
    MODELS_DIR,
    MOSS_TTS_MODEL_ID,
    MOSS_TTS_CODEC_WEIGHTS_DIR,
    MINIMAX_MUSIC3_MODEL_ID,
    OMNI_MODEL_SETUP,
    OMNI_MODEL_VARIANTS,
    OVERRIDES_DIR,
    STABLE_AUDIO_MODELS,
    STABLE_AUDIO_VAES,
    WORKER_LOG_DIR,
    ace_step_core_status,
    ace_step_lm_path,
    ace_step_model_path,
    ace_step_vae_path,
    audio_lab_vae_path,
    audio_lab_weights_path,
    clap_weights_path,
    get_variant,
    is_ace_step_lm_installed,
    is_ace_step_lora_installed,
    is_ace_step_model_installed,
    is_ace_step_vae_installed,
    is_audio_lab_variant_installed,
    is_audio_lab_vae_installed,
    is_clap_installed,
)
from helpers import (
    bounded_lines,
    declared_size_gb,
    is_relative_to,
    safe_child_path,
    safe_subtree_path,
)
from jobs import DuplicateJobError, hf_tqdm_parser
from security import is_valid_hf_token, write_secret_file
from snapshot_install import inspect_installed_snapshot
from state import SERVER_DIR, comfy_manager, jobs, worker_registry

logger = logging.getLogger(__name__)

router = APIRouter()

_WEIGHT_EXTS = {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf"}

# Job kinds that the legacy /api/setup/jobs* alias surfaces.
_INSTALL_KINDS = {
    "model_install",
    "lora_install",
    "audio_lab_install",
    "ace_step_install",
    "default_install",
    "comfy_asset_install",
}

# repo id format: <org>/<name>, lenient on chars but bounded
_HF_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_CUSTOM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_RUNTIME_DEPS_CACHE: dict[str, tuple[float, dict]] = {}
_RUNTIME_DEPS_CACHE_TTL_S = 60.0
_HF_TOKEN_STATUS_CACHE: dict[str, object] = {}
_HF_TOKEN_STATUS_TTL_S = 300.0
_JOB_LOG_TAIL_MAX_BYTES = 512 * 1024
_KNOWN_WARNING_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"pip(?:'s)? dependency resolver does not currently take into account",
            re.IGNORECASE,
        ),
        "pip dependency resolver warning",
    ),
    (
        re.compile(r"\brequires .+ but you have .+ which is incompatible\b", re.IGNORECASE),
        "package version compatibility warning",
    ),
    (
        re.compile(r"running pip as the ['\"]?root['\"]? user", re.IGNORECASE),
        "pip root-user warning",
    ),
)


def _classify_minimax_music3_candidate(repo_id: str, tags: list[str], kind: str) -> dict:
    """Conservative compatibility label for Music 3 Hub search results."""
    repo_lower = (repo_id or "").lower()
    tag_set = {str(tag).lower() for tag in tags}
    if kind == "lora":
        return {
            "compatibility": "discovery-only",
            "runtime_compatible": False,
            "reason": (
                "No verified MiniMax Music 3 PEFT attachment contract is available; "
                "inspect component targets before adding runtime support."
            ),
        }
    if repo_lower == "minimaxai/minimax-music3":
        return {
            "compatibility": "official",
            "runtime_compatible": True,
            "reason": "Pinned official Diffusers modular snapshot.",
        }
    if repo_lower == "technobaptist/minimax-music3":
        return {
            "compatibility": "mirror-unverified",
            "runtime_compatible": False,
            "reason": "Full-size community mirror; not a verified optimization.",
        }
    if repo_lower == "diffusers/minimax-music3-aoti":
        return {
            "compatibility": "hardware-specific-supplement",
            "runtime_compatible": False,
            "reason": "Compiled supplement for RTX 6000 PRO, not a standalone 3090/3060 model.",
        }
    if "tiny" in repo_lower or "diffusers-internal-dev" in repo_lower:
        return {
            "compatibility": "test-fixture",
            "runtime_compatible": False,
            "reason": "Tiny/internal fixture is not a quality generation model.",
        }
    if "comfyui" in tag_set or repo_lower.startswith("comfy-org/"):
        return {
            "compatibility": "other-runtime",
            "runtime_compatible": False,
            "reason": "ComfyUI packaging is outside the standalone Music 3 worker.",
        }
    return {
        "compatibility": "candidate-unverified",
        "runtime_compatible": False,
        "reason": "Inspect architecture, component layout, license, and claimed optimization first.",
    }
_FAILURE_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"no space left|needs at least .+gb free|disk quota", re.IGNORECASE),
        "disk",
        "Disk space is insufficient for this install.",
    ),
    (
        re.compile(
            r"gated|accept(_url| the model card)|requires authorization|access denied|"
            r"unauthorized|401|403|license",
            re.IGNORECASE,
        ),
        "access",
        "HuggingFace access or token authorization is required.",
    ),
    (
        re.compile(
            r"temporary failure|failed to resolve|name resolution|connectionpool|"
            r"max retries|dns|read timed out|connection aborted|network is unreachable",
            re.IGNORECASE,
        ),
        "network",
        "The download could not reliably reach HuggingFace.",
    ),
    (
        re.compile(
            r"failed building wheel|could not build wheels|resolutionimpossible|"
            r"subprocess-exited-with-error|cannot install",
            re.IGNORECASE,
        ),
        "dependency",
        "Python package installation failed.",
    ),
    (
        re.compile(r"modulenotfounderror|no module named", re.IGNORECASE),
        "missing_dependency",
        "A required Python module is missing.",
    ),
)


class InstallVariantRequest(BaseModel):
    model: str
    variant_id: str


class InstallLoraRequest(BaseModel):
    repo: str
    name: str | None = None


class HFTokenRequest(BaseModel):
    token: str = Field(min_length=23, max_length=256)


def _read_hf_token() -> str:
    try:
        return HF_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _hf_token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_hf_token(token: str) -> dict:
    """Return a non-secret validation summary for a HuggingFace token."""
    if not token:
        return {"token_saved": False, "token_valid": False, "token_error": "No token saved"}
    if not is_valid_hf_token(token):
        return {
            "token_saved": False,
            "token_valid": False,
            "token_error": "Token format is invalid",
        }

    fp = _hf_token_fingerprint(token)
    now = time.time()
    cached_fp = _HF_TOKEN_STATUS_CACHE.get("fingerprint")
    cached_at = float(_HF_TOKEN_STATUS_CACHE.get("checked_at") or 0.0)
    cached_status = _HF_TOKEN_STATUS_CACHE.get("status")
    if cached_fp == fp and isinstance(cached_status, dict) and (now - cached_at) < _HF_TOKEN_STATUS_TTL_S:
        return dict(cached_status)

    status = {
        "token_saved": True,
        "token_valid": None,
        "token_user": None,
        "token_error": None,
        "checked_at": now,
    }
    req = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        status["token_valid"] = True
        status["token_user"] = payload.get("name") or payload.get("fullname")
    except urllib.error.HTTPError as e:
        status["token_valid"] = False if e.code in (401, 403) else None
        if e.code in (401, 403):
            status["token_error"] = "HuggingFace rejected this token"
        else:
            status["token_error"] = f"HuggingFace token check failed with HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        status["token_valid"] = None
        status["token_error"] = f"Could not verify token now: {e}"

    _HF_TOKEN_STATUS_CACHE.clear()
    _HF_TOKEN_STATUS_CACHE.update({
        "fingerprint": fp,
        "checked_at": now,
        "status": dict(status),
    })
    return status


# Audio Lab install request shapes
class AudioLabInstallRequest(BaseModel):
    variant_id: str = Field(min_length=1, max_length=128)


class AudioLabCustomInstallRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    name: str | None = Field(default=None, max_length=128)
    kind: str = Field(default="model")  # model | vae | clap


# ACE-Step install request shapes
class AceStepInstallRequest(BaseModel):
    variant_id: str = Field(min_length=1, max_length=128)


class AceStepLoraInstallRequest(BaseModel):
    # Registry path: pass only `name`.
    # Ad-hoc community LoRA: pass `repo` (with optional `name` override).
    name: str | None = Field(default=None, max_length=128)
    repo: str | None = Field(default=None, min_length=3, max_length=200)


class AceStepCustomInstallRequest(BaseModel):
    repo: str = Field(min_length=3, max_length=200)
    name: str | None = Field(default=None, max_length=128)
    kind: str = Field(default="model")  # model | lm | vae | lora


# ---------------------------------------------------------------------------
# Setup status
# ---------------------------------------------------------------------------
def _runtime_deps_status(model_id: str) -> dict:
    cached = _RUNTIME_DEPS_CACHE.get(model_id)
    now = time.time()
    if cached and (now - cached[0]) < _RUNTIME_DEPS_CACHE_TTL_S:
        return dict(cached[1])

    optional: dict[str, str] = {}
    if model_id == AUDIO_LAB_MODEL_ID:
        required = {
            "diffusers": "diffusers",
            "transformers": "transformers",
            "einops_exts": "einops-exts",
        }
        optional = {
            "stable_audio_tools": "stable-audio-tools native loader",
        }
    elif model_id == ACE_STEP_MODEL_ID:
        required = {
            "acestep": "ACE-Step",
            "loguru": "loguru",
            "vector_quantize_pytorch": "vector-quantize-pytorch",
            "peft": "peft",
            "diffusers": "diffusers",
        }
    else:
        return {"runtime_ready": True, "runtime_missing": []}

    def _missing_module(module: str) -> bool:
        try:
            return importlib.util.find_spec(module) is None
        except (ImportError, AttributeError, ValueError):
            return True

    missing = [
        label for module, label in required.items()
        if _missing_module(module)
    ]
    optional_missing = [
        label for module, label in optional.items()
        if _missing_module(module)
    ]
    result = {
        "runtime_ready": not missing,
        "runtime_missing": missing,
        "runtime_optional_missing": optional_missing,
        "runtime_message": (
            "" if not missing
            else "Missing runtime packages: " + ", ".join(missing)
        ),
    }
    _RUNTIME_DEPS_CACHE[model_id] = (now, result)
    return dict(result)


def _download_required_gb(declared_gb: int | float) -> int:
    try:
        multiplier = float(os.environ.get("OMNI_DOWNLOAD_HEADROOM_MULTIPLIER", "1.35"))
    except ValueError:
        multiplier = 1.35
    try:
        reserve = float(os.environ.get("OMNI_DOWNLOAD_HEADROOM_GB", "4"))
    except ValueError:
        reserve = 4.0
    return max(1, int(math.ceil(float(declared_gb or 1) * multiplier + reserve)))


def _models_disk_status() -> dict:
    try:
        usage = shutil.disk_usage(MODELS_DIR)
    except OSError as e:
        return {"available": False, "error": str(e)}
    gb = 1024 ** 3
    return {
        "available": True,
        "path": str(MODELS_DIR),
        "total_gb": round(usage.total / gb, 1),
        "used_gb": round(usage.used / gb, 1),
        "free_gb": round(usage.free / gb, 1),
    }


def _missing_default_install_items() -> list[dict]:
    items: list[dict] = []
    for model_id, info in OMNI_MODEL_SETUP.items():
        if model_id in (AUDIO_LAB_MODEL_ID, ACE_STEP_MODEL_ID):
            continue
        if info.get("default_install") is False:
            continue
        weights_dir = info.get("weights_dir")
        override = info.get("override")
        weights_ok = bool(weights_dir) and _model_weights_present(
            weights_dir, MOSS_TTS_CODEC_WEIGHTS_DIR if model_id == MOSS_TTS_MODEL_ID else None,
        )
        override_ok = True if not override else (OVERRIDES_DIR / override / ".install_complete").exists()
        if not (weights_ok and override_ok):
            items.append({
                "kind": "omni",
                "id": model_id,
                "display": info.get("display", model_id),
                "declared_gb": declared_size_gb(info.get("weights_size")),
            })

    al_assets = _list_audio_lab_assets()
    al_models = al_assets.get("models") or []
    al_claps = al_assets.get("claps") or []
    quick = next((m for m in al_models if m.get("quickstart")), None) or next((m for m in al_models if m.get("default")), None)
    if quick and not quick.get("installed"):
        items.append({
            "kind": "audio-model",
            "id": quick["variant_id"],
            "display": quick.get("display") or quick["variant_id"],
            "declared_gb": quick.get("size_gb") or 5,
        })
    clap = next((c for c in al_claps if c.get("default")), None)
    if clap and not clap.get("installed"):
        items.append({
            "kind": "clap-model",
            "id": clap["variant_id"],
            "display": clap.get("display") or clap["variant_id"],
            "declared_gb": clap.get("size_gb") or 2,
        })

    ace_model_id = next((m_id for m_id, m in ACE_STEP_MODELS.items() if m.get("default")), "ace-xl-turbo")
    ace_model = ACE_STEP_MODELS.get(ace_model_id) or {}
    if not is_ace_step_model_installed(ace_model_id):
        items.append({
            "kind": "ace-model",
            "id": ace_model_id,
            "display": ace_model.get("display") or ace_model_id,
            "declared_gb": ace_model.get("size_gb") or 19,
        })
    core = ace_step_core_status()
    if not core.get("core_ready"):
        core_model = ACE_STEP_MODELS.get("ace-1.5") or {}
        items.append({
            "kind": "ace-core",
            "id": "ace-1.5",
            "display": core_model.get("display") or "ACE-Step shared core",
            "declared_gb": core_model.get("size_gb") or 12,
        })
    default_lm = ace_model.get("default_lm")
    if default_lm and not is_ace_step_lm_installed(default_lm):
        lm = ACE_STEP_LMS.get(default_lm) or {}
        items.append({
            "kind": "ace-lm",
            "id": default_lm,
            "display": lm.get("display") or default_lm,
            "declared_gb": lm.get("size_gb") or 4,
        })
    for item in items:
        item["required_gb"] = _download_required_gb(item.get("declared_gb") or 1)
    return items


@router.get("/api/setup/status")
async def setup_status():
    return await asyncio.to_thread(_setup_status_sync)


def _setup_status_sync():
    """Check installation status of all omni models."""
    status = {}
    for model_id, info in OMNI_MODEL_SETUP.items():
        # audio_lab lives under MODELS_DIR/audio_lab/* with its own per-variant
        # registry — see /api/audio_lab/status. Skip the generic omni-model
        # path discovery here so the legacy Setup tab doesn't try to surface
        # an Install button that would call the wrong install_model.sh path.
        # ace_step uses the same dedicated-route treatment.
        if model_id in (AUDIO_LAB_MODEL_ID, ACE_STEP_MODEL_ID):
            continue

        override_name = info.get("override")
        override_ok = True
        if override_name:
            override_dir = OVERRIDES_DIR / override_name
            override_ok = (override_dir / ".install_complete").exists()

        has_weights = _model_weights_present(
            info["weights_dir"],
            MOSS_TTS_CODEC_WEIGHTS_DIR if model_id == MOSS_TTS_MODEL_ID else None,
        )

        variant_status = []
        for v in OMNI_MODEL_VARIANTS.get(model_id, []):
            v_has_weights = _model_weights_present(
                v["weights_dir"], v.get("paired_codec_dir"),
            )
            variant_status.append({
                "variant_id": v["variant_id"],
                "display": v["display"],
                "installed": v_has_weights,
                "default": v.get("default", False),
                "vram": v.get("vram"),
                "vram_gb": v.get("vram_gb"),
                "size_gb": v.get("size_gb"),
            })

        status[model_id] = {
            "display": info["display"],
            "weights_installed": has_weights,
            "override_installed": override_ok,
            "installed": has_weights and override_ok,
            "variants": variant_status,
        }

    status["comfyui"] = {
        "display": "ComfyUI",
        "installed": comfy_manager.is_installed(),
        "manager_installed": (COMFYUI_DIR / "custom_nodes" / "ComfyUI-Manager").exists(),
    }

    # Audio Lab and ACE-Step use per-asset install routes under their own tabs.
    # Surface summary status here so the Setup tab can show progress without
    # calling the legacy /api/setup/install/{model} path (which returns 400).
    al_assets = _list_audio_lab_assets()
    al_models = al_assets.get("models") or []
    al_claps = al_assets.get("claps") or []
    al_models_installed = sum(1 for m in al_models if m.get("installed"))
    al_claps_installed = sum(1 for c in al_claps if c.get("installed"))
    al_variant_status = [
        {
            "variant_id": m["variant_id"],
            "display": m["display"],
            "installed": bool(m.get("installed")),
            "default": bool(m.get("default")),
            "repo": m.get("repo"),
            "gated": bool(m.get("gated")),
            "quickstart": bool(m.get("quickstart")),
            "vram_gb": m.get("vram_gb"),
            "size_gb": m.get("size_gb"),
            "accept_url": (
                f"https://huggingface.co/{m.get('repo')}"
                if m.get("gated") and m.get("repo") else None
            ),
        }
        for m in al_models
    ]
    al_clap_status = [
        {
            "variant_id": c["variant_id"],
            "display": c["display"],
            "installed": bool(c.get("installed")),
            "default": bool(c.get("default")),
            "vram_gb": c.get("vram_gb") or c.get("size_gb"),
            "size_gb": c.get("size_gb"),
        }
        for c in al_claps
    ]
    al_quick_sa = next(
        (m for m in al_models if m.get("quickstart")),
        next((m for m in al_models if m.get("default")), None),
    )
    al_quick_sa_variant = (
        al_quick_sa.get("variant_id") if al_quick_sa else "sao-open-small"
    )
    status[AUDIO_LAB_MODEL_ID] = {
        "display": OMNI_MODEL_SETUP[AUDIO_LAB_MODEL_ID]["display"],
        "desc": OMNI_MODEL_SETUP[AUDIO_LAB_MODEL_ID]["desc"],
        "dedicated_tab": "audio-lab",
        **_runtime_deps_status(AUDIO_LAB_MODEL_ID),
        "models_total": len(al_models),
        "models_installed": al_models_installed,
        "claps_total": len(al_claps),
        "claps_installed": al_claps_installed,
        "installed": al_models_installed > 0 and al_claps_installed > 0,
        "variants": al_variant_status,
        "claps": al_clap_status,
        "default_sa_variant": next(
            (m["variant_id"] for m in al_models if m.get("default")),
            "sao-open-1.0",
        ),
        # Ungated quick-start for Setup → Install defaults (sao-open-1.0 needs
        # HF license acceptance at huggingface.co).
        "quick_sa_variant": al_quick_sa_variant,
        "quick_sa_gated": bool(al_quick_sa and al_quick_sa.get("gated")),
        "quick_sa_accept_url": (
            f"https://huggingface.co/{al_quick_sa.get('repo')}"
            if al_quick_sa and al_quick_sa.get("gated") and al_quick_sa.get("repo")
            else None
        ),
        "default_clap_variant": next(
            (c["variant_id"] for c in al_claps if c.get("default")),
            "larger-clap-general",
        ),
    }

    as_assets = _list_ace_step_assets()
    as_models = as_assets.get("models") or []
    as_lms = as_assets.get("lms") or []
    as_models_installed = sum(1 for m in as_models if m.get("installed"))
    as_lms_installed = sum(1 for lm in as_lms if lm.get("installed"))
    as_native_models_installed = sum(
        1 for m in as_models
        if m.get("installed") and m.get("format", "native") == "native"
    )
    as_lm_required = any(
        bool(m.get("installed") and m.get("default_lm"))
        for m in as_models
    )
    as_variant_status = [
        {
            "variant_id": m["variant_id"],
            "display": m["display"],
            "installed": bool(m.get("installed")),
            "default": bool(m.get("default")),
            "format": m.get("format"),
            "default_lm": m.get("default_lm"),
            "vram_gb": m.get("vram_gb"),
            "size_gb": m.get("size_gb"),
        }
        for m in as_models
    ]
    as_lm_status = [
        {
            "variant_id": lm["variant_id"],
            "display": lm["display"],
            "installed": bool(lm.get("installed")),
            "default": bool(lm.get("default")),
            "vram_gb": lm.get("vram_gb"),
            "size_gb": lm.get("size_gb"),
        }
        for lm in as_lms
    ]
    ace_core = ace_step_core_status()
    as_ready = (
        as_models_installed > 0
        and (not as_lm_required or as_lms_installed > 0)
        and (as_native_models_installed == 0 or bool(ace_core.get("core_ready")))
    )
    status[ACE_STEP_MODEL_ID] = {
        "display": OMNI_MODEL_SETUP[ACE_STEP_MODEL_ID]["display"],
        "desc": OMNI_MODEL_SETUP[ACE_STEP_MODEL_ID]["desc"],
        "dedicated_tab": "ace-step",
        **_runtime_deps_status(ACE_STEP_MODEL_ID),
        **ace_core,
        "models_total": len(as_models),
        "models_installed": as_models_installed,
        "native_models_installed": as_native_models_installed,
        "lms_total": len(as_lms),
        "lms_installed": as_lms_installed,
        "lm_required": as_lm_required,
        "installed": as_ready,
        # The Server tab's generic variant dropdown reads this field for
        # every model family. ACE-Step has its own status route, but exposing
        # the same shape here keeps installed DiT variants visible there too.
        "variants": as_variant_status,
        "lms": as_lm_status,
        "default_model_variant": next(
            (m["variant_id"] for m in as_models if m.get("default")),
            "ace-xl-turbo",
        ),
        "default_lm_variant": next(
            (lm["variant_id"] for lm in as_lms if lm.get("default")),
            "ace-lm-0.6b",
        ),
    }

    missing_defaults = _missing_default_install_items()
    required_gb = sum(int(item.get("required_gb") or 0) for item in missing_defaults)
    disk = _models_disk_status()
    disk["missing_defaults"] = missing_defaults
    disk["missing_defaults_required_gb"] = required_gb
    if disk.get("available") and disk.get("free_gb") is not None:
        disk["has_space_for_missing_defaults"] = float(disk["free_gb"]) >= required_gb
    status["disk"] = disk

    status["huggingface"] = _check_hf_token(_read_hf_token())

    return {"status": status}


# ---------------------------------------------------------------------------
# Legacy alias adapter — translate a Job to the pre-phase-2 dict shape so
# existing UI poll loops keep working without changes.
# ---------------------------------------------------------------------------
def _job_target_key(job) -> str:
    meta = job.meta or {}
    if meta.get("target_key"):
        return str(meta["target_key"])
    if job.active_key:
        return str(job.active_key)
    if job.kind == "model_install":
        if meta.get("variant_id"):
            return f"variant:{meta.get('model')}:{meta.get('variant_id')}"
        return f"model:{meta.get('model')}"
    if job.kind == "lora_install":
        return f"lora:{meta.get('name') or meta.get('repo')}"
    return f"{job.kind}:{meta.get('kind') or meta.get('variant_id') or job.job_id}"


def _model_weights_present(weights_dir_name: str, paired_codec_dir: str | None = None) -> bool:
    roots = [MODELS_DIR / "omni" / weights_dir_name]
    if paired_codec_dir:
        roots.append(MODELS_DIR / "omni" / paired_codec_dir)
    return all(inspect_installed_snapshot(root)["valid"] for root in roots)


def _group_install_jobs(job_list: list) -> dict[str, dict]:
    by_target: dict[str, list] = {}
    for job in sorted(job_list, key=lambda j: j.started_at):
        by_target.setdefault(_job_target_key(job), []).append(job)

    grouped: dict[str, dict] = {}
    for target_key, target_jobs in by_target.items():
        latest = max(target_jobs, key=lambda j: j.started_at)
        total_attempts = len(target_jobs)
        previous_failures = 0
        final_status = {
            "queued": "queued",
            "running": "running",
            "done": "completed",
            "error": "failed",
            "cancelling": "cancelling",
            "cancelled": "cancelled",
        }.get(latest.status, latest.status)
        for index, job in enumerate(target_jobs, start=1):
            grouped[job.job_id] = {
                "target_key": target_key,
                "attempt": index,
                "target_attempts": total_attempts,
                "previous_failures": previous_failures,
                "latest_for_target": job.job_id == latest.job_id,
                "final_status_for_target": final_status,
            }
            if job.status == "error":
                previous_failures += 1
    return grouped


def _legacy_job_dict(job, group: dict | None = None) -> dict:
    legacy_status = {
        "queued": "queued",
        "running": "running",
        "done": "completed",
        "error": "failed",
        "cancelling": "cancelling",
        "cancelled": "cancelled",
    }.get(job.status, job.status)

    stdout_tail = "".join(job.stdout_tail)[-4000:]
    progress = dict(job.progress) if job.progress else None
    progress_percent = None
    if progress and progress.get("total"):
        try:
            total = max(1, int(progress["total"]))
            current = max(0, int(progress.get("current") or 0))
            progress_percent = round(min(100.0, current * 100.0 / total), 1)
        except (TypeError, ValueError):
            progress_percent = None

    elapsed_seconds = max(0.0, (job.finished_at or time.time()) - job.started_at)
    phase = progress.get("message") if progress else None
    if not phase:
        phase = {
            "running": "starting",
            "cancelling": "cancelling",
            "completed": "completed",
            "failed": "failed",
            "cancelled": "cancelled",
        }.get(legacy_status, legacy_status)

    out: dict = {
        "job_id": job.job_id,
        "status": legacy_status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "output": stdout_tail,
        "error": job.error,
        "progress": progress,
        "progress_percent": progress_percent,
        "elapsed_seconds": round(elapsed_seconds, 1),
        "phase": phase,
        "pid": job.process_pid,
        "returncode": job.process_returncode,
    }
    if group:
        out.update(group)
        out["superseded"] = not bool(group.get("latest_for_target", True))
    out.update(_classify_install_output(
        "\n".join(part for part in (stdout_tail, job.error or "") if part),
        legacy_status,
    ))
    meta = job.meta or {}
    log_path = meta.get("log_path")
    if log_path:
        out["log_available"] = Path(str(log_path)).exists()
        out["log_name"] = Path(str(log_path)).name
    if job.kind == "model_install":
        if meta.get("variant_id"):
            out["kind"] = "variant"
            out["model"] = meta.get("model")
            out["variant_id"] = meta.get("variant_id")
            out["variant_display"] = meta.get("variant_display")
        else:
            out["kind"] = "model"
            out["model"] = meta.get("model")
    elif job.kind == "lora_install":
        out["kind"] = "lora"
        out["repo"] = meta.get("repo")
        out["name"] = meta.get("name")
    elif job.kind == "audio_lab_install":
        # meta["kind"] holds the granular asset kind (audio-model/audio-vae/
        # audio-clap/audio-custom). Surface it as asset_kind so the UI can
        # distinguish; legacy "kind" stays the JobStore kind for the cancel
        # endpoint to keep working.
        out["kind"] = job.kind
        out["asset_kind"] = meta.get("kind")
        for k, v in meta.items():
            if k == "kind":
                continue
            if k not in out:
                out[k] = v
    elif job.kind == "default_install":
        out["kind"] = "defaults"
        out.update({k: v for k, v in meta.items() if k not in out})
    else:
        out["kind"] = job.kind
        out.update({k: v for k, v in meta.items() if k not in out})
    return out


def _classify_install_output(text: str, status: str) -> dict:
    warning_labels: list[str] = []
    for pattern, label in _KNOWN_WARNING_RULES:
        if pattern.search(text) and label not in warning_labels:
            warning_labels.append(label)

    classification: dict = {
        "warning_summary": None,
        "known_warning_count": len(warning_labels),
        "failure_kind": None,
        "failure_summary": None,
    }
    if warning_labels:
        classification["warning_summary"] = "; ".join(warning_labels)

    if status == "failed":
        for pattern, kind, summary in _FAILURE_RULES:
            if pattern.search(text):
                classification["failure_kind"] = kind
                classification["failure_summary"] = summary
                break
        if not classification["failure_kind"]:
            classification["failure_kind"] = "unknown"
            classification["failure_summary"] = "The installer exited with an error. Check the log tail."
    return classification


def _install_job_log_path(job) -> Path:
    raw = (job.meta or {}).get("log_path")
    if not raw:
        raise HTTPException(status_code=404, detail="This job has no log file")
    path = Path(str(raw)).resolve(strict=False)
    base = WORKER_LOG_DIR.resolve(strict=False)
    if not is_relative_to(path, base):
        raise HTTPException(status_code=400, detail="Invalid job log path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job log file is not available yet")
    return path


def _tail_job_log(path: Path, lines: int) -> dict:
    size = path.stat().st_size
    start = max(0, size - _JOB_LOG_TAIL_MAX_BYTES)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    if start > 0:
        text = text.split("\n", 1)[-1]
    selected = text.splitlines()[-lines:]
    return {
        "log_name": path.name,
        "lines": selected,
        "truncated": start > 0,
        "bytes_read": len(raw),
        "size_bytes": size,
    }


# ---------------------------------------------------------------------------
# Install endpoints
# ---------------------------------------------------------------------------
def _enqueue_install(
    *,
    label: str,
    log_stem: str,
    script_args: list[str],
    meta: dict,
    active_key: str,
    kind: str,
):
    # Embed a short uniqueifier so consecutive runs of the same identity
    # don't clobber each other's log file. The job_id is generated inside
    # JobStore so we can't use it here, but a fresh short uuid keeps logs
    # discoverable and aligns with how install jobs were named pre-refactor.
    log_uniq = str(uuid.uuid4())[:8]
    log_path = WORKER_LOG_DIR / f"install_{log_stem}_{log_uniq}.log"
    script = SERVER_DIR / "install_model.sh"
    enriched_meta = dict(meta)
    enriched_meta["log_path"] = str(log_path)
    enriched_meta["target_key"] = active_key
    try:
        job = jobs.enqueue_subprocess(
            kind=kind,
            argv=["bash", str(script), *script_args],
            cwd=str(SERVER_DIR),
            meta=enriched_meta,
            active_key=active_key,
            log_path=str(log_path),
            progress_parser=hf_tqdm_parser,
        )
    except DuplicateJobError:
        raise HTTPException(status_code=409, detail=f"{label} is already installing")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to enqueue %s install", label)
        raise HTTPException(
            status_code=500,
            detail=f"Could not start {label} install: {e}",
        )
    logger.info("Enqueued %s install (job %s, log %s)",
                label, job.job_id, log_path.name)
    return job


@router.post("/api/setup/install/{model}")
async def install_model(model: str):
    if model in (AUDIO_LAB_MODEL_ID, ACE_STEP_MODEL_ID, MINIMAX_MUSIC3_MODEL_ID):
        tab = {
            AUDIO_LAB_MODEL_ID: "Audio Lab",
            ACE_STEP_MODEL_ID: "ACE Step",
            MINIMAX_MUSIC3_MODEL_ID: "Music 3",
        }[model]
        raise HTTPException(
            status_code=400,
            detail=(
                f"{model} cannot be installed from the Setup tab. "
                f"Open the {tab} tab and use Install there "
                f"(or click Install defaults in Setup → Audio Generation)."
            ),
        )
    if model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    script = SERVER_DIR / "install_model.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="install_model.sh not found")

    job = _enqueue_install(
        label=model,
        log_stem=model,
        script_args=[model],
        meta={"model": model},
        active_key=f"model:{model}",
        kind="model_install",
    )
    # Use the job_id in the log file name retroactively isn't possible
    # without renaming, but the log path is captured in meta for future ref.
    return {"job_id": job.job_id, "model": model, "status": job.status}


@router.post("/api/setup/install-variant")
async def install_variant(req: InstallVariantRequest):
    if req.model in (AUDIO_LAB_MODEL_ID, ACE_STEP_MODEL_ID):
        tab = "Audio Lab" if req.model == AUDIO_LAB_MODEL_ID else "ACE Step"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{req.model} cannot be installed from the Setup tab. "
                f"Open the {tab} tab and install assets there."
            ),
        )
    variant = get_variant(req.model, req.variant_id)
    if not variant:
        raise HTTPException(status_code=400,
                            detail=f"Unknown variant {req.variant_id} for {req.model}")
    script_args = [
        "variant", variant["repo"], variant["weights_dir"],
        str(declared_size_gb(variant.get("size"))),
    ]
    if req.model == MINIMAX_MUSIC3_MODEL_ID:
        script_args = ["minimax-music3-model", req.variant_id]
    job = _enqueue_install(
        label=f"{req.model}/{req.variant_id}",
        log_stem=f"variant_{req.model}_{req.variant_id}",
        script_args=script_args,
        meta={
            "model": req.model,
            "variant_id": req.variant_id,
            "variant_display": variant["display"],
            "repo": variant["repo"],
            "weights_dir": variant["weights_dir"],
        },
        active_key=f"variant:{req.model}:{req.variant_id}",
        kind="model_install",
    )
    return {"job_id": job.job_id, "model": req.model,
            "variant_id": req.variant_id, "status": job.status}


@router.post("/api/loras/install")
async def install_lora(req: InstallLoraRequest):
    repo = req.repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", repo):
        raise HTTPException(status_code=400,
                            detail="Invalid repo ID (must be org/name)")

    name = req.name.strip() if req.name else repo.replace("/", "--")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
        raise HTTPException(status_code=400, detail="Invalid LoRA name")

    job = _enqueue_install(
        label=f"lora/{name}",
        log_stem=f"lora_{name}",
        script_args=["lora", repo, name],
        meta={"repo": repo, "name": name},
        active_key=f"lora:{name}",
        kind="lora_install",
    )
    return {"job_id": job.job_id, "repo": repo, "name": name, "status": job.status}


@router.post("/api/setup/install-missing-defaults")
async def install_missing_defaults():
    job = _enqueue_install(
        label="missing recommended defaults",
        log_stem="missing_defaults",
        script_args=["install-missing-defaults"],
        meta={
            "name": "Missing recommended defaults",
            "description": "Installs any missing Omni, Audio Lab, CLAP, and ACE-Step recommended defaults.",
        },
        active_key="defaults:missing",
        kind="default_install",
    )
    return {"job_id": job.job_id, "status": job.status}


# ---------------------------------------------------------------------------
# Legacy /api/setup/jobs* — shape preserved
# ---------------------------------------------------------------------------
@router.get("/api/setup/jobs")
async def list_install_jobs():
    install_jobs = jobs.list(kinds=_INSTALL_KINDS)
    grouped = _group_install_jobs(install_jobs)
    return {"jobs": [_legacy_job_dict(j, grouped.get(j.job_id)) for j in install_jobs]}


@router.get("/api/setup/jobs/{job_id}")
async def get_install_job(job_id: str):
    job = jobs.get(job_id)
    if not job or job.kind not in _INSTALL_KINDS:
        raise HTTPException(status_code=404, detail="Job not found")
    install_jobs = jobs.list(kinds=_INSTALL_KINDS)
    grouped = _group_install_jobs(install_jobs)
    return _legacy_job_dict(job, grouped.get(job.job_id))


@router.get("/api/setup/jobs/{job_id}/log")
async def get_install_job_log(job_id: str, lines: int = 200):
    job = jobs.get(job_id)
    if not job or job.kind not in _INSTALL_KINDS:
        raise HTTPException(status_code=404, detail="Job not found")
    path = _install_job_log_path(job)
    tail = _tail_job_log(path, bounded_lines(lines, default=200, maximum=2000))
    tail["job_id"] = job_id
    return tail


@router.post("/api/setup/jobs/{job_id}/cancel")
async def cancel_install_job(job_id: str):
    job = jobs.get(job_id)
    if not job or job.kind not in _INSTALL_KINDS:
        raise HTTPException(status_code=404, detail="Job not found")
    new_status = jobs.cancel(job_id)
    legacy = {
        "queued": "cancelled",
        "running": "running",
        "done": "completed",
        "error": "failed",
        "cancelling": "cancelling",
        "cancelled": "cancelled",
        "missing": "unknown",
    }.get(new_status, new_status)
    return {"job_id": job_id, "status": legacy}


# ---------------------------------------------------------------------------
# Variants and LoRAs
# ---------------------------------------------------------------------------
@router.get("/api/variants/{model}")
async def get_variants(model: str):
    variants = OMNI_MODEL_VARIANTS.get(model, [])
    result = []
    for v in variants:
        installed = await asyncio.to_thread(
            _model_weights_present, v["weights_dir"], v.get("paired_codec_dir"),
        )
        result.append({**v, "installed": installed})
    return {"model": model, "variants": result}


@router.get("/api/loras")
async def list_loras():
    if not LORA_DIR.exists():
        return {"loras": []}
    loras = []
    for d in sorted(LORA_DIR.iterdir()):
        if not d.is_dir():
            continue
        has_adapter = (d / "adapter_model.safetensors").exists() or \
                      (d / "adapter_model.bin").exists()
        try:
            size_mb = sum(
                f.stat().st_size
                for f in d.rglob("*") if f.is_file()
            ) / 1024 / 1024
        except OSError:
            size_mb = 0
        loras.append({
            "name": d.name,
            "has_adapter": has_adapter,
            "size_mb": round(size_mb, 1),
        })
    return {"loras": loras}


@router.delete("/api/loras/{name}")
async def delete_lora(name: str):
    lora_path = safe_child_path(LORA_DIR, name)
    if not lora_path.exists() or not lora_path.is_dir():
        raise HTTPException(status_code=404, detail="LoRA not found")
    shutil.rmtree(lora_path)
    return {"status": "deleted", "name": name}


@router.get("/api/search/hf")
async def search_hf(q: str, kind: str = "model", limit: int = 20,
                    family: str | None = None):
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query required")
    if kind not in ("model", "lora"):
        raise HTTPException(status_code=400, detail="Invalid search kind")
    if family not in (None, "minimax_music3"):
        raise HTTPException(status_code=400, detail="Invalid search family")

    def _do_search():
        from huggingface_hub import HfApi
        api = HfApi()
        kwargs = {
            "search": q.strip(),
            "limit": max(1, min(limit, 50)),
            "sort": "downloads",
            "direction": -1,
        }
        if kind == "lora":
            kwargs["filter"] = "peft"
        try:
            return list(api.list_models(**kwargs))
        except Exception as e:
            return e

    try:
        results = await asyncio.to_thread(_do_search)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"HF search failed: {e}")

    if isinstance(results, Exception):
        raise HTTPException(status_code=502, detail=f"HF search failed: {results}")

    rows = []
    for model in results:
        tags = (getattr(model, "tags", None) or [])[:20]
        repo_id = getattr(model, "id", None) or getattr(model, "modelId", None)
        row = {
            "repo_id": repo_id,
            "author": getattr(model, "author", None),
            "downloads": getattr(model, "downloads", 0) or 0,
            "likes": getattr(model, "likes", 0) or 0,
            "tags": tags[:10],
            "pipeline_tag": getattr(model, "pipeline_tag", None),
        }
        if family == "minimax_music3":
            row.update(_classify_minimax_music3_candidate(repo_id or "", tags, kind))
        rows.append(row)
    return {"results": rows, "family": family, "kind": kind}


@router.post("/api/setup/hf-token")
async def save_hf_token(req: HFTokenRequest):
    token = req.token.strip()
    if not is_valid_hf_token(token):
        raise HTTPException(status_code=400, detail="Invalid HuggingFace token format")
    checked = await asyncio.to_thread(_check_hf_token, token)
    if checked.get("token_valid") is False:
        raise HTTPException(
            status_code=400,
            detail=checked.get("token_error") or "HuggingFace rejected this token",
        )
    write_secret_file(HF_TOKEN_FILE, token)
    _HF_TOKEN_STATUS_CACHE.clear()
    _HF_TOKEN_STATUS_CACHE.update({
        "fingerprint": _hf_token_fingerprint(token),
        "checked_at": time.time(),
        "status": checked,
    })
    return {"status": "saved", "huggingface": checked}


# ---------------------------------------------------------------------------
# Audio Lab — Stable Audio + CLAP install / delete / status
# ---------------------------------------------------------------------------
# Storage convention:
#   MODELS_DIR/audio_lab/<sa_variant.weights_dir>/    — Stable Audio variants
#   MODELS_DIR/audio_lab/vae/<vae_variant.weights_dir>/ — VAE swaps (path is in registry)
#   MODELS_DIR/audio_lab/clap/<clap_variant.weights_dir>/ — CLAP models (path is in registry)
#   MODELS_DIR/audio_lab/custom/{model,vae,clap}/<name>/ — user-installed via Custom URL
# Install jobs use kind="audio_lab_install" with meta.kind set to one of
# {audio-model, audio-vae, audio-clap, audio-custom}; the legacy job adapter
# unpacks meta into the response shape the tab-audio-lab.js poller expects.

def _list_audio_lab_assets() -> dict:
    """Build the manifest the UI's Audio Lab tab consumes."""
    def _block(v_id: str, v: dict, installed_fn) -> dict:
        return {
            "variant_id": v_id,
            "display":    v["display"],
            "repo":       v.get("repo"),
            "size_gb":    v.get("size_gb"),
            "vram_gb":    v.get("vram_gb"),
            "format":     v.get("format"),
            "tier":       v.get("tier"),
            "tags":       v.get("tags", []),
            "gated":      v.get("gated", False),
            "quickstart":  v.get("quickstart", False),
            "default":    v.get("default", False),
            "installed":  installed_fn(v_id),
        }

    def _custom_asset_has_weights(path) -> bool:
        try:
            return any(
                f.is_file() and f.suffix.lower() in _WEIGHT_EXTS
                for f in path.rglob("*")
            )
        except OSError:
            return False

    custom_root = AUDIO_LAB_ROOT / "custom"
    custom: dict = {"model": [], "vae": [], "clap": []}
    if custom_root.exists():
        for kind_dir in custom_root.iterdir():
            if not kind_dir.is_dir() or kind_dir.name not in custom:
                continue
            for sub in kind_dir.iterdir():
                if not sub.is_dir():
                    continue
                if not (sub / ".install_complete").exists():
                    continue
                if not _custom_asset_has_weights(sub):
                    continue
                try:
                    repo_line = (sub / ".install_complete").read_text().strip()
                except OSError:
                    repo_line = ""
                custom[kind_dir.name].append({
                    "name": sub.name, "repo": repo_line, "path": str(sub),
                })

    return {
        "models": [_block(v_id, v, is_audio_lab_variant_installed) for v_id, v in STABLE_AUDIO_MODELS.items()],
        "vaes":   [_block(v_id, v, is_audio_lab_vae_installed)      for v_id, v in STABLE_AUDIO_VAES.items()],
        "claps":  [_block(v_id, v, is_clap_installed)               for v_id, v in CLAP_MODELS.items()],
        "custom": custom,
    }


@router.get("/api/audio_lab/status")
async def audio_lab_status():
    """Full install state of every Audio Lab asset (registry + custom)."""
    data = _list_audio_lab_assets()
    data["runtime"] = _runtime_deps_status(AUDIO_LAB_MODEL_ID)
    return data


@router.post("/api/audio_lab/install-model")
async def audio_lab_install_model(req: AudioLabInstallRequest):
    v = STABLE_AUDIO_MODELS.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown Stable Audio variant: {req.variant_id}")
    job = _enqueue_install(
        label=f"audio-model/{req.variant_id}",
        log_stem=f"audio_model_{req.variant_id}",
        script_args=["audio-model", req.variant_id],
        meta={
            "kind": "audio-model",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "gated": bool(v.get("gated")),
            "accept_url": (
                f"https://huggingface.co/{v['repo']}"
                if v.get("gated") and v.get("repo") else None
            ),
            "weights_dir": v["weights_dir"],
            "tier": v.get("tier"),
            "format": v.get("format"),
        },
        active_key=f"audio-model:{req.variant_id}",
        kind="audio_lab_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


def _guard_audio_asset_delete(family: str) -> None:
    from routers import ace_step, audio_lab
    module = ace_step if family == ACE_STEP_MODEL_ID else audio_lab
    if module._OPERATION_GATE.locked() or any(w.model == family and w.status != "dead" for w in worker_registry.all_workers()):
        raise HTTPException(409, "Unload and delete the engine's workers before deleting its assets")
    kinds = {"default_install", "ace_step_install" if family == ACE_STEP_MODEL_ID else "audio_lab_install"}
    if any(job.status in {"queued", "running", "cancelling"} for job in jobs.list(kinds=kinds)):
        raise HTTPException(409, "Wait for active asset installations to finish before deleting assets")


@router.delete("/api/audio_lab/install-model/{variant_id}")
async def audio_lab_delete_model(variant_id: str):
    _guard_audio_asset_delete(AUDIO_LAB_MODEL_ID)
    v = STABLE_AUDIO_MODELS.get(variant_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"Unknown variant: {variant_id}")
    path = audio_lab_weights_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    # safe_child_path verifies path is strictly inside AUDIO_LAB_ROOT.
    safe = safe_child_path(AUDIO_LAB_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/audio_lab/install-vae")
async def audio_lab_install_vae(req: AudioLabInstallRequest):
    if req.variant_id == "default":
        raise HTTPException(status_code=400, detail="default VAE requires no install")
    v = STABLE_AUDIO_VAES.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown VAE variant: {req.variant_id}")
    job = _enqueue_install(
        label=f"audio-vae/{req.variant_id}",
        log_stem=f"audio_vae_{req.variant_id}",
        script_args=["audio-vae", req.variant_id],
        meta={
            "kind": "audio-vae",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "weights_dir": v["weights_dir"],
        },
        active_key=f"audio-vae:{req.variant_id}",
        kind="audio_lab_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


@router.delete("/api/audio_lab/install-vae/{variant_id}")
async def audio_lab_delete_vae(variant_id: str):
    _guard_audio_asset_delete(AUDIO_LAB_MODEL_ID)
    if variant_id == "default":
        raise HTTPException(status_code=400, detail="cannot delete default VAE")
    v = STABLE_AUDIO_VAES.get(variant_id)
    if not v or not v.get("weights_dir"):
        raise HTTPException(status_code=404, detail=f"Unknown VAE variant: {variant_id}")
    path = audio_lab_vae_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    # weights_dir is multi-segment ("vae/<name>") — safe_subtree_path handles slashes.
    safe = safe_subtree_path(AUDIO_LAB_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/audio_lab/install-clap")
async def audio_lab_install_clap(req: AudioLabInstallRequest):
    v = CLAP_MODELS.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown CLAP variant: {req.variant_id}")
    job = _enqueue_install(
        label=f"clap-model/{req.variant_id}",
        log_stem=f"clap_{req.variant_id}",
        script_args=["clap-model", req.variant_id],
        meta={
            "kind": "audio-clap",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "weights_dir": v["weights_dir"],
        },
        active_key=f"clap-model:{req.variant_id}",
        kind="audio_lab_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


@router.delete("/api/audio_lab/install-clap/{variant_id}")
async def audio_lab_delete_clap(variant_id: str):
    _guard_audio_asset_delete(AUDIO_LAB_MODEL_ID)
    v = CLAP_MODELS.get(variant_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"Unknown CLAP variant: {variant_id}")
    path = clap_weights_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    # weights_dir is multi-segment ("clap/<name>") — safe_subtree_path handles slashes.
    safe = safe_subtree_path(AUDIO_LAB_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/audio_lab/install-custom")
async def audio_lab_install_custom(req: AudioLabCustomInstallRequest):
    repo = req.repo.strip()
    if not _HF_REPO_RE.fullmatch(repo):
        raise HTTPException(status_code=400, detail="Invalid HF repo id (expected <org>/<name>)")
    if req.kind not in ("model", "vae", "clap"):
        raise HTTPException(status_code=400, detail="kind must be one of: model, vae, clap")
    name = (req.name or "").strip() or repo.replace("/", "_")
    if not _CUSTOM_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    job = _enqueue_install(
        label=f"audio-custom/{req.kind}/{name}",
        log_stem=f"audio_custom_{req.kind}_{name}",
        script_args=["audio-custom", repo, name, req.kind],
        meta={
            "kind": "audio-custom",
            "custom_kind": req.kind,
            "name": name,
            "repo": repo,
            "weights_dir": f"custom/{req.kind}/{name}",
        },
        active_key=f"audio-custom:{req.kind}:{name}",
        kind="audio_lab_install",
    )
    return {"job_id": job.job_id, "name": name, "kind": req.kind, "status": job.status}


@router.delete("/api/audio_lab/install-custom/{kind}/{name}")
async def audio_lab_delete_custom(kind: str, name: str):
    _guard_audio_asset_delete(AUDIO_LAB_MODEL_ID)
    if kind not in ("model", "vae", "clap"):
        raise HTTPException(status_code=400, detail="kind must be one of: model, vae, clap")
    if not _CUSTOM_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    # Guard against traversal — the resolved path must be under AUDIO_LAB_ROOT/custom/<kind>/.
    base = AUDIO_LAB_ROOT / "custom" / kind
    safe = safe_child_path(base, name)
    if not safe.exists():
        return {"status": "not_installed", "name": name, "kind": kind}
    shutil.rmtree(safe)
    return {"status": "deleted", "name": name, "kind": kind}


# ---------------------------------------------------------------------------
# ACE-Step install endpoints
# ---------------------------------------------------------------------------

def _list_ace_step_assets() -> dict:
    """Mirror of _list_audio_lab_assets for ACE-Step (registry + custom installs)."""
    def _block(key: str, v: dict, check_fn) -> dict:
        return {
            "variant_id": key, "name": key,  # both keys for UI compatibility
            "display": v["display"],
            "repo": v.get("repo"),
            "size_gb": v.get("size_gb"),
            "vram_gb": v.get("vram_gb"),
            "format": v.get("format"),
            "tier": v.get("tier"),
            "default": v.get("default", False),
            "default_lm": v.get("default_lm"),
            "enables_mode": v.get("enables_mode"),
            "supported_tasks": v.get("supported_tasks", []),
            "available": v.get("available", True),
            "unavailable_reason": v.get("unavailable_reason"),
            "adapter_files": v.get("adapter_files", []),
            "installed": bool(check_fn(key)),
        }

    custom_root = ACE_STEP_ROOT / "custom"
    custom = {"model": [], "lm": [], "vae": [], "lora": []}
    if custom_root.exists():
        for kind_dir in custom_root.iterdir():
            if kind_dir.name in custom and kind_dir.is_dir():
                for sub in kind_dir.iterdir():
                    if not sub.is_dir():
                        continue
                    if not (sub / ".install_complete").exists():
                        continue
                    try:
                        repo_line = (sub / ".install_complete").read_text().strip()
                    except OSError:
                        repo_line = ""
                    custom[kind_dir.name].append({
                        "name": sub.name, "repo": repo_line, "path": str(sub),
                    })

    return {
        "models": [_block(k, v, is_ace_step_model_installed) for k, v in ACE_STEP_MODELS.items()],
        "lms":    [_block(k, v, is_ace_step_lm_installed)    for k, v in ACE_STEP_LMS.items()],
        "vaes":   [_block(k, v, is_ace_step_vae_installed)   for k, v in ACE_STEP_VAES.items()],
        "loras":  [_block(k, v, is_ace_step_lora_installed)  for k, v in ACE_STEP_LORAS.items()],
        "custom": custom,
        **ace_step_core_status(),
    }


@router.get("/api/ace_step/status")
async def ace_step_status():
    """Full install state of every ACE-Step asset (registry + custom)."""
    data = _list_ace_step_assets()
    data["runtime"] = _runtime_deps_status(ACE_STEP_MODEL_ID)
    return data


@router.post("/api/ace_step/install-model")
async def ace_step_install_model(req: AceStepInstallRequest):
    v = ACE_STEP_MODELS.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown ACE-Step variant: {req.variant_id}")
    auto_installs: list[str] = []
    if v.get("format") == "native" and req.variant_id != "ace-1.5":
        auto_installs.append("ace-1.5 shared core")
    if v.get("default_lm"):
        auto_installs.append(f"recommended LM {v.get('default_lm')}")
    job = _enqueue_install(
        label=f"ace-model/{req.variant_id}",
        log_stem=f"ace_model_{req.variant_id}",
        script_args=["ace-model", req.variant_id],
        meta={
            "kind": "ace-model",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "weights_dir": v["weights_dir"],
            "tier": v.get("tier"),
            "format": v.get("format"),
            "auto_installs": auto_installs,
        },
        active_key=f"ace-model:{req.variant_id}",
        kind="ace_step_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


@router.delete("/api/ace_step/install-model/{variant_id}")
async def ace_step_delete_model(variant_id: str):
    _guard_audio_asset_delete(ACE_STEP_MODEL_ID)
    v = ACE_STEP_MODELS.get(variant_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"Unknown variant: {variant_id}")
    path = ace_step_model_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    # weights_dir is multi-segment ("models/<name>") — safe_subtree_path handles slashes.
    safe = safe_subtree_path(ACE_STEP_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/ace_step/install-lm")
async def ace_step_install_lm(req: AceStepInstallRequest):
    v = ACE_STEP_LMS.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown ACE-Step LM: {req.variant_id}")
    job = _enqueue_install(
        label=f"ace-lm/{req.variant_id}",
        log_stem=f"ace_lm_{req.variant_id}",
        script_args=["ace-lm", req.variant_id],
        meta={
            "kind": "ace-lm",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "weights_dir": v["weights_dir"],
        },
        active_key=f"ace-lm:{req.variant_id}",
        kind="ace_step_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


@router.delete("/api/ace_step/install-lm/{variant_id}")
async def ace_step_delete_lm(variant_id: str):
    _guard_audio_asset_delete(ACE_STEP_MODEL_ID)
    v = ACE_STEP_LMS.get(variant_id)
    if not v:
        raise HTTPException(status_code=404, detail=f"Unknown LM: {variant_id}")
    path = ace_step_lm_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    safe = safe_subtree_path(ACE_STEP_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/ace_step/install-vae")
async def ace_step_install_vae(req: AceStepInstallRequest):
    if req.variant_id == "default":
        raise HTTPException(status_code=400, detail="default VAE requires no install")
    v = ACE_STEP_VAES.get(req.variant_id)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown ACE-Step VAE: {req.variant_id}")
    job = _enqueue_install(
        label=f"ace-vae/{req.variant_id}",
        log_stem=f"ace_vae_{req.variant_id}",
        script_args=["ace-vae", req.variant_id],
        meta={
            "kind": "ace-vae",
            "variant_id": req.variant_id,
            "variant_display": v["display"],
            "repo": v["repo"],
            "weights_dir": v["weights_dir"],
        },
        active_key=f"ace-vae:{req.variant_id}",
        kind="ace_step_install",
    )
    return {"job_id": job.job_id, "variant_id": req.variant_id, "status": job.status}


@router.delete("/api/ace_step/install-vae/{variant_id}")
async def ace_step_delete_vae(variant_id: str):
    _guard_audio_asset_delete(ACE_STEP_MODEL_ID)
    if variant_id == "default":
        raise HTTPException(status_code=400, detail="cannot delete default VAE")
    v = ACE_STEP_VAES.get(variant_id)
    if not v or not v.get("weights_dir"):
        raise HTTPException(status_code=404, detail=f"Unknown VAE variant: {variant_id}")
    path = ace_step_vae_path(variant_id)
    if not path or not path.exists():
        return {"status": "not_installed", "variant_id": variant_id}
    safe = safe_subtree_path(ACE_STEP_ROOT, v["weights_dir"])
    shutil.rmtree(safe)
    return {"status": "deleted", "variant_id": variant_id}


@router.post("/api/ace_step/install-lora")
async def ace_step_install_lora(req: AceStepLoraInstallRequest):
    # Two paths:
    #   1) registry lookup: caller supplies `name` that exists in ACE_STEP_LORAS
    #   2) ad-hoc community LoRA: caller supplies `repo` (and optional `name`).
    if req.repo:
        repo = req.repo.strip()
        if not _HF_REPO_RE.fullmatch(repo):
            raise HTTPException(status_code=400, detail="Invalid HF repo id (expected <org>/<name>)")
        name = (req.name or "").strip() or repo.replace("/", "_")
        if not _CUSTOM_NAME_RE.fullmatch(name):
            raise HTTPException(status_code=400, detail="Invalid name")
        # Ad-hoc LoRA lands at loras/<name> (same as registry layout) so it's
        # discoverable by the same path resolver. Avoids collisions by failing
        # if the dir already exists.
        target = ACE_STEP_ROOT / "loras" / name
        if target.exists():
            raise HTTPException(status_code=409, detail=f"LoRA '{name}' already installed; pick a different name")
        job = _enqueue_install(
            label=f"ace-lora/{name}",
            log_stem=f"ace_lora_{name}",
            script_args=["ace-lora", name, repo],
            meta={
                "kind": "ace-lora", "name": name, "repo": repo,
                "weights_dir": f"loras/{name}", "registry": "adhoc",
            },
            active_key=f"ace-lora:{name}",
            kind="ace_step_install",
        )
        return {"job_id": job.job_id, "name": name, "status": job.status}
    # Registry path
    if not req.name:
        raise HTTPException(status_code=400, detail="Either `name` (registry) or `repo` (ad-hoc) must be provided")
    v = ACE_STEP_LORAS.get(req.name)
    if not v:
        raise HTTPException(status_code=400, detail=f"Unknown ACE-Step LoRA: {req.name}")
    if not v.get("available", True) or not v.get("repo"):
        raise HTTPException(
            status_code=409,
            detail=v.get("unavailable_reason") or f"ACE-Step LoRA '{req.name}' is not available upstream.",
        )
    job = _enqueue_install(
        label=f"ace-lora/{req.name}",
        log_stem=f"ace_lora_{req.name}",
        script_args=["ace-lora", req.name],
        meta={
            "kind": "ace-lora", "name": req.name,
            "repo": v["repo"], "weights_dir": v["weights_dir"],
            "tier": v.get("tier"), "registry": "official_or_community",
            "enables_mode": v.get("enables_mode"),
        },
        active_key=f"ace-lora:{req.name}",
        kind="ace_step_install",
    )
    return {"job_id": job.job_id, "name": req.name, "status": job.status}


@router.delete("/api/ace_step/install-lora/{name}")
async def ace_step_delete_lora(name: str):
    _guard_audio_asset_delete(ACE_STEP_MODEL_ID)
    if not _CUSTOM_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    # Registry-defined LoRAs and ad-hoc ones both land at loras/<name>.
    safe = safe_subtree_path(ACE_STEP_ROOT, f"loras/{name}")
    if not safe.exists():
        return {"status": "not_installed", "name": name}
    shutil.rmtree(safe)
    return {"status": "deleted", "name": name}


@router.post("/api/ace_step/install-custom")
async def ace_step_install_custom(req: AceStepCustomInstallRequest):
    repo = req.repo.strip()
    if not _HF_REPO_RE.fullmatch(repo):
        raise HTTPException(status_code=400, detail="Invalid HF repo id (expected <org>/<name>)")
    if req.kind not in ("model", "lm", "vae", "lora"):
        raise HTTPException(status_code=400, detail="kind must be one of: model, lm, vae, lora")
    name = (req.name or "").strip() or repo.replace("/", "_")
    if not _CUSTOM_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    job = _enqueue_install(
        label=f"ace-custom/{req.kind}/{name}",
        log_stem=f"ace_custom_{req.kind}_{name}",
        script_args=["ace-custom", repo, name, req.kind],
        meta={
            "kind": "ace-custom",
            "custom_kind": req.kind, "name": name, "repo": repo,
            "weights_dir": f"custom/{req.kind}/{name}",
        },
        active_key=f"ace-custom:{req.kind}:{name}",
        kind="ace_step_install",
    )
    return {"job_id": job.job_id, "name": name, "kind": req.kind, "status": job.status}


@router.delete("/api/ace_step/install-custom/{kind}/{name}")
async def ace_step_delete_custom(kind: str, name: str):
    _guard_audio_asset_delete(ACE_STEP_MODEL_ID)
    if kind not in ("model", "lm", "vae", "lora"):
        raise HTTPException(status_code=400, detail="kind must be one of: model, lm, vae, lora")
    if not _CUSTOM_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Invalid name")
    base = ACE_STEP_ROOT / "custom" / kind
    safe = safe_child_path(base, name)
    if not safe.exists():
        return {"status": "not_installed", "name": name, "kind": kind}
    shutil.rmtree(safe)
    return {"status": "deleted", "name": name, "kind": kind}
