"""Audio Lab loaders + inference helpers for the omni_worker subprocess.

Lives outside ``omni_worker.py`` so that file doesn't sprawl. The worker
imports the public surface (the ``_AudioLabState`` class plus the load /
unload / infer functions) and wires them to FastAPI endpoints.

Phase 2 implements:
 * Diffusers-format Stable Audio loading via ``StableAudioPipeline``
 * CLAP loading via ``transformers.ClapModel`` + ``ClapProcessor``
 * Text-to-audio inference (``audio_gen``)
 * CLAP scoring (``audio_score``)
 * Component-granular unload

Phases 3-4 will add:
 * Native-format loading via ``stable_audio_tools.create_model_from_config``
 * Audio-to-audio, inpainting, unconditional, VAE encode/decode/reconstruct
 * VAE swap
"""

from __future__ import annotations

import base64
import gc
import io
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    AUDIO_LAB_RF_SAMPLERS,
    AUDIO_LAB_V_SAMPLERS,
    AUDIO_LAB_ROOT,
    audio_lab_vae_path,
    audio_lab_weights_path,
    clap_weights_path,
    get_audio_lab_model,
    get_audio_lab_vae,
    get_clap_model,
    is_audio_lab_variant_installed,
    is_audio_lab_vae_installed,
    is_clap_installed,
)

logger = logging.getLogger(__name__)


# AUD-1: user-supplied variant/VAE identifiers (especially the part after a
# 'custom:' prefix) are concatenated into filesystem paths under a model root.
# A single allowlisted path segment — no separators, no '..', no NUL, no leading
# dot. Reject anything that could escape the root before it ever touches the FS.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_AUDIO_WEIGHT_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin")


def _validate_name_segment(name: str, kind: str) -> str:
    """AUD-1: reject empty names, path separators, '..', NUL, and anything
    outside a strict allowlist before building a path. Returns the name."""
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"Invalid {kind} name {name!r}: must match {_SAFE_NAME_RE.pattern} "
            f"and contain no path separators, '..', or NUL."
        )
    return name


def _ensure_within_root(candidate, root, kind: str) -> Path:
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


def _has_weight_file(path: Path) -> bool:
    try:
        return any(
            f.is_file() and f.suffix.lower() in _AUDIO_WEIGHT_EXTS
            for f in path.rglob("*")
        )
    except OSError:
        return False


def _custom_asset_dir(kind: str, name: str) -> Path:
    """Return a validated custom Audio Lab asset path under custom/<kind>/."""
    safe_name = _validate_name_segment(name, f"custom {kind}")
    root = AUDIO_LAB_ROOT / "custom" / kind
    path = _ensure_within_root(root / safe_name, root, f"custom {kind}")
    if not path.exists():
        raise FileNotFoundError(
            f"Custom Audio Lab {kind} '{safe_name}' not found at {path}. "
            f"Install it from the Audio Lab Custom HuggingFace Repo field first."
        )
    if not _has_weight_file(path):
        raise FileNotFoundError(
            f"Custom Audio Lab {kind} '{safe_name}' has no recognized weight files."
        )
    return path


def _require_stable_audio_tools(feature: str) -> None:
    try:
        import stable_audio_tools  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            f"{feature} requires stable-audio-tools, but it is not installed. "
            f"Run the app setup/venv repair, then retry."
        ) from e


def _trust_remote_code() -> bool:
    """AUD-5: trust_remote_code lets a user-installable HF repo run arbitrary
    Python at load time (custom modeling/config code). Default OFF; require an
    explicit opt-in via OMNI_AUDIO_TRUST_REMOTE_CODE for untrusted weights."""
    return os.environ.get("OMNI_AUDIO_TRUST_REMOTE_CODE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _warn_load_mismatch(load_result, what: str) -> None:
    """Surface a partial/mismatched state_dict load.

    ``load_state_dict(..., strict=False)`` swallows missing/unexpected keys,
    which silently yields a garbage model. This logs a clear WARNING with
    counts and a few example keys so the mismatch is visible.
    """
    missing = list(getattr(load_result, "missing_keys", None) or [])
    unexpected = list(getattr(load_result, "unexpected_keys", None) or [])
    if not missing and not unexpected:
        return
    logger.warning(
        "%s: state_dict load mismatch — %d missing, %d unexpected keys "
        "(missing e.g. %s; unexpected e.g. %s)",
        what, len(missing), len(unexpected),
        missing[:3], unexpected[:3],
    )


# ---------------------------------------------------------------------------
# Worker-side state
# ---------------------------------------------------------------------------
@dataclass
class _AudioLabState:
    """In-process state for the audio_lab worker."""
    device: str = "cuda:0"
    # Stable Audio
    sa_variant: str | None = None
    sa_format: str | None = None      # "diffusers" | "native"
    sa_objective: str | None = None   # "v" | "rectified_flow" | "rf_denoiser"
    sa_pipe: Any = None               # StableAudioPipeline or (model, model_config)
    sa_lora_stack: list = field(default_factory=list)
    vae_swap: str | None = None       # variant_id of swapped VAE, or None
    # CLAP
    clap_variant: str | None = None
    clap_model: Any = None
    clap_processor: Any = None
    # Synchronization — one global lock; SA generation and CLAP scoring share
    # the GPU so we serialize them rather than running concurrently.
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Best-effort cancellation. Diffusion steps inside the pipe call can't
    # be interrupted, but the flag is checked between candidates (in the
    # gateway's fan-out) and between CLAP windows.
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Sample rate of the currently loaded SA model (cached at load time).
    sa_sample_rate: int = 44100
    sa_max_duration_s: float = 120.0


def request_cancel(state: _AudioLabState) -> dict:
    """Set the cancel flag. Returns the new state."""
    state.cancel_event.set()
    return {"cancelled": True}


def clear_cancel(state: _AudioLabState) -> None:
    state.cancel_event.clear()


def smoke_import() -> None:
    """Imports run at worker startup to fail loudly if the deps are missing.

    Imports are local so they don't load until the audio_lab loader runs —
    other worker models (qwen, minicpm, etc.) shouldn't pay this cost.
    """
    import diffusers  # noqa: F401
    import transformers  # noqa: F401  # ClapModel lives here


def init_state(device: str) -> _AudioLabState:
    """Worker initial state — no sub-models loaded yet."""
    smoke_import()
    return _AudioLabState(device=device)


# ---------------------------------------------------------------------------
# Stable Audio loading
# ---------------------------------------------------------------------------
def _resolve_variant(variant_id: str) -> tuple[Any, "Path | None", str]:
    """Return (info_dict, weights_dir, format) for a registry or custom variant.

    Custom variants are referenced via 'custom:<name>' and live under
    AUDIO_LAB_ROOT/custom/model/<name>/. Format is auto-detected from
    on-disk contents (model_index.json → diffusers, model_config.json → native).
    """
    if variant_id.startswith("custom:"):
        name = variant_id.split(":", 1)[1]
        path = _custom_asset_dir("model", name)
        if not path.exists():
            raise FileNotFoundError(
                f"Custom Stable Audio install '{name}' not found at {path}. "
                f"Install it via the Audio Lab → Custom HF Repo field first."
            )
        fmt, load_dir = _detect_custom_format(path)
        info = {
            "display": f"Custom: {name}",
            "weights_dir": f"custom/model/{name}",
            "format": fmt,
            "tier": "custom",
            "max_duration_s": 120,
        }
        return info, load_dir, fmt

    info = get_audio_lab_model(variant_id)
    if not info:
        raise ValueError(f"Unknown Stable Audio variant: {variant_id}")
    if not is_audio_lab_variant_installed(variant_id):
        raise FileNotFoundError(
            f"Variant {variant_id} is not installed. Run the Audio Lab → "
            f"Install button or `omni-cli audio-lab install-model {variant_id}` first."
        )
    return info, audio_lab_weights_path(variant_id), info.get("format", "diffusers")


def _detect_custom_format(weights_dir: "Path") -> tuple[str, "Path"]:
    """diffusers if model_index.json is present (the diffusers sentinel),
    else native if model_config.json is present, else 'unknown'.

    Returns (format, load_dir). Diffusers repos can be nested under a single
    subfolder; in that case the load_dir must be the folder containing
    model_index.json, not the custom install root.
    """
    if (weights_dir / "model_index.json").exists():
        return "diffusers", weights_dir
    if (weights_dir / "model_config.json").exists():
        return "native", weights_dir
    # Recursive look-up: some uploaders nest configs.
    for p in weights_dir.rglob("model_index.json"):
        return "diffusers", p.parent
    for p in weights_dir.rglob("model_config.json"):
        return "native", weights_dir
    raise FileNotFoundError(
        f"Custom install at {weights_dir} has no model_index.json (diffusers) "
        f"or model_config.json (native). Can't determine loader."
    )


def load_sa(state: _AudioLabState, variant_id: str,
            vae_variant: str | None = None) -> dict:
    """Load the chosen Stable Audio variant. Hot-swaps if a different one
    is already loaded. Returns a small status dict."""
    info, weights_dir, fmt = _resolve_variant(variant_id)
    max_duration_s = float(info.get("max_duration_s") or 120.0)

    with state.lock:
        _free_sa(state)
        if fmt == "diffusers":
            _load_sa_diffusers(state, variant_id, weights_dir, vae_variant)
        elif fmt == "native":
            _load_sa_native(state, variant_id, weights_dir, vae_variant)
        else:
            raise ValueError(f"Unknown variant format: {fmt}")
        state.sa_variant = variant_id
        state.sa_format = fmt
        state.vae_swap = vae_variant if (vae_variant and vae_variant != "default") else None
        state.sa_max_duration_s = max_duration_s

    return {
        "loaded": True,
        "variant_id": variant_id,
        "format": fmt,
        "diffusion_objective": state.sa_objective,
        "valid_samplers": list(_samplers_for_objective(state.sa_objective)),
        "vae_swap": state.vae_swap,
        "sample_rate": state.sa_sample_rate,
        "max_duration_s": state.sa_max_duration_s,
    }


def _load_sa_diffusers(state: _AudioLabState, variant_id: str,
                       weights_dir, vae_variant: str | None) -> None:
    """Diffusers path: pipe = StableAudioPipeline.from_pretrained(local_dir)."""
    import torch
    from diffusers import StableAudioPipeline

    dtype = torch.float16 if state.device.startswith("cuda") else torch.float32
    logger.info("Loading Stable Audio (diffusers) %s on %s (%s)",
                variant_id, state.device, dtype)
    # AUD-5: remote-code execution is opt-in (default OFF) for user-installable repos.
    pipe = StableAudioPipeline.from_pretrained(
        str(weights_dir), torch_dtype=dtype, trust_remote_code=_trust_remote_code(),
    )
    pipe = pipe.to(state.device)

    # Optional VAE swap. The community `lyraaaa/sao_vae_tuned_100k` ships
    # in the stable-audio-tools native format, so we load the autoencoder
    # via that toolkit and assign it onto the diffusers pipe.
    if vae_variant and vae_variant != "default":
        _apply_vae_swap_diffusers(pipe, vae_variant, dtype, state.device)

    state.sa_pipe = pipe
    state.sa_objective = "v"
    try:
        state.sa_sample_rate = int(pipe.vae.config.sampling_rate)
    except AttributeError:
        state.sa_sample_rate = 44100
    logger.info("Stable Audio loaded (sr=%d)", state.sa_sample_rate)


def _bundled_sao10_config_path() -> "Path | None":
    """If the user has sao-open-1.0 installed (which ships a diffusers model
    along with `model_config.json` in some uploads), point at that config as
    a last-resort fallback for native variants that omit theirs.

    Returns None if neither sao-open-1.0 nor an explicit fallback file is
    available — caller must surface a clear error in that case.
    """
    p = audio_lab_weights_path("sao-open-1.0")
    if p:
        candidate = _find_model_config_in_dir(p)
        if candidate:
            return candidate
    return None


def _find_model_config_in_dir(weights_dir) -> "Path | None":
    """Search a directory tree for the first ``model_config.json``."""
    direct = weights_dir / "model_config.json"
    if direct.exists():
        return direct
    for p in weights_dir.rglob("model_config.json"):
        return p
    return None


def _find_native_weights(weights_dir) -> "Path | None":
    """First .ckpt / .safetensors in the tree (preferring .safetensors)."""
    for ext in (".safetensors", ".ckpt"):
        direct = weights_dir / f"model{ext}"
        if direct.exists():
            return direct
    for ext in (".safetensors", ".ckpt"):
        for p in weights_dir.rglob(f"*{ext}"):
            return p
    return None


def _stable_audio_2_0_vae_config() -> dict:
    """Minimal canonical config for Stable Audio's 2.0 autoencoder.

    ``lyraaaa/sao_vae_tuned_100k`` intentionally ships raw checkpoints with
    no config and declares this exact stock architecture in its README.  Keep
    only the inference fields consumed by stable-audio-tools' factory.
    """
    return {
        "model_type": "autoencoder",
        "sample_size": 65536,
        "sample_rate": 44100,
        "audio_channels": 2,
        "model": {
            "encoder": {
                "type": "oobleck",
                "config": {
                    "in_channels": 2,
                    "channels": 128,
                    "c_mults": [1, 2, 4, 8, 16],
                    "strides": [2, 4, 4, 8, 8],
                    "latent_dim": 128,
                    "use_snake": True,
                },
            },
            "decoder": {
                "type": "oobleck",
                "config": {
                    "out_channels": 2,
                    "channels": 128,
                    "c_mults": [1, 2, 4, 8, 16],
                    "strides": [2, 4, 4, 8, 8],
                    "latent_dim": 64,
                    "use_snake": True,
                    "final_tanh": False,
                },
            },
            "bottleneck": {"type": "vae"},
            "latent_dim": 64,
            "downsampling_ratio": 2048,
            "io_channels": 2,
        },
    }


def _vae_model_config(vae_variant: str, vae_dir: Path) -> dict:
    """Resolve a VAE config from the package or an explicit registry profile."""
    import json as _json

    cfg_path = _find_model_config_in_dir(vae_dir)
    if cfg_path is not None:
        with open(cfg_path) as fh:
            return _json.load(fh)

    info = get_audio_lab_vae(vae_variant) or {}
    profile = info.get("config_profile")
    if profile == "stable_audio_2_0_vae":
        return _stable_audio_2_0_vae_config()
    raise FileNotFoundError(f"No model_config.json in VAE dir {vae_dir}")


def _find_vae_weights(vae_variant: str, vae_dir: Path) -> "Path | None":
    """Honor an explicit checkpoint selection before generic discovery."""
    info = get_audio_lab_vae(vae_variant) or {}
    preferred = info.get("weights_file")
    if preferred:
        candidate = vae_dir / preferred
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(
            f"Configured VAE weights '{preferred}' not found in {vae_dir}"
        )
    return _find_native_weights(vae_dir)


def _load_sa_native(state: _AudioLabState, variant_id: str,
                    weights_dir, vae_variant: str | None) -> None:
    """Native path via stable-audio-tools: build the model from its config,
    load the .ckpt, optionally swap the VAE pretransform."""
    import json as _json
    import torch
    _require_stable_audio_tools("Native Stable Audio loading")
    from stable_audio_tools.models.factory import create_model_from_config
    from stable_audio_tools.models.utils import load_ckpt_state_dict

    cfg_path = _find_model_config_in_dir(weights_dir)
    if cfg_path is None:
        cfg_path = _bundled_sao10_config_path()
        if cfg_path is None:
            raise FileNotFoundError(
                f"No model_config.json in {weights_dir} and sao-open-1.0 isn't "
                f"installed for fallback. Native variants need a config. Install "
                f"sao-open-1.0 (which ships one) or pick a Tier 1 / Tier 2 variant."
            )
        logger.warning("Native variant %s has no config — falling back to "
                       "sao-open-1.0's config at %s", variant_id, cfg_path)
    with open(cfg_path) as fh:
        model_config = _json.load(fh)

    weights_path = _find_native_weights(weights_dir)
    if weights_path is None:
        raise FileNotFoundError(
            f"No .ckpt or .safetensors found in {weights_dir}"
        )

    dtype = torch.float16 if state.device.startswith("cuda") else torch.float32
    logger.info("Loading Stable Audio (native) %s from %s (cfg=%s)",
                variant_id, weights_path.name, cfg_path.name)
    model = create_model_from_config(model_config)
    sd = load_ckpt_state_dict(str(weights_path))
    _load_result = model.load_state_dict(sd, strict=False)
    _warn_load_mismatch(_load_result, f"Stable Audio native {variant_id}")
    model = model.to(state.device).to(dtype).eval()

    if vae_variant and vae_variant != "default":
        _apply_vae_swap_native(model, vae_variant, dtype, state.device)

    # Sample rate lives at model_config["sample_rate"] (top level) or
    # model_config["model"]["sample_rate"]. Default to 44100.
    sr = (
        model_config.get("sample_rate")
        or (model_config.get("model") or {}).get("sample_rate")
        or 44100
    )
    state.sa_pipe = (model, model_config)
    state.sa_objective = str(
        ((model_config.get("model") or {}).get("diffusion") or {}).get(
            "diffusion_objective"
        )
        or getattr(model, "diffusion_objective", "")
        or "v"
    )
    state.sa_sample_rate = int(sr)
    logger.info("Stable Audio native loaded (sr=%d)", state.sa_sample_rate)


# ---------------------------------------------------------------------------
# VAE swap helpers (diffusers + native paths)
# ---------------------------------------------------------------------------
def _resolve_vae_dir(vae_variant: str | None) -> Path:
    if not vae_variant or vae_variant == "default":
        raise ValueError("default VAE does not have a separate weights directory")
    if vae_variant.startswith("custom:"):
        return _custom_asset_dir("vae", vae_variant.split(":", 1)[1])
    vae_info = get_audio_lab_vae(vae_variant)
    if not vae_info or vae_info.get("variant_id") == "default":
        raise ValueError(f"Unknown VAE variant: {vae_variant}")
    if not is_audio_lab_vae_installed(vae_variant):
        raise FileNotFoundError(
            f"VAE variant '{vae_variant}' is not installed. "
            f"Install it from the Audio Lab tab."
        )
    vae_dir = audio_lab_vae_path(vae_variant)
    if not vae_dir:
        raise FileNotFoundError(f"VAE variant '{vae_variant}' has no weights path")
    return vae_dir


def _apply_vae_swap_diffusers(pipe, vae_variant: str, dtype, device: str) -> None:
    """Replace pipe.vae with a stable-audio-tools-format autoencoder.

    The swapped VAE is a native pretransform; we wrap it minimally so that
    pipe.vae.encode / pipe.vae.decode keep working with the diffusers code path.
    """
    vae_dir = _resolve_vae_dir(vae_variant)

    _require_stable_audio_tools("Stable Audio VAE swap")
    from stable_audio_tools.models.factory import create_model_from_config
    from stable_audio_tools.models.utils import load_ckpt_state_dict

    vae_cfg = _vae_model_config(vae_variant, vae_dir)
    weights_path = _find_vae_weights(vae_variant, vae_dir)
    if weights_path is None:
        raise FileNotFoundError(f"No weights file in VAE dir {vae_dir}")

    vae_model = create_model_from_config(vae_cfg)
    sd = load_ckpt_state_dict(str(weights_path))
    _load_result = vae_model.load_state_dict(sd, strict=False)
    _warn_load_mismatch(_load_result, f"diffusers VAE swap '{vae_variant}'")
    vae_model = vae_model.to(device).to(dtype).eval()

    # Adapt the native VAE to the diffusers .encode()/.decode() contract.
    # StableAudioPipeline expects pipe.vae to return an object with .latent_dist
    # on encode and .sample on decode. We wrap with a thin shim that mirrors
    # AutoencoderOobleck's interface.
    prev_vae = pipe.vae
    prev_config = getattr(prev_vae, "config", None)
    pipe.vae = _NativeVAEShim(vae_model, prev_config)
    # M2: explicitly drop and reclaim the old VAE's VRAM. Without this, the
    # caching allocator hangs onto it until next allocator-reclaim.
    del prev_vae
    _cuda_cleanup()
    logger.info("Swapped diffusers VAE → native '%s'", vae_variant)


def _apply_vae_swap_native(model, vae_variant: str, dtype, device: str) -> None:
    """Replace model.pretransform with a swapped autoencoder for a native SA."""
    vae_dir = _resolve_vae_dir(vae_variant)

    _require_stable_audio_tools("Stable Audio VAE swap")
    from stable_audio_tools.models.factory import create_model_from_config
    from stable_audio_tools.models.utils import load_ckpt_state_dict

    vae_cfg = _vae_model_config(vae_variant, vae_dir)
    weights_path = _find_vae_weights(vae_variant, vae_dir)
    if weights_path is None:
        raise FileNotFoundError(f"No weights file in VAE dir {vae_dir}")

    vae_model = create_model_from_config(vae_cfg)
    sd = load_ckpt_state_dict(str(weights_path))
    _load_result = vae_model.load_state_dict(sd, strict=False)
    _warn_load_mismatch(_load_result, f"native VAE swap '{vae_variant}'")
    vae_model = vae_model.to(device).to(dtype).eval()
    pretransform = getattr(model, "pretransform", None)
    if pretransform is None:
        raise ValueError("Loaded native Stable Audio model has no pretransform to swap")
    # Native diffusion models wrap their autoencoder in AutoencoderPretransform
    # (which owns scaling/chunking behavior). Replace only the wrapped model so
    # those semantics remain intact. Fall back to replacing the pretransform
    # itself for custom native models that expose a raw autoencoder.
    if hasattr(pretransform, "model"):
        prev_vae = pretransform.model
        pretransform.model = vae_model
        del prev_vae
    else:
        model.pretransform = vae_model
        del pretransform
    _cuda_cleanup()
    logger.info("Swapped native pretransform → '%s'", vae_variant)


class _NativeVAEShim:
    """Adapter so a stable-audio-tools VAE quacks like a diffusers VAE.

    StableAudioPipeline expects (at minimum):
      * ``.encode(wav).latent_dist.sample()`` returns the latent tensor
      * ``.decode(latent).sample`` returns the audio tensor
      * ``.config.sampling_rate`` and other ``.config`` lookups

    The shim explicitly handles encode/decode/to/eval/parameters/device and
    forwards any other attribute access to the wrapped native model via
    ``__getattr__``. This catches diffusers code that touches `dtype`,
    `scaling_factor`, sub-modules like `encoder`, etc. — which we can't
    enumerate here without running against the actual pipeline.

    Caveat: `.config` returns the *original* diffusers config (sampling_rate
    etc. unchanged). If the swapped VAE has a different SR than the model
    expects, generation will resample incorrectly. Document this in the UI.
    """
    def __init__(self, native_model, prev_config) -> None:
        # Bypass our __getattr__ for these two — set via __dict__ directly so
        # __getattr__ doesn't see them as missing and recurse.
        object.__setattr__(self, "_m", native_model)
        object.__setattr__(self, "config", prev_config)

    def encode(self, x):
        import torch
        with torch.no_grad():
            z = self._m.encode(x) if hasattr(self._m, "encode") else self._m(x)
        # If the native encoder already returns an object that quacks like a
        # diffusers EncodeOutput, just pass it through. Otherwise wrap.
        if hasattr(z, "latent_dist"):
            return z
        return _LatentDistShim(z)

    def decode(self, z):
        import torch
        with torch.no_grad():
            x = self._m.decode(z) if hasattr(self._m, "decode") else self._m(z)
        if hasattr(x, "sample"):
            return x
        return _DecodeShim(x)

    def to(self, *a, **kw):
        self._m.to(*a, **kw)
        return self

    def eval(self):
        self._m.eval()
        return self

    def train(self, mode: bool = True):
        self._m.train(mode)
        return self

    def parameters(self, recurse: bool = True):
        return self._m.parameters(recurse=recurse)

    def buffers(self, recurse: bool = True):
        return self._m.buffers(recurse=recurse)

    def state_dict(self, *a, **kw):
        return self._m.state_dict(*a, **kw)

    def __call__(self, *a, **kw):
        return self._m(*a, **kw)

    @property
    def device(self):
        try:
            return next(self._m.parameters()).device
        except StopIteration:
            import torch
            return torch.device("cpu")

    @property
    def dtype(self):
        try:
            return next(self._m.parameters()).dtype
        except StopIteration:
            import torch
            return torch.float32

    @property
    def hop_length(self):
        """Diffusers name for the native autoencoder's downsampling ratio."""
        ratio = getattr(self._m, "downsampling_ratio", None)
        if ratio is not None:
            return int(ratio)
        configured = getattr(self.config, "hop_length", None)
        if configured is not None:
            return int(configured)
        raise AttributeError("hop_length")

    # Names we keep on the shim itself; everything else proxies.
    _SHIM_ATTRS = frozenset({"_m", "config"})

    def __getattr__(self, name):
        # __getattr__ only fires when normal attribute lookup fails. Forward
        # to the wrapped model so diffusers internals (`.encoder`, `.decoder`,
        # `.scaling_factor`, custom configs, etc.) keep working.
        m = self.__dict__.get("_m")
        if m is None:
            raise AttributeError(name)
        return getattr(m, name)

    def __setattr__(self, name, value):
        # Keep our two own attributes on the shim. Diffusers occasionally
        # mutates pipe.vae attributes (e.g., dtype after .to()); send those
        # to the wrapped model so they actually take effect.
        if name in self._SHIM_ATTRS:
            object.__setattr__(self, name, value)
            return
        m = self.__dict__.get("_m")
        if m is None:
            object.__setattr__(self, name, value)
        else:
            setattr(m, name, value)


class _LatentDistShim:
    def __init__(self, z) -> None:
        self._z = z

    def sample(self, *_args, **_kwargs):
        return self._z

    def mode(self):
        return self._z

    @property
    def latent_dist(self):
        """Allow the shim to serve as both EncodeOutput and its distribution."""
        return self

    @property
    def mean(self):
        return self._z


class _DecodeShim:
    def __init__(self, x) -> None:
        self.sample = x


# ---------------------------------------------------------------------------
# CLAP loading
# ---------------------------------------------------------------------------
def load_clap(state: _AudioLabState, variant_id: str) -> dict:
    if variant_id.startswith("custom:"):
        weights_dir = _custom_asset_dir("clap", variant_id.split(":", 1)[1])
    else:
        info = get_clap_model(variant_id)
        if not info:
            raise ValueError(f"Unknown CLAP variant: {variant_id}")
        if not is_clap_installed(variant_id):
            raise FileNotFoundError(
                f"CLAP variant {variant_id} not installed. Install it via the UI "
                f"or `omni-cli audio-lab install-clap {variant_id}`."
            )
        weights_dir = clap_weights_path(variant_id)

    import torch
    from transformers import ClapModel, ClapProcessor

    with state.lock:
        _free_clap(state)
        dtype = torch.float32
        logger.info("Loading CLAP %s on %s (%s)", variant_id, state.device, dtype)
        # AUD-5: remote-code execution is opt-in (default OFF) for user-installable repos.
        trc = _trust_remote_code()
        # ClapProcessor is a feature_extractor + tokenizer wrapper; safe on CPU.
        processor = ClapProcessor.from_pretrained(str(weights_dir), trust_remote_code=trc)
        model = ClapModel.from_pretrained(str(weights_dir), trust_remote_code=trc)
        model = model.to(state.device).eval()
        state.clap_model = model
        state.clap_processor = processor
        state.clap_variant = variant_id
        logger.info("CLAP loaded")
    return {"loaded": True, "variant_id": variant_id}


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------
def _free_sa(state: _AudioLabState) -> None:
    if state.sa_pipe is None:
        return
    try:
        del state.sa_pipe
    except (AttributeError, NameError):
        pass
    state.sa_pipe = None
    state.sa_variant = None
    state.sa_format = None
    state.sa_objective = None
    state.vae_swap = None
    state.sa_max_duration_s = 120.0
    _cuda_cleanup()


def _free_clap(state: _AudioLabState) -> None:
    if state.clap_model is None:
        return
    try:
        del state.clap_model
        del state.clap_processor
    except (AttributeError, NameError):
        pass
    state.clap_model = None
    state.clap_processor = None
    state.clap_variant = None
    _cuda_cleanup()


def _cuda_cleanup() -> None:
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.empty_cache()
    except Exception:
        pass


def unload(state: _AudioLabState, component: str = "all") -> dict:
    """component ∈ {sa, clap, vae, all}."""
    with state.lock:
        if component in ("sa", "all"):
            _free_sa(state)
        if component in ("clap", "all"):
            _free_clap(state)
        if component == "vae":
            logger.warning(
                "VAE reset requires reloading the active Stable Audio model; "
                "use the gateway /api/audio_lab/unload endpoint."
            )
    return current_state(state)


def current_state(state: _AudioLabState) -> dict:
    return {
        "device": state.device,
        "sa": (
            None if state.sa_variant is None
            else {"variant_id": state.sa_variant, "format": state.sa_format,
                  "vae_swap": state.vae_swap, "sample_rate": state.sa_sample_rate,
                  "max_duration_s": state.sa_max_duration_s,
                  "diffusion_objective": state.sa_objective,
                  "valid_samplers": list(_samplers_for_objective(state.sa_objective))}
        ),
        "clap": None if state.clap_variant is None else {"variant_id": state.clap_variant},
        "lora_stack": list(state.sa_lora_stack),
        "cancelled": state.cancel_event.is_set(),
    }


# ---------------------------------------------------------------------------
# Text-to-audio inference
# ---------------------------------------------------------------------------
def _samplers_for_objective(objective: str | None) -> tuple[str, ...]:
    """Return samplers accepted by stable-audio-tools for one objective."""
    if objective in ("rectified_flow", "rf_denoiser"):
        return AUDIO_LAB_RF_SAMPLERS
    if objective == "v":
        return AUDIO_LAB_V_SAMPLERS
    return AUDIO_LAB_V_SAMPLERS + AUDIO_LAB_RF_SAMPLERS


def _validate_native_sampler(state: _AudioLabState, sampler: str | None) -> None:
    if not sampler:
        return
    valid = _samplers_for_objective(state.sa_objective)
    if sampler not in valid:
        choices = ", ".join(valid)
        raise ValueError(
            f"Sampler '{sampler}' is incompatible with diffusion objective "
            f"'{state.sa_objective or 'unknown'}'. Choose one of: {choices}"
        )


def _resolve_seed(seed):
    import torch
    if seed is None:
        seed = int(torch.empty(1, dtype=torch.int64).random_().item())
    return int(seed) & 0x7FFFFFFF


def _validate_duration(state: _AudioLabState, duration_s: float) -> float:
    if duration_s <= 0:
        raise ValueError("duration_s must be greater than 0")
    max_duration = float(state.sa_max_duration_s or 120.0)
    if duration_s > max_duration:
        raise ValueError(
            f"duration_s {duration_s:g}s exceeds the loaded model limit "
            f"of {max_duration:g}s"
        )
    return duration_s


def _native_conditioning(prompt: str, negative_prompt: str | None,
                         duration_s: float, batch_size: int = 1
                         ) -> tuple[list[dict], list[dict] | None]:
    """Build positive and negative conditioning batches for Stable Audio.

    ``generate_diffusion_cond`` accepts negative conditioning in its dedicated
    ``negative_conditioning`` argument. Appending it to the positive list
    doubles the conditioner batch and then classifier-free guidance doubles it
    again, producing a 4-vs-2 tensor mismatch for a one-waveform request.
    """
    size = max(1, int(batch_size))

    def batch(text: str) -> list[dict]:
        return [
            {
                "prompt": text,
                "seconds_start": 0,
                "seconds_total": duration_s,
            }
            for _ in range(size)
        ]

    positive = batch(prompt or "")
    negative = batch(str(negative_prompt)) if negative_prompt else None
    return positive, negative


def _native_run(state: _AudioLabState, *, prompt: str | None,
                negative_prompt: str | None,
                duration_s: float, steps: int, cfg_scale: float,
                sampler: str | None, sigma_min: float | None,
                sigma_max: float | None, seed: int,
                batch_size: int = 1,
                init_audio=None,
                init_noise_level: float | None = None,
                mask_args: dict | None = None):
    """Single call into stable_audio_tools.generate_diffusion_cond.
    Returns the (channels, samples) waveform as a CPU float32 tensor.

    Caller holds state.lock — this is invoked under it.
    """
    import torch
    from stable_audio_tools.inference.generation import generate_diffusion_cond

    _validate_native_sampler(state, sampler)
    model, model_config = state.sa_pipe
    sr = state.sa_sample_rate
    duration_s = _validate_duration(state, duration_s)
    sample_size = int(sr * duration_s)

    conditioning, negative_conditioning = _native_conditioning(
        prompt or "", negative_prompt, duration_s, batch_size
    )
    kwargs = {
        "model": model,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "conditioning": conditioning,
        "sample_size": sample_size,
        "sample_rate": sr,
        "seed": seed,
        "device": state.device,
        "batch_size": batch_size,
    }
    if negative_conditioning is not None:
        kwargs["negative_conditioning"] = negative_conditioning
    if sampler:
        kwargs["sampler_type"] = sampler
    if sigma_min is not None:
        kwargs["sigma_min"] = sigma_min
    if sigma_max is not None:
        kwargs["sigma_max"] = sigma_max
    if init_audio is not None:
        kwargs["init_audio"] = init_audio
    if init_noise_level is not None:
        kwargs["init_noise_level"] = init_noise_level
    if mask_args is not None:
        kwargs["mask_args"] = mask_args

    audio = generate_diffusion_cond(**kwargs)
    if audio is None:
        raise RuntimeError(
            "stable-audio-tools returned no waveform; verify the loaded model's "
            "diffusion objective and sampler compatibility"
        )
    # Returned shape: (batch, channels, samples) — take first.
    if isinstance(audio, torch.Tensor):
        audio = audio.detach().cpu().to(torch.float32)
        if audio.dim() == 3:
            if batch_size == 1:
                return audio[0]
            return [audio[i] for i in range(audio.shape[0])]
    return audio


def _validate_diffusers_controls(state, params, *, a2a=False):
    if state.sa_format == "native":
        return
    if any(params.get(key) is not None for key in ("sampler", "sigma_min", "sigma_max")):
        raise ValueError("sampler and sigma controls require a native Stable Audio model")
    if a2a and params.get("init_noise_level") is not None:
        raise ValueError("init_noise_level requires a native Stable Audio model; omit it for diffusers A2A")


def infer_audio_gen(state: _AudioLabState, params: dict) -> dict:
    """Text-to-audio with diffusers or native dispatch based on state.sa_format."""
    _validate_diffusers_controls(state, params)
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    negative_prompt = params.get("negative_prompt")
    duration_s = float(params.get("duration_s") or 10.0)
    steps = int(params.get("steps") or 100)
    cfg_scale = float(params.get("cfg_scale") if params.get("cfg_scale") is not None else 7.0)
    sampler = params.get("sampler")
    sigma_min = params.get("sigma_min")
    sigma_max = params.get("sigma_max")
    seed = _resolve_seed(params.get("seed"))
    num_waveforms = int(params.get("num_waveforms_per_prompt") or 1)
    num_waveforms = max(1, min(16, num_waveforms))

    with state.lock:
        if state.sa_pipe is None:
            raise RuntimeError("No Stable Audio model loaded — call /audio_lab/load_sa first")
        duration_s = _validate_duration(state, duration_s)
        if state.sa_format == "native":
            waveforms = _native_run(state, prompt=prompt, negative_prompt=negative_prompt,
                                     duration_s=duration_s, steps=steps,
                                     cfg_scale=cfg_scale, sampler=sampler,
                                     sigma_min=sigma_min, sigma_max=sigma_max,
                                     seed=seed, batch_size=num_waveforms)
            if not isinstance(waveforms, list):
                waveforms = [waveforms]
        else:
            import torch
            generator = torch.Generator(device=state.device).manual_seed(seed)
            result = state.sa_pipe(
                prompt=prompt,
                negative_prompt=negative_prompt,
                audio_end_in_s=duration_s,
                num_inference_steps=steps,
                guidance_scale=cfg_scale,
                num_waveforms_per_prompt=num_waveforms,
                generator=generator,
                output_type="pt",
            )
            audios = result.audios
            if isinstance(audios, torch.Tensor):
                if audios.dim() == 2:
                    waveforms = [audios]
                else:
                    waveforms = [audios[i] for i in range(audios.shape[0])]
            else:
                waveforms = list(audios)

    encoded = []
    for idx, waveform in enumerate(waveforms[:num_waveforms], start=1):
        audio_bytes, dur = _waveform_to_wav_bytes(waveform, state.sa_sample_rate)
        encoded.append({
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "sample_rate": state.sa_sample_rate,
            "duration_s": dur,
            "seed": seed,
            "waveform_index": idx,
        })
    if not encoded:
        raise RuntimeError("Stable Audio returned no waveforms")
    if num_waveforms == 1:
        return encoded[0]
    return {
        "audios": encoded,
        "sample_rate": state.sa_sample_rate,
        "duration_s": encoded[0]["duration_s"] if encoded else duration_s,
        "seed": seed,
        "num_waveforms_per_prompt": len(encoded),
    }


def _waveform_to_wav_bytes(waveform, sample_rate: int) -> tuple[bytes, float]:
    """Encode a torch tensor (channels, samples) to in-memory 16-bit PCM WAV."""
    import torch
    import soundfile as sf
    # Convert to CPU float32 and clamp to [-1, 1].
    if isinstance(waveform, torch.Tensor):
        wav = waveform.detach().cpu().to(torch.float32).clamp(-1.0, 1.0).numpy()
    else:
        import numpy as np
        wav = np.asarray(waveform, dtype="float32").clip(-1.0, 1.0)
    # soundfile expects (samples, channels) for stereo or (samples,) for mono.
    if wav.ndim == 2:
        wav = wav.T
    buf = io.BytesIO()
    sf.write(buf, wav, sample_rate, format="WAV", subtype="PCM_16")
    duration_s = float(wav.shape[0]) / float(sample_rate)
    return buf.getvalue(), duration_s


# ---------------------------------------------------------------------------
# CLAP scoring
# ---------------------------------------------------------------------------
def _decode_audio_base64(audio_base64: str, target_channels: int | None = None
                          ) -> tuple[Any, int]:
    """base64 → (numpy float32 (samples,channels) or (samples,), sample_rate)."""
    import numpy as np
    import soundfile as sf
    try:
        raw = base64.b64decode(audio_base64, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid base64 audio: {e}") from e
    with sf.SoundFile(io.BytesIO(raw)) as source:
        if not (8000 <= source.samplerate <= 192000) or not (1 <= source.channels <= 8):
            raise ValueError("Audio sample rate or channel count exceeds supported bounds")
        if source.frames <= 0 or source.frames / source.samplerate > 600 or source.frames * source.channels > 32_000_000:
            raise ValueError("Decoded audio exceeds the 600-second / 32-million-sample limit")
        sr = source.samplerate
        data = source.read(dtype="float32", always_2d=False)
    if target_channels == 1 and data.ndim == 2:
        data = data.mean(axis=1)
    if target_channels == 2 and data.ndim == 1:
        data = np.stack([data, data], axis=1)
    return data, int(sr)


def _waveform_to_pipe_tensor(data, sample_rate: int, target_sr: int, device: str):
    """Resample + reshape to (1, channels, samples) on `device`. Returns the tensor."""
    import numpy as np
    import torch
    if sample_rate != target_sr:
        try:
            import torchaudio.functional as F
            arr = torch.from_numpy(np.asarray(data, dtype="float32"))
            if arr.ndim == 1:
                arr = arr.unsqueeze(0)             # → (1, samples)
            else:
                arr = arr.T                         # (samples, ch) → (ch, samples)
            arr = F.resample(arr, sample_rate, target_sr)
        except (ImportError, RuntimeError):
            # Fallback: librosa
            import librosa
            mono = data if data.ndim == 1 else data.mean(axis=1)
            arr = librosa.resample(np.asarray(mono, dtype="float32"),
                                    orig_sr=sample_rate, target_sr=target_sr)
            arr = torch.from_numpy(arr).unsqueeze(0)
    else:
        arr = torch.from_numpy(np.asarray(data, dtype="float32"))
        if arr.ndim == 1:
            arr = arr.unsqueeze(0)
        else:
            arr = arr.T
    if arr.size(0) == 1:
        # mono → stereo if the pipe wants stereo. We don't know what the pipe
        # wants here, so default to passing through as-is; the pipe broadcasts.
        pass
    return arr.unsqueeze(0).to(device)  # (1, channels, samples)


def _component_dtype(component):
    dtype = getattr(component, "dtype", None)
    if dtype is not None:
        return dtype
    try:
        return next(component.parameters()).dtype
    except (AttributeError, StopIteration):
        return None


def _component_input_tensor(component, tensor, device: str):
    """Cast a tensor to the dtype/device expected by a torch component."""
    import torch

    return tensor.to(
        device=device,
        dtype=_component_dtype(component) or torch.float32,
    )


def _diffusers_audio_tensor(pipe, tensor, device: str):
    """Move input audio to the dtype used by the diffusers VAE.

    StableAudioPipeline does not cast ``initial_audio_waveforms`` before its
    first VAE convolution.  Loading the pipeline in fp16 while passing the
    float32 tensor produced by soundfile therefore fails with a bias/input
    dtype mismatch.  Prefer the VAE dtype and fall back to the transformer or
    float32 for unusual/custom pipelines.
    """
    import torch

    for name in ("vae", "transformer"):
        component = getattr(pipe, name, None)
        if component is None:
            continue
        dtype = _component_dtype(component)
        if dtype is not None:
            return tensor.to(device=device, dtype=dtype)
    return tensor.to(device=device, dtype=torch.float32)


# ---------------------------------------------------------------------------
# Audio-to-audio (A2A) — diffusers path uses initial_audio_waveforms
# ---------------------------------------------------------------------------
def infer_audio_a2a(state: _AudioLabState, params: dict) -> dict:
    """Audio-to-audio variation. Diffusers and native dispatch."""
    _validate_diffusers_controls(state, params, a2a=True)
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    init_b64 = params.get("init_audio_base64") or ""
    if not init_b64:
        raise ValueError("init_audio_base64 is required for A2A")
    init_noise = float(params.get("init_noise_level") if params.get("init_noise_level") is not None else 0.7)
    if not (0.0 <= init_noise <= 1.0):
        raise ValueError("init_noise_level must be in [0.0, 1.0]")
    duration_s = float(params.get("duration_s") or 10.0)
    steps = int(params.get("steps") or 100)
    cfg_scale = float(params.get("cfg_scale") if params.get("cfg_scale") is not None else 7.0)
    sampler = params.get("sampler")
    sigma_min = params.get("sigma_min")
    sigma_max = params.get("sigma_max")
    seed = _resolve_seed(params.get("seed"))
    negative_prompt = params.get("negative_prompt")

    with state.lock:
        if state.sa_pipe is None:
            raise RuntimeError("No Stable Audio model loaded")
        duration_s = _validate_duration(state, duration_s)

        data, in_sr = _decode_audio_base64(init_b64)
        # M1: honor the client-supplied init_sample_rate override (useful when
        # the wav header is wrong / re-encoded / from a non-standard source).
        override_sr = params.get("init_sample_rate")
        if override_sr:
            in_sr = int(override_sr)
        target_sr = state.sa_sample_rate
        init_tensor = _waveform_to_pipe_tensor(data, in_sr, target_sr, state.device)

        if state.sa_format == "native":
            # generate_diffusion_cond takes init_audio as (sample_rate, tensor)
            # at shape (channels, samples). init_tensor here is (1, channels, samples).
            waveform = _native_run(
                state, prompt=prompt, negative_prompt=negative_prompt,
                duration_s=duration_s, steps=steps, cfg_scale=cfg_scale,
                sampler=sampler, sigma_min=sigma_min, sigma_max=sigma_max,
                seed=seed,
                init_audio=(target_sr, init_tensor.squeeze(0)),
                init_noise_level=init_noise,
            )
        else:
            import torch
            generator = torch.Generator(device=state.device).manual_seed(seed)
            init_tensor = _diffusers_audio_tensor(
                state.sa_pipe, init_tensor, state.device,
            )
            result = state.sa_pipe(
                prompt=prompt,
                negative_prompt=negative_prompt,
                initial_audio_waveforms=init_tensor,
                initial_audio_sampling_rate=target_sr,
                audio_end_in_s=duration_s,
                num_inference_steps=steps,
                guidance_scale=cfg_scale,
                num_waveforms_per_prompt=1,
                generator=generator,
                output_type="pt",
            )
            waveform = result.audios[0]

    audio_bytes, dur = _waveform_to_wav_bytes(waveform, state.sa_sample_rate)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": state.sa_sample_rate,
        "duration_s": dur,
        "seed": seed,
        "init_noise_level": init_noise,
    }


# ---------------------------------------------------------------------------
# Inpainting — diffusers fallback splices a regenerated region into the init
# ---------------------------------------------------------------------------
def infer_audio_inpaint(state: _AudioLabState, params: dict) -> dict:
    """Mask-region inpainting. Native variants use proper latent-space inpaint
    via stable_audio_tools mask_args; diffusers variants fall back to a
    crossfade-splice approach (StableAudioPipeline doesn't expose latent masking)."""
    _validate_diffusers_controls(state, params)

    # Native path: use generate_diffusion_cond's mask_args.
    if state.sa_format == "native":
        return _native_inpaint(state, params)
    # Diffusers fallback: splice.
    return _splice_inpaint(state, params)


def _native_inpaint(state: _AudioLabState, params: dict) -> dict:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    init_b64 = params.get("init_audio_base64") or ""
    if not init_b64:
        raise ValueError("init_audio_base64 is required for inpaint")
    mask_start = float(params.get("mask_start_s") or 0.0)
    mask_end = float(params.get("mask_end_s") or 0.0)
    if mask_end <= mask_start:
        raise ValueError("mask_end_s must be greater than mask_start_s")
    steps = int(params.get("steps") or 100)
    cfg_scale = float(params.get("cfg_scale") if params.get("cfg_scale") is not None else 7.0)
    sampler = params.get("sampler")
    sigma_min = params.get("sigma_min")
    sigma_max = params.get("sigma_max")
    seed = _resolve_seed(params.get("seed"))
    negative_prompt = params.get("negative_prompt")

    with state.lock:
        if state.sa_pipe is None:
            raise RuntimeError("No Stable Audio model loaded")
        data, in_sr = _decode_audio_base64(init_b64)
        override_sr = params.get("init_sample_rate")
        if override_sr:
            in_sr = int(override_sr)
        target_sr = state.sa_sample_rate
        init_tensor = _waveform_to_pipe_tensor(data, in_sr, target_sr, state.device)
        duration_s = float(params.get("duration_s") or
                            (init_tensor.shape[-1] / target_sr))
        duration_s = _validate_duration(state, duration_s)

        mask_args = {
            "maskstart": mask_start,
            "maskend":   mask_end,
            "softnessL": 0.04,
            "softnessR": 0.04,
            "marination": 0.0,
        }
        waveform = _native_run(
            state, prompt=prompt, negative_prompt=negative_prompt,
            duration_s=duration_s, steps=steps, cfg_scale=cfg_scale,
            sampler=sampler, sigma_min=sigma_min, sigma_max=sigma_max,
            seed=seed,
            init_audio=(target_sr, init_tensor.squeeze(0)),
            mask_args=mask_args,
        )

    audio_bytes, dur = _waveform_to_wav_bytes(waveform, state.sa_sample_rate)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": state.sa_sample_rate,
        "duration_s": dur,
        "seed": seed,
        "mask_start_s": mask_start,
        "mask_end_s": mask_end,
        "method": "native",
    }


def _splice_inpaint(state: _AudioLabState, params: dict) -> dict:
    """Diffusers fallback: splice mode for inpaint (regen + crossfade)."""
    import numpy as np
    import torch

    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    init_b64 = params.get("init_audio_base64") or ""
    if not init_b64:
        raise ValueError("init_audio_base64 is required for inpaint")
    mask_start = float(params.get("mask_start_s") or 0.0)
    mask_end = float(params.get("mask_end_s") or 0.0)
    if mask_end <= mask_start:
        raise ValueError("mask_end_s must be greater than mask_start_s")
    steps = int(params.get("steps") or 100)
    cfg_scale = float(params.get("cfg_scale") if params.get("cfg_scale") is not None else 7.0)
    seed = params.get("seed")
    if seed is None:
        seed = int(torch.empty(1, dtype=torch.int64).random_().item())
    seed = int(seed) & 0x7FFFFFFF
    negative_prompt = params.get("negative_prompt")

    with state.lock:
        if state.sa_pipe is None:
            raise RuntimeError("No Stable Audio model loaded")

        init_data, in_sr = _decode_audio_base64(init_b64)
        override_sr = params.get("init_sample_rate")
        if override_sr:
            in_sr = int(override_sr)
        target_sr = state.sa_sample_rate

        # Resample init to model sr; keep stereo if present.
        init_tensor = _waveform_to_pipe_tensor(init_data, in_sr, target_sr, "cpu")
        init_np = init_tensor.squeeze(0).cpu().numpy()  # (channels, samples) or (1, samples)
        n_samples = init_np.shape[-1]
        duration_s = float(params.get("duration_s") or (n_samples / target_sr))
        duration_s = _validate_duration(state, duration_s)

        # Generate a fresh full-length waveform with the prompt.
        generator = torch.Generator(device=state.device).manual_seed(seed)
        result = state.sa_pipe(
            prompt=prompt,
            negative_prompt=negative_prompt,
            initial_audio_waveforms=_diffusers_audio_tensor(
                state.sa_pipe, init_tensor, state.device,
            ),
            initial_audio_sampling_rate=target_sr,
            audio_end_in_s=duration_s,
            num_inference_steps=steps,
            guidance_scale=cfg_scale,
            num_waveforms_per_prompt=1,
            generator=generator,
            output_type="pt",
        )
        gen_np = result.audios[0].cpu().to(torch.float32).numpy()
        if init_np.ndim == 2 and gen_np.ndim == 1:
            gen_np = np.tile(gen_np[None, :], (init_np.shape[0], 1))
        elif init_np.ndim == 1 and gen_np.ndim == 2:
            gen_np = gen_np.mean(axis=0)
        elif init_np.ndim == 2 and gen_np.ndim == 2 and gen_np.shape[0] != init_np.shape[0]:
            if gen_np.shape[0] == 1:
                gen_np = np.tile(gen_np, (init_np.shape[0], 1))
            else:
                gen_np = np.tile(gen_np[:1], (init_np.shape[0], 1))
        if gen_np.shape[-1] != n_samples:
            # Resize generated to match init length (centered crop / right-pad).
            if gen_np.shape[-1] > n_samples:
                gen_np = gen_np[..., :n_samples]
            else:
                pad = n_samples - gen_np.shape[-1]
                gen_np = np.pad(gen_np, ((0, 0), (0, pad)) if gen_np.ndim == 2 else (0, pad))

    # Splice with short crossfades at the mask boundaries to avoid clicks.
    crossfade_s = 0.04   # 40 ms
    xf_samples = int(crossfade_s * target_sr)
    start = max(0, int(mask_start * target_sr))
    end = min(n_samples, int(mask_end * target_sr))
    if end <= start:
        raise ValueError("mask region is zero-length after sample conversion")

    # Initialize output with the source, then overwrite the mask region.
    out = np.copy(init_np)
    src_region_start = max(0, start - xf_samples)
    src_region_end = min(n_samples, end + xf_samples)

    def _linspace(a, b, n):
        if n <= 0:
            return np.empty((0,), dtype="float32")
        return np.linspace(a, b, n, dtype="float32")

    # Pre-mask crossfade: ramp out source, ramp in gen
    pre_xf = min(xf_samples, start - src_region_start, end - start)
    if pre_xf > 0:
        a = _linspace(1.0, 0.0, pre_xf)
        b = 1.0 - a
        if out.ndim == 2:
            for c in range(out.shape[0]):
                out[c, start - pre_xf:start] = (
                    out[c, start - pre_xf:start] * a + gen_np[c, start - pre_xf:start] * b
                )
        else:
            out[start - pre_xf:start] = (
                out[start - pre_xf:start] * a + gen_np[start - pre_xf:start] * b
            )
    # Body
    if out.ndim == 2:
        out[:, start:end] = gen_np[:, start:end]
    else:
        out[start:end] = gen_np[start:end]
    # Post-mask crossfade: ramp out gen, ramp in source
    post_xf = min(xf_samples, src_region_end - end, end - start)
    if post_xf > 0:
        a = _linspace(0.0, 1.0, post_xf)
        b = 1.0 - a
        if out.ndim == 2:
            for c in range(out.shape[0]):
                out[c, end:end + post_xf] = out[c, end:end + post_xf] * a + gen_np[c, end:end + post_xf] * b
        else:
            out[end:end + post_xf] = out[end:end + post_xf] * a + gen_np[end:end + post_xf] * b

    audio_bytes, dur = _waveform_to_wav_bytes(out, target_sr)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": target_sr,
        "duration_s": dur,
        "seed": seed,
        "mask_start_s": mask_start,
        "mask_end_s": mask_end,
        "method": "splice",
    }


# ---------------------------------------------------------------------------
# Unconditional generation — diffusers path: empty prompt with full denoise
# ---------------------------------------------------------------------------
def infer_audio_uncond(state: _AudioLabState, params: dict) -> dict:
    """No-prompt generation. Both diffusers and native paths supported."""
    _validate_diffusers_controls(state, params)
    duration_s = float(params.get("duration_s") or 10.0)
    steps = int(params.get("steps") or 100)
    sampler = params.get("sampler")
    sigma_min = params.get("sigma_min")
    sigma_max = params.get("sigma_max")
    seed = _resolve_seed(params.get("seed"))

    with state.lock:
        if state.sa_pipe is None:
            raise RuntimeError("No Stable Audio model loaded")
        duration_s = _validate_duration(state, duration_s)
        if state.sa_format == "native":
            waveform = _native_run(
                state, prompt="", negative_prompt=None,
                duration_s=duration_s, steps=steps, cfg_scale=1.0,
                sampler=sampler, sigma_min=sigma_min, sigma_max=sigma_max,
                seed=seed,
            )
        else:
            import torch
            generator = torch.Generator(device=state.device).manual_seed(seed)
            result = state.sa_pipe(
                prompt="",
                audio_end_in_s=duration_s,
                num_inference_steps=steps,
                guidance_scale=1.0,
                num_waveforms_per_prompt=1,
                generator=generator,
                output_type="pt",
            )
            waveform = result.audios[0]
    audio_bytes, dur = _waveform_to_wav_bytes(waveform, state.sa_sample_rate)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": state.sa_sample_rate,
        "duration_s": dur,
        "seed": seed,
    }


# ---------------------------------------------------------------------------
# VAE only
# ---------------------------------------------------------------------------
def _get_pipe_vae(state: _AudioLabState):
    """Return an object with .encode(x).latent_dist.sample() and .decode(z).sample.
    Works for both diffusers (pipe.vae) and native (model.pretransform via shim)."""
    if state.sa_pipe is None:
        raise RuntimeError("No Stable Audio model loaded")
    if state.sa_format == "native":
        model, _cfg = state.sa_pipe
        pretransform = getattr(model, "pretransform", None)
        if pretransform is None:
            raise RuntimeError("Native model has no pretransform (VAE) attribute")
        return _NativeVAEShim(pretransform, None)
    vae = getattr(state.sa_pipe, "vae", None)
    if vae is None:
        raise RuntimeError("Loaded model has no VAE attribute")
    return vae


def vae_encode(state: _AudioLabState, audio_base64: str,
                sample_rate: int | None = None) -> dict:
    """Encode a wav to a latent tensor. Returns base64-encoded .pt bytes
    plus the shape so the caller can verify on decode."""
    import torch
    with state.lock:
        vae = _get_pipe_vae(state)
        data, in_sr = _decode_audio_base64(audio_base64)
        if sample_rate:
            in_sr = sample_rate
        target_sr = state.sa_sample_rate
        wav = _waveform_to_pipe_tensor(data, in_sr, target_sr, state.device)
        wav = _component_input_tensor(vae, wav, state.device)
        with torch.no_grad():
            enc = vae.encode(wav)
            latents = enc.latent_dist.sample() if hasattr(enc, "latent_dist") else enc
    buf = io.BytesIO()
    torch.save(latents.cpu(), buf)
    return {
        "latent_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        "shape": list(latents.shape),
        "sample_rate": target_sr,
    }


def vae_decode(state: _AudioLabState, latent_base64: str,
                shape: list | None = None) -> dict:
    """Decode latent bytes back to a wav."""
    import torch
    try:
        raw = base64.b64decode(latent_base64, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid base64 latent: {e}") from e
    # weights_only=True forces tensor-only deserialization (no pickle code
    # execution). torch>=1.13 supports this; we pin torch>=2.6 so it's safe.
    try:
        latents = torch.load(io.BytesIO(raw), map_location="cpu", weights_only=True)
    except Exception as e:
        raise ValueError(f"Invalid latent payload (not a tensor): {e}") from e
    if not isinstance(latents, torch.Tensor):
        raise ValueError("Latent payload must be a torch.Tensor")
    if shape is not None and list(latents.shape) != shape:
        raise ValueError("Latent shape does not match the declared shape")
    if latents.ndim != 3 or latents.shape[0] != 1 or latents.numel() > 8_000_000:
        raise ValueError("Latent dimensions exceed the supported single-output decode limit")
    with state.lock:
        vae = _get_pipe_vae(state)
        latents = _component_input_tensor(vae, latents, state.device)
        with torch.no_grad():
            decoded = vae.decode(latents)
            if hasattr(decoded, "sample"):
                decoded = decoded.sample
    # decoded shape: (batch, channels, samples) — take first.
    waveform = decoded[0]
    audio_bytes, dur = _waveform_to_wav_bytes(waveform, state.sa_sample_rate)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": state.sa_sample_rate,
        "duration_s": dur,
    }


def vae_reconstruct(state: _AudioLabState, audio_base64: str,
                     sample_rate: int | None = None) -> dict:
    """Encode then decode in-process. Returns the round-tripped audio plus
    an RMS diff metric for quick quality eyeballing."""
    import torch
    with state.lock:
        vae = _get_pipe_vae(state)
        data, in_sr = _decode_audio_base64(audio_base64)
        if sample_rate:
            in_sr = sample_rate
        target_sr = state.sa_sample_rate
        wav = _waveform_to_pipe_tensor(data, in_sr, target_sr, state.device)
        wav = _component_input_tensor(vae, wav, state.device)
        with torch.no_grad():
            enc = vae.encode(wav)
            latents = enc.latent_dist.sample() if hasattr(enc, "latent_dist") else enc
            dec = vae.decode(latents)
            decoded = dec.sample if hasattr(dec, "sample") else dec
        recon = decoded[0]
        orig = wav[0]
        n = min(orig.shape[-1], recon.shape[-1])
        diff = (recon[..., :n].float() - orig[..., :n].float())
        rms = float(torch.sqrt((diff * diff).mean()).item())

    audio_bytes, dur = _waveform_to_wav_bytes(recon, state.sa_sample_rate)
    return {
        "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        "sample_rate": state.sa_sample_rate,
        "duration_s": dur,
        "diff_rms": rms,
    }


def infer_audio_score(state: _AudioLabState, text: str,
                      audio_base64: str, sample_rate: int | None = None) -> dict:
    """Cosine similarity between CLAP text and audio embeddings."""
    import numpy as np
    import torch

    text = (text or "").strip()
    if not text:
        raise ValueError("text prompt is required for scoring")

    with state.lock:
        if state.clap_model is None or state.clap_processor is None:
            raise RuntimeError("No CLAP model loaded - call /audio_lab/load_clap first")

        data, sr = _decode_audio_base64(audio_base64)
        if data.ndim == 2:
            data = data.mean(axis=1)
        if sample_rate and sample_rate != sr:
            sr = sample_rate

        target_sr = 48000
        data = np.asarray(data, dtype="float32")
        if sr != target_sr:
            try:
                import torchaudio.functional as F
                arr = torch.from_numpy(data).unsqueeze(0)
                data = F.resample(arr, sr, target_sr).squeeze(0).cpu().numpy()
            except Exception:
                import librosa
                data = librosa.resample(data, orig_sr=sr, target_sr=target_sr)
            sr = target_sr

        max_window_samples = int(target_sr * 10.0)
        if data.shape[0] <= max_window_samples:
            chunks = [data]
        else:
            chunks = [
                data[i:i + max_window_samples]
                for i in range(0, data.shape[0], max_window_samples)
            ]

        scores = []
        for chunk in chunks:
            if state.cancel_event.is_set():
                raise RuntimeError("scoring cancelled")

            model_dtype = next(state.clap_model.parameters()).dtype
            if model_dtype != torch.float32:
                state.clap_model.to(dtype=torch.float32)

            text_inputs = state.clap_processor(text=[text], return_tensors="pt")
            audio_inputs = state.clap_processor(
                audios=np.asarray(chunk, dtype="float32").flatten(),
                sampling_rate=target_sr,
                return_tensors="pt",
                padding=True,
            )
            text_inputs = {
                k: v.to(state.device)
                for k, v in text_inputs.items()
                if torch.is_tensor(v)
            }
            audio_inputs = {
                k: v.to(state.device)
                for k, v in audio_inputs.items()
                if torch.is_tensor(v)
            }
            with torch.no_grad():
                text_embeds = state.clap_model.get_text_features(**text_inputs)
                audio_embeds = state.clap_model.get_audio_features(**audio_inputs)
                cos = torch.nn.functional.cosine_similarity(
                    text_embeds.float(), audio_embeds.float(), dim=-1
                ).mean().item()
            scores.append(float(cos))

        score = float(np.mean(scores)) if scores else 0.0
    return {"score": score, "windows": len(chunks)}
