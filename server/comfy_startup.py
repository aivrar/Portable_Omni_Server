"""Safe, UI-facing ComfyUI startup option catalog.

The gateway owns networking, ports, directories, device isolation, and the
ComfyUI checkout.  This module exposes the remaining useful runtime switches
as structured values and converts them to a strictly allowlisted argv.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def _choice(value: str, label: str, args: list[str] | None = None) -> dict:
    return {"value": value, "label": label, "args": args or []}


_GROUPS = [
    {
        "name": "Memory & caching",
        "options": [
            {
                "key": "cache_policy", "label": "Cache policy", "kind": "select",
                "default": "auto", "description": "Choose how node results are cached.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("classic", "Classic / aggressive", ["--cache-classic"]),
                    _choice("lru_10", "LRU - 10 results", ["--cache-lru", "10"]),
                    _choice("lru_50", "LRU - 50 results", ["--cache-lru", "50"]),
                    _choice("lru_100", "LRU - 100 results", ["--cache-lru", "100"]),
                    _choice("ram_auto", "RAM pressure - automatic", ["--cache-ram"]),
                    _choice("ram_4", "RAM pressure - 4 GB headroom", ["--cache-ram", "4"]),
                    _choice("ram_8", "RAM pressure - 8 GB headroom", ["--cache-ram", "8"]),
                    _choice("none", "No cache", ["--cache-none"]),
                ],
            },
            {
                "key": "reserve_vram", "label": "Reserve VRAM (GB)", "kind": "number",
                "default": None, "min": 0, "max": 128, "step": 0.25,
                "flag": "--reserve-vram",
                "description": "Leave this much GPU memory available to the OS and other apps.",
            },
            {
                "key": "async_offload", "label": "Async weight offload", "kind": "select",
                "default": "auto", "description": "Control asynchronous model offloading.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("enabled", "Enabled", ["--async-offload"]),
                    _choice("disabled", "Disabled", ["--disable-async-offload"]),
                ],
            },
            {
                "key": "dynamic_vram", "label": "Dynamic VRAM", "kind": "select",
                "default": "auto", "description": "Override ComfyUI's platform default.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("enabled", "Enabled", ["--enable-dynamic-vram"]),
                    _choice("disabled", "Disabled", ["--disable-dynamic-vram"]),
                ],
            },
            {
                "key": "mmap_mode", "label": "Model file mmap", "kind": "select",
                "default": "auto", "description": "Control memory-mapped checkpoint loading.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("torch", "mmap Torch files", ["--mmap-torch-files"]),
                    _choice("disabled", "Disable safetensors mmap", ["--disable-mmap"]),
                ],
            },
            {
                "key": "disable_smart_memory", "label": "Aggressive RAM offload",
                "kind": "boolean", "default": False, "flag": "--disable-smart-memory",
                "description": "Offload models to regular RAM instead of retaining them in VRAM.",
            },
        ],
    },
    {
        "name": "Component precision",
        "options": [
            {
                "key": "unet_precision", "label": "Diffusion model / UNet", "kind": "select",
                "default": "inherit", "description": "Override the main precision preset for the diffusion model.",
                "choices": [
                    _choice("inherit", "Inherit preset"),
                    _choice("fp64", "fp64", ["--fp64-unet"]),
                    _choice("fp32", "fp32", ["--fp32-unet"]),
                    _choice("bf16", "bf16", ["--bf16-unet"]),
                    _choice("fp16", "fp16", ["--fp16-unet"]),
                    _choice("fp8_e4m3fn", "fp8 e4m3fn", ["--fp8_e4m3fn-unet"]),
                    _choice("fp8_e5m2", "fp8 e5m2", ["--fp8_e5m2-unet"]),
                    _choice("fp8_e8m0fnu", "fp8 e8m0fnu", ["--fp8_e8m0fnu-unet"]),
                ],
            },
            {
                "key": "vae_precision", "label": "VAE precision", "kind": "select",
                "default": "inherit", "description": "Override VAE precision.",
                "choices": [
                    _choice("inherit", "Inherit preset"),
                    _choice("fp32", "fp32", ["--fp32-vae"]),
                    _choice("bf16", "bf16", ["--bf16-vae"]),
                    _choice("fp16", "fp16 (may cause black images)", ["--fp16-vae"]),
                ],
            },
            {
                "key": "text_encoder_precision", "label": "Text encoder precision", "kind": "select",
                "default": "inherit", "description": "Override CLIP/text-encoder weight precision.",
                "choices": [
                    _choice("inherit", "Inherit preset"),
                    _choice("fp32", "fp32", ["--fp32-text-enc"]),
                    _choice("bf16", "bf16", ["--bf16-text-enc"]),
                    _choice("fp16", "fp16", ["--fp16-text-enc"]),
                    _choice("fp8_e4m3fn", "fp8 e4m3fn", ["--fp8_e4m3fn-text-enc"]),
                    _choice("fp8_e5m2", "fp8 e5m2", ["--fp8_e5m2-text-enc"]),
                ],
            },
            {
                "key": "cpu_vae", "label": "Run VAE on CPU", "kind": "boolean",
                "default": False, "flag": "--cpu-vae",
                "description": "Save VRAM by moving VAE execution to the CPU.",
            },
            {
                "key": "fp16_intermediates", "label": "fp16 intermediates (experimental)",
                "kind": "boolean", "default": False, "flag": "--fp16-intermediates",
                "description": "Use fp16 tensors between nodes instead of fp32.",
            },
            {
                "key": "supports_fp8_compute", "label": "Assume FP8 compute support",
                "kind": "boolean", "default": False, "flag": "--supports-fp8-compute",
                "description": "Advanced override; only enable when the device genuinely supports it.",
            },
        ],
    },
    {
        "name": "Attention & performance",
        "options": [
            {
                "key": "attention", "label": "Cross-attention backend", "kind": "select",
                "default": "auto", "description": "Force a supported attention implementation.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("split", "Split (--use-split-cross-attention)", ["--use-split-cross-attention"]),
                    _choice("quad", "Sub-quadratic (--use-quad-cross-attention)", ["--use-quad-cross-attention"]),
                    _choice("pytorch", "PyTorch (--use-pytorch-cross-attention)", ["--use-pytorch-cross-attention"]),
                    _choice("sage", "SageAttention (--use-sage-attention)", ["--use-sage-attention"]),
                    _choice("flash", "FlashAttention (--use-flash-attention)", ["--use-flash-attention"]),
                ],
            },
            {
                "key": "cuda_malloc", "label": "cudaMallocAsync", "kind": "select",
                "default": "auto", "description": "Override the CUDA allocator default.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("enabled", "Enabled", ["--cuda-malloc"]),
                    _choice("disabled", "Disabled", ["--disable-cuda-malloc"]),
                ],
            },
            {
                "key": "attention_upcast", "label": "Attention upcasting", "kind": "select",
                "default": "auto", "description": "Force or suppress attention upcasting.",
                "choices": [
                    _choice("auto", "Automatic"),
                    _choice("force", "Force upcast", ["--force-upcast-attention"]),
                    _choice("disabled", "Never upcast", ["--dont-upcast-attention"]),
                ],
            },
            {
                "key": "disable_xformers", "label": "Disable xFormers", "kind": "boolean",
                "default": False, "flag": "--disable-xformers",
                "description": "Use another attention implementation even when xFormers is installed.",
            },
            {
                "key": "enable_triton_backend", "label": "Enable Triton backend", "kind": "boolean",
                "default": False, "flag": "--enable-triton-backend",
                "description": "Enable the optional comfy-kitchen Triton backend.",
            },
            {
                "key": "force_channels_last", "label": "Channels-last tensors", "kind": "boolean",
                "default": False, "flag": "--force-channels-last",
                "description": "Force channels-last inference format.",
            },
            {
                "key": "force_non_blocking", "label": "Force non-blocking operations", "kind": "boolean",
                "default": False, "flag": "--force-non-blocking",
                "description": "May improve performance on some non-NVIDIA systems.",
            },
            {
                "key": "fast", "label": "All experimental fast features", "kind": "boolean",
                "default": False, "flag": "--fast",
                "description": "Untested optimizations may reduce quality or crash ComfyUI.",
            },
            {
                "key": "deterministic", "label": "Deterministic algorithms", "kind": "boolean",
                "default": False, "flag": "--deterministic",
                "description": "Prefer slower deterministic PyTorch algorithms where available.",
            },
        ],
    },
    {
        "name": "Server behavior",
        "options": [
            {
                "key": "preview_size", "label": "Preview maximum size", "kind": "integer",
                "default": 512, "min": 64, "max": 4096, "step": 64,
                "flag": "--preview-size", "omit_default": True,
                "description": "Maximum sampler preview dimension in pixels.",
            },
            {
                "key": "max_upload_size", "label": "Maximum upload (MB)", "kind": "number",
                "default": 100, "min": 1, "max": 4096, "step": 1,
                "flag": "--max-upload-size", "omit_default": True,
                "description": "Maximum request upload size accepted by ComfyUI.",
            },
            {
                "key": "hashing", "label": "File hashing", "kind": "select",
                "default": "sha256", "description": "Hash used for duplicate file/content comparison.",
                "choices": [
                    _choice("sha256", "SHA-256"),
                    _choice("sha512", "SHA-512", ["--default-hashing-function", "sha512"]),
                    _choice("sha1", "SHA-1", ["--default-hashing-function", "sha1"]),
                    _choice("md5", "MD5", ["--default-hashing-function", "md5"]),
                ],
            },
            {
                "key": "manager_ui", "label": "ComfyUI-Manager UI", "kind": "select",
                "default": "default", "description": "Choose the Manager interface mode.",
                "choices": [
                    _choice("default", "Default"),
                    _choice("disabled", "Disable UI/endpoints", ["--disable-manager-ui"]),
                    _choice("legacy", "Legacy UI", ["--enable-manager-legacy-ui"]),
                ],
            },
            {
                "key": "verbose", "label": "Log level", "kind": "select",
                "default": "INFO", "description": "ComfyUI process logging verbosity.",
                "choices": [
                    _choice("DEBUG", "Debug", ["--verbose", "DEBUG"]),
                    _choice("INFO", "Info"),
                    _choice("WARNING", "Warning", ["--verbose", "WARNING"]),
                    _choice("ERROR", "Error", ["--verbose", "ERROR"]),
                    _choice("CRITICAL", "Critical", ["--verbose", "CRITICAL"]),
                ],
            },
            {
                "key": "disable_metadata", "label": "Do not save prompt metadata", "kind": "boolean",
                "default": False, "flag": "--disable-metadata",
                "description": "Exclude workflow/prompt metadata from generated files.",
            },
            {
                "key": "disable_all_custom_nodes", "label": "Disable all custom nodes", "kind": "boolean",
                "default": False, "flag": "--disable-all-custom-nodes",
                "description": "Starts without custom nodes, including OmniBridge and Manager.",
            },
            {
                "key": "disable_api_nodes", "label": "Disable API nodes", "kind": "boolean",
                "default": False, "flag": "--disable-api-nodes",
                "description": "Also prevents the ComfyUI frontend from communicating with the internet.",
            },
            {
                "key": "multi_user", "label": "Multi-user storage", "kind": "boolean",
                "default": False, "flag": "--multi-user",
                "description": "Enable per-user ComfyUI storage.",
            },
            {
                "key": "enable_assets", "label": "Enable ComfyUI assets system", "kind": "boolean",
                "default": False, "flag": "--enable-assets",
                "description": "Enable asset API routes, synchronization, and background scanning.",
            },
            {
                "key": "compress_responses", "label": "Compress response bodies", "kind": "boolean",
                "default": False, "flag": "--enable-compress-response-body",
                "description": "Compress HTTP response bodies from the ComfyUI instance.",
            },
        ],
    },
]


def startup_catalog() -> list[dict]:
    """Return the public catalog without internal argv implementation details."""
    groups = deepcopy(_GROUPS)
    for group in groups:
        for option in group["options"]:
            option.pop("flag", None)
            option.pop("omit_default", None)
            for choice in option.get("choices", []):
                choice.pop("args", None)
    return groups


def _option_index() -> dict[str, dict]:
    return {
        option["key"]: option
        for group in _GROUPS
        for option in group["options"]
    }


def build_startup_args(options: dict[str, Any] | None) -> tuple[list[str], dict[str, Any]]:
    """Validate structured startup options and return ``(argv, normalized)``."""
    raw = options or {}
    if not isinstance(raw, dict):
        raise ValueError("startup_options must be an object")
    specs = _option_index()
    unknown = sorted(set(raw) - set(specs))
    if unknown:
        raise ValueError(f"Unknown ComfyUI startup option(s): {', '.join(unknown)}")

    argv: list[str] = []
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        spec = specs[key]
        kind = spec["kind"]
        if kind == "boolean":
            if type(value) is not bool:
                raise ValueError(f"{key} must be true or false")
            normalized[key] = value
            if value:
                argv.append(spec["flag"])
            continue
        if kind == "select":
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            choices = {choice["value"]: choice for choice in spec["choices"]}
            if value not in choices:
                raise ValueError(f"Invalid {key}: {value}")
            normalized[key] = value
            argv.extend(choices[value].get("args") or [])
            continue
        if kind in {"integer", "number"}:
            if value is None:
                normalized[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be numeric")
            if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"{key} must be an integer")
            if value < spec["min"] or value > spec["max"]:
                raise ValueError(f"{key} must be between {spec['min']} and {spec['max']}")
            normalized[key] = value
            if not (spec.get("omit_default") and value == spec.get("default")):
                argv.extend([spec["flag"], str(value)])
            continue
        raise ValueError(f"Unsupported startup option kind: {kind}")
    return argv, normalized


COMPONENT_PRECISION_KEYS = frozenset({
    "unet_precision", "vae_precision", "text_encoder_precision",
})
