"""
Omni Worker - A single-model inference server launched as a subprocess.

Usage: python omni_worker.py --model qwen_omni_3b --port 8202 --device cuda:0

Each worker loads ONE omni model and exposes:
  GET  /health  - Status, device, VRAM info
  POST /infer   - Multi-modal inference
  POST /load    - (Re)load the model
  POST /unload  - Unload model from GPU
"""

import argparse
import asyncio
import base64
import collections
import gc
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    VENV_DIR, OVERRIDES_DIR, MODELS_DIR, CACHE_DIR,
    MODEL_OVERRIDE_MAP, setup_environment,
    INFER_MAX_TEXT_CHARS, INFER_MAX_IMAGE_BASE64_CHARS,
    INFER_MAX_IMAGE_BYTES, INFER_MAX_IMAGE_PIXELS,
    INFER_MAX_NEW_TOKENS, INFER_MAX_TEMPERATURE, INFER_MAX_TOP_P,
    AUDIO_LAB_MAX_BASE64_CHARS,
    ACE_STEP_MAX_BASE64_CHARS,
    MINIMAX_MUSIC3_MAX_DURATION_S,
    MINIMAX_MUSIC3_MAX_TEXT_CHARS,
)

setup_environment()

try:
    from resource_limits import raise_nofile_limit
    _nofile = raise_nofile_limit()
except Exception as _nofile_exc:  # pragma: no cover - worker still starts
    _nofile = {"applied": False, "reason": str(_nofile_exc)}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
if _nofile.get("applied"):
    logger.info("Raised RLIMIT_NOFILE to soft=%s hard=%s", _nofile.get("soft"), _nofile.get("hard"))
elif _nofile.get("reason") not in {None, "already-at-or-above-target"}:
    logger.warning("Could not raise RLIMIT_NOFILE: %s", _nofile.get("reason"))


def _parse_major_minor(version: str) -> tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", version)
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2))


def _require_min_version(package: str, version: str, minimum: tuple[int, int]) -> None:
    current = _parse_major_minor(version)
    if current < minimum:
        min_text = ".".join(map(str, minimum))
        raise RuntimeError(
            f"{package}>={min_text} is required (found {version}). "
            f"Re-run server/setup.sh to upgrade the shared venv."
        )


def _inject_venv(model: str) -> None:
    """Inject shared base venv + model override into sys.path."""
    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    base_sp = VENV_DIR / "lib" / py_ver / "site-packages"
    if base_sp.exists() and str(base_sp) not in sys.path:
        sys.path.insert(0, str(base_sp))
        logger.info("Injected base venv: %s", base_sp)

    override_name = MODEL_OVERRIDE_MAP.get(model)
    if override_name:
        override_dir = OVERRIDES_DIR / override_name
        if override_dir.exists() and str(override_dir) not in sys.path:
            sys.path.insert(0, str(override_dir))
            logger.info("Injected override: %s", override_dir)

    # AnyGPT repo clone was removed (multi-GB LFS, stalls on clone).
    # The model uses standard transformers AutoModelForCausalLM directly.


# ---------------------------------------------------------------------------
# Worker state
# ---------------------------------------------------------------------------
_model_name: str = ""
_device: str = "cuda:0"
_precision: str | None = None
_variant_id: str | None = None          # variant selected from OMNI_MODEL_VARIANTS
_variant_weights_dir: str | None = None  # resolved weights_dir from variant registry
_lora_path: str | None = None            # absolute path to LoRA adapter dir
_placement_config: dict = {}
_model_obj = None
_loaded: bool = False
_load_lock = threading.Lock()

# AUD-5 (omni base loaders): the omni architectures (Qwen-Omni, MiniCPM-o,
# AnyGPT, Qwen3-Omni, Nemotron) ship custom modeling code and require
# trust_remote_code=_TRUST_REMOTE_CODE to load at all, so this defaults to True — unlike the
# audio loaders' OMNI_AUDIO_TRUST_REMOTE_CODE, whose models do not need it and
# default OFF. A security-conscious operator who only runs models that load
# from stock transformers can set OMNI_TRUST_REMOTE_CODE=0 to refuse executing
# repository code at load time (this will break models that genuinely need it).
_TRUST_REMOTE_CODE = os.environ.get("OMNI_TRUST_REMOTE_CODE", "1").strip().lower() not in (
    "0", "false", "no", "off", "")


def _apply_lora(model, lora_path: str):
    """Load a PEFT LoRA adapter on top of the base model and merge it in.

    merge_and_unload() fuses the LoRA weights into the base model so there
    is zero inference overhead. The tradeoff is that the worker cannot
    switch LoRAs without reloading the base model -- which matches our
    single-purpose worker lifecycle.
    """
    from peft import PeftModel
    logger.info("Loading LoRA adapter from %s", lora_path)
    model = PeftModel.from_pretrained(model, lora_path)
    model = model.merge_and_unload()
    logger.info("LoRA adapter merged into base model")
    return model


def _resolve_precision(precision: str | None, device: str):
    import torch
    if precision == "fp32":
        return torch.float32
    elif precision == "fp16":
        return torch.float16
    elif precision == "bf16":
        return torch.bfloat16
    return torch.bfloat16 if device.startswith("cuda") else torch.float32


def _get_vram_free_mb(device: str) -> int:
    try:
        import torch
        if device.startswith("cuda") and torch.cuda.is_available():
            idx = int(device.split(":")[1]) if ":" in device else 0
            free, _ = torch.cuda.mem_get_info(idx)
            return int(free / 1024 / 1024)
    except Exception:
        pass
    return 0


def _worker_placement_from_env() -> dict:
    raw = os.environ.get("OMNI_WORKER_PLACEMENT", "").strip()
    if not raw:
        return {}
    if len(raw) > 131072:
        raise RuntimeError("OMNI_WORKER_PLACEMENT is unexpectedly large")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed.get("valid"):
        raise RuntimeError("Worker received an invalid placement plan")
    return parsed


def _worker_offload_dir() -> Path:
    safe_model = re.sub(r"[^A-Za-z0-9_.-]+", "_", _model_name or "model")
    return CACHE_DIR / "offload" / f"{safe_model}-{os.getpid()}"


def _hf_load_kwargs(dtype, device: str) -> dict:
    """Build bounded Hugging Face load arguments from the analyzed plan."""
    kwargs = {"torch_dtype": dtype}
    if not _placement_config:
        kwargs["device_map"] = device
        return kwargs

    mode = str(_placement_config.get("mode") or "single")
    hf_device_map = _placement_config.get("hf_device_map")
    kwargs["device_map"] = hf_device_map or (device if mode == "single" else "auto")
    logical_limits = dict(_placement_config.get("logical_max_memory_mb") or {})
    if logical_limits:
        max_memory = {}
        for logical, value in logical_limits.items():
            key = logical
            if str(logical).startswith("cuda:"):
                key = int(str(logical).split(":", 1)[1])
            max_memory[key] = f"{max(0, int(value))}MiB"
        kwargs["max_memory"] = max_memory
    if mode in ("auto", "manual"):
        kwargs["low_cpu_mem_usage"] = True
    if _placement_config.get("allow_cpu"):
        offload_dir = _worker_offload_dir()
        offload_dir.mkdir(parents=True, exist_ok=True)
        kwargs["offload_folder"] = str(offload_dir)
        kwargs["offload_state_dict"] = True
    return kwargs


def _require_model_vram(required_mb: int, label: str, device: str) -> int:
    if _placement_config:
        limits = dict(_placement_config.get("logical_max_memory_mb") or {})
        total_mb = sum(max(0, int(value)) for value in limits.values())
        if total_mb < required_mb:
            raise RuntimeError(
                f"{label} requires about {required_mb} MB memory, but the analyzed "
                f"placement budget is {total_mb} MB"
            )
        return total_mb
    return _require_cuda_vram(device, required_mb, label)


def _model_input_device(model, fallback: str):
    device_map = getattr(model, "hf_device_map", None)
    if isinstance(device_map, dict):
        preferred = sorted(
            device_map.items(),
            key=lambda item: (
                0 if any(token in str(item[0]).lower() for token in (
                    "embed_tokens", "word_embeddings", "wte", "embedding"
                )) else 1,
                str(item[0]),
            ),
        )
        for _, target in preferred:
            if target in ("cpu", "disk", None):
                continue
            if isinstance(target, int):
                return f"cuda:{target}"
            return str(target)
    return getattr(model, "device", fallback)


def _require_cuda_vram(device: str, required_mb: int, label: str) -> int:
    """Fail before checkpoint deserialization when a CUDA variant cannot fit."""
    if not device.startswith("cuda"):
        return 0
    free_mb = _get_vram_free_mb(device)
    if free_mb < required_mb:
        raise RuntimeError(
            f"{label} requires about {required_mb} MB VRAM, but {device} has "
            f"only {free_mb} MB free; select a fitting quantized variant or CPU"
        )
    return free_mb


def _get_vram_info(device: str) -> tuple[int, int]:
    try:
        import torch
        if device.startswith("cuda") and torch.cuda.is_available():
            idx = int(device.split(":")[1]) if ":" in device else 0
            free, total = torch.cuda.mem_get_info(idx)
            used = total - free
            return int(used / 1024 / 1024), int(total / 1024 / 1024)
    except Exception:
        pass
    return 0, 0


def _gpu_memory_snapshot() -> list[dict]:
    physical_to_logical = dict(_placement_config.get("gpu_device_map") or {})
    if not physical_to_logical and _device.startswith("cuda:"):
        physical_to_logical = {_device: _device}
    uuids = list(_placement_config.get("gpu_uuids") or [])
    snapshot = []
    for index, (physical, logical) in enumerate(physical_to_logical.items()):
        used_mb, total_mb = _get_vram_info(str(logical))
        snapshot.append({
            "physical_device": physical,
            "uuid": uuids[index] if index < len(uuids) else None,
            "logical_device": logical,
            "vram_used_mb": used_mb,
            "vram_total_mb": total_mb,
        })
    return snapshot


# ============================================================
# Model loaders
# ============================================================
def _qwen_gptq_options(model_dir: Path, weights_dirname: str) -> dict | None:
    """Return the model-specific GPTQ load config for Qwen2.5-Omni.

    Optimum's generic block-pattern detector does not recognize the nested
    Qwen2.5-Omni Thinker path. GPTQModel's upstream definition identifies the
    quantized decoder stack as ``thinker.model.layers``.
    """
    if "gptq" not in weights_dirname.lower():
        return None
    config_path = model_dir / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read GPTQ model config: {config_path}") from exc
    options = raw.get("quantization_config")
    if not isinstance(options, dict) or options.get("quant_method") != "gptq":
        raise RuntimeError(f"Invalid GPTQ quantization config: {config_path}")
    options = dict(options)
    options.setdefault("block_name_to_quantize", "thinker.model.layers")
    return options


def _stage_local_transformers_code(
    model_dir: Path,
    module_name: str,
    *,
    modules_cache: Path | None = None,
) -> Path:
    """Stage all trusted local Python files for a dynamic Transformers model.

    Transformers' relative-import scanner misses some MiniCPM-o modules. The
    checkpoint is already loaded with ``trust_remote_code``; copying its local
    Python files into the normal dynamic-module cache makes that trust path
    complete without fetching or modifying model files.
    """
    if modules_cache is None:
        from transformers.dynamic_module_utils import HF_MODULES_CACHE
        modules_cache = Path(HF_MODULES_CACHE)
    package_dir = Path(modules_cache) / "transformers_modules"
    target = package_dir / module_name
    target.mkdir(parents=True, exist_ok=True)
    for init_dir in (package_dir, target):
        (init_dir / "__init__.py").touch(exist_ok=True)
    sources = list(model_dir.glob("*.py"))
    if not sources:
        raise RuntimeError(f"No local model code found in {model_dir}")
    for source in sources:
        shutil.copy2(source, target / source.name)
    return target


def _ensure_minicpm_whisper_attention_registry(module=None) -> bool:
    """Restore the Whisper attention registry removed in Transformers 4.57."""
    if module is None:
        from transformers.models.whisper import modeling_whisper as module
    if hasattr(module, "WHISPER_ATTENTION_CLASSES"):
        return False
    attention = module.WhisperAttention

    class MiniCPMWhisperAttentionCompat(attention):
        def forward(self, *args, **kwargs):
            cache = kwargs.get("past_key_values", kwargs.get("past_key_value"))
            result = super().forward(*args, **kwargs)
            if isinstance(result, tuple) and len(result) == 2:
                return result[0], result[1], cache
            return result

    module.WHISPER_ATTENTION_CLASSES = {
        name: MiniCPMWhisperAttentionCompat
        for name in (
            "eager",
            "sdpa",
            "flash_attention_2",
            "flash_attention_3",
            "flex_attention",
        )
    }
    return True


def _ensure_minicpm_dynamic_cache_compat(module=None) -> bool:
    """Restore DynamicCache.seen_tokens removed in Transformers 4.57."""
    if module is None:
        from transformers import cache_utils as module
    cache_class = module.DynamicCache
    if hasattr(cache_class, "seen_tokens"):
        return False
    cache_class.seen_tokens = property(lambda self: self.get_seq_length())
    return True


def _load_qwen_omni(device: str, size: str = "3b"):
    import torch
    import transformers
    from transformers import AutoConfig, AutoModelForCausalLM, AutoProcessor

    _require_min_version("torch", torch.__version__, (2, 6))
    _require_min_version("transformers", transformers.__version__, (4, 45))

    # Variant resolution: if a variant weights_dir was set at startup, use it;
    # otherwise fall back to the default qwen-omni-{size} path.
    weights_dirname = _variant_weights_dir or f"qwen-omni-{size}"
    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists() or not any(
        f.suffix in (".safetensors", ".bin", ".pt", ".pth")
        for f in model_dir.rglob("*") if f.is_file()
    ):
        raise FileNotFoundError(f"Qwen weights not found or incomplete: {model_dir}")

    dtype = _resolve_precision(_precision, device)
    free_mb = sum(
        int(value) for key, value in
        dict(_placement_config.get("logical_max_memory_mb") or {}).items()
        if str(key).startswith("cuda:")
    ) or _get_vram_free_mb(device)
    logger.info("Loading Qwen2.5-Omni-%s from %s (VRAM free: %dMB, dtype: %s)",
                size, weights_dirname, free_mb, dtype)

    # Check if this is a pre-quantized variant (AWQ, GPTQ) -- those bring
    # their own quantization config from config.json, so we skip the
    # dynamic BitsAndBytesConfig path.
    is_prequant = any(
        tok in weights_dirname.lower() for tok in ("awq", "gptq", "int4", "int8")
    )

    load_kwargs = _hf_load_kwargs(dtype, device)
    gptq_options = _qwen_gptq_options(model_dir, weights_dirname)
    if gptq_options:
        # A caller-supplied GPTQConfig cannot override structural fields when
        # the checkpoint already embeds quantization_config. Put the hint on
        # the in-memory model config so Transformers passes it to Optimum.
        model_config = AutoConfig.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE,
        )
        model_config.quantization_config = gptq_options
        load_kwargs["config"] = model_config
    if not is_prequant and free_mb > 0 and free_mb < 28000 and size == "7b":
        logger.info("VRAM below safe bf16 headroom for Qwen2.5-Omni-7B -- using int4 quantization")
        from transformers import BitsAndBytesConfig
        load_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=dtype,
            bnb_4bit_quant_type="nf4",
        )
        load_kwargs["device_map"] = "auto"

    try:
        processor = AutoProcessor.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE)
        model = AutoModelForCausalLM.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE, **load_kwargs)
    except Exception as first_err:
        # Fallback: try Qwen-specific class (available in transformers >= 4.45)
        try:
            from transformers import Qwen2_5OmniForConditionalGeneration, Qwen2_5OmniProcessor
        except ImportError:
            raise RuntimeError(
                f"AutoModel failed ({first_err}) and Qwen-specific classes not available. "
                f"Upgrade transformers: pip install transformers>=4.45"
            ) from first_err
        processor = Qwen2_5OmniProcessor.from_pretrained(str(model_dir))
        model = Qwen2_5OmniForConditionalGeneration.from_pretrained(
            str(model_dir), **load_kwargs)

    # Apply LoRA adapter if one was specified at startup
    if _lora_path:
        model = _apply_lora(model, _lora_path)

    model.eval()
    return {"model": model, "processor": processor, "size": size}


def _load_minicpm(device: str):
    from transformers import AutoModel, AutoTokenizer

    weights_dirname = _variant_weights_dir or "minicpm-o"
    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists():
        raise FileNotFoundError(f"MiniCPM weights not found: {model_dir}")

    dtype = _resolve_precision(_precision, device)
    logger.info("Loading MiniCPM-o from %s (dtype: %s)", weights_dirname, dtype)

    _ensure_minicpm_whisper_attention_registry()
    _ensure_minicpm_dynamic_cache_compat()
    _stage_local_transformers_code(model_dir, "minicpm_hyphen_o")

    model = AutoModel.from_pretrained(
        str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE,
        torch_dtype=dtype, device_map=device,
    )

    # Apply LoRA adapter if specified
    if _lora_path:
        model = _apply_lora(model, _lora_path)

    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(
        str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE)

    return {"model": model, "tokenizer": tokenizer}


def _load_moshi(device: str):

    weights_dirname = _variant_weights_dir or "moshi"
    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists():
        raise FileNotFoundError(f"Moshi weights not found: {model_dir}")

    logger.info("Loading Moshi...")
    try:
        from moshi.models.loaders import get_mimi, get_moshi_lm
        mimi = get_mimi(str(model_dir / "tokenizer-e351c8d8-checkpoint125.safetensors"),
                        device=device)
        lm = get_moshi_lm(str(model_dir / "model.safetensors"), device=device)
        lm_gen = None
        try:
            from moshi.models.lm_gen import LMGen
            lm_gen = LMGen(lm)
        except Exception:
            pass
        return {"mimi": mimi, "lm": lm, "lm_gen": lm_gen}
    except Exception as e:
        logger.error("Moshi load failed: %s", e)
        raise


def _load_anygpt(device: str):
    from transformers import AutoModelForCausalLM, AutoTokenizer

    weights_dirname = _variant_weights_dir or "anygpt"
    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists():
        raise FileNotFoundError(f"AnyGPT weights not found: {model_dir}")

    dtype = _resolve_precision(_precision, device)
    logger.info("Loading AnyGPT from %s (dtype: %s)", weights_dirname, dtype)

    tokenizer = AutoTokenizer.from_pretrained(
        str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE, use_fast=False)
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE,
        **_hf_load_kwargs(dtype, device),
    )
    model.eval()
    return {"model": model, "tokenizer": tokenizer}


def _load_qwen3_omni(device: str):
    """Qwen3-Omni 30B (MoE) — Thinker-Talker architecture. Apache 2.0.

    transformers >=4.57 ships ``Qwen3OmniMoeForConditionalGeneration`` and
    ``Qwen3OmniMoeProcessor`` natively; we prefer those classes when present
    and fall back to ``AutoModel`` + ``trust_remote_code`` for older
    installs. Pre-quantized checkpoints (FP8 / NVFP4 from NVIDIA-style
    distributions) carry their own quant config — we don't add a runtime
    BitsAndBytes wrapper here.
    """
    weights_dirname = _variant_weights_dir or "qwen3-omni-30b-instruct"
    free_mb = _require_model_vram(60_000, "Qwen3-Omni 30B", device)

    import torch
    import transformers
    from transformers import AutoModel, AutoProcessor
    _require_min_version("torch", torch.__version__, (2, 6))
    _require_min_version("transformers", transformers.__version__, (4, 57))

    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists() or not any(
        f.suffix in (".safetensors", ".bin", ".pt", ".pth")
        for f in model_dir.rglob("*") if f.is_file()
    ):
        raise FileNotFoundError(
            f"Qwen3-Omni weights not found or incomplete: {model_dir}")

    dtype = _resolve_precision(_precision, device)
    logger.info("Loading Qwen3-Omni from %s (VRAM free: %dMB, dtype: %s)",
                weights_dirname, free_mb, dtype)

    load_kwargs = _hf_load_kwargs(dtype, device)
    try:
        from transformers import (
            Qwen3OmniMoeForConditionalGeneration,
            Qwen3OmniMoeProcessor,
        )
        processor = Qwen3OmniMoeProcessor.from_pretrained(str(model_dir))
        model = Qwen3OmniMoeForConditionalGeneration.from_pretrained(
            str(model_dir), **load_kwargs)
    except ImportError:
        # Older transformers — rely on the model repo's own modeling code.
        processor = AutoProcessor.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE)
        model = AutoModel.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE, **load_kwargs)

    if _lora_path:
        model = _apply_lora(model, _lora_path)
    model.eval()
    return {"model": model, "processor": processor, "size": "30b"}


def _load_nemotron_omni(device: str):
    """NVIDIA Nemotron 3 Nano Omni 30B (MoE) — text/image/audio/video.

    The BF16, FP8, and NVFP4 variants each ship complete weights for direct
    load. We use ``AutoModel`` + ``trust_remote_code`` since the architecture
    relies on NVIDIA-specific modeling files rather than a class registered
    in stock transformers. Quant variants self-describe via their own
    config; we don't wrap with BitsAndBytes here.
    """
    weights_dirname = _variant_weights_dir or "nemotron-nano-omni-30b-bf16"
    required_mb = 62_000
    if "nvfp4" in weights_dirname:
        required_mb = 21_000
    elif "fp8" in weights_dirname:
        required_mb = 33_000
    _require_model_vram(
        required_mb, f"Nemotron 3 Nano Omni ({weights_dirname})", device,
    )

    from transformers import AutoModel, AutoProcessor, AutoTokenizer

    model_dir = MODELS_DIR / "omni" / weights_dirname
    if not model_dir.exists() or not any(
        f.suffix in (".safetensors", ".bin", ".pt", ".pth")
        for f in model_dir.rglob("*") if f.is_file()
    ):
        raise FileNotFoundError(
            f"Nemotron Nano Omni weights not found: {model_dir}")

    dtype = _resolve_precision(_precision, device)
    logger.info("Loading Nemotron 3 Nano Omni from %s (dtype: %s)",
                weights_dirname, dtype)

    model = AutoModel.from_pretrained(
        str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE,
        **_hf_load_kwargs(dtype, device),
    )

    # Some Nemotron releases expose only a tokenizer, not a multimodal
    # processor. Try processor first, fall back to tokenizer-only.
    try:
        processor = AutoProcessor.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE)
    except Exception:
        processor = AutoTokenizer.from_pretrained(
            str(model_dir), trust_remote_code=_TRUST_REMOTE_CODE)

    model.eval()
    return {"model": model, "processor": processor, "size": "30b"}


def _load_audio_lab(device: str):
    """Audio Lab worker: returns an empty state object. Actual SA / CLAP
    loading happens via the dedicated ``/audio_lab/load_sa`` and
    ``/audio_lab/load_clap`` endpoints, not at worker boot."""
    from audio_lab_loaders import init_state
    return init_state(device)


def _load_ace_step(device: str):
    """ACE-Step worker: returns an empty state object. Actual model / LM /
    VAE / LoRA loading happens via the dedicated ``/ace_step/*`` endpoints,
    not at worker boot."""
    from ace_step_loaders import init_state
    return init_state(device)


def _load_minimax_music3(device: str):
    """MiniMax Music 3 worker boots empty and loads weights explicitly."""
    from minimax_music3_loaders import init_state
    return init_state(device)


def _load_moss_tts(device: str):
    from moss_tts_loaders import load_moss_tts
    return load_moss_tts(device, _precision)


def _load_moss_sfx(device: str):
    from moss_tts_loaders import load_moss_sfx
    return load_moss_sfx(device, _precision)


_LOADERS = {
    "qwen_omni_3b": lambda dev: _load_qwen_omni(dev, "3b"),
    "qwen_omni_7b": lambda dev: _load_qwen_omni(dev, "7b"),
    "minicpm_o": _load_minicpm,
    "moshi": _load_moshi,
    "anygpt": _load_anygpt,
    "qwen3_omni": _load_qwen3_omni,
    "nemotron_nano_omni": _load_nemotron_omni,
    "audio_lab": _load_audio_lab,
    "ace_step": _load_ace_step,
    "minimax_music3": _load_minimax_music3,
    "moss_tts": _load_moss_tts,
    "moss_sfx": _load_moss_sfx,
}


def _load_model():
    global _model_obj, _loaded
    with _load_lock:
        if _loaded:
            return
        loader = _LOADERS.get(_model_name)
        if not loader:
            raise ValueError(f"Unknown model: {_model_name}")
        _model_obj = loader(_device)
        _loaded = True
        logger.info("Model %s loaded on %s", _model_name, _device)


def _unload_model():
    global _model_obj, _loaded
    with _load_lock:
        _model_obj = None
        _loaded = False
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                with torch.cuda.device(index):
                    torch.cuda.synchronize()
                    torch.cuda.empty_cache()
    except Exception:
        pass
    offload_dir = _worker_offload_dir()
    offload_root = (CACHE_DIR / "offload").resolve()
    try:
        if offload_dir.resolve().parent == offload_root:
            shutil.rmtree(offload_dir, ignore_errors=True)
    except OSError:
        pass
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim:
            malloc_trim(0)
    except Exception:
        pass
    logger.info("Model unloaded")


def _get_loaded_model_obj():
    with _load_lock:
        if not _loaded or _model_obj is None:
            raise HTTPException(status_code=503, detail="Model not loaded")
        return _model_obj


def _decode_image(image_b64: str, convert: str | None = None):
    if len(image_b64) > INFER_MAX_IMAGE_BASE64_CHARS:
        raise HTTPException(status_code=413, detail="Image payload too large")
    try:
        img_bytes = base64.b64decode(image_b64, validate=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid base64 image") from e
    if len(img_bytes) > INFER_MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Decoded image too large")

    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes))
    width, height = img.size
    if width <= 0 or height <= 0 or width * height > INFER_MAX_IMAGE_PIXELS:
        raise HTTPException(status_code=413, detail="Image dimensions too large")
    if convert:
        img = img.convert(convert)
    return img


def _decode_audio_16k(audio_b64: str):
    if len(audio_b64) > AUDIO_LAB_MAX_BASE64_CHARS:
        raise HTTPException(status_code=413, detail="Audio payload too large")
    if audio_b64.startswith("data:") and "," in audio_b64:
        audio_b64 = audio_b64.split(",", 1)[1]
    try:
        audio_bytes = base64.b64decode(audio_b64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid base64 audio") from exc
    try:
        import librosa
        audio, _sample_rate = librosa.load(io.BytesIO(audio_bytes), sr=16000, mono=True, duration=60.001)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Unsupported or invalid audio") from exc
    if audio.size == 0:
        raise HTTPException(status_code=400, detail="Audio is empty")
    if audio.size > 16000 * 60:
        raise HTTPException(status_code=413, detail="Audio exceeds 60 second worker limit")
    return audio


def _minicpm_chat_args(params: dict) -> tuple[list[dict], dict]:
    if params.get("video"):
        raise HTTPException(status_code=501, detail="MiniCPM video input is not wired")
    if params.get("image") and params.get("audio"):
        raise HTTPException(
            status_code=400,
            detail="MiniCPM image and audio must be sent in separate requests",
        )

    text = params.get("text", "")
    if params.get("image"):
        content = [_decode_image(params["image"], convert="RGB"), text]
    elif params.get("audio"):
        content = [text, _decode_audio_16k(params["audio"])]
    else:
        content = [text]

    temperature = float(params.get("temperature", 0.7))
    chat_args = {
        "max_new_tokens": int(params.get("max_new_tokens", 512)),
        "sampling": temperature > 0,
    }
    if temperature > 0:
        chat_args["temperature"] = temperature
        chat_args["top_p"] = float(params.get("top_p", 0.9))
    if params.get("audio"):
        chat_args["use_tts_template"] = True
        chat_args["generate_audio"] = False
    return [{"role": "user", "content": content}], chat_args


# ============================================================
# Inference
# ============================================================
def _reject_unwired_omni_media(params: dict) -> None:
    unsupported = [name for name in ("audio", "video") if params.get(name)]
    if unsupported:
        raise HTTPException(
            status_code=501,
            detail=(
                f"{_model_name} does not yet wire "
                f"{'/'.join(unsupported)} input through its Omni worker"
            ),
        )


def _infer_qwen_omni(params: dict) -> dict:
    _reject_unwired_omni_media(params)
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    processor = model_obj["processor"]

    text = params.get("text", "")
    max_tokens = params.get("max_new_tokens", 512)
    temperature = params.get("temperature", 0.7)

    messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]

    # Add image if provided
    if params.get("image"):
        img = _decode_image(params["image"])
        messages[0]["content"].insert(0, {"type": "image", "image": img})

    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_tensors="pt", return_dict=True,
    )
    input_device = _model_input_device(model, _device)
    inputs = {k: v.to(input_device) for k, v in inputs.items()}

    # Qwen2.5-Omni's generate() returns either a LongTensor (text-only) or a
    # tuple (text_ids, audio_waveform) when its Talker/audio path is active.
    # Qwen3-Omni inherits the same Thinker-Talker output shape. We're doing
    # batch text inference here so disable audio when supported, and fall
    # back defensively to outputs[0] if the model still returns a tuple.
    gen_kwargs = {
        "max_new_tokens": max_tokens,
        "temperature": temperature if temperature > 0 else None,
        "top_p": params.get("top_p", 0.9),
        "do_sample": temperature > 0,
    }
    # `return_audio` is recognized by the Qwen2.5/3 Omni generate() signature
    # via **kwargs even when not in the base GenerationConfig — passing
    # False short-circuits the Talker stage entirely.
    try:
        import inspect
        sig = inspect.signature(model.generate)
        if "return_audio" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            gen_kwargs["return_audio"] = False
    except (TypeError, ValueError):
        pass

    import torch
    with torch.no_grad():
        outputs = model.generate(**inputs, **gen_kwargs)

    # Tuple shape: (text_ids,) or (text_ids, audio_waveform)
    if isinstance(outputs, tuple):
        outputs = outputs[0]

    response = processor.batch_decode(
        outputs[:, inputs["input_ids"].shape[1]:],
        skip_special_tokens=True,
    )[0]
    return {"text": response}


def _infer_minicpm(params: dict) -> dict:
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    tokenizer = model_obj["tokenizer"]

    msgs, chat_args = _minicpm_chat_args(params)
    response = model.chat(msgs=msgs, tokenizer=tokenizer, **chat_args)
    if isinstance(response, tuple):
        response = response[0]
    return {"text": response}


def _infer_moshi(params: dict) -> dict:
    raise HTTPException(
        status_code=501,
        detail="Moshi requires streaming audio — batch inference not supported"
    )


def _infer_anygpt(params: dict) -> dict:
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    tokenizer = model_obj["tokenizer"]

    text = params.get("text", "")
    max_tokens = params.get("max_new_tokens", 256)

    input_device = _model_input_device(model, _device)
    inputs = tokenizer(text, return_tensors="pt").to(input_device)

    import torch
    with torch.no_grad():
        outputs = model.generate(
            **inputs, max_new_tokens=max_tokens,
            do_sample=params.get("temperature", 0.7) > 0,
            temperature=params.get("temperature", 0.7) or None,
            top_p=params.get("top_p", 0.9),
        )

    response = tokenizer.decode(
        outputs[0][inputs["input_ids"].shape[1]:],
        skip_special_tokens=True,
    )
    return {"text": response}


def _infer_moss_sfx(params: dict) -> dict:
    from moss_tts_loaders import infer_moss_sfx
    return infer_moss_sfx(_get_loaded_model_obj(), params)


def _infer_moss_tts_text(params: dict) -> dict:
    raise HTTPException(
        status_code=501,
        detail="MOSS-TTS is an audio-output model; use /api/tts/moss_tts or /v1/audio/speech.",
    )


# ---------------------------------------------------------------------------
# Streaming inference (phase 8)
#
# Each handler is a *generator* that yields ``str`` deltas as they're
# produced. The handler may also raise ``GeneratorExit`` cleanly when the
# caller cancels. Cancellation is signalled via a per-job ``threading.Event``
# in ``_ABORT_FLAGS``; the streamer checks it on each yielded token. Models
# that don't expose token-level streaming (Moshi, MiniCPM-o legacy chat) fall
# back to a single-shot result wrapped in one delta.
# ---------------------------------------------------------------------------
# Insertion-ordered so we can evict only the OLDEST pending abort when the
# bound is hit, instead of dropping every legitimate pending request. Values
# are unused; only the keys (job ids) and their order matter.
_ABORT_FLAGS: dict[str, threading.Event] = {}
_PENDING_ABORTS: "collections.OrderedDict[str, None]" = collections.OrderedDict()
_ABORT_LOCK = threading.Lock()


def _register_stream_flag(job_id: str) -> threading.Event:
    """Called by /infer/stream when a streaming job starts.

    If /abort raced ahead and registered a pending abort, the new flag is
    pre-set so the streamer terminates immediately.
    """
    with _ABORT_LOCK:
        flag = _ABORT_FLAGS.get(job_id)
        if flag is None:
            flag = threading.Event()
            _ABORT_FLAGS[job_id] = flag
        if job_id in _PENDING_ABORTS:
            flag.set()
            _PENDING_ABORTS.pop(job_id, None)
        return flag


def _request_abort(job_id: str) -> bool:
    """Called by /abort. Sets an existing flag if /infer/stream is live;
    otherwise records the request so a not-yet-started stream picks it up.

    Returns True if a live stream was aborted, False if recorded as pending.
    Pending entries are bounded by ``_MAX_PENDING_ABORTS`` to prevent the
    set from growing unboundedly under hostile load.
    """
    with _ABORT_LOCK:
        flag = _ABORT_FLAGS.get(job_id)
        if flag is not None:
            flag.set()
            return True
        if len(_PENDING_ABORTS) >= _MAX_PENDING_ABORTS:
            # Evict only the single oldest entry — pending aborts are
            # best-effort, but dropping ALL of them would discard legitimate
            # requests that haven't been picked up by their streams yet.
            _PENDING_ABORTS.popitem(last=False)
        # Re-insert at the end to track insertion order for eviction.
        _PENDING_ABORTS.pop(job_id, None)
        _PENDING_ABORTS[job_id] = None
        return False


def _drop_abort_flag(job_id: str) -> None:
    """Called by /infer/stream after the stream finishes."""
    with _ABORT_LOCK:
        _ABORT_FLAGS.pop(job_id, None)
        _PENDING_ABORTS.pop(job_id, None)


_MAX_PENDING_ABORTS = 1024


def _hf_stream_generate(model, processor, inputs, gen_kwargs, abort_flag):
    """Common HF streaming generator using TextIteratorStreamer."""
    from transformers import (
        TextIteratorStreamer, StoppingCriteria, StoppingCriteriaList,
    )
    streamer = TextIteratorStreamer(
        processor.tokenizer if hasattr(processor, "tokenizer") else processor,
        skip_prompt=True,
        skip_special_tokens=True,
    )
    gen_kwargs = dict(gen_kwargs)
    gen_kwargs["streamer"] = streamer

    # Wire abort_flag into generation so the producer thread halts promptly
    # instead of running to completion (still pinning the GPU) after a cancel.
    class _AbortCriteria(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            return abort_flag.is_set()

    gen_kwargs["stopping_criteria"] = StoppingCriteriaList([_AbortCriteria()])

    import torch

    # A generation failure must reach the consumer as an error rather than
    # masquerading as a clean end-of-stream (which would persist empty text
    # as a successful "done").
    err_holder: dict[str, BaseException] = {}

    def _runner():
        try:
            with torch.no_grad():
                model.generate(**inputs, **gen_kwargs)
        except Exception as e:
            logger.error("Stream generate error: %s", e, exc_info=True)
            err_holder["error"] = e
            streamer.text_queue.put(streamer.stop_signal)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    completed = False
    try:
        for piece in streamer:
            if abort_flag.is_set():
                break
            if piece:
                yield piece
        else:
            completed = True
    finally:
        # A disconnected consumer must stop the producer before its inference
        # lock can be released. The gateway retires a producer that stays stuck.
        if not completed:
            abort_flag.set()
        try:
            thread.join()
        except RuntimeError:
            pass
    # Surface a producer-side failure to the SSE layer as an error event.
    # Skip on abort: a user-requested cancel is not an error.
    if "error" in err_holder:
        raise err_holder["error"]


def _infer_qwen_omni_stream(params: dict, abort_flag: threading.Event):
    _reject_unwired_omni_media(params)
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    processor = model_obj["processor"]

    text = params.get("text", "")
    max_tokens = params.get("max_new_tokens", 512)
    temperature = params.get("temperature", 0.7)
    top_p = params.get("top_p", 0.9)

    messages = [{"role": "user", "content": [{"type": "text", "text": text}]}]
    if params.get("image"):
        img = _decode_image(params["image"])
        messages[0]["content"].insert(0, {"type": "image", "image": img})

    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True,
        return_tensors="pt", return_dict=True,
    )
    input_device = _model_input_device(model, _device)
    inputs = {k: v.to(input_device) for k, v in inputs.items()}

    gen_kwargs = {
        "max_new_tokens": max_tokens,
        "do_sample": temperature > 0,
        "temperature": temperature if temperature > 0 else None,
        "top_p": top_p,
    }
    # Disable Qwen Omni's audio (Talker) path during text streaming to avoid
    # the (text_ids, audio_waveform) tuple shape that breaks token slicing.
    try:
        import inspect
        sig = inspect.signature(model.generate)
        if "return_audio" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            gen_kwargs["return_audio"] = False
    except (TypeError, ValueError):
        pass
    yield from _hf_stream_generate(model, processor, inputs, gen_kwargs, abort_flag)


def _infer_anygpt_stream(params: dict, abort_flag: threading.Event):
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    tokenizer = model_obj["tokenizer"]

    text = params.get("text", "")
    max_tokens = params.get("max_new_tokens", 256)
    temperature = params.get("temperature", 0.7)
    top_p = params.get("top_p", 0.9)

    input_device = _model_input_device(model, _device)
    inputs = tokenizer(text, return_tensors="pt").to(input_device)
    gen_kwargs = {
        "max_new_tokens": max_tokens,
        "do_sample": temperature > 0,
        "temperature": temperature if temperature > 0 else None,
        "top_p": top_p,
    }
    yield from _hf_stream_generate(model, tokenizer, inputs, gen_kwargs, abort_flag)


def _infer_minicpm_stream(params: dict, abort_flag: threading.Event):
    """MiniCPM-o exposes stream=True in its chat method natively."""
    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    tokenizer = model_obj["tokenizer"]

    msgs, chat_args = _minicpm_chat_args(params)

    try:
        gen = model.chat(
            msgs=msgs, tokenizer=tokenizer, stream=True, **chat_args,
        )
    except TypeError:
        # Older MiniCPM-o builds without stream= - fall back to one-shot delta.
        result = model.chat(msgs=msgs, tokenizer=tokenizer, **chat_args)
        if isinstance(result, tuple):
            result = result[0]
        if abort_flag.is_set():
            return
        yield result
        return

    for piece in gen:
        if abort_flag.is_set():
            break
        if piece:
            yield piece


_INFER = {
    "qwen_omni_3b": _infer_qwen_omni,
    "qwen_omni_7b": _infer_qwen_omni,
    "minicpm_o": _infer_minicpm,
    "moshi": _infer_moshi,
    "anygpt": _infer_anygpt,
    # Qwen3-Omni and Nemotron 3 Nano Omni both expose the standard
    # processor.apply_chat_template + model.generate shape, so the existing
    # qwen-omni inference path handles them. Dedicated audio-out / video
    # encoders for Qwen3's Talker decoder can be wired separately later.
    "qwen3_omni": _infer_qwen_omni,
    "nemotron_nano_omni": _infer_qwen_omni,
    "moss_tts": _infer_moss_tts_text,
    "moss_sfx": _infer_moss_sfx,
}


_INFER_STREAM = {
    "qwen_omni_3b": _infer_qwen_omni_stream,
    "qwen_omni_7b": _infer_qwen_omni_stream,
    "minicpm_o": _infer_minicpm_stream,
    "anygpt": _infer_anygpt_stream,
    "qwen3_omni": _infer_qwen_omni_stream,
    "nemotron_nano_omni": _infer_qwen_omni_stream,
    # moshi: not supported (audio-only stream model)
}


# ============================================================
# FastAPI app
# ============================================================
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool, iterate_in_threadpool
import uvicorn

app = FastAPI(title="Omni Worker")

# WOR-5: serialize model-using / model-mutating operations within this worker.
# The gateway normally single-flights via atomic_pick_and_mark_busy, but a
# direct caller to the worker bypasses that, so two /infer requests (or /infer
# racing /load or /unload) could otherwise run concurrent GPU work or mutate
# the model object mid-generation. This asyncio.Lock is acquired for the whole
# duration of /load, /unload, /infer, and /infer/stream (held across the entire
# streamed response). It is an asyncio.Lock (not a threading.Lock) so the
# endpoints become ``async def`` and the lock is acquired/released on the event
# loop; the actual blocking GPU work is offloaded to the threadpool while the
# lock is held so /abort (which never takes this lock) stays responsive.
_INFER_LOCK = asyncio.Lock()


class InferRequest(BaseModel):
    text: str = Field(default="", max_length=INFER_MAX_TEXT_CHARS)
    image: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    audio: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    video: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    max_new_tokens: int = Field(default=512, ge=1, le=INFER_MAX_NEW_TOKENS)
    temperature: float = Field(default=0.7, ge=0.0, le=INFER_MAX_TEMPERATURE)
    top_p: float = Field(default=0.9, ge=0.0, le=INFER_MAX_TOP_P)
    model_params: dict | None = None


@app.get("/health")
def health():
    gpu_memory = _gpu_memory_snapshot()
    vram_used = sum(item["vram_used_mb"] for item in gpu_memory)
    vram_total = sum(item["vram_total_mb"] for item in gpu_memory)
    return {
        "status": "ready" if _loaded else "idle",
        "busy": _INFER_LOCK.locked(),
        "model": _model_name,
        "device": _device,
        "loaded": _loaded,
        "variant": _variant_id,
        "lora": os.path.basename(_lora_path) if _lora_path else None,
        "placement_mode": str(_placement_config.get("mode") or "single"),
        "gpu_memory": gpu_memory,
        "vram_used_mb": vram_used,
        "vram_total_mb": vram_total,
    }


@app.post("/load")
async def load_model():
    # WOR-5: hold _INFER_LOCK so a (re)load cannot mutate the model object
    # while an /infer or /infer/stream generation is in flight. The blocking
    # load runs in the threadpool so the event loop (and /abort) stay free.
    async with _INFER_LOCK:
        try:
            await run_in_threadpool(_load_model)
            return {"status": "ok", "model": _model_name}
        except Exception as e:
            logger.error("Load failed: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/unload")
async def unload_model():
    # WOR-5: hold _INFER_LOCK so an unload cannot free the model out from under
    # an in-flight generation. Offload the (blocking) GPU teardown to the
    # threadpool while the lock is held.
    async with _INFER_LOCK:
        await run_in_threadpool(_unload_model)
        return {"status": "ok"}


@app.post("/infer")
async def infer(req: InferRequest):
    if not _loaded:
        raise HTTPException(status_code=503, detail="Model not loaded")
    infer_fn = _INFER.get(_model_name)
    if not infer_fn:
        raise HTTPException(status_code=400, detail=f"No inference handler for {_model_name}")
    # WOR-5: serialize GPU inference against other /infer calls and against
    # /load + /unload by holding _INFER_LOCK for the whole generation. The
    # blocking inference runs in the threadpool so the event loop stays free.
    async with _INFER_LOCK:
        try:
            params = req.model_dump()
            result = await run_in_threadpool(infer_fn, params)
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Inference error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Streaming inference (phase 8)
# ---------------------------------------------------------------------------
class StreamInferRequest(InferRequest):
    job_id: str = Field(min_length=1, max_length=64)


class AbortRequest(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)


@app.post("/infer/stream")
async def infer_stream(req: StreamInferRequest):
    """SSE token stream. Events:

    * ``data: {"delta": "..."}`` for each generated piece
    * ``data: {"done": true}`` when generation finishes
    * ``data: {"error": "..."}`` on inference failure
    * ``data: {"cancelled": true}`` when an /abort flipped the flag mid-run
    """
    from fastapi.responses import StreamingResponse
    if not _loaded:
        raise HTTPException(status_code=503, detail="Model not loaded")
    stream_fn = _INFER_STREAM.get(_model_name)
    if not stream_fn:
        raise HTTPException(
            status_code=501,
            detail=f"Streaming not supported for {_model_name}",
        )

    params = req.model_dump()

    # WOR-5: hold _INFER_LOCK for the entire streamed generation so a
    # concurrent /infer, /load, or /unload cannot run GPU work or mutate the
    # model while tokens are still being produced. The lock is acquired inside
    # the async SSE body (so it is held exactly for the lifetime of the
    # response body) and released in ``finally`` — which runs on normal stream
    # end, on error, and on client disconnect (Starlette closes the async
    # generator, raising GeneratorExit through the ``async with``). /abort does
    # NOT take this lock; it only sets the abort flag, so an in-flight
    # generation can still be cancelled while the lock is held here.
    #
    # The underlying stream generator (stream_fn) is synchronous and runs the
    # blocking model.generate loop; we drive it through iterate_in_threadpool
    # so each token step executes off the event loop, keeping /abort responsive.
    async def _sse():
        import json as _json
        async with _INFER_LOCK:
            # Register the abort flag HERE, inside the body, so its lifetime is
            # tied to this generator's ``finally`` (which calls _drop_abort_flag).
            # Registering it before the response body is returned would leak the
            # flag from the unbounded _ABORT_FLAGS if the client disconnects
            # before the body's first iteration — in that case the body, and
            # thus its finally, never runs. (review fix)
            abort_flag = _register_stream_flag(req.job_id)
            # Keep a reference to the underlying sync generator so we can close
            # it explicitly on early exit (abort / client disconnect). Closing
            # runs its own ``finally`` (thread.join of the producer) so we don't
            # leak the generate() worker thread or skip its cleanup. (WOR-5)
            gen = stream_fn(params, abort_flag)
            try:
                async for piece in iterate_in_threadpool(gen):
                    if abort_flag.is_set():
                        yield f"data: {_json.dumps({'cancelled': True})}\n\n"
                        return
                    yield f"data: {_json.dumps({'delta': piece})}\n\n"
                if abort_flag.is_set():
                    yield f"data: {_json.dumps({'cancelled': True})}\n\n"
                else:
                    yield f"data: {_json.dumps({'done': True})}\n\n"
            except Exception as e:
                logger.error("Stream inference error: %s", e, exc_info=True)
                yield f"data: {_json.dumps({'error': str(e)})}\n\n"
            finally:
                # Run on the threadpool: gen.close() raises GeneratorExit into
                # the sync generator, whose finally joins the producer thread
                # (up to 5s) — keep that off the event loop. (WOR-5)
                import anyio
                abort_flag.set()
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(gen.close)
                    _drop_abort_flag(req.job_id)

    return StreamingResponse(_sse(), media_type="text/event-stream")


@app.post("/abort")
def abort(req: AbortRequest):
    """Set the per-job abort flag. The streaming generator polls it on each token."""
    aborted_live = _request_abort(req.job_id)
    return {
        "status": "abort_requested" if aborted_live else "abort_pending",
        "job_id": req.job_id,
    }


# ---------------------------------------------------------------------------
# TTS (sprint B item 3)
#
# Models that natively produce audio fill in ``_INFER_TTS[model_name]`` with
# a function ``(params) -> {"audio_base64": "...", "format": "wav"}``. The
# worker returns 501 for any model not registered, which the gateway maps
# to a clean "not supported" error rather than a generic 500.
# ---------------------------------------------------------------------------
class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    voice: str | None = None
    response_format: str = Field(default="wav")
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    model_params: dict | None = None


def _infer_moss_tts_audio(params: dict) -> dict:
    from moss_tts_loaders import infer_moss_tts
    return infer_moss_tts(_get_loaded_model_obj(), params)


def _ensure_minicpm_tts_hidden_states_compat(model_obj: dict) -> bool:
    if model_obj.get("tts_hidden_states_compat"):
        return False
    model = model_obj["model"]
    original = model._get_last_spk_embeds
    original_decode = getattr(model, "_decode", None)

    if callable(original_decode):
        def compatible_decode(inputs_embeds, tokenizer, attention_mask, **kwargs):
            import torch

            outputs = original_decode(inputs_embeds, tokenizer, attention_mask, **kwargs)
            generation_hidden_states = getattr(outputs, "hidden_states", None) or ()
            if not any(item is not None for item in generation_hidden_states):
                with torch.inference_mode():
                    prompt_outputs = model.llm(
                        input_ids=None,
                        inputs_embeds=inputs_embeds,
                        attention_mask=attention_mask,
                        output_hidden_states=True,
                        return_dict=True,
                        use_cache=False,
                    )
                prompt_hidden_states = getattr(prompt_outputs, "hidden_states", None) or ()
                model_obj["tts_prompt_last_hidden_state"] = next(
                    (item for item in reversed(prompt_hidden_states) if item is not None),
                    None,
                )
            return outputs

        model._decode = compatible_decode

    def compatible_get_last_spk_embeds(inputs, outputs):
        hidden_states = getattr(outputs, "hidden_states", None)
        if hidden_states:
            filtered = tuple(item for item in hidden_states if item is not None)
            if filtered:
                outputs.hidden_states = filtered
                return original(inputs, outputs)

        # Transformers 4.57 can return an all-None generation hidden-state
        # history for this trusted remote-code model.  ``compatible_decode``
        # captures the prompt state while its embeddings are still available.
        last_hidden_state = model_obj.get("tts_prompt_last_hidden_state")
        if last_hidden_state is None:
            raise RuntimeError("MiniCPM prompt decode returned no hidden states for TTS")
        spk_bound = inputs["spk_bounds"][0][-1]
        return last_hidden_state[0, spk_bound[0] : spk_bound[1]]

    model._get_last_spk_embeds = compatible_get_last_spk_embeds
    model_obj["tts_hidden_states_compat"] = True
    return True


def _infer_minicpm_tts_audio(params: dict) -> dict:
    if str(params.get("response_format") or "wav").lower() != "wav":
        raise HTTPException(status_code=400, detail="MiniCPM TTS supports WAV only")
    if abs(float(params.get("speed") or 1.0) - 1.0) > 1e-6:
        raise HTTPException(status_code=400, detail="MiniCPM TTS speed control is not supported")

    model_obj = _get_loaded_model_obj()
    model = model_obj["model"]
    tokenizer = model_obj["tokenizer"]
    _ensure_minicpm_tts_hidden_states_compat(model_obj)
    if not model_obj.get("tts_ready"):
        model.init_tts()
        model_obj["tts_ready"] = True
    model_obj.pop("tts_prompt_last_hidden_state", None)

    voice = str(params.get("voice") or "clear natural voice").strip()
    text = str(params.get("text") or "").strip()
    instruction = (
        f"Speak in a {voice}. Read the following text exactly and do not add words."
    )
    tmp_dir = CACHE_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="minicpm_tts_", suffix=".wav", dir=tmp_dir, delete=False,
    ) as handle:
        output_path = Path(handle.name)
    try:
        response = model.chat(
            msgs=[{"role": "user", "content": [instruction, text]}],
            tokenizer=tokenizer,
            sampling=True,
            temperature=0.3,
            max_new_tokens=min(512, max(128, len(text) * 2)),
            use_tts_template=True,
            generate_audio=True,
            output_audio_path=str(output_path),
        )
        if not output_path.is_file() or output_path.stat().st_size <= 44:
            raise RuntimeError("MiniCPM TTS did not produce a WAV file")
        return {
            "audio_base64": base64.b64encode(output_path.read_bytes()).decode("ascii"),
            "format": "wav",
            "text": response[0] if isinstance(response, tuple) else response,
        }
    finally:
        output_path.unlink(missing_ok=True)


_INFER_TTS: dict = {
    # Wire up native handlers when the model loader supports audio output.
    "moss_tts": _infer_moss_tts_audio,
    "minicpm_o": _infer_minicpm_tts_audio,
}


@app.post("/infer/tts")
async def infer_tts(req: TTSRequest):
    if not _loaded:
        raise HTTPException(status_code=503, detail="Model not loaded")
    handler = _INFER_TTS.get(_model_name)
    if not handler:
        raise HTTPException(
            status_code=501,
            detail=f"TTS not implemented for {_model_name}",
        )
    async with _INFER_LOCK:
        try:
            return await run_in_threadpool(handler, req.model_dump())
        except HTTPException:
            raise
        except Exception as e:
            logger.error("TTS error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Audio Lab endpoints (Stable Audio + CLAP)
#
# Active only when this worker was launched with --model audio_lab. _model_obj
# in that case is an audio_lab_loaders._AudioLabState. Each endpoint is a thin
# wrapper that asserts the worker is in audio_lab mode and delegates to the
# functions in audio_lab_loaders.
# ---------------------------------------------------------------------------
class _AudioLabLoadSARequest(BaseModel):
    sa_variant: str = Field(min_length=1, max_length=128)
    vae_variant: str | None = Field(default=None, max_length=128)


class _AudioLabLoadCLAPRequest(BaseModel):
    clap_variant: str = Field(min_length=1, max_length=128)


class _AudioLabUnloadRequest(BaseModel):
    component: str = Field(default="all", pattern="^(sa|clap|vae|all)$")


class _AudioLabGenRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    negative_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    duration_s: float = Field(default=10.0, ge=1.0, le=120.0)
    steps: int = Field(default=100, ge=1, le=500)
    cfg_scale: float = Field(default=7.0, ge=0.0, le=20.0)
    sigma_min: float | None = Field(default=None, ge=0.0)
    sigma_max: float | None = Field(default=None, ge=0.0)
    sampler: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    num_waveforms_per_prompt: int = Field(default=1, ge=1, le=16)


class _AudioLabA2ARequest(_AudioLabGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    init_noise_level: float | None = Field(default=None, ge=0.0, le=1.0)


class _AudioLabInpaintRequest(_AudioLabGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    mask_start_s: float = Field(ge=0.0, le=180.0)
    mask_end_s: float = Field(ge=0.0, le=180.0)


class _AudioLabUncondRequest(BaseModel):
    duration_s: float = Field(default=10.0, ge=1.0, le=120.0)
    steps: int = Field(default=100, ge=1, le=500)
    sigma_min: float | None = Field(default=None, ge=0.0)
    sigma_max: float | None = Field(default=None, ge=0.0)
    sampler: str | None = Field(default=None, max_length=64)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class _AudioLabVAEEncodeRequest(BaseModel):
    audio_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)


class _AudioLabVAEDecodeRequest(BaseModel):
    latent_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    shape: list[int] | None = None


class _AudioLabScoreRequest(BaseModel):
    text: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    audio_base64: str = Field(min_length=8, max_length=AUDIO_LAB_MAX_BASE64_CHARS)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)


def _require_audio_lab() -> object:
    """Pull the audio_lab state out of _model_obj, or 503 if we're not in
    audio_lab mode / nothing is initialized yet."""
    if _model_name != "audio_lab":
        raise HTTPException(
            status_code=400,
            detail=f"Audio Lab endpoints require --model audio_lab; this worker runs {_model_name}",
        )
    if not _loaded or _model_obj is None:
        raise HTTPException(status_code=503, detail="Audio Lab worker not initialized")
    return _model_obj


@app.post("/audio_lab/load_sa")
def audio_lab_load_sa(req: _AudioLabLoadSARequest):
    state = _require_audio_lab()
    from audio_lab_loaders import load_sa
    try:
        return load_sa(state, req.sa_variant, req.vae_variant)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_lab load_sa failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/audio_lab/load_clap")
def audio_lab_load_clap(req: _AudioLabLoadCLAPRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import load_clap
    try:
        return load_clap(state, req.clap_variant)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_lab load_clap failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/audio_lab/unload")
def audio_lab_unload(req: _AudioLabUnloadRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import unload
    return unload(state, req.component)


@app.get("/audio_lab/state")
def audio_lab_state():
    state = _require_audio_lab()
    from audio_lab_loaders import current_state
    return current_state(state)


@app.post("/audio_lab/cancel")
def audio_lab_cancel():
    """Set the worker's cancel flag. Best-effort: in-flight diffusion steps
    can't be interrupted, but the flag is checked between fan-out candidates
    (in the gateway) and between CLAP scoring windows."""
    state = _require_audio_lab()
    from audio_lab_loaders import request_cancel
    return request_cancel(state)


@app.post("/audio_lab/cancel/clear")
def audio_lab_cancel_clear():
    state = _require_audio_lab()
    from audio_lab_loaders import clear_cancel
    clear_cancel(state)
    return {"cleared": True}


@app.post("/infer/audio_gen")
def infer_audio_gen(req: _AudioLabGenRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import infer_audio_gen as _gen
    try:
        return _gen(state, req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_gen failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/audio_score")
def infer_audio_score(req: _AudioLabScoreRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import infer_audio_score as _score
    try:
        return _score(state, req.text, req.audio_base64, req.sample_rate)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_score failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/audio_a2a")
def infer_audio_a2a(req: _AudioLabA2ARequest):
    state = _require_audio_lab()
    from audio_lab_loaders import infer_audio_a2a as _a2a
    try:
        return _a2a(state, req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_a2a failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/audio_inpaint")
def infer_audio_inpaint(req: _AudioLabInpaintRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import infer_audio_inpaint as _inpaint
    try:
        return _inpaint(state, req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_inpaint failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/audio_uncond")
def infer_audio_uncond(req: _AudioLabUncondRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import infer_audio_uncond as _uncond
    try:
        return _uncond(state, req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("audio_uncond failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/vae_encode")
def infer_vae_encode(req: _AudioLabVAEEncodeRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import vae_encode as _enc
    try:
        return _enc(state, req.audio_base64, req.sample_rate)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("vae_encode failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/vae_decode")
def infer_vae_decode(req: _AudioLabVAEDecodeRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import vae_decode as _dec
    try:
        return _dec(state, req.latent_base64, req.shape)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("vae_decode failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/infer/vae_reconstruct")
def infer_vae_reconstruct(req: _AudioLabVAEEncodeRequest):
    state = _require_audio_lab()
    from audio_lab_loaders import vae_reconstruct as _rec
    try:
        return _rec(state, req.audio_base64, req.sample_rate)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("vae_reconstruct failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# ACE-Step endpoints
# Active only when this worker was launched with --model ace_step. _model_obj
# in that case is an ace_step_loaders._AceStepState. Each endpoint is a thin
# wrapper that asserts the worker is in ace_step mode and delegates.
# ---------------------------------------------------------------------------
class _AceStepLoadModelRequest(BaseModel):
    model_variant: str = Field(min_length=1, max_length=128)
    vae_variant: str | None = Field(default=None, max_length=128)
    bf16: bool = True
    cpu_offload: bool = False
    int8: bool = False
    torch_compile: bool = False


class _AceStepLoadLMRequest(BaseModel):
    lm_variant: str = Field(min_length=1, max_length=128)
    lm_device: str | None = Field(default=None, max_length=64)
    backend: str | None = Field(default=None, pattern="^(pt|vllm)$")


class _AceStepUnloadRequest(BaseModel):
    component: str = Field(default="all", pattern="^(model|lm|vae|all)$")


class _AceStepLoraAttachRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    multiplier: float = Field(default=1.0, ge=-2.0, le=2.0)
    adapter_file: str | None = Field(
        default=None,
        min_length=13,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.safetensors$",
    )


class _AceStepLoraDetachRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class _AceStepGenRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    prompt: str = Field(min_length=1, max_length=INFER_MAX_TEXT_CHARS)
    lyrics: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 4)
    negative_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    duration_s: float = Field(default=60.0, ge=1.0, le=600.0)
    steps: int | None = Field(default=None, ge=2, le=200)
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    guidance_interval: list[float] | None = Field(default=None, min_length=2, max_length=2)
    scheduler: str = Field(default="euler", pattern="^(euler|heun|dpmpp)$")
    shift: float | None = Field(default=None, ge=1.0, le=5.0)
    bpm: int | None = Field(default=None, ge=30, le=300)
    keyscale: str | None = Field(default=None, max_length=32)
    timesignature: str | None = Field(default=None, max_length=16)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    bf16: bool = True
    overlapped_decode: bool = True


class _AceStepA2ARequest(_AceStepGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    init_noise_level: float = Field(default=0.6, ge=0.0, le=1.0)


class _AceStepRepaintRequest(_AceStepGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    mask_start_s: float = Field(ge=0.0, le=600.0)
    mask_end_s: float = Field(ge=0.0, le=600.0)

    @model_validator(mode="after")
    def _check_mask_order(self):
        if self.mask_end_s <= self.mask_start_s:
            raise ValueError(
                f"mask_end_s ({self.mask_end_s}) must be greater than mask_start_s ({self.mask_start_s})"
            )
        return self


class _AceStepEditRequest(_AceStepGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    edit_mode: str = Field(pattern="^(only_lyrics|remix)$")
    source_prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    source_lyrics: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS * 4)


class _AceStepExtendRequest(_AceStepGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    extend_mode: str = Field(pattern="^(prepend|append)$")
    extend_duration_s: float = Field(default=30.0, ge=1.0, le=300.0)


class _AceStepCoverRequest(_AceStepGenRequest):
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)


class _AceStepVocal2BGMRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    init_audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    init_sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    duration_s: float | None = Field(default=None, ge=1.0, le=600.0)
    steps: int | None = Field(default=None, ge=2, le=200)
    cfg_scale: float | None = Field(default=None, ge=0.0, le=20.0)
    scheduler: str = Field(default="euler", pattern="^(euler|heun|dpmpp)$")
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)


class _AceStepAnalyzeRequest(BaseModel):
    audio_base64: str = Field(min_length=8, max_length=ACE_STEP_MAX_BASE64_CHARS)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)


class _AceStepLooseRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class _MiniMaxMusic3LoadRequest(BaseModel):
    model_variant: str = Field(default="official-diffusers", max_length=128)
    bf16: bool = True
    cpu_offload: bool = True


class _MiniMaxMusic3GenerateRequest(BaseModel):
    job_id: str = Field(min_length=6, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    prompt: str = Field(min_length=1, max_length=MINIMAX_MUSIC3_MAX_TEXT_CHARS)
    lyrics: str = Field(min_length=1, max_length=MINIMAX_MUSIC3_MAX_TEXT_CHARS)
    duration_s: float = Field(default=60.0, ge=1.0, le=MINIMAX_MUSIC3_MAX_DURATION_S)
    seed: int = Field(default=0, ge=0, le=2**63 - 1)


def _require_minimax_music3() -> object:
    if _model_name != "minimax_music3":
        raise HTTPException(
            status_code=400,
            detail=(
                "MiniMax Music 3 endpoints require --model minimax_music3; "
                f"this worker runs {_model_name}"
            ),
        )
    if not _loaded or _model_obj is None:
        raise HTTPException(status_code=503, detail="MiniMax Music 3 worker not initialized")
    return _model_obj


@app.post("/minimax_music3/load_model")
def minimax_music3_load_model(req: _MiniMaxMusic3LoadRequest):
    state = _require_minimax_music3()
    from minimax_music3_loaders import load_model
    try:
        return load_model(
            state,
            req.model_variant,
            bf16=req.bf16,
            cpu_offload=req.cpu_offload,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("minimax_music3 load failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/minimax_music3/state")
def minimax_music3_state():
    from minimax_music3_loaders import current_state
    return current_state(_require_minimax_music3())


@app.post("/minimax_music3/unload")
def minimax_music3_unload():
    from minimax_music3_loaders import unload
    return unload(_require_minimax_music3())


@app.post("/infer/minimax_music3_generate")
def infer_minimax_music3_generate(req: _MiniMaxMusic3GenerateRequest):
    state = _require_minimax_music3()
    from minimax_music3_loaders import infer_generate
    try:
        return infer_generate(state, req.model_dump())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (ValueError, FileExistsError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("minimax_music3 generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _require_ace_step() -> object:
    """Pull the ace_step state out of _model_obj, or 503 if we're not in
    ace_step mode / nothing is initialized yet."""
    if _model_name != "ace_step":
        raise HTTPException(
            status_code=400,
            detail=f"ACE-Step endpoints require --model ace_step; this worker runs {_model_name}",
        )
    if not _loaded or _model_obj is None:
        raise HTTPException(status_code=503, detail="ACE-Step worker not initialized")
    return _model_obj


def _ace_call(fn_name: str, req_dict: dict, log_label: str):
    """Common dispatch: pull module function, run, translate exceptions."""
    state = _require_ace_step()
    import ace_step_loaders
    fn = getattr(ace_step_loaders, fn_name)
    try:
        return fn(state, req_dict)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("%s failed: %s", log_label, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ace_step/load_model")
def ace_step_load_model(req: _AceStepLoadModelRequest):
    state = _require_ace_step()
    from ace_step_loaders import load_model
    try:
        return load_model(
            state,
            req.model_variant,
            vae_variant=req.vae_variant,
            bf16=req.bf16,
            cpu_offload=req.cpu_offload,
            int8=req.int8,
            torch_compile=req.torch_compile,
        )
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("ace_step load_model failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ace_step/load_lm")
def ace_step_load_lm(req: _AceStepLoadLMRequest):
    state = _require_ace_step()
    from ace_step_loaders import load_lm
    try:
        return load_lm(state, req.lm_variant, lm_device=req.lm_device, backend=req.backend)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("ace_step load_lm failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ace_step/unload")
def ace_step_unload(req: _AceStepUnloadRequest):
    state = _require_ace_step()
    from ace_step_loaders import unload
    return unload(state, req.component)


@app.get("/ace_step/state")
def ace_step_state():
    state = _require_ace_step()
    from ace_step_loaders import current_state
    return current_state(state)


@app.post("/ace_step/cancel")
def ace_step_cancel():
    """Set cancel flag. Best-effort: in-flight diffusion steps can't be
    interrupted, but the flag is checked between fan-out candidates and
    between CLAP scoring windows by the gateway."""
    state = _require_ace_step()
    from ace_step_loaders import request_cancel
    return request_cancel(state)


@app.post("/ace_step/cancel/clear")
def ace_step_cancel_clear():
    state = _require_ace_step()
    from ace_step_loaders import clear_cancel
    clear_cancel(state)
    return {"cleared": True}


@app.post("/ace_step/lora/attach")
def ace_step_lora_attach(req: _AceStepLoraAttachRequest):
    state = _require_ace_step()
    from ace_step_loaders import attach_lora
    try:
        return attach_lora(state, req.name, req.multiplier, req.adapter_file)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("ace_step lora_attach failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ace_step/lora/detach")
def ace_step_lora_detach(req: _AceStepLoraDetachRequest):
    state = _require_ace_step()
    from ace_step_loaders import detach_lora
    try:
        return detach_lora(state, req.name)
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("ace_step lora_detach failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/ace_step/lora/list")
def ace_step_lora_list():
    state = _require_ace_step()
    from ace_step_loaders import list_loras
    return list_loras(state)


@app.post("/infer/ace_generate")
def infer_ace_generate(req: _AceStepGenRequest):
    return _ace_call("infer_generate", req.model_dump(), "ace_generate")


@app.post("/infer/ace_a2a")
def infer_ace_a2a(req: _AceStepA2ARequest):
    return _ace_call("infer_a2a", req.model_dump(), "ace_a2a")


@app.post("/infer/ace_repaint")
def infer_ace_repaint(req: _AceStepRepaintRequest):
    return _ace_call("infer_repaint", req.model_dump(), "ace_repaint")


@app.post("/infer/ace_edit")
def infer_ace_edit(req: _AceStepEditRequest):
    return _ace_call("infer_edit", req.model_dump(), "ace_edit")


@app.post("/infer/ace_extend")
def infer_ace_extend(req: _AceStepExtendRequest):
    return _ace_call("infer_extend", req.model_dump(), "ace_extend")


@app.post("/infer/ace_cover")
def infer_ace_cover(req: _AceStepCoverRequest):
    return _ace_call("infer_cover", req.model_dump(), "ace_cover")


@app.post("/infer/ace_vocal2bgm")
def infer_ace_vocal2bgm(req: _AceStepVocal2BGMRequest):
    return _ace_call("infer_vocal2bgm", req.model_dump(), "ace_vocal2bgm")


@app.post("/infer/ace_lyric2vocal")
def infer_ace_lyric2vocal(req: _AceStepGenRequest):
    return _ace_call("infer_lyric2vocal", req.model_dump(), "ace_lyric2vocal")


@app.post("/infer/ace_text2samples")
def infer_ace_text2samples(req: _AceStepGenRequest):
    return _ace_call("infer_text2samples", req.model_dump(), "ace_text2samples")


@app.post("/infer/ace_extract")
def infer_ace_extract(req: _AceStepLooseRequest):
    return _ace_call("infer_extract", req.model_dump(exclude_none=True), "ace_extract")


@app.post("/infer/ace_lego")
def infer_ace_lego(req: _AceStepLooseRequest):
    return _ace_call("infer_lego", req.model_dump(exclude_none=True), "ace_lego")


@app.post("/infer/ace_complete")
def infer_ace_complete(req: _AceStepLooseRequest):
    return _ace_call("infer_complete", req.model_dump(exclude_none=True), "ace_complete")


@app.post("/infer/ace_create_sample")
def infer_ace_create_sample(req: _AceStepLooseRequest):
    return _ace_call("infer_create_sample", req.model_dump(exclude_none=True), "ace_create_sample")


@app.post("/infer/ace_format_sample")
def infer_ace_format_sample(req: _AceStepLooseRequest):
    return _ace_call("infer_format_sample", req.model_dump(exclude_none=True), "ace_format_sample")


@app.post("/infer/ace_understand")
def infer_ace_understand(req: _AceStepLooseRequest):
    return _ace_call("infer_understand_music", req.model_dump(exclude_none=True), "ace_understand")


@app.post("/infer/ace_analyze")
def infer_ace_analyze(req: _AceStepAnalyzeRequest):
    """BPM / key / loudness analysis. Doesn't require model load — uses librosa."""
    state = _require_ace_step()
    from ace_step_loaders import analyze_audio
    try:
        return analyze_audio(state, req.audio_base64, req.sample_rate)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("ace_analyze failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--precision", default=None)
    parser.add_argument("--variant", default=None,
                       help="Variant ID from OMNI_MODEL_VARIANTS")
    parser.add_argument("--variant-weights-dir", default=None,
                       help="Directory name under models/omni/ for this variant")
    parser.add_argument("--lora", default=None,
                       help="Absolute path to LoRA adapter directory")
    args = parser.parse_args()

    _model_name = args.model
    _device = args.device
    _precision = args.precision
    _variant_id = args.variant
    _variant_weights_dir = args.variant_weights_dir
    _lora_path = args.lora
    _placement_config = _worker_placement_from_env()

    _inject_venv(_model_name)

    logger.info("Worker starting: model=%s port=%d device=%s precision=%s variant=%s lora=%s placement=%s pool=%s",
                _model_name, args.port, _device,
                _precision or "auto",
                _variant_id or "default",
                os.path.basename(_lora_path) if _lora_path else "none",
                _placement_config.get("mode", "single"),
                _placement_config.get("gpu_pool", [_device]))

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
