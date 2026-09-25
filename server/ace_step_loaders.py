"""ACE-Step loaders + inference helpers for the omni_worker subprocess.

Lives outside ``omni_worker.py`` so that file doesn't sprawl. The worker
imports the public surface (the ``_AceStepState`` class plus the load /
unload / infer functions) and wires them to FastAPI endpoints.

Covers Omni's ACE-Step modes. Under ACE-Step v1.5 they map to the upstream
task types text2music, cover, repaint, and base-only complete where available:

  T2M (text+lyrics → song)         : infer_generate
  A2A (style-transfer)             : infer_a2a
  Repaint (masked regen)           : infer_repaint
  Edit (only_lyrics / remix)       : infer_edit
  Extend (prepend / append)        : infer_extend
  Cover (re-sing in new style)     : infer_cover
  Vocal→BGM (instrumental from voc): infer_vocal2bgm
  Lyric→Vocal (LoRA-enabled)       : infer_lyric2vocal
  Text→Samples (LoRA-enabled)      : infer_text2samples
  Analyze (BPM / key / loudness)   : analyze_audio (uses librosa; no DiT)

Cross-worker CLAP scoring is the gateway's job — see
``server/routers/ace_step.py``.

Defensive coding notes
----------------------
The ``acestep`` package is loaded via ``--no-deps`` from upstream git, and the
exact attribute layout (``pipe.text_encoder`` vs ``pipe.lm`` vs ``pipe.tokenizer``)
isn't formally documented across versions. We probe for the common names and
raise a clear error if none match. The pipe call accepts kwargs in a single
call surface; unknown modes pass the appropriate flag.
"""

from __future__ import annotations

import base64
import gc
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from config import (
    ACE_STEP_ROOT,
    ACE_STEP_UPSTREAM_LM_DIRS,
    ace_step_core_component_path,
    ace_step_lm_available_path,
    ace_step_lora_path,
    ace_step_model_path,
    ace_step_vae_path,
    get_ace_step_lm,
    get_ace_step_lora,
    get_ace_step_model,
    get_ace_step_vae,
    is_ace_step_lora_installed,
    is_ace_step_model_installed,
    is_ace_step_vae_installed,
)

logger = logging.getLogger(__name__)


# AUD-1: user-supplied model/LM/LoRA/VAE identifiers (especially the part after
# a 'custom:' prefix) are concatenated into filesystem paths under a model root.
# A single allowlisted path segment — no separators, no '..', no NUL, no leading
# dot. Reject anything that could escape the root before it ever touches the FS.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_name_segment(name: str, kind: str) -> str:
    """AUD-1: reject empty names, path separators, '..', NUL, and anything
    outside a strict allowlist before building a path. Returns the name."""
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"Invalid {kind} name {name!r}: must match {_SAFE_NAME_RE.pattern} "
            f"and contain no path separators, '..', or NUL."
        )
    return name


def _ensure_within_root(candidate: Path, root: Path, kind: str) -> Path:
    """AUD-1: resolve the candidate path and assert it is contained within the
    resolved model root, raising on any escape (symlink / traversal)."""
    resolved = Path(candidate).resolve()
    root_resolved = Path(root).resolve()
    is_inside = getattr(resolved, "is_relative_to", None)
    if is_inside is not None:
        contained = resolved.is_relative_to(root_resolved)
    else:  # Python < 3.9 fallback
        contained = (str(resolved) == str(root_resolved) or
                     str(resolved).startswith(str(root_resolved) + os.sep))
    if not contained:
        raise ValueError(
            f"Resolved {kind} path {resolved} escapes the model root {root_resolved}."
        )
    return resolved


def _trust_remote_code() -> bool:
    """AUD-5: trust_remote_code lets a user-installable HF repo run arbitrary
    Python at load time (custom modeling/config code). Default OFF; require an
    explicit opt-in via OMNI_AUDIO_TRUST_REMOTE_CODE for untrusted weights."""
    return os.environ.get("OMNI_AUDIO_TRUST_REMOTE_CODE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_ACE_V15_CONFIG_NAMES = {
    "ace-xl-turbo": "acestep-v15-xl-turbo",
    "ace-xl-sft": "acestep-v15-xl-sft",
    "ace-xl-base": "acestep-v15-xl-base",
    "ace-1.5": "acestep-v15-turbo",
    "ace-base": "acestep-v15-base",
    "ace-sft": "acestep-v15-sft",
    "ace-turbo-cont": "acestep-v15-turbo-continuous",
}
_ACE_V15_TEXT_ENCODER_DIR = "Qwen3-Embedding-0.6B"
_ACE_V15_STAGE_ROOT = ACE_STEP_ROOT / "checkpoints"
_ACE_V15_DEFAULT_LM_ID = "ace-lm-1.7b"
_ACE_V15_DEFAULT_LM_DIR = ACE_STEP_UPSTREAM_LM_DIRS[_ACE_V15_DEFAULT_LM_ID]


def _contains_model_weights(path: Path) -> bool:
    if not path.exists():
        return False
    names = {
        "model.safetensors",
        "model.safetensors.index.json",
        "pytorch_model.bin",
        "pytorch_model.bin.index.json",
        "diffusion_pytorch_model.safetensors",
        "diffusion_pytorch_model.safetensors.index.json",
        "diffusion_pytorch_model.bin",
        "diffusion_pytorch_model.bin.index.json",
    }
    return any((path / name).exists() for name in names)


def _replace_link_or_copy(src: Path, dst: Path) -> None:
    src = Path(src).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src:
                return
        except OSError:
            pass
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    try:
        os.symlink(src, dst, target_is_directory=src.is_dir())
    except OSError:
        # Last resort for filesystems that disallow symlinks. This can be
        # expensive for model directories, but keeps the loader functional.
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _ace_v15_config_name(variant_id: str, info: dict, weights_dir: Path) -> str:
    if variant_id in _ACE_V15_CONFIG_NAMES:
        return _ACE_V15_CONFIG_NAMES[variant_id]
    repo = (info or {}).get("repo") or ""
    tail = repo.rsplit("/", 1)[-1]
    if tail in _ACE_V15_CONFIG_NAMES.values():
        return tail
    if (weights_dir / "config.json").exists():
        return weights_dir.name
    raise ValueError(f"Cannot resolve ACE-Step v1.5 config name for {variant_id!r}")


def _find_shared_ace_component(name: str) -> Path | None:
    path = ace_step_core_component_path(name)
    return Path(path) if path else None


def _prepare_v15_checkpoints(
    variant_id: str,
    info: dict,
    weights_dir: Path,
    vae_variant: str | None,
) -> tuple[Path, str, str | None]:
    """Stage Omni-managed ACE-Step assets into upstream's checkpoints layout."""
    config_name = _ace_v15_config_name(variant_id, info, weights_dir)
    model_src = weights_dir / config_name if _contains_model_weights(weights_dir / config_name) else weights_dir
    if not _contains_model_weights(model_src):
        raise FileNotFoundError(f"ACE-Step DiT weights are incomplete at {model_src}")

    missing: list[str] = []
    text_src = _find_shared_ace_component(_ACE_V15_TEXT_ENCODER_DIR)
    if text_src is None or not _contains_model_weights(text_src):
        text_src = None
        missing.append(_ACE_V15_TEXT_ENCODER_DIR)

    # Upstream's initialize_service() calls check_main_model_exists() before it
    # honors a selected LM or absolute VAE override. That completeness check
    # always requires the bundled official VAE and 1.7B LM in checkpoints.
    # Stage both from Omni's managed install so an offline-ready status cannot
    # unexpectedly turn into a full ACE-Step/Ace-Step1.5 network download.
    shared_vae_src = _find_shared_ace_component("vae")
    if shared_vae_src is None or not _contains_model_weights(shared_vae_src):
        shared_vae_src = None
        missing.append("vae")

    bundled_lm_path = ace_step_lm_available_path(_ACE_V15_DEFAULT_LM_ID)
    bundled_lm_src = Path(bundled_lm_path) if bundled_lm_path else None
    if bundled_lm_src is None or not _contains_model_weights(bundled_lm_src):
        bundled_lm_src = None
        missing.append(_ACE_V15_DEFAULT_LM_DIR)

    vae_checkpoint: str | None = None
    if vae_variant and vae_variant != "default":
        if not is_ace_step_vae_installed(vae_variant):
            raise FileNotFoundError(
                f"VAE {vae_variant} is not installed. Use ACE Step -> Install VAE first."
            )
        vae_checkpoint = str(ace_step_vae_path(vae_variant))
    else:
        if shared_vae_src is not None:
            vae_checkpoint = str(shared_vae_src)

    if missing:
        raise FileNotFoundError(
            "ACE-Step v1.5 shared core components are missing: "
            + ", ".join(missing)
            + ". Install the ACE-Step v1.5 root/main model (`ace-1.5`) once; "
            "XL/Base/SFT DiT variants reuse those shared text-encoder and VAE files."
        )

    _ACE_V15_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    _replace_link_or_copy(model_src, _ACE_V15_STAGE_ROOT / config_name)
    _replace_link_or_copy(text_src, _ACE_V15_STAGE_ROOT / _ACE_V15_TEXT_ENCODER_DIR)
    _replace_link_or_copy(shared_vae_src, _ACE_V15_STAGE_ROOT / "vae")
    _replace_link_or_copy(bundled_lm_src, _ACE_V15_STAGE_ROOT / _ACE_V15_DEFAULT_LM_DIR)
    return _ACE_V15_STAGE_ROOT, config_name, vae_checkpoint


# ---------------------------------------------------------------------------
# Worker-side state
# ---------------------------------------------------------------------------
@dataclass
class _AceStepState:
    """In-process state for the ace_step worker. One DiT + one LM + LoRA stack
    + optional VAE swap. CLAP lives in the audio_lab worker (cross-worker)."""
    device: str = "cuda:0"
    # DiT base
    model_variant: str | None = None
    model_format: str | None = None       # "native" | "diffusers"
    model_pipe: Any = None                # AceStepHandler or DiffusionPipeline
    model_checkpoint_dir: str | None = None
    model_sample_rate: int = 48000
    model_max_duration_s: float = 600.0
    model_default_steps: int = 50
    model_default_cfg: float = 4.0
    # LM (5Hz Qwen3 text encoder)
    lm_variant: str | None = None
    lm_model: Any = None
    lm_tokenizer: Any = None
    lm_device: str | None = None
    lm_backend: str | None = None
    # VAE swap
    vae_swap: str | None = None
    vae_object: Any = None
    # Set when a swapped VAE is freed (unload component="vae"). The swap loads
    # weights *into* the bundled VAE object, so the original weights aren't
    # retained — we can't simply restore them. This flag forces the bundled
    # VAE to be reloaded (via a faithful model reload) on the next inference.
    vae_needs_reload: bool = False
    # Original load_model kwargs, captured so vae_needs_reload can rebuild the
    # pipe with the same precision/offload settings as the original load.
    model_load_kwargs: dict = field(default_factory=dict)
    # LoRA stack (each entry: {"name", "multiplier", "peft_handle"})
    lora_stack: list = field(default_factory=list)
    # Synchronization — DiT inference + LM forward share the GPU; serialize.
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Best-effort cancellation. Diffusion steps can't be interrupted, but the
    # flag is checked between candidates in the gateway's fan-out.
    cancel_event: threading.Event = field(default_factory=threading.Event)


def request_cancel(state: _AceStepState) -> dict:
    state.cancel_event.set()
    return {"cancelled": True}


def clear_cancel(state: _AceStepState) -> None:
    state.cancel_event.clear()


def smoke_import() -> None:
    """Imports run at worker startup to fail loudly if deps are missing.
    Kept local so other worker models don't pay the import cost."""
    import acestep  # noqa: F401
    from acestep.handler import AceStepHandler  # noqa: F401
    from acestep.inference import GenerationConfig, GenerationParams, generate_music  # noqa: F401
    import peft  # noqa: F401
    import diffusers  # noqa: F401  # for diffusers-format path


def init_state(device: str) -> _AceStepState:
    smoke_import()
    return _AceStepState(device=device)


# ---------------------------------------------------------------------------
# Variant resolution (registry + custom installs)
# ---------------------------------------------------------------------------
def _resolve_model_variant(variant_id: str) -> tuple[dict, Any, str]:
    """Return (info, weights_dir_Path, format) for a registry or custom variant.

    Custom variants are referenced via 'custom:<name>' and live under
    ACE_STEP_ROOT/custom/model/<name>/. Format auto-detects.
    """
    if variant_id.startswith("custom:"):
        name = _validate_name_segment(variant_id.split(":", 1)[1], "custom model")  # AUD-1
        path = ACE_STEP_ROOT / "custom" / "model" / name
        path = _ensure_within_root(path, ACE_STEP_ROOT, "custom model")  # AUD-1
        if not path.exists():
            raise FileNotFoundError(
                f"Custom ACE-Step model '{name}' not found at {path}. "
                f"Install via the ACE Step → Custom HF Repo field first."
            )
        fmt = _detect_custom_format(path)
        return ({"display": f"Custom: {name}", "weights_dir": f"custom/model/{name}",
                 "format": fmt, "tier": "custom"}, path, fmt)

    info = get_ace_step_model(variant_id)
    if not info:
        raise ValueError(f"Unknown ACE-Step model: {variant_id}")
    if not is_ace_step_model_installed(variant_id):
        raise FileNotFoundError(
            f"Model {variant_id} is not installed. Use ACE Step → Install "
            f"or `omni-cli ace-step install-model {variant_id}` first."
        )
    return info, ace_step_model_path(variant_id), info.get("format", "native")


def _detect_custom_format(weights_dir) -> str:
    if (weights_dir / "model_index.json").exists():
        return "diffusers"
    if (weights_dir / "config.json").exists() or (weights_dir / "model_config.json").exists():
        return "native"
    for _ in weights_dir.rglob("model_index.json"):
        return "diffusers"
    for _ in weights_dir.rglob("config.json"):
        return "native"
    raise FileNotFoundError(
        f"Custom install at {weights_dir} has no model_index.json or config.json. "
        f"Can't determine loader format."
    )


def _resolve_lm_variant(variant_id: str) -> tuple[dict, Any]:
    if variant_id.startswith("custom:"):
        name = _validate_name_segment(variant_id.split(":", 1)[1], "custom LM")  # AUD-1
        path = ACE_STEP_ROOT / "custom" / "lm" / name
        path = _ensure_within_root(path, ACE_STEP_ROOT, "custom LM")  # AUD-1
        if not path.exists():
            raise FileNotFoundError(f"Custom ACE-Step LM '{name}' not found at {path}.")
        return ({"display": f"Custom LM: {name}", "weights_dir": f"custom/lm/{name}"}, path)
    info = get_ace_step_lm(variant_id)
    if not info:
        raise ValueError(f"Unknown ACE-Step LM: {variant_id}")
    lm_path = ace_step_lm_available_path(variant_id)
    if lm_path is None:
        raise FileNotFoundError(
            f"LM {variant_id} is not installed. Use ACE Step → Install LM "
            f"or `omni-cli ace-step install-lm {variant_id}` first."
        )
    return info, Path(lm_path)


def _resolve_lora(name: str) -> tuple[dict, Any]:
    if name.startswith("custom:"):
        raw = _validate_name_segment(name.split(":", 1)[1], "custom LoRA")  # AUD-1
        path = ACE_STEP_ROOT / "custom" / "lora" / raw
        path = _ensure_within_root(path, ACE_STEP_ROOT, "custom LoRA")  # AUD-1
        if not path.exists():
            raise FileNotFoundError(f"Custom ACE-Step LoRA '{raw}' not found at {path}.")
        return ({"display": f"Custom LoRA: {raw}", "weights_dir": f"custom/lora/{raw}"}, path)
    info = get_ace_step_lora(name)
    if not info:
        # Fall back: ad-hoc LoRAs land at loras/<name>; allow loading even if
        # not in the curated registry, as long as the dir exists.
        safe = _validate_name_segment(name, "ad-hoc LoRA")  # AUD-1
        path = ACE_STEP_ROOT / "loras" / safe
        path = _ensure_within_root(path, ACE_STEP_ROOT, "ad-hoc LoRA")  # AUD-1
        if not path.exists():
            raise ValueError(f"Unknown LoRA: {name}")
        return ({"display": name, "weights_dir": f"loras/{name}", "tier": "adhoc"}, path)
    if not is_ace_step_lora_installed(name):
        raise FileNotFoundError(
            f"LoRA {name} is not installed. Use ACE Step → Install "
            f"or `omni-cli ace-step install-lora --name {name}` first."
        )
    return info, ace_step_lora_path(name)


def _resolve_lora_adapter_view(info: dict, lora_path: Path,
                               adapter_file: str | None) -> Path:
    """Return a PEFT directory view for one selectable file in a LoRA pack."""
    if adapter_file is None:
        return lora_path
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.safetensors", adapter_file):
        raise ValueError("adapter_file must be a safetensors basename without path separators.")
    declared = {
        item.get("filename")
        for item in (info.get("adapter_files") or [])
        if isinstance(item, dict)
    }
    if declared and adapter_file not in declared:
        raise ValueError(
            f"adapter_file '{adapter_file}' is not declared for this LoRA pack. "
            f"Allowed: {sorted(declared)}"
        )
    source = lora_path / adapter_file
    config = lora_path / "adapter_config.json"
    if not source.is_file():
        raise FileNotFoundError(f"LoRA adapter file not found: {adapter_file}")
    if not config.is_file():
        raise FileNotFoundError(f"LoRA adapter_config.json not found in {lora_path}")

    view = lora_path / ".omni_adapter_views" / Path(adapter_file).stem
    view.mkdir(parents=True, exist_ok=True)
    for target, link in (
        (config, view / "adapter_config.json"),
        (source, view / "adapter_model.safetensors"),
    ):
        if link.is_symlink() or link.exists():
            try:
                if link.resolve() == target.resolve():
                    continue
            except OSError:
                pass
            link.unlink()
        try:
            link.symlink_to(target)
        except OSError:
            # Windows commonly denies symlink creation to non-elevated apps.
            # The view is on the same model filesystem, so a hard link keeps
            # the adapter zero-copy; copying is the final portable fallback.
            try:
                os.link(target, link)
            except OSError:
                shutil.copy2(target, link)
    return view


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def _free_model(state: _AceStepState) -> None:
    """Drop the current pipe and free GPU. Detaches LoRAs as a side-effect."""
    pipe = state.model_pipe
    if state.lora_stack:
        for entry in list(state.lora_stack):
            try:
                _detach_lora_handle(entry, pipe=pipe)
            except Exception:
                logger.exception("Failed to detach LoRA %r during model swap", entry.get("name"))
        state.lora_stack.clear()
    state.model_pipe = None
    state.model_variant = None
    state.model_format = None
    state.model_checkpoint_dir = None
    state.vae_swap = None
    state.vae_object = None
    state.vae_needs_reload = False
    state.model_load_kwargs = {}
    _cuda_cleanup(state)


def _free_lm(state: _AceStepState) -> None:
    if state.lm_model is not None and hasattr(state.lm_model, "unload"):
        try:
            state.lm_model.unload()
        except Exception:
            logger.warning("ACE-Step LM unload failed", exc_info=True)
    state.lm_variant = None
    state.lm_model = None
    state.lm_tokenizer = None
    state.lm_device = None
    state.lm_backend = None
    _cuda_cleanup(state)


def _free_vae(state: _AceStepState) -> None:
    # _apply_vae_swap loads the swapped weights *into* the pipe's bundled VAE
    # object (in-place load_state_dict), so the original bundled weights are
    # not retained and can't be restored directly. Mark the pipe as needing a
    # bundled-VAE reload; _generate_common honors this before the next run so
    # the pipe stops using the now-orphaned swapped VAE.
    if state.model_pipe is not None and state.vae_swap is not None:
        state.vae_needs_reload = True
    state.vae_swap = None
    state.vae_object = None
    _cuda_cleanup(state)


def _cuda_cleanup(state: _AceStepState) -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            for index in range(int(torch.cuda.device_count())):
                try:
                    with torch.cuda.device(index):
                        torch.cuda.empty_cache()
                    torch.cuda.synchronize(index)
                except Exception:
                    continue
            try:
                torch.cuda.synchronize(state.device)
            except Exception:
                pass
    except Exception:
        pass


def load_model(state: _AceStepState, variant_id: str, *,
               vae_variant: str | None = None,
               bf16: bool = True, cpu_offload: bool = False,
               int8: bool = False, torch_compile: bool = False) -> dict:
    """Load an ACE-Step DiT. Hot-swaps if a different variant is loaded.

    Optional ``vae_variant`` swaps the bundled VAE at load time. Optional
    flags control precision and offload (helpful on 16 GB cards).
    """
    info, weights_dir, fmt = _resolve_model_variant(variant_id)
    load_kwargs = {"bf16": bf16, "cpu_offload": cpu_offload,
                   "int8": int8, "torch_compile": torch_compile}

    with state.lock:
        _free_model(state)
        # If any step below raises after _free_model, state is left
        # partially-freed (stale lora_stack, half-assigned pipe). Reset to a
        # clean UNLOADED state and re-raise so the next call sees consistent
        # state rather than a corrupt half-load.
        try:
            if fmt == "native":
                _load_model_native(state, variant_id, info, weights_dir,
                                   vae_variant=vae_variant,
                                   bf16=bf16,
                                   cpu_offload=cpu_offload, int8=int8,
                                   torch_compile=torch_compile)
            elif fmt == "diffusers":
                _load_model_diffusers(state, info, weights_dir, bf16=bf16,
                                      cpu_offload=cpu_offload, int8=int8,
                                      torch_compile=torch_compile)
            else:
                raise ValueError(f"Unknown model format: {fmt}")
            state.model_variant = variant_id
            state.model_format = fmt
            state.model_sample_rate = int(info.get("sample_rate", 48000))
            state.model_max_duration_s = float(info.get("max_duration_s", 600))
            state.model_default_steps = int(info.get("steps_default", 50))
            state.model_default_cfg = float(info.get("cfg_default", 4.0))
            state.model_load_kwargs = dict(load_kwargs)
            state.vae_needs_reload = False

            # Apply VAE swap if requested. Default keeps whatever ships in the
            # checkpoint — no-op in that case.
            if vae_variant and vae_variant != "default" and fmt == "diffusers":
                _apply_vae_swap(state, vae_variant)
            elif vae_variant and vae_variant != "default":
                state.vae_swap = vae_variant
        except Exception:
            _free_model(state)  # clears pipe/variant/format/vae/lora_stack + cuda cleanup
            state.model_load_kwargs = {}
            state.vae_needs_reload = False
            raise

    return current_state(state)


def _load_model_native(state, variant_id, info, weights_dir, *,
                       bf16, cpu_offload, int8, torch_compile, vae_variant=None):
    try:
        from acestep.handler import AceStepHandler
    except ImportError as exc:
        raise RuntimeError(
            "ACE-Step v1.5 handler API is missing. Re-run server/setup.sh to repair "
            "the shared venv."
        ) from exc

    checkpoint_dir, config_name, vae_checkpoint = _prepare_v15_checkpoints(
        variant_id, info, Path(weights_dir), vae_variant,
    )
    handler = AceStepHandler()
    device = "cuda" if str(state.device).startswith("cuda") else str(state.device)
    quantization = "int8_weight_only" if int8 else None
    if quantization:
        try:
            import torchao  # noqa: F401
        except Exception:
            logger.warning("INT8 requested for ACE-Step but torchao is unavailable; loading bf16/fp16")
            quantization = None

    status_msg, ok = handler.initialize_service(
        project_root=str(ACE_STEP_ROOT),
        config_path=config_name,
        device=device,
        compile_model=torch_compile,
        offload_to_cpu=cpu_offload,
        offload_dit_to_cpu=cpu_offload,
        quantization=quantization,
        use_mlx_dit=False,
        vae_checkpoint=vae_checkpoint,
    )
    if not ok:
        raise RuntimeError(status_msg)
    state.model_pipe = handler
    state.model_checkpoint_dir = str(checkpoint_dir)
    return


def _load_model_diffusers(state, info, weights_dir, *, bf16, cpu_offload, int8, torch_compile):
    import torch
    from diffusers import DiffusionPipeline
    dtype = torch.bfloat16 if bf16 else torch.float32
    # AUD-5: remote-code execution is opt-in (default OFF) for user-installable repos.
    pipe = DiffusionPipeline.from_pretrained(
        str(weights_dir), trust_remote_code=_trust_remote_code(), torch_dtype=dtype,
    )
    if not cpu_offload and hasattr(pipe, "to"):
        pipe = pipe.to(state.device)
    if cpu_offload and hasattr(pipe, "enable_sequential_cpu_offload"):
        try:
            pipe.enable_sequential_cpu_offload()
        except Exception:
            logger.warning("CPU offload requested but pipe doesn't support it; ignoring")
    if int8:
        _maybe_int8_quantize(pipe)
    if torch_compile and hasattr(torch, "compile"):
        try:
            target = pipe.transformer if hasattr(pipe, "transformer") else pipe
            if hasattr(target, "forward"):
                target.forward = torch.compile(target.forward, mode="reduce-overhead")
        except Exception:
            logger.warning("torch.compile failed; running uncompiled", exc_info=True)
    if state.lm_model is not None:
        _attach_lm_to_pipe(pipe, state.lm_model, state.lm_tokenizer)
    state.model_pipe = pipe


def _maybe_int8_quantize(pipe) -> None:
    """Best-effort dynamic INT8 of the DiT transformer. Skipped if bitsandbytes
    isn't available or quantization fails (e.g. unsupported module types)."""
    try:
        import bitsandbytes  # noqa: F401
        # The acestep transformer typically lives at pipe.transformer.
        # If it doesn't exist, treat as no-op.
        target = getattr(pipe, "transformer", None)
        if target is None:
            target = getattr(pipe, "model", None)
        if target is None:
            logger.info("INT8: no transformer/model attribute on pipe; skipping")
            return
        # Dynamic INT8 via torch's quantize_dynamic is a safe default.
        import torch.nn as nn
        import torch.quantization as tq
        target = tq.quantize_dynamic(target, {nn.Linear}, dtype=__import__("torch").qint8)
        if hasattr(pipe, "transformer"):
            pipe.transformer = target
        else:
            pipe.model = target
    except Exception as e:
        logger.warning("INT8 quantization failed; running fp/bf16: %s", e)


# ---------------------------------------------------------------------------
# LM swap
# ---------------------------------------------------------------------------
def _visible_cuda_count() -> int:
    try:
        import torch
        return int(torch.cuda.device_count()) if torch.cuda.is_available() else 0
    except Exception:
        return 0


def _resolve_lm_runtime_device(state: _AceStepState, lm_device: str | None) -> str:
    """Pick the worker-logical device for the 5Hz LM.

    Official ACE initializes DiT and LM as separate handlers, each with its
    own ``device``. When this worker can see a second CUDA device, default the
    planner onto ``cuda:1`` so the DiT can keep ``cuda:0``.
    """
    requested = str(lm_device or "").strip()
    if requested:
        return requested
    if str(state.device or "").startswith("cuda") and _visible_cuda_count() > 1:
        return "cuda:1"
    return str(state.device or "cpu")


def load_lm(
    state: _AceStepState,
    variant_id: str,
    lm_device: str | None = None,
    backend: str | None = None,
) -> dict:
    """Load a 5Hz LM and attach it to the loaded DiT.

    The pipeline likely already bundles an LM; this overrides it. Probes
    common attribute names: ``set_lm``, ``lm``, ``text_encoder``, ``condition_encoder``.
    """
    if state.model_pipe is None:
        raise RuntimeError("Load a DiT model before swapping its LM.")
    info, lm_path = _resolve_lm_variant(variant_id)
    if state.model_pipe.__class__.__name__ == "AceStepHandler":
        import torch
        from transformers import AutoModelForCausalLM
        from acestep.llm_inference import LLMHandler

        class _DirectDeviceLLMHandler(LLMHandler):
            """Load the 5Hz LM in the requested dtype on the target GPU.

            Upstream ``_load_pytorch_model`` does ``from_pretrained()`` (fp32)
            then ``.to(device).to(dtype)``. That transient fp32 copy needs ~16 GB
            for the 4B planner and will not fit a 12 GB card. Official ACE only
            survives this on 24 GB because DiT+LM share one large GPU.
            """

            def _load_pytorch_model(self, model_path: str, device: str):
                self.llm = AutoModelForCausalLM.from_pretrained(
                    model_path,
                    trust_remote_code=True,
                    torch_dtype=self.dtype,
                )
                if not self.offload_to_cpu:
                    self.llm = self.llm.to(device)
                else:
                    self.llm = self.llm.to("cpu")
                self.llm.eval()
                self.llm_backend = "pt"
                self.llm_initialized = True
                logger.info(
                    "5Hz LM initialized successfully using PyTorch backend on %s",
                    device,
                )
                return True, (
                    f"5Hz LM initialized successfully\nModel: {model_path}\n"
                    f"Backend: PyTorch\nDevice: {device}"
                )

        lm_name = ACE_STEP_UPSTREAM_LM_DIRS.get(variant_id, Path(lm_path).name)
        _replace_link_or_copy(Path(lm_path), _ACE_V15_STAGE_ROOT / lm_name)
        handler = _DirectDeviceLLMHandler()
        backend = str(
            backend
            or os.environ.get("OMNI_ACE_STEP_LM_BACKEND")
            or "pt"
        ).strip().lower()
        if backend not in {"pt", "vllm"}:
            backend = "pt"
        resolved = _resolve_lm_runtime_device(state, lm_device)
        # Official initialize() only treats the exact token "cuda" as CUDA for
        # dtype selection. Indexed devices such as cuda:1 still .to() correctly
        # when dtype is passed explicitly.
        ace_device = "cuda" if resolved in {"cuda", "cuda:0"} else resolved
        init_kwargs: dict[str, Any] = {
            "checkpoint_dir": str(_ACE_V15_STAGE_ROOT),
            "lm_model_path": lm_name,
            "backend": backend,
            "device": ace_device,
        }
        if str(resolved).startswith("cuda"):
            init_kwargs["dtype"] = torch.bfloat16
        with state.lock:
            _free_lm(state)
            status_msg, ok = handler.initialize(**init_kwargs)
            if not ok:
                raise RuntimeError(status_msg)
            state.lm_variant = variant_id
            state.lm_model = handler
            state.lm_tokenizer = getattr(handler, "llm_tokenizer", None)
            state.lm_device = resolved
            state.lm_backend = backend
        return current_state(state)

    import torch
    from transformers import AutoModel, AutoTokenizer
    with state.lock:
        _free_lm(state)
        # ACE-Step LMs are Qwen3-based; the repo may ship custom modeling code.
        # AUD-5: executing that code is opt-in (default OFF) for user-installable
        # repos — gate trust_remote_code behind OMNI_AUDIO_TRUST_REMOTE_CODE.
        trc = _trust_remote_code()
        dtype = torch.bfloat16
        resolved = _resolve_lm_runtime_device(state, lm_device)
        lm = AutoModel.from_pretrained(
            str(lm_path), trust_remote_code=trc, torch_dtype=dtype,
        ).to(resolved).eval()
        try:
            tok = AutoTokenizer.from_pretrained(str(lm_path), trust_remote_code=trc)
        except Exception:
            tok = None
        _attach_lm_to_pipe(state.model_pipe, lm, tok)
        state.lm_variant = variant_id
        state.lm_model = lm
        state.lm_tokenizer = tok
        state.lm_device = resolved
        state.lm_backend = "pt"
    return current_state(state)


def _attach_lm_to_pipe(pipe, lm, tokenizer) -> None:
    """Try the documented setter names, fall back to direct attribute set."""
    for setter in ("set_lm", "set_text_encoder", "set_condition_encoder"):
        if hasattr(pipe, setter):
            try:
                getattr(pipe, setter)(lm, tokenizer)
                return
            except TypeError:
                try:
                    getattr(pipe, setter)(lm)
                    if tokenizer is not None and hasattr(pipe, "tokenizer"):
                        pipe.tokenizer = tokenizer
                    return
                except Exception as e:
                    logger.warning("Setter %s failed: %s", setter, e)
    # No setter — direct attribute assignment. Try the common names.
    for attr in ("lm", "text_encoder", "condition_encoder"):
        if hasattr(pipe, attr):
            setattr(pipe, attr, lm)
            if tokenizer is not None and hasattr(pipe, "tokenizer"):
                pipe.tokenizer = tokenizer
            return
    raise NotImplementedError(
        "Could not attach LM to ACEStepPipeline — none of "
        "set_lm/set_text_encoder/set_condition_encoder methods found, "
        "and no `lm`/`text_encoder`/`condition_encoder` attribute exists. "
        "The acestep package version may have changed its API."
    )


# ---------------------------------------------------------------------------
# VAE swap (Phase 4)
# ---------------------------------------------------------------------------
def _apply_vae_swap(state: _AceStepState, vae_variant: str) -> None:
    info = get_ace_step_vae(vae_variant)
    if not info or vae_variant == "default":
        return
    if not is_ace_step_vae_installed(vae_variant):
        raise FileNotFoundError(
            f"VAE {vae_variant} is not installed. Use ACE Step → Install VAE "
            f"or `omni-cli ace-step install-vae {vae_variant}` first."
        )
    vae_path = ace_step_vae_path(vae_variant)
    import torch
    from safetensors.torch import load_file as load_safetensors

    # Try to locate the VAE weights file (varies by uploader).
    weight_file = None
    for cand in ("model.safetensors", "vae.safetensors", "pytorch_model.bin", "vae.pt"):
        if (vae_path / cand).exists():
            weight_file = vae_path / cand
            break
    if weight_file is None:
        # Recursive fallback — pick the largest safetensors/bin file.
        candidates = sorted(
            [p for p in vae_path.rglob("*") if p.is_file()
             and p.suffix in (".safetensors", ".bin", ".pt", ".ckpt")],
            key=lambda p: p.stat().st_size, reverse=True,
        )
        if not candidates:
            raise FileNotFoundError(f"No VAE weights found under {vae_path}")
        weight_file = candidates[0]

    try:
        if weight_file.suffix == ".safetensors":
            sd = load_safetensors(str(weight_file), device=state.device)
        else:
            sd = torch.load(str(weight_file), map_location=state.device, weights_only=True)
    except Exception as e:
        raise RuntimeError(
            f"Failed to load VAE weights from {weight_file}: {e}. "
            "The community VAE may use an incompatible state-dict layout."
        )

    # Find the pipe's VAE attribute. Probe common names.
    target_attr = None
    for cand in ("vae", "pretransform"):
        if hasattr(state.model_pipe, cand):
            target_attr = cand
            break
    if target_attr is None:
        raise NotImplementedError(
            "Pipe has no `vae` or `pretransform` attribute — cannot swap VAE."
        )
    target = getattr(state.model_pipe, target_attr)
    try:
        missing, unexpected = target.load_state_dict(sd, strict=False)
    except Exception as e:
        raise RuntimeError(
            f"Loading VAE state_dict into pipe.{target_attr} failed: {e}. "
            f"The community VAE's keys likely don't match the bundled VAE's "
            f"shape. Try a different VAE variant or report the mismatch."
        )
    # Catch the silent-partial-load failure mode: if NO keys from the swap
    # matched, target is unchanged and we'd silently generate with the bundled
    # VAE. Surface this clearly rather than producing garbage at inference.
    matched = len(sd) - len(unexpected or [])
    if matched <= 0:
        raise RuntimeError(
            f"VAE swap {vae_variant} produced 0 matching keys against "
            f"pipe.{target_attr} (had {len(sd)} keys, {len(unexpected or [])} unexpected). "
            "The community VAE uses an incompatible state-dict layout."
        )
    if missing and len(missing) > len(sd) // 2:
        logger.warning(
            "VAE swap %s left %d keys un-overwritten on pipe.%s (out of %d total). "
            "Partial swap — results may differ from a clean install.",
            vae_variant, len(missing), target_attr, len(missing) + matched,
        )
    state.vae_swap = vae_variant
    state.vae_object = target


# ---------------------------------------------------------------------------
# LoRA stack
# ---------------------------------------------------------------------------
def attach_lora(state: _AceStepState, name: str, multiplier: float,
                adapter_file: str | None = None) -> dict:
    if state.model_pipe is None:
        raise RuntimeError("Load a DiT model before attaching LoRAs.")
    # Reject duplicates by name.
    for entry in state.lora_stack:
        if entry["name"] == name:
            raise ValueError(f"LoRA '{name}' already attached. Detach first to re-attach with a new multiplier.")
    info, lora_path = _resolve_lora(name)
    selected_path = _resolve_lora_adapter_view(info, Path(lora_path), adapter_file)
    handle = _attach_lora_to_pipe(state.model_pipe, selected_path, multiplier, name)
    state.lora_stack.append({
        "name": name, "multiplier": multiplier,
        "path": str(selected_path), "adapter_file": adapter_file,
        "handle": handle,
        "tier": info.get("tier"),
        "enables_mode": info.get("enables_mode"),
    })
    return list_loras(state)


def detach_lora(state: _AceStepState, name: str) -> dict:
    for i, entry in enumerate(list(state.lora_stack)):
        if entry["name"] == name:
            _detach_lora_handle(entry, pipe=state.model_pipe)
            del state.lora_stack[i]
            return list_loras(state)
    raise KeyError(f"LoRA '{name}' is not currently attached.")


def list_loras(state: _AceStepState) -> dict:
    return {
        "loras": [
            {"name": e["name"], "multiplier": e["multiplier"],
             "adapter_file": e.get("adapter_file"),
             "tier": e.get("tier"), "enables_mode": e.get("enables_mode")}
            for e in state.lora_stack
        ]
    }


def _attach_lora_to_pipe(pipe, lora_path, multiplier, name) -> Any:
    """Attach a PEFT LoRA. Returns a handle for later detach. Probes common
    attach methods on the pipeline."""
    if pipe.__class__.__name__ == "AceStepHandler":
        if not hasattr(pipe, "add_lora"):
            raise NotImplementedError("AceStepHandler has no add_lora method.")
        msg = pipe.add_lora(str(lora_path), adapter_name=name)
        _raise_on_ace_lora_error(msg)
        if hasattr(pipe, "set_active_lora_adapter"):
            active_msg = pipe.set_active_lora_adapter(name)
            _raise_on_ace_lora_error(active_msg)
        if hasattr(pipe, "set_lora_scale"):
            scale = max(0.0, min(1.0, float(multiplier)))
            scale_msg = pipe.set_lora_scale(name, scale)
            _raise_on_ace_lora_error(scale_msg)
        if hasattr(pipe, "set_use_lora"):
            use_msg = pipe.set_use_lora(True)
            _raise_on_ace_lora_error(use_msg)
        return {"kind": "ace_v15", "adapter_name": name, "pipe_ref_id": id(pipe)}

    # diffusers pipelines often expose load_lora_weights(...) directly.
    if hasattr(pipe, "load_lora_weights"):
        try:
            pipe.load_lora_weights(str(lora_path), adapter_name=name)
            # Set scale if supported.
            if hasattr(pipe, "set_adapters"):
                try:
                    pipe.set_adapters([name], adapter_weights=[multiplier])
                except Exception:
                    pass
            return {"kind": "diffusers", "adapter_name": name, "pipe": pipe}
        except Exception as e:
            logger.warning("pipe.load_lora_weights failed (%s); trying PEFT direct.", e)

    # PEFT direct: wrap pipe.transformer (or .model) with PeftModel.
    from peft import PeftModel
    target_attr = None
    for cand in ("transformer", "model", "unet"):
        if hasattr(pipe, cand):
            target_attr = cand
            break
    if target_attr is None:
        raise NotImplementedError(
            "Pipe has no transformer/model/unet attribute — cannot attach LoRA via PEFT."
        )
    base = getattr(pipe, target_attr)
    # If the base is ALREADY a PeftModel (i.e. another LoRA is already attached),
    # add the adapter onto it instead of double-wrapping.
    is_peft = base.__class__.__name__ == "PeftModel" or hasattr(base, "load_adapter")
    if is_peft and hasattr(base, "load_adapter"):
        try:
            base.load_adapter(str(lora_path), adapter_name=name)
            try:
                if hasattr(base, "set_adapters"):
                    # Combine with any existing adapters at unit weight.
                    base.set_adapters([name], adapter_weights=[multiplier])
            except Exception:
                pass
            return {"kind": "peft_add", "adapter_name": name,
                    "target_attr": target_attr, "pipe_ref_id": id(pipe)}
        except Exception as e:
            logger.warning("load_adapter on existing PeftModel failed (%s); falling back to fresh wrap.", e)

    peft_model = PeftModel.from_pretrained(base, str(lora_path), adapter_name=name)
    if hasattr(peft_model, "set_adapter"):
        try:
            peft_model.set_adapter(name)
        except Exception:
            pass
    setattr(pipe, target_attr, peft_model)
    return {"kind": "peft_wrap", "adapter_name": name, "target_attr": target_attr,
            "wrapped_base": base, "pipe_ref_id": id(pipe)}


def _raise_on_ace_lora_error(message: Any) -> None:
    text = str(message or "")
    lower = text.lower()
    if any(token in lower for token in (
        "failed", "not supported", "not initialized", "not found",
        "invalid", "error", "already in use", "unknown adapter",
    )):
        raise RuntimeError(text)


def _recover_failed_final_ace_lora_detach(pipe, name: str) -> bool:
    """Recover ACE-Step 1.5 when its final-adapter removal is half-complete.

    ACE deletes the PEFT adapter before calling ``PeftModel.get_base_model``.
    Some PEFT versions then raise ``KeyError(active_adapter)`` from that call,
    even though the adapter deletion itself succeeded. Reach the underlying
    model without consulting the stale active adapter, restore ACE's CPU
    backup, and reset the same bookkeeping its success path would reset.
    """
    active = getattr(pipe, "_active_loras", None)
    if active:
        return False
    model = getattr(pipe, "model", None)
    decoder = getattr(model, "decoder", None) if model is not None else None
    if decoder is None:
        return False
    peft_base = getattr(decoder, "base_model", None)
    base = getattr(peft_base, "model", None)
    if base is None:
        return False

    backup = getattr(pipe, "_base_decoder", None)
    if backup is not None and hasattr(base, "load_state_dict"):
        base.load_state_dict(backup, strict=False)
    if hasattr(base, "to"):
        device = getattr(pipe, "device", None)
        dtype = getattr(pipe, "dtype", None)
        if device is not None:
            base = base.to(device)
        if dtype is not None:
            base = base.to(dtype)
    if hasattr(base, "eval"):
        base.eval()
    model.decoder = base

    pipe.lora_loaded = False
    pipe.use_lora = False
    pipe._adapter_type = None
    pipe._active_loras = {}
    pipe._lora_adapter_registry = {}
    pipe._lora_active_adapter = None
    pipe._lora_scale_state = {}
    service = getattr(pipe, "_lora_service", None)
    if service is not None:
        service.registry = {}
        service.scale_state = {}
        service.active_adapter = None
        service.last_scale_report = {}
    # The backup is about 3 GB for the 2B ACE decoder. It is only needed while
    # adapters are attached; a future attach will create a fresh one.
    pipe._base_decoder = None
    logger.warning(
        "Recovered ACE-Step final LoRA detach after upstream PEFT active-adapter failure (%s)",
        name,
    )
    return True


def _detach_lora_handle(entry, pipe=None) -> None:
    """Detach a LoRA. Caller passes `pipe` so we can actually unwind the PEFT
    wrapper. If pipe is None (e.g. called during model swap), we skip the
    unwrap — model is being freed anyway."""
    handle = entry.get("handle")
    if not handle:
        return
    kind = handle.get("kind")
    name = handle.get("adapter_name")
    if kind == "ace_v15":
        if pipe is None:
            return
        if hasattr(pipe, "remove_lora"):
            msg = pipe.remove_lora(name)
            try:
                _raise_on_ace_lora_error(msg)
            except RuntimeError:
                if _recover_failed_final_ace_lora_detach(pipe, name):
                    return
                raise RuntimeError(
                    f"AceStepHandler remove_lora({name}) failed: {msg}"
                )
            # ACE-Step 1.5 deliberately leaves the decoder inside an empty
            # PeftModel when its memory-saving path has no base-state backup.
            # The wrapper then retains the deleted adapter as its active name,
            # and the next base-model inference raises KeyError(name). PEFT
            # adapters do not mutate their base weights, so unwrap that empty
            # shell exactly when upstream reports this condition.
            if "base decoder still wrapped" in str(msg).lower():
                model = getattr(pipe, "model", None)
                decoder = getattr(model, "decoder", None) if model is not None else None
                get_base_model = getattr(decoder, "get_base_model", None)
                if callable(get_base_model):
                    model.decoder = get_base_model()
                    logger.info(
                        "Unwrapped empty PEFT decoder after removing final ACE-Step LoRA '%s'",
                        name,
                    )
            if not getattr(pipe, "lora_loaded", True):
                # Upstream keeps this large CPU backup after a successful final
                # detach. Omni treats detach as a resource-release boundary.
                pipe._base_decoder = None
        return
    if kind == "diffusers":
        held = handle.get("pipe")
        target = held if held is not None else pipe
        if target is None:
            return
        # Try delete_adapter / unload_lora_weights, in that order.
        if hasattr(target, "delete_adapters"):
            try:
                target.delete_adapters([name])
                return
            except Exception:
                pass
        if hasattr(target, "delete_adapter"):
            try:
                target.delete_adapter(name)
                return
            except Exception:
                pass
        if hasattr(target, "unload_lora_weights"):
            try:
                target.unload_lora_weights()
            except Exception:
                pass
        return
    if kind == "peft_add":
        # Adapter was added to an existing PeftModel; remove just this adapter.
        if pipe is None:
            return
        target_attr = handle.get("target_attr")
        base = getattr(pipe, target_attr, None) if target_attr else None
        if base is None:
            return
        if hasattr(base, "delete_adapter"):
            try:
                base.delete_adapter(name)
            except Exception:
                logger.warning("delete_adapter(%s) failed on PeftModel", name, exc_info=True)
        return
    if kind == "peft_wrap":
        # We replaced pipe.<attr> with a PeftModel; this entry was the FIRST
        # LoRA, and subsequent `peft_add` entries added adapters to the same
        # PeftModel. Restoring `wrapped_base` here would also destroy those
        # later adapters. Instead, delete *just this adapter* from the
        # PeftModel — leaves any other LoRAs intact.
        if pipe is None:
            return
        target_attr = handle.get("target_attr")
        peft_model = getattr(pipe, target_attr, None) if target_attr else None
        if peft_model is None or not hasattr(peft_model, "delete_adapter"):
            return
        try:
            peft_model.delete_adapter(name)
        except Exception:
            logger.warning("delete_adapter(%s) failed on PeftModel during peft_wrap detach",
                           name, exc_info=True)
        return


# ---------------------------------------------------------------------------
# Unload / state
# ---------------------------------------------------------------------------
def unload(state: _AceStepState, component: str = "all") -> dict:
    with state.lock:
        if component in ("all", "model"):
            _free_model(state)
        if component in ("all", "lm"):
            _free_lm(state)
        if component in ("all", "vae"):
            _free_vae(state)
    return current_state(state)


def current_state(state: _AceStepState) -> dict:
    """Snapshot of what's loaded right now."""
    model_info = get_ace_step_model(state.model_variant) if state.model_variant else None
    return {
        "device": state.device,
        "model_variant": state.model_variant,
        "model_format": state.model_format,
        "model_checkpoint_dir": state.model_checkpoint_dir,
        "model_supported_tasks": (model_info or {}).get("supported_tasks", []),
        "model_loaded": state.model_pipe is not None,
        "model_sample_rate": state.model_sample_rate,
        "model_max_duration_s": state.model_max_duration_s,
        "model_default_steps": state.model_default_steps,
        "model_default_cfg": state.model_default_cfg,
        "model_load_kwargs": dict(state.model_load_kwargs),
        "lm_variant": state.lm_variant,
        "lm_loaded": state.lm_model is not None,
        "lm_device": state.lm_device,
        "lm_backend": state.lm_backend,
        "vae_swap": state.vae_swap,
        "loras": [{"name": e["name"], "multiplier": e["multiplier"],
                   "tier": e.get("tier"), "enables_mode": e.get("enables_mode")}
                  for e in state.lora_stack],
        "cancelled": state.cancel_event.is_set(),
    }


# ---------------------------------------------------------------------------
# Audio I/O helpers
# ---------------------------------------------------------------------------
def _decode_audio_b64(audio_b64: str, sample_rate_hint: int | None) -> tuple[Any, int]:
    """Decode a base64-encoded WAV/FLAC/MP3 into a torch float32 tensor
    shaped (channels, samples). Returns (audio_tensor, sample_rate)."""
    import numpy as np
    import soundfile as sf
    import torch
    try:
        raw = base64.b64decode(audio_b64)
    except Exception as e:
        raise ValueError(f"Invalid audio_base64: {e}")
    try:
        data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception as e:
        raise ValueError(
            f"Could not decode audio: {e}. Supported formats: WAV, FLAC, OGG, MP3."
        )
    # soundfile returns (samples, channels); we want (channels, samples).
    data = np.transpose(data, (1, 0))
    if sample_rate_hint and sample_rate_hint != sr:
        # Caller declared a different rate. Trust the file's actual rate.
        logger.info("audio rate hint=%d, file=%d — using file rate", sample_rate_hint, sr)
    return torch.from_numpy(np.ascontiguousarray(data)), int(sr)


def _encode_audio_to_b64(audio, sample_rate: int, fmt: str = "WAV") -> str:
    """Encode a torch float32 tensor (channels, samples) to base64 WAV."""
    import numpy as np
    import soundfile as sf
    import torch
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().to(torch.float32).numpy()
    # soundfile wants (samples, channels).
    if audio.ndim == 2 and audio.shape[0] in (1, 2) and audio.shape[1] > audio.shape[0]:
        audio = audio.T
    elif audio.ndim == 1:
        audio = audio[:, None]
    audio = np.clip(audio, -1.0, 1.0)
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, subtype="PCM_16", format=fmt)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _resample_if_needed(audio, src_sr: int, dst_sr: int):
    if src_sr == dst_sr:
        return audio
    import torchaudio.functional as F  # torch's resampler is more accurate than librosa for short clips
    return F.resample(audio, src_sr, dst_sr)


def _write_temp_wav_from_request(state: _AceStepState, req: dict, tmp_paths: list[str]) -> str:
    """Decode request init audio and write a 48 kHz stereo WAV for ACE-Step v1.5."""
    audio, sr = _decode_audio_b64(req["init_audio_base64"], req.get("init_sample_rate"))
    audio = _resample_if_needed(audio, sr, state.model_sample_rate)
    import numpy as np
    import soundfile as sf
    import torch
    if hasattr(audio, "detach"):
        audio_np = audio.detach().cpu().to(torch.float32).numpy()
    else:
        audio_np = np.asarray(audio, dtype=np.float32)
    if audio_np.ndim == 1:
        audio_np = audio_np[None, :]
    if audio_np.shape[0] == 1:
        audio_np = np.repeat(audio_np, 2, axis=0)
    audio_np = audio_np[:2].T
    audio_np = np.clip(audio_np, -1.0, 1.0)
    temp_root = ACE_STEP_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="ace_step_", suffix=".wav", dir=str(temp_root))
    os.close(fd)
    sf.write(path, audio_np, state.model_sample_rate, subtype="PCM_16", format="WAV")
    tmp_paths.append(path)
    return path


def _audio_duration_from_file(path: str) -> float | None:
    try:
        import soundfile as sf
        info = sf.info(path)
        if info.samplerate > 0:
            return float(info.frames) / float(info.samplerate)
    except Exception:
        logger.warning("Could not inspect audio duration for %s", path, exc_info=True)
    return None


def _pad_wav_file(path: str, pad_s: float, *, side: str, tmp_paths: list[str]) -> str:
    """Pad silence onto a WAV so extend can reuse official repaint."""
    import numpy as np
    import soundfile as sf

    data, sample_rate = sf.read(path, always_2d=True)
    frames = max(1, int(round(float(pad_s) * float(sample_rate))))
    silence = np.zeros((frames, data.shape[1]), dtype=data.dtype)
    if side == "prepend":
        out = np.concatenate([silence, data], axis=0)
    else:
        out = np.concatenate([data, silence], axis=0)
    temp_root = ACE_STEP_ROOT / "tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    fd, new_path = tempfile.mkstemp(prefix="ace_step_pad_", suffix=".wav", dir=str(temp_root))
    os.close(fd)
    sf.write(new_path, out, sample_rate, subtype="PCM_16", format="WAV")
    tmp_paths.append(new_path)
    return new_path


# ---------------------------------------------------------------------------
# Inference — common dispatch
# ---------------------------------------------------------------------------
def _require_model_loaded(state: _AceStepState) -> None:
    if state.model_pipe is None:
        raise RuntimeError("No ACE-Step model loaded — call /ace_step/load_model first.")


def _honor_vae_reload(state: _AceStepState) -> None:
    """If a swapped VAE was freed, rebuild the pipe with its bundled VAE.

    Freeing a VAE swap (unload component="vae") can't restore the original
    bundled weights in place, so the pipe is flagged for reload. We rebuild it
    here — without the swap — using the original load kwargs so precision and
    offload match the prior load. Caller must hold state.lock. LoRAs attached
    after the swap are dropped by the rebuild, matching a clean model load.
    """
    if not state.vae_needs_reload or state.model_pipe is None:
        return
    variant_id = state.model_variant
    load_kwargs = dict(state.model_load_kwargs)
    logger.info("Reloading bundled VAE for %s (swapped VAE was freed)", variant_id)
    info, weights_dir, fmt = _resolve_model_variant(variant_id)
    _free_model(state)
    try:
        if fmt == "native":
            _load_model_native(state, variant_id, info, weights_dir, **load_kwargs)
        elif fmt == "diffusers":
            _load_model_diffusers(state, info, weights_dir, **load_kwargs)
        else:
            raise ValueError(f"Unknown model format: {fmt}")
        state.model_variant = variant_id
        state.model_format = fmt
        state.model_sample_rate = int(info.get("sample_rate", 48000))
        state.model_max_duration_s = float(info.get("max_duration_s", 600))
        state.model_default_steps = int(info.get("steps_default", 50))
        state.model_default_cfg = float(info.get("cfg_default", 4.0))
        state.model_load_kwargs = load_kwargs
        state.vae_needs_reload = False
    except Exception:
        _free_model(state)
        state.model_load_kwargs = {}
        state.vae_needs_reload = False
        raise


def _seed_generator(state: _AceStepState, seed: int | None):
    import torch
    g = torch.Generator(device=state.device)
    if seed is None:
        seed = torch.seed() & 0x7FFFFFFF
    g.manual_seed(int(seed))
    return g, int(seed)


def _resolve_steps_cfg(state: _AceStepState, req: dict) -> tuple[int, float]:
    steps = req.get("steps")
    cfg = req.get("cfg_scale")
    if steps is None:
        steps = state.model_default_steps
    if cfg is None:
        cfg = state.model_default_cfg
    return int(steps), float(cfg)


def _resolve_duration(state: _AceStepState, req: dict) -> float:
    d = float(req.get("duration_s", 60.0))
    return min(d, state.model_max_duration_s)


def _is_turbo_model(state: _AceStepState) -> bool:
    variant = str(state.model_variant or "")
    if "turbo" in variant or variant == "ace-1.5":
        return True
    return float(getattr(state, "model_default_cfg", 4.0) or 4.0) <= 1.0


def _resolve_shift(state: _AceStepState, req: dict) -> float:
    if req.get("shift") is not None:
        return float(req["shift"])
    info = get_ace_step_model(state.model_variant) if state.model_variant else None
    if info and info.get("shift_default") is not None:
        return float(info["shift_default"])
    return 3.0 if _is_turbo_model(state) else 1.0


def _resolve_sampler_mode(scheduler: str) -> str:
    """Keep the requested solver. v1.5 used to collapse dpmpp to euler."""
    name = str(scheduler or "euler").strip().lower()
    if name in {"euler", "heun", "dpmpp"}:
        return name
    return "euler"


def _v15_request_overrides(state: _AceStepState, req: dict) -> dict[str, Any]:
    """Map Omni generate fields onto official ACE-Step v1.5 GenerationParams."""
    infer_method = str(req.get("infer_method") or "ode").strip().lower()
    if infer_method not in {"ode", "sde"}:
        infer_method = "ode"
    # Gradio turns DCW on for Turbo and off for SFT/base. The Python
    # GenerationParams default is True, so Omni was running SFT with DCW
    # unless the caller overrode it.
    if req.get("dcw_enabled") is None:
        dcw_enabled = _is_turbo_model(state)
    else:
        dcw_enabled = bool(req.get("dcw_enabled"))
    overrides: dict[str, Any] = {
        "shift": _resolve_shift(state, req),
        "sampler_mode": _resolve_sampler_mode(req.get("scheduler", "euler")),
        "infer_method": infer_method,
        "use_adg": bool(req.get("use_adg", False)),
        "dcw_enabled": dcw_enabled,
    }
    interval = req.get("guidance_interval")
    if isinstance(interval, (list, tuple)) and len(interval) == 2:
        overrides["cfg_interval_start"] = float(interval[0])
        overrides["cfg_interval_end"] = float(interval[1])
    negative = str(req.get("negative_prompt") or "").strip()
    if negative:
        overrides["lm_negative_prompt"] = negative
    if req.get("bpm") is not None:
        overrides["bpm"] = int(req["bpm"])
    keyscale = str(req.get("keyscale") or "").strip()
    if keyscale:
        overrides["keyscale"] = keyscale
    timesignature = str(req.get("timesignature") or "").strip()
    if timesignature:
        overrides["timesignature"] = timesignature
    # Gradio tags lyrics with an ISO language. Omni used to omit this, so
    # the DiT rendered "# Languages unknown" and vocals collapsed.
    vocal_language = str(req.get("vocal_language") or "").strip() or "en"
    overrides["vocal_language"] = vocal_language
    if req.get("lm_temperature") is not None:
        overrides["lm_temperature"] = float(req["lm_temperature"])
    if req.get("lm_cfg_scale") is not None:
        overrides["lm_cfg_scale"] = float(req["lm_cfg_scale"])
    if req.get("lm_top_k") is not None:
        overrides["lm_top_k"] = int(req["lm_top_k"])
    if req.get("lm_top_p") is not None:
        overrides["lm_top_p"] = float(req["lm_top_p"])
    if req.get("use_constrained_decoding") is not None:
        overrides["use_constrained_decoding"] = bool(req.get("use_constrained_decoding"))
    if req.get("use_cot_lyrics") is not None:
        overrides["use_cot_lyrics"] = bool(req.get("use_cot_lyrics"))
    if req.get("dcw_mode"):
        overrides["dcw_mode"] = str(req["dcw_mode"])
    if req.get("dcw_scaler") is not None:
        overrides["dcw_scaler"] = float(req["dcw_scaler"])
    if req.get("dcw_high_scaler") is not None:
        overrides["dcw_high_scaler"] = float(req["dcw_high_scaler"])
    if req.get("dcw_wavelet"):
        overrides["dcw_wavelet"] = str(req["dcw_wavelet"])
    if req.get("enable_normalization") is not None:
        overrides["enable_normalization"] = bool(req.get("enable_normalization"))
    if req.get("normalization_db") is not None:
        overrides["normalization_db"] = float(req["normalization_db"])
    if req.get("fade_in_s") is not None:
        overrides["fade_in_duration"] = float(req["fade_in_s"])
    if req.get("fade_out_s") is not None:
        overrides["fade_out_duration"] = float(req["fade_out_s"])
    if req.get("velocity_norm_threshold") is not None:
        overrides["velocity_norm_threshold"] = float(req["velocity_norm_threshold"])
    if req.get("velocity_ema_factor") is not None:
        overrides["velocity_ema_factor"] = float(req["velocity_ema_factor"])
    codes = str(req.get("audio_codes") or "").strip()
    if codes:
        overrides["audio_codes"] = codes
    timesteps = req.get("timesteps")
    if isinstance(timesteps, (list, tuple)) and timesteps:
        overrides["timesteps"] = [float(item) for item in timesteps]
    return overrides


def _filter_generation_kwargs(param_cls, kwargs: dict[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(param_cls)}
    return {key: value for key, value in kwargs.items() if key in names}


def _build_generation_params(kwargs: dict[str, Any]):
    """Drop kwargs the installed acestep GenerationParams does not declare."""
    from acestep.inference import GenerationParams

    return GenerationParams(**_filter_generation_kwargs(GenerationParams, kwargs))


# Mode-critical kwargs that MUST reach the pipe for the mode to behave correctly.
# If we filter and none of these aliases survive, raise rather than silently
# fall back to plain T2M. Groups are deliberately narrow — `task` is shared
# across multiple modes so it's NOT a critical marker for any single mode.
_MODE_CRITICAL_ALIAS_GROUPS = (
    # Any init-audio mode: if init_audio is dropped, a2a/repaint/edit/extend/
    # cover/vocal2bgm all silently degrade to T2M. Raise loudly instead.
    frozenset({"init_audio", "init_audio_path"}),
    # Repaint: at least one of these mask-related kwargs must make it through.
    frozenset({"repaint_start", "mask_start_s", "mask"}),
    # Edit: edit_mode or edit_type must reach the pipe.
    frozenset({"edit_mode", "edit_type"}),
    # Extend: extend_mode must reach the pipe.
    frozenset({"extend_mode"}),
    # Cover: cover_mode marker.
    frozenset({"cover_mode"}),
    # Vocal→BGM: explicit marker (don't pair with the generic `task` key).
    frozenset({"vocal2bgm"}),
)


def _pipe_call(pipe, kwargs: dict) -> Any:
    """Filter kwargs to what the pipe's __call__ accepts. Log anything dropped
    so silent mismatches between our schema and the pipe's API surface are
    visible. Raise loudly if a mode-critical alias group is entirely filtered."""
    import inspect
    sig = None
    try:
        sig = inspect.signature(pipe.__call__)
    except (TypeError, ValueError):
        try:
            sig = inspect.signature(pipe)
        except (TypeError, ValueError):
            sig = None

    if sig is None:
        return pipe(**kwargs)

    params = sig.parameters
    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_kwargs:
        return pipe(**kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params}
    dropped = sorted(set(kwargs.keys()) - set(filtered.keys()))
    if dropped:
        logger.warning(
            "Pipe rejected %d kwarg(s) — silently dropped: %s. "
            "If the inference mode depends on these, the result will be incorrect.",
            len(dropped), dropped,
        )
        # If we dropped a mode-critical alias group entirely AND the user
        # actually passed one of those aliases (i.e. it was in kwargs), bail
        # so the mode failure is visible.
        for group in _MODE_CRITICAL_ALIAS_GROUPS:
            passed = group & set(kwargs.keys())
            survived = group & set(filtered.keys())
            if passed and not survived:
                raise RuntimeError(
                    f"Mode-critical kwargs {sorted(passed)} were rejected by the "
                    f"ACE-Step pipeline (accepted params: {sorted(params.keys())}). "
                    f"This usually means the acestep package version doesn't match "
                    f"what this code expects. Update the acestep install or report "
                    f"the kwarg-name mismatch."
                )
    return pipe(**filtered)


def _extract_audio_from_result(result, sample_rate: int) -> Any:
    """ACEStepPipeline result shape varies. Probe a few attribute / key names."""
    # Most common: .audios (list[Tensor]), .audio (Tensor), .waveforms.
    for attr in ("audios", "audio", "waveforms", "samples"):
        v = getattr(result, attr, None)
        if v is None and isinstance(result, dict):
            v = result.get(attr)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            if len(v) > 1:
                logger.warning(
                    "Pipeline returned %d audio samples; using the first. "
                    "Multi-sample batches are not currently surfaced.", len(v),
                )
            return v[0]
        return v
    raise RuntimeError(
        "Could not find audio output on pipeline result. "
        "Inspected: audios, audio, waveforms, samples. "
        f"Got type {type(result).__name__}."
    )


def _generate_v15_common(state: _AceStepState, req: dict, *, mode: str,
                         extra_kwargs: dict | None = None) -> dict:
    from acestep.inference import GenerationConfig, generate_music

    steps, cfg = _resolve_steps_cfg(state, req)
    duration = _resolve_duration(state, req)
    seed = req.get("seed")
    lm_ready = state.lm_model is not None and getattr(state.lm_model, "llm_initialized", False)
    if req.get("thinking") is None:
        use_lm = lm_ready
    else:
        use_lm = lm_ready and bool(req.get("thinking"))
    lyrics = req.get("lyrics") or ""
    scheduler = req.get("scheduler", "euler")
    sampler_mode = _resolve_sampler_mode(scheduler)
    shift = _resolve_shift(state, req)
    prompt = req.get("prompt") or ""
    task_type = "text2music"
    src_audio = None
    reference_audio = None
    instruction = "Fill the audio semantic mask based on the given conditions:"
    _ACE_TRACKS = (
        "vocals", "backing_vocals", "drums", "bass", "guitar", "keyboard",
        "percussion", "strings", "synth", "fx", "brass", "woodwinds",
    )
    audio_cover_strength = 1.0
    cover_noise_strength = 0.0
    flow_edit_morph = False
    flow_edit_source_caption = ""
    flow_edit_source_lyrics = ""
    repainting_start = 0.0
    repainting_end = -1.0
    tmp_paths: list[str] = []

    try:
        source_duration = None
        if mode in (
            "a2a", "repaint", "edit", "extend", "cover", "vocal2bgm",
            "extract", "lego", "complete",
        ):
            if not req.get("init_audio_base64"):
                raise ValueError(f"Mode '{mode}' requires init_audio_base64")
            src_audio = _write_temp_wav_from_request(state, req, tmp_paths)
            source_duration = _audio_duration_from_file(src_audio)
        if req.get("reference_audio_base64"):
            reference_audio = _write_temp_wav_from_request(
                state,
                {
                    "init_audio_base64": req["reference_audio_base64"],
                    "init_sample_rate": req.get("reference_sample_rate") or req.get("init_sample_rate"),
                },
                tmp_paths,
            )

        if mode == "generate":
            task_type = "text2music"
        elif mode == "a2a":
            task_type = "cover"
            audio_cover_strength = max(0.0, min(1.0, 1.0 - float(req.get("init_noise_level", 0.6))))
        elif mode == "cover":
            task_type = "cover"
            audio_cover_strength = 0.8
        elif mode == "repaint":
            task_type = "repaint"
            repainting_start = float(req.get("mask_start_s", 0.0))
            repainting_end = float(req.get("mask_end_s", -1.0))
            if repainting_end <= repainting_start:
                raise ValueError(f"mask_end_s ({repainting_end}) must be > mask_start_s ({repainting_start})")
            instruction = "Repaint the mask area based on the given conditions:"
        elif mode == "edit":
            edit_mode = req.get("edit_mode", "only_lyrics")
            if edit_mode not in ("only_lyrics", "remix"):
                raise ValueError(f"edit_mode must be only_lyrics or remix; got {edit_mode}")
            task_type = "cover"
            if edit_mode == "only_lyrics":
                audio_cover_strength = 0.9
                flow_edit_morph = True
                flow_edit_source_caption = req.get("source_prompt") or ""
                flow_edit_source_lyrics = req.get("source_lyrics") or ""
            else:
                audio_cover_strength = 0.5
        elif mode == "extend":
            extend_mode = req.get("extend_mode", "append")
            if extend_mode not in ("prepend", "append"):
                raise ValueError(f"extend_mode must be prepend or append; got {extend_mode}")
            extend_s = float(req.get("extend_duration_s", 30.0))
            original_duration = source_duration or 0.0
            if src_audio:
                src_audio = _pad_wav_file(
                    src_audio, extend_s, side=extend_mode, tmp_paths=tmp_paths,
                )
                source_duration = _audio_duration_from_file(src_audio)
            # Official ACE has no prepend primitive. Pad silence and repaint
            # the new region so SFT/base can both extend.
            task_type = "repaint"
            instruction = "Repaint the mask area based on the given conditions:"
            if extend_mode == "prepend":
                repainting_start = 0.0
                repainting_end = extend_s
            else:
                repainting_start = original_duration
                repainting_end = -1.0
            if source_duration is not None:
                duration = min(state.model_max_duration_s, source_duration)
        elif mode == "extract":
            track = str(req.get("track_name") or "").strip().lower()
            if track not in _ACE_TRACKS:
                raise ValueError(f"track_name must be one of {list(_ACE_TRACKS)}; got {track!r}")
            task_type = "extract"
            instruction = f"Extract the {track} track from the audio:"
            if source_duration is not None and not req.get("duration_s"):
                duration = min(state.model_max_duration_s, source_duration)
        elif mode == "lego":
            track = str(req.get("track_name") or "").strip().lower()
            if track not in _ACE_TRACKS:
                raise ValueError(f"track_name must be one of {list(_ACE_TRACKS)}; got {track!r}")
            task_type = "lego"
            instruction = f"Generate the {track} track based on the audio context:"
            if source_duration is not None and not req.get("duration_s"):
                duration = min(state.model_max_duration_s, source_duration)
        elif mode == "complete":
            raw_tracks = req.get("track_names") or req.get("track_name") or ""
            if isinstance(raw_tracks, str):
                tracks = [item.strip().lower() for item in raw_tracks.split(",") if item.strip()]
            else:
                tracks = [str(item).strip().lower() for item in raw_tracks if str(item).strip()]
            bad = [item for item in tracks if item not in _ACE_TRACKS]
            if not tracks or bad:
                raise ValueError(
                    f"complete requires official track names from {list(_ACE_TRACKS)}; got {raw_tracks!r}"
                )
            task_type = "complete"
            instruction = "Complete the input track with " + ", ".join(tracks) + ":"
            if source_duration is not None and not req.get("duration_s"):
                duration = min(state.model_max_duration_s, source_duration)
        elif mode == "vocal2bgm":
            task_type = "complete"
            prompt = prompt or "instrumental accompaniment for the provided vocal track"
            lyrics = "[Instrumental]"
            instruction = "Complete the input vocal track with DRUMS | BASS | GUITAR | KEYBOARD | SYNTH:"
            if source_duration is not None and not req.get("duration_s"):
                duration = min(state.model_max_duration_s, source_duration)
        elif mode in ("lyric2vocal", "text2samples"):
            task_type = "text2music"
            if mode == "text2samples" and not lyrics:
                lyrics = "[Instrumental]"
        else:
            raise NotImplementedError(f"ACE-Step v1.5 mode '{mode}' is not supported")

        if req.get("audio_cover_strength") is not None:
            audio_cover_strength = max(0.0, min(1.0, float(req["audio_cover_strength"])))
        if req.get("cover_noise_strength") is not None:
            cover_noise_strength = max(0.0, min(1.0, float(req["cover_noise_strength"])))

        info = get_ace_step_model(state.model_variant) if state.model_variant else None
        supported = set((info or {}).get("supported_tasks") or [])
        if supported and task_type not in supported:
            raise NotImplementedError(
                f"Loaded model '{state.model_variant}' supports {sorted(supported)}, "
                f"but mode '{mode}' requires upstream task '{task_type}'. "
                "Load an ACE-Step base checkpoint for extract/lego/complete-style modes."
            )

        params = _build_generation_params({
            "task_type": task_type,
            "instruction": instruction,
            "reference_audio": reference_audio,
            "src_audio": src_audio,
            "caption": prompt,
            "lyrics": lyrics or "[Instrumental]",
            "instrumental": not bool((lyrics or "").strip()) or (lyrics or "").strip().lower() == "[instrumental]",
            "duration": duration,
            "inference_steps": steps,
            "guidance_scale": cfg,
            "seed": int(seed) if seed is not None else -1,
            "sampler_mode": sampler_mode,
            "repainting_start": repainting_start,
            "repainting_end": repainting_end,
            "audio_cover_strength": audio_cover_strength,
            "cover_noise_strength": cover_noise_strength,
            "flow_edit_morph": flow_edit_morph,
            "flow_edit_source_caption": flow_edit_source_caption,
            "flow_edit_source_lyrics": flow_edit_source_lyrics,
            "thinking": use_lm,
            # Codes stay on whenever the LM is used. Caption/metadata CoT
            # rewrite is opt-in: official SFT is musically garbled when CoT
            # replaces a finished caption, and silent when the LM is off
            # (no 5Hz codes at all).
            "use_cot_metas": use_lm and bool(req.get("use_cot_metas", False)),
            "use_cot_caption": use_lm and bool(req.get("use_cot_caption", False)),
            "use_cot_language": use_lm and bool(req.get("use_cot_language", False)),
            **_v15_request_overrides(state, req),
        })
        batch_size = int(req.get("batch_size") or 1)
        batch_size = max(1, min(8, batch_size))
        config = GenerationConfig(
            batch_size=batch_size,
            use_random_seed=seed is None,
            seeds=[int(seed)] if seed is not None else None,
            audio_format="wav",
        )

        t0 = time.time()
        with state.lock:
            if state.cancel_event.is_set():
                raise RuntimeError("Cancelled before run started")
            result = generate_music(
                state.model_pipe,
                state.lm_model if use_lm else None,
                params,
                config,
                save_dir=None,
            )
        elapsed = time.time() - t0

        if not result.success or not result.audios:
            raise RuntimeError(result.error or result.status_message or "ACE-Step generation failed")
        encoded = []
        for item in result.audios:
            audio = item.get("tensor")
            sample_rate = int(item.get("sample_rate") or state.model_sample_rate)
            if audio is None:
                raise RuntimeError("ACE-Step generation returned no audio tensor")
            encoded.append({
                "audio_base64": _encode_audio_to_b64(audio, sample_rate),
                "sample_rate": sample_rate,
                "seed": (item.get("params") or {}).get("seed", seed),
                "audio_codes": str((item.get("params") or {}).get("audio_codes") or ""),
                "duration_s": duration,
            })
        first = encoded[0]
        audio_b64, actual_seed, audio_codes = first["audio_base64"], first["seed"], first["audio_codes"]
        sample_rate = first["sample_rate"]
        return {
            "audios": encoded,
            "mode": mode,
            "task_type": task_type,
            "audio_base64": audio_b64,
            "audio_codes": audio_codes,
            "sample_rate": sample_rate,
            "duration_s": duration,
            "steps": steps,
            "cfg_scale": cfg,
            "scheduler": scheduler,
            "sampler_mode": sampler_mode,
            "shift": shift,
            "seed": actual_seed,
            "model_variant": state.model_variant,
            "lm_variant": state.lm_variant,
            "vae_swap": state.vae_swap,
            "loras": [e["name"] for e in state.lora_stack],
            "elapsed_s": round(elapsed, 3),
        }
    finally:
        for path in tmp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _generate_common(state: _AceStepState, req: dict, *, mode: str,
                     extra_kwargs: dict | None = None) -> dict:
    """Common body for every inference mode. Acquires lock, runs pipe, encodes audio."""
    _require_model_loaded(state)
    if state.model_pipe.__class__.__name__ == "AceStepHandler":
        return _generate_v15_common(state, req, mode=mode, extra_kwargs=extra_kwargs)
    if int(req.get("batch_size", 1)) != 1:
        raise ValueError("Batch generation requires the native ACE-Step v1.5 backend")
    steps, cfg = _resolve_steps_cfg(state, req)
    duration = _resolve_duration(state, req)
    generator, seed = _seed_generator(state, req.get("seed"))

    kwargs: dict = {
        "prompt": req.get("prompt"),
        "lyrics": req.get("lyrics") or "",
        "negative_prompt": req.get("negative_prompt"),
        "duration": duration,
        "num_inference_steps": steps,
        "guidance_scale": cfg,
        "scheduler": req.get("scheduler", "euler"),
        "shift": _resolve_shift(state, req),
        "generator": generator,
    }
    gi = req.get("guidance_interval")
    if gi and isinstance(gi, (list, tuple)) and len(gi) == 2:
        kwargs["guidance_interval"] = list(gi)
        kwargs["cfg_interval_start"] = float(gi[0])
        kwargs["cfg_interval_end"] = float(gi[1])
    if req.get("bpm") is not None:
        kwargs["bpm"] = int(req["bpm"])
    if req.get("keyscale"):
        kwargs["keyscale"] = str(req["keyscale"]).strip()
    if req.get("timesignature"):
        kwargs["timesignature"] = str(req["timesignature"]).strip()
    if req.get("overlapped_decode") is not None:
        kwargs["overlapped_decode"] = bool(req.get("overlapped_decode"))
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    t0 = time.time()
    with state.lock:
        if state.cancel_event.is_set():
            raise RuntimeError("Cancelled before run started")
        # Restore the bundled VAE if a prior unload(vae) orphaned the swap.
        _honor_vae_reload(state)
        result = _pipe_call(state.model_pipe, kwargs)
    elapsed = time.time() - t0

    audio = _extract_audio_from_result(result, state.model_sample_rate)
    audio_b64 = _encode_audio_to_b64(audio, state.model_sample_rate)
    return {
        "mode": mode,
        "audio_base64": audio_b64,
        "sample_rate": state.model_sample_rate,
        "duration_s": duration,
        "steps": steps,
        "cfg_scale": cfg,
        "scheduler": req.get("scheduler", "euler"),
        "seed": seed,
        "model_variant": state.model_variant,
        "lm_variant": state.lm_variant,
        "vae_swap": state.vae_swap,
        "loras": [e["name"] for e in state.lora_stack],
        "elapsed_s": round(elapsed, 3),
    }


# ---------------------------------------------------------------------------
# Inference — modes
# ---------------------------------------------------------------------------
def infer_generate(state: _AceStepState, req: dict) -> dict:
    """T2M — pure text+lyrics → song."""
    return _generate_common(state, req, mode="generate")


def infer_a2a(state: _AceStepState, req: dict) -> dict:
    """Style-transfer from a reference clip."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    extra = {
        "init_audio": init_audio,
        "strength": 1.0 - float(req.get("init_noise_level", 0.6)),
        "init_noise_level": float(req.get("init_noise_level", 0.6)),
    }
    return _generate_common(state, req, mode="a2a", extra_kwargs=extra)


def infer_repaint(state: _AceStepState, req: dict) -> dict:
    """Masked segment regeneration."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    mask_start = float(req.get("mask_start_s", 0.0))
    mask_end = float(req.get("mask_end_s", 0.0))
    if mask_end <= mask_start:
        raise ValueError(f"mask_end_s ({mask_end}) must be > mask_start_s ({mask_start})")
    extra = {
        "init_audio": init_audio,
        "repaint_start": mask_start, "repaint_end": mask_end,
        "mask_start_s": mask_start, "mask_end_s": mask_end,
    }
    return _generate_common(state, req, mode="repaint", extra_kwargs=extra)


def infer_edit(state: _AceStepState, req: dict) -> dict:
    """Edit only_lyrics (re-sing same melody, new words) or remix (style change)."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    edit_mode = req.get("edit_mode", "only_lyrics")
    if edit_mode not in ("only_lyrics", "remix"):
        raise ValueError(f"edit_mode must be only_lyrics or remix; got {edit_mode}")
    extra = {"init_audio": init_audio, "edit_mode": edit_mode, "edit_type": edit_mode}
    return _generate_common(state, req, mode="edit", extra_kwargs=extra)


def infer_extend(state: _AceStepState, req: dict) -> dict:
    """Prepend or append to an existing track."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    extend_mode = req.get("extend_mode", "append")
    if extend_mode not in ("prepend", "append"):
        raise ValueError(f"extend_mode must be prepend or append; got {extend_mode}")
    extra = {
        "init_audio": init_audio,
        "extend_mode": extend_mode,
        "extend_duration_s": float(req.get("extend_duration_s", 30.0)),
    }
    return _generate_common(state, req, mode="extend", extra_kwargs=extra)


def infer_extract(state: _AceStepState, req: dict) -> dict:
    """Base-model stem extract."""
    return _generate_common(state, req, mode="extract")


def infer_lego(state: _AceStepState, req: dict) -> dict:
    """Base-model add-a-track."""
    return _generate_common(state, req, mode="lego")


def infer_complete(state: _AceStepState, req: dict) -> dict:
    """Base-model auto-arrange / complete."""
    return _generate_common(state, req, mode="complete")


def infer_cover(state: _AceStepState, req: dict) -> dict:
    """Re-sing an existing track in a new style. Effectively A2A with high
    strength and the original's structure retained."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    extra = {"init_audio": init_audio, "cover_mode": True, "task": "cover"}
    return _generate_common(state, req, mode="cover", extra_kwargs=extra)


def infer_vocal2bgm(state: _AceStepState, req: dict) -> dict:
    """Strip vocals from the input, generate an instrumental accompaniment.

    Default duration is derived from the input audio length unless explicitly
    overridden — matches the documented CLI behavior."""
    init_audio, init_sr = _decode_audio_b64(
        req["init_audio_base64"], req.get("init_sample_rate"),
    )
    init_audio = _resample_if_needed(init_audio, init_sr, state.model_sample_rate)
    extra = {
        "init_audio": init_audio,
        "task": "vocal2bgm", "vocal2bgm": True,
    }
    req2 = dict(req)
    req2.setdefault("prompt", "instrumental accompaniment")
    # Derive duration from input length when not explicitly provided.
    if not req2.get("duration_s"):
        try:
            samples = int(init_audio.shape[-1])
            req2["duration_s"] = float(samples) / float(state.model_sample_rate)
        except Exception:
            # Fall back to the global default if shape introspection fails.
            req2["duration_s"] = 60.0
    return _generate_common(state, req2, mode="vocal2bgm", extra_kwargs=extra)


def _autoattach_mode_lora(state: _AceStepState, lora_name: str) -> bool:
    """Attach the named mode-LoRA if it's installed and not yet on the stack.
    Returns True if it was attached here (caller should detach in finally)."""
    if any(e["name"] == lora_name for e in state.lora_stack):
        return False
    info = get_ace_step_lora(lora_name)
    if info and not info.get("available", True):
        raise FileNotFoundError(
            info.get("unavailable_reason")
            or f"The '{lora_name}' LoRA has not been released upstream."
        )
    if not is_ace_step_lora_installed(lora_name):
        raise FileNotFoundError(
            f"This mode requires the '{lora_name}' LoRA. Install it from "
            f"ACE Step → LoRAs or `omni-cli ace-step install-lora --name {lora_name}`."
        )
    attach_lora(state, lora_name, 1.0)
    return True


def infer_lyric2vocal(state: _AceStepState, req: dict) -> dict:
    """Vocal-only generation. Auto-attaches the lyric2vocal LoRA."""
    attached_here = _autoattach_mode_lora(state, "lyric2vocal")
    try:
        return _generate_common(state, req, mode="lyric2vocal",
                                extra_kwargs={"task": "lyric2vocal"})
    finally:
        if attached_here:
            try:
                detach_lora(state, "lyric2vocal")
            except Exception:
                logger.exception("Failed to auto-detach lyric2vocal LoRA")


def _require_lm(state: _AceStepState):
    if state.lm_model is None or not getattr(state.lm_model, "llm_initialized", False):
        raise RuntimeError("Load a 5Hz LM first (POST /api/ace_step/load with lm_variant).")
    return state.lm_model


def _lm_sample_kwargs(req: dict) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if req.get("lm_temperature") is not None:
        kwargs["temperature"] = float(req["lm_temperature"])
    if req.get("lm_top_k") is not None:
        kwargs["top_k"] = int(req["lm_top_k"])
    if req.get("lm_top_p") is not None:
        kwargs["top_p"] = float(req["lm_top_p"])
    if req.get("use_constrained_decoding") is not None:
        kwargs["use_constrained_decoding"] = bool(req.get("use_constrained_decoding"))
    return kwargs


def _lm_result_dict(result) -> dict:
    payload = {
        "success": bool(getattr(result, "success", False)),
        "error": getattr(result, "error", None),
        "status_message": getattr(result, "status_message", "") or "",
        "caption": getattr(result, "caption", "") or "",
        "lyrics": getattr(result, "lyrics", "") or "",
        "bpm": getattr(result, "bpm", None),
        "duration_s": getattr(result, "duration", None),
        "keyscale": getattr(result, "keyscale", "") or "",
        "vocal_language": getattr(result, "language", "") or "",
        "timesignature": getattr(result, "timesignature", "") or "",
    }
    if hasattr(result, "instrumental"):
        payload["instrumental"] = bool(result.instrumental)
    if not payload["success"]:
        raise RuntimeError(payload["error"] or payload["status_message"] or "ACE-Step LM helper failed")
    return payload


def infer_create_sample(state: _AceStepState, req: dict) -> dict:
    """Official Simple Mode: style query -> caption, lyrics, metadata."""
    from acestep.inference import create_sample

    handler = _require_lm(state)
    query = str(req.get("query") or req.get("prompt") or "").strip()
    if not query:
        raise ValueError("create_sample requires query or prompt")
    result = create_sample(
        handler,
        query,
        instrumental=bool(req.get("instrumental", False)),
        vocal_language=req.get("vocal_language"),
        **_lm_sample_kwargs(req),
    )
    return _lm_result_dict(result)


def infer_format_sample(state: _AceStepState, req: dict) -> dict:
    """Official Format: expand user caption/lyrics into structured metadata."""
    from acestep.inference import format_sample

    handler = _require_lm(state)
    caption = str(req.get("prompt") or req.get("caption") or "").strip()
    lyrics = str(req.get("lyrics") or "")
    if not caption:
        raise ValueError("format_sample requires prompt/caption")
    user_metadata = {}
    if req.get("bpm") is not None:
        user_metadata["bpm"] = int(req["bpm"])
    if req.get("duration_s") is not None:
        user_metadata["duration"] = float(req["duration_s"])
    if req.get("keyscale"):
        user_metadata["keyscale"] = str(req["keyscale"])
    if req.get("timesignature"):
        user_metadata["timesignature"] = str(req["timesignature"])
    if req.get("vocal_language"):
        user_metadata["language"] = str(req["vocal_language"])
    result = format_sample(
        handler,
        caption,
        lyrics,
        user_metadata=user_metadata or None,
        **_lm_sample_kwargs(req),
    )
    return _lm_result_dict(result)


def infer_understand_music(state: _AceStepState, req: dict) -> dict:
    """Official understand_music: 5Hz codes -> caption/lyrics/metadata."""
    from acestep.inference import understand_music

    handler = _require_lm(state)
    codes = str(req.get("audio_codes") or "").strip()
    if not codes:
        raise ValueError("understand_music requires audio_codes")
    result = understand_music(handler, codes, **_lm_sample_kwargs(req))
    return _lm_result_dict(result)


def infer_text2samples(state: _AceStepState, req: dict) -> dict:
    """Short-sample / instrument-clip generation. Auto-attaches text2samples LoRA."""
    attached_here = _autoattach_mode_lora(state, "text2samples")
    try:
        return _generate_common(state, req, mode="text2samples",
                                extra_kwargs={"task": "text2samples"})
    finally:
        if attached_here:
            try:
                detach_lora(state, "text2samples")
            except Exception:
                logger.exception("Failed to auto-detach text2samples LoRA")


# ---------------------------------------------------------------------------
# Audio analysis (BPM / key / loudness) — Phase 5
# ---------------------------------------------------------------------------
def analyze_audio(state: _AceStepState, audio_b64: str, sample_rate: int | None) -> dict:
    """librosa-based BPM/key/loudness estimate. Doesn't load the DiT — runs
    even on an empty worker. Cheap (sub-second on a 1-min clip)."""
    import numpy as np
    audio, sr = _decode_audio_b64(audio_b64, sample_rate)
    # Mono mix for analysis.
    mono = audio.mean(dim=0).cpu().numpy() if hasattr(audio, "mean") else audio.mean(axis=0)
    mono = mono.astype(np.float32)

    out: dict = {"sample_rate": sr, "duration_s": float(mono.size) / float(sr)}

    try:
        import librosa
    except ImportError:
        out["error"] = "librosa not available — install it in the shared venv"
        return out

    try:
        tempo, _ = librosa.beat.beat_track(y=mono, sr=sr)
        # librosa 0.10 returns scalar, 0.11+ may return numpy array of length 1.
        if hasattr(tempo, "item"):
            tempo = float(tempo.item()) if tempo.size == 1 else float(tempo[0])
        out["bpm"] = round(float(tempo), 2)
    except Exception as e:
        out["bpm_error"] = str(e)

    try:
        chroma = librosa.feature.chroma_cqt(y=mono, sr=sr)
        avg = chroma.mean(axis=1)
        key_idx = int(np.argmax(avg))
        out["key"] = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][key_idx]
        out["key_confidence"] = float(avg[key_idx] / (avg.sum() + 1e-9))
    except Exception as e:
        out["key_error"] = str(e)

    try:
        rms = librosa.feature.rms(y=mono)[0]
        out["loudness_db_rms"] = round(20.0 * float(np.log10(max(1e-6, float(rms.mean())))), 2)
        out["peak_amplitude"] = round(float(np.max(np.abs(mono))), 4)
    except Exception as e:
        out["loudness_error"] = str(e)

    return out
