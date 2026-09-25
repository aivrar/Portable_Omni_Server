"""Deterministic, update-resistant GPU placement for Comfy API workflows.

The planner operates on API-format graphs and Omni's stable routing/staged
nodes.  It never patches ComfyUI itself and never loads a model.  Physical GPU
UUIDs are accepted for durable policies; a running instance's primary-first
GPU pool is translated to portable ``primary`` / ``auxiliary:N`` node inputs.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any


_WEIGHT_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}
_ROUTE_CLASS = {
    "model": "OmniRouteModel",
    "clip": "OmniRouteCLIP",
    "vae": "OmniRouteVAE",
    "audio_encoder": "OmniRouteAudioEncoder",
}
_ROUTE_INPUT = {
    "model": "model",
    "clip": "clip",
    "vae": "vae",
    "audio_encoder": "audio_encoder",
}
_DEFAULT_ESTIMATE_MB = {
    "model": 10240,
    "clip": 6144,
    "vae": 2048,
    "audio_encoder": 3072,
    "latent_upscaler": 2048,
    "model_patch": 256,
    "lora": 512,
}
_CATEGORY_CANDIDATES = {
    "model": ("diffusion_models", "unet", "checkpoints", "gguf"),
    "clip": ("text_encoders", "clip", "checkpoints"),
    "vae": ("vae", "checkpoints"),
    "audio_encoder": ("audio_encoders",),
    "latent_upscaler": ("latent_upscale_models",),
    "model_patch": ("model_patches",),
    "lora": ("loras",),
}


def _cpu_offload_budget_mb() -> int:
    """Return reclaim-aware RAM available for explicit low-VRAM offload."""
    fallback = max(0, int(os.environ.get("OMNI_COMFY_CPU_OFFLOAD_FALLBACK_MB", "8192")))
    reserve_mb = max(1024, int(os.environ.get("OMNI_COMFY_CPU_OFFLOAD_RESERVE_MB", "2048")))
    group = Path(
        os.environ.get(
            "OMNI_WORKLOAD_MEMORY_CGROUP",
            "/sys/fs/cgroup/memory/omni_studio/workloads",
        )
    )
    try:
        limit = int((group / "memory.limit_in_bytes").read_text().strip())
        usage = int((group / "memory.usage_in_bytes").read_text().strip())
        cache = 0
        for line in (group / "memory.stat").read_text().splitlines():
            key, _, raw = line.partition(" ")
            if key in {"cache", "total_cache"}:
                cache = max(cache, int(raw or 0))
        effective = max(0, usage - min(usage, cache))
        return max(0, int((limit - effective) / 1024 / 1024) - reserve_mb)
    except (OSError, ValueError):
        return fallback


def _host_model_budget_mb() -> int:
    """Return reclaim-aware RAM available for an ordinary mapped model stack."""
    reserve_mb = max(
        1024,
        int(os.environ.get("OMNI_COMFY_HOST_MODEL_RESERVE_MB", "4096")),
    )
    group = Path(
        os.environ.get(
            "OMNI_WORKLOAD_MEMORY_CGROUP",
            "/sys/fs/cgroup/memory/omni_studio/workloads",
        )
    )
    try:
        limit = int((group / "memory.limit_in_bytes").read_text().strip())
        usage = int((group / "memory.usage_in_bytes").read_text().strip())
        cache = 0
        for line in (group / "memory.stat").read_text().splitlines():
            key, _, raw = line.partition(" ")
            if key in {"cache", "total_cache"}:
                cache = max(cache, int(raw or 0))
        effective = max(0, usage - min(usage, cache))
        return max(0, int((limit - effective) / 1024 / 1024) - reserve_mb)
    except (OSError, ValueError):
        pass

    # cgroup v2 uses memory.max/current and reports reclaimable file pages in
    # memory.stat. "max" means the enclosing host is the applicable boundary.
    try:
        raw_limit = (group / "memory.max").read_text().strip()
        if raw_limit != "max":
            limit = int(raw_limit)
            usage = int((group / "memory.current").read_text().strip())
            cache = 0
            for line in (group / "memory.stat").read_text().splitlines():
                key, _, raw = line.partition(" ")
                if key in {"file", "file_mapped"}:
                    cache = max(cache, int(raw or 0))
            effective = max(0, usage - min(usage, cache))
            return max(0, int((limit - effective) / 1024 / 1024) - reserve_mb)
    except (OSError, ValueError):
        pass

    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, raw = line.partition(":")
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) // 1024
        available = min(values.get("MemTotal", 0), values.get("MemAvailable", 0))
        return max(0, available - reserve_mb)
    except (OSError, ValueError, IndexError):
        return max(0, int(os.environ.get("OMNI_COMFY_HOST_MODEL_FALLBACK_MB", "24576")))


def normalize_policy(raw: dict | None) -> dict:
    """Return a bounded, JSON-safe placement policy."""
    value = raw if isinstance(raw, dict) else {}
    mode = str(value.get("mode") or "auto").strip().lower()
    if mode not in {"single", "auto", "manual"}:
        raise ValueError("placement mode must be single, auto, or manual")

    eligible = _clean_string_list(value.get("eligible_devices"), 64)
    overrides = value.get("overrides") if isinstance(value.get("overrides"), dict) else {}
    if len(overrides) > 512:
        raise ValueError("placement overrides support at most 512 entries")
    clean_overrides: dict[str, str] = {}
    for raw_key, raw_target in overrides.items():
        key = str(raw_key or "").strip()
        target = str(raw_target or "").strip()
        if not key or not target or len(key) > 256 or len(target) > 160:
            raise ValueError("placement override keys or targets are invalid")
        clean_overrides[key] = target

    reserve_map = value.get("device_reserve_mb")
    reserve_map = reserve_map if isinstance(reserve_map, dict) else {}
    if len(reserve_map) > 64:
        raise ValueError("device_reserve_mb supports at most 64 entries")
    clean_reserve: dict[str, int] = {}
    for raw_key, raw_amount in reserve_map.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        clean_reserve[key] = _bounded_int(raw_amount, 0, 262144, "device reserve")

    primary = str(value.get("primary_device") or "").strip() or None
    return {
        "mode": mode,
        "eligible_devices": eligible,
        "primary_device": primary,
        "reserve_mb": _bounded_int(value.get("reserve_mb", 1024), 0, 262144, "reserve_mb"),
        "device_reserve_mb": clean_reserve,
        "overrides": clean_overrides,
        "require_all": bool(value.get("require_all", False)),
        "allow_cpu": bool(value.get("allow_cpu", False)),
    }


def _bounded_int(value: Any, minimum: int, maximum: int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _clean_string_list(value: Any, limit: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"eligible_devices must be a list of at most {limit} entries")
    result: list[str] = []
    for raw in value:
        item = str(raw or "").strip()
        if not item or len(item) > 160:
            raise ValueError("eligible device identifiers are invalid")
        if item not in result:
            result.append(item)
    return result


def _logical_target(logical_id: str) -> str:
    if logical_id == "cuda:0":
        return "primary"
    if logical_id.startswith("cuda:"):
        return f"auxiliary:{logical_id.split(':', 1)[1]}"
    return logical_id


def _device_aliases(device: dict) -> set[str]:
    return {
        str(value).strip().casefold()
        for value in (
            device.get("physical_id"), device.get("stable_id"), device.get("uuid"),
            device.get("logical_id"), device.get("target"),
        )
        if str(value or "").strip()
    }


def _resolve_device(devices: list[dict], requested: str | None) -> dict | None:
    wanted = str(requested or "").strip().casefold()
    if not wanted:
        return None
    return next((item for item in devices if wanted in _device_aliases(item)), None)


def instance_devices(instance: Any, detected_devices: list[dict], policy: dict) -> tuple[list[dict], list[str]]:
    """Describe CUDA devices visible inside one running Comfy instance."""
    warnings: list[str] = []
    rows = {
        str(item.get("id") or ""): item
        for item in detected_devices
        if isinstance(item, dict) and str(item.get("id") or "").startswith("cuda:")
    }
    if instance is None:
        return [], ["Start a ComfyUI instance before applying a GPU placement plan."]

    physical_pool = list(getattr(instance, "gpu_pool", None) or [])
    primary = str(getattr(instance, "device", "") or "")
    if not physical_pool and primary.startswith("cuda:"):
        physical_pool = [primary]
    mapping = dict(getattr(instance, "gpu_device_map", None) or {})
    if not mapping:
        mapping = {physical: f"cuda:{index}" for index, physical in enumerate(physical_pool)}

    devices: list[dict] = []
    instance_process = getattr(instance, "process", None)
    instance_pid = int(
        getattr(instance_process, "pid", 0)
        or getattr(instance, "pid", 0)
        or 0
    )
    for physical in physical_pool:
        source = rows.get(physical, {})
        logical = str(mapping.get(physical) or f"cuda:{len(devices)}")
        total = int(source.get("vram_total_mb") or 0)
        free = max(0, int(source.get("vram_free_mb", 0) or 0))
        stable = str(source.get("stable_id") or source.get("uuid") or physical)
        reserve = policy["device_reserve_mb"].get(
            stable,
            policy["device_reserve_mb"].get(physical, policy["reserve_mb"]),
        )
        compute_pids = {
            int(pid) for pid in (source.get("compute_pids") or [])
            if str(pid).isdigit()
        }
        # A matching Comfy PID does not prove its resident allocation can be
        # reused by this graph: it can be an irreducible CUDA context, a
        # different model, or a deep-cloned routed component. Treat current
        # free VRAM as the dependable budget. Warm reruns that are blocked can
        # explicitly unload first and then be analyzed again.
        instance_only_vram = bool(
            instance_pid and compute_pids and compute_pids == {instance_pid}
        )
        capacity = min(total or free, free)
        devices.append({
            "physical_id": physical,
            "uuid": source.get("uuid"),
            "stable_id": stable,
            "logical_id": logical,
            "target": _logical_target(logical),
            "name": str(source.get("name") or physical),
            "vram_total_mb": total,
            "vram_free_mb": free,
            "reserve_mb": reserve,
            "usable_mb": max(0, capacity - reserve),
            "compute_pids": sorted(compute_pids),
            "reusable_instance_vram": False,
            "instance_only_vram": instance_only_vram,
        })

    if any(item.get("instance_only_vram") for item in devices):
        warnings.append(
            "The selected Comfy instance already owns VRAM. Current-free "
            "accounting does not assume those allocations are reusable; "
            "explicitly unload and analyze again if a warm rerun is blocked."
        )

    if not devices and primary == "cpu" and policy.get("allow_cpu"):
        devices.append({
            "physical_id": "cpu", "uuid": None, "stable_id": "cpu",
            "logical_id": "cpu", "target": "cpu", "name": "CPU",
            "vram_total_mb": 0, "vram_free_mb": 0, "reserve_mb": 0,
            "usable_mb": _host_model_budget_mb(),
        })
    return devices, warnings


def _model_name(inputs: dict, preferred_fields: tuple[str, ...] = ()) -> str:
    for field in preferred_fields:
        value = inputs.get(field)
        if isinstance(value, str) and Path(value).suffix.lower() in _WEIGHT_SUFFIXES:
            return value.replace("\\", "/").strip("/")
    for field, value in inputs.items():
        if isinstance(value, str) and Path(value).suffix.lower() in _WEIGHT_SUFFIXES:
            lower = str(field).lower()
            if any(token in lower for token in ("name", "model", "ckpt", "unet", "clip", "vae")):
                return value.replace("\\", "/").strip("/")
    return ""


def _component(
    node_id: str,
    role: str,
    name: str,
    *,
    binding: dict,
    staged: bool = False,
    stage: int | None = None,
    estimate_fraction: float = 1.0,
    runtime_overhead_mb: int = 0,
    extra_weight_refs: list[dict[str, str]] | None = None,
) -> dict:
    return {
        "component_id": f"{node_id}:{role}",
        "node_id": node_id,
        "role": role,
        "model_name": name,
        "staged": staged,
        "stage": stage,
        "estimate_fraction": estimate_fraction,
        "runtime_overhead_mb": max(0, int(runtime_overhead_mb)),
        "extra_weight_refs": list(extra_weight_refs or []),
        "binding": binding,
    }


def _humo_runtime_overhead_mb(workflow: dict) -> int:
    """Estimate HuMo activation headroom from its requested video volume."""
    baseline = 480 * 480 * 49
    largest = 0
    for raw_node in workflow.values():
        if not isinstance(raw_node, dict):
            continue
        if str(raw_node.get("class_type") or "") != "WanHuMoImageToVideo":
            continue
        inputs = raw_node.get("inputs") if isinstance(raw_node.get("inputs"), dict) else {}
        try:
            width = max(16, int(inputs.get("width", 480)))
            height = max(16, int(inputs.get("height", 480)))
            length = max(1, int(inputs.get("length", 49)))
        except (TypeError, ValueError):
            width, height, length = 480, 480, 49
        largest = max(largest, width * height * length)
    if not largest:
        return 0
    # The 480-square, 49-frame qualification run needed roughly another
    # 2.5 GiB beyond weight-derived estimates during the first Wan conv.
    return max(512, int(round(2500 * (largest / baseline))))


def _ltx_decode_runtime_profile(
    workflow: dict, node_id: str, inputs: dict,
) -> tuple[int, dict | None]:
    """Estimate tiled LTX video-decode activations from its latent ancestry.

    VAE weight size alone badly underestimates full-temporal decode. The
    empirical envelope is calibrated to the qualified 1024x640x121 LTX 2.5
    graph: temporal 64 fits a 12 GiB device, while temporal 2048 (effectively
    all 121 frames) needs the 24 GiB device. This remains an admission estimate;
    it does not load weights or execute the graph.
    """
    by_id = {str(raw_id): raw_node for raw_id, raw_node in workflow.items()}
    pending: list[str] = []
    sample_link = inputs.get("samples")
    if isinstance(sample_link, (list, tuple)) and len(sample_link) >= 2:
        pending.append(str(sample_link[0]))

    seen: set[str] = set()
    width = height = length = batch_size = None
    spatial_scale = 1
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        raw_node = by_id.get(current)
        if not isinstance(raw_node, dict):
            continue
        cls = str(raw_node.get("class_type") or "")
        node_inputs = raw_node.get("inputs") if isinstance(raw_node.get("inputs"), dict) else {}
        if cls == "OmniLTXStageEmptyAVLatent":
            try:
                width = max(16, int(node_inputs.get("width", 0)))
                height = max(16, int(node_inputs.get("height", 0)))
                length_value = node_inputs.get("length", 0)
                if isinstance(length_value, (list, tuple)):
                    duration = by_id.get(str(length_value[0]), {})
                    if duration.get("class_type") == "OmniLTXStageDurationPredictor":
                        limits = duration.get("inputs", {})
                        # Round upward to the LTX eight-frame grid, including its initial frame.
                        length_value = int(float(limits.get("max_seconds", 20)) * float(limits.get("frame_rate", 24))) + 8
                    else:
                        # A dynamic upstream length has no proven smaller bound.
                        length_value = 16384
                length = max(1, int(length_value))
                batch_size = max(1, int(node_inputs.get("batch_size", 1)))
            except (TypeError, ValueError):
                width = height = length = batch_size = None
        elif cls == "OmniLTXStageSpatialRefine":
            upscaler = str(node_inputs.get("upscale_model_name") or "")
            matched = re.search(r"(?:^|[-_])x([2-9][0-9]*)(?:[-_.]|$)", upscaler, re.IGNORECASE)
            spatial_scale *= int(matched.group(1)) if matched else 2
        for value in node_inputs.values():
            if (
                isinstance(value, (list, tuple))
                and len(value) >= 2
                and str(value[0]) in by_id
                and str(value[0]) not in seen
            ):
                pending.append(str(value[0]))

    if not all(value is not None for value in (width, height, length, batch_size)):
        return 0, None
    width *= spatial_scale
    height *= spatial_scale
    try:
        temporal_size = max(8, int(inputs.get("temporal_size", 64)))
        temporal_overlap = max(0, int(inputs.get("temporal_overlap", 8)))
        tile_size = max(64, int(inputs.get("tile_size", 512)))
    except (TypeError, ValueError):
        temporal_size, temporal_overlap, tile_size = 64, 8, 512
    active_frames = min(length, temporal_size + temporal_overlap)
    area_scale = (width * height) / float(1024 * 640)
    # 1.5 GiB fixed workspace plus an empirical per-active-frame envelope.
    # Scale by output area and batch, retaining a small floor for tiny clips.
    overhead_mb = max(
        512,
        int(round((1536 + 58 * active_frames) * area_scale * batch_size)),
    )
    return overhead_mb, {
        "width": width,
        "height": height,
        "length": length,
        "batch_size": batch_size,
        "tile_size": tile_size,
        "temporal_size": temporal_size,
        "temporal_overlap": temporal_overlap,
        "active_frames": active_frames,
        "runtime_overhead_mb": overhead_mb,
    }


def _uses_humo_distilled_lora(workflow: dict) -> bool:
    has_humo = False
    has_distilled_lora = False
    for raw_node in workflow.values():
        if not isinstance(raw_node, dict):
            continue
        inputs = raw_node.get("inputs") if isinstance(raw_node.get("inputs"), dict) else {}
        unet_name = str(inputs.get("unet_name") or "").casefold()
        lora_name = str(inputs.get("lora_name") or "").casefold()
        has_humo = has_humo or "humo_" in unet_name
        has_distilled_lora = has_distilled_lora or (
            "lightx2v" in lora_name and "distill" in lora_name
        )
    return has_humo and has_distilled_lora


def discover_components(workflow: dict, object_info: dict | None = None) -> tuple[list[dict], list[str]]:
    """Find independently placeable model, encoder, and VAE components."""
    components: list[dict] = []
    warnings: list[str] = []
    humo_runtime_overhead = _humo_runtime_overhead_mb(workflow)
    object_info = object_info if isinstance(object_info, dict) else {}
    for raw_id, raw_node in workflow.items():
        if not isinstance(raw_node, dict):
            continue
        node_id = str(raw_id)
        cls = str(raw_node.get("class_type") or "")
        inputs = raw_node.get("inputs") if isinstance(raw_node.get("inputs"), dict) else {}
        if cls.startswith("OmniRoute"):
            continue
        if cls == "OmniH3StageConditioning":
            components.append(_component(
                node_id, "clip", str(inputs.get("clip_name") or ""), staged=True, stage=1,
                binding={"kind": "input", "field": "device"},
            ))
            continue
        if cls == "OmniLTXStageConditioning":
            components.append(_component(
                node_id, "clip", str(inputs.get("clip_name") or ""), staged=True, stage=1,
                binding={"kind": "input", "field": "device"},
            ))
            continue
        if cls == "OmniLTXStageDurationPredictor":
            components.append(_component(
                node_id, "model", str(inputs.get("unet_name") or ""), staged=True, stage=2,
                binding={"kind": "input", "field": "device"},
            ))
            components[-1]["component_id"] = f"{node_id}:model"
            components.append(_component(
                node_id, "model_patch", str(inputs.get("duration_head_name") or ""),
                staged=True, stage=2,
                binding={"kind": "input", "field": "device"},
            ))
            components[-1]["component_id"] = f"{node_id}:duration_head"
            continue
        if cls == "OmniLTXStageEmptyAVLatent":
            components.append(_component(
                node_id, "vae", str(inputs.get("audio_vae_name") or ""), staged=True, stage=2,
                binding={"kind": "input", "field": "device"},
            ))
            components[-1]["component_id"] = f"{node_id}:audio_vae"
            components[-1]["role"] = "audio_vae"
            continue
        if cls == "OmniLTXStageGuide":
            components.append(_component(
                node_id, "vae", str(inputs.get("video_vae_name") or ""), staged=True, stage=3,
                binding={"kind": "input", "field": "device"},
            ))
            components[-1]["component_id"] = f"{node_id}:video_vae"
            components[-1]["role"] = "video_vae"
            continue
        if cls == "OmniLTXStageSampler":
            components.append(_component(
                node_id, "model", str(inputs.get("unet_name") or ""), staged=True, stage=4,
                binding={"kind": "input", "field": "device"},
            ))
            continue
        if cls == "OmniLTXStageSpatialRefine":
            components.append(_component(
                node_id, "latent_upscaler", str(inputs.get("upscale_model_name") or ""),
                staged=True, stage=5,
                binding={"kind": "input", "field": "upscale_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:latent_upscaler"
            components.append(_component(
                node_id, "vae", str(inputs.get("video_vae_name") or ""), staged=True, stage=5,
                binding={"kind": "input", "field": "upscale_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:video_vae"
            components[-1]["role"] = "video_vae"
            components.append(_component(
                node_id, "model", str(inputs.get("unet_name") or ""), staged=True, stage=6,
                binding={"kind": "input", "field": "model_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:model"
            continue
        if cls == "OmniLTXStageAVDecode":
            decode_overhead_mb, activation_profile = _ltx_decode_runtime_profile(
                workflow, node_id, inputs,
            )
            components.append(_component(
                node_id, "vae", str(inputs.get("video_vae_name") or ""), staged=True, stage=7,
                binding={"kind": "input", "field": "video_device"},
                runtime_overhead_mb=decode_overhead_mb,
            ))
            components[-1]["component_id"] = f"{node_id}:video_vae"
            components[-1]["role"] = "video_vae"
            if activation_profile is not None:
                components[-1]["activation_profile"] = activation_profile
                if activation_profile["active_frames"] == activation_profile["length"]:
                    warnings.append(
                        f"{node_id}:video_vae decodes all {activation_profile['length']} frames "
                        "in one temporal tile; route it to a GPU with activation headroom."
                    )
            components.append(_component(
                node_id, "vae", str(inputs.get("audio_vae_name") or ""), staged=True, stage=8,
                binding={"kind": "input", "field": "audio_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:audio_vae"
            components[-1]["role"] = "audio_vae"
            continue
        if cls == "OmniH3StageFL2VConditioning":
            components.append(_component(
                node_id, "clip", str(inputs.get("clip_name") or ""), staged=True, stage=1,
                binding={"kind": "input", "field": "clip_device"},
            ))
            components.append(_component(
                node_id, "vae", str(inputs.get("video_vae_name") or ""), staged=True, stage=1,
                binding={"kind": "input", "field": "vae_device"},
            ))
            continue
        if cls == "OmniH3StageSampler":
            lora_name = str(inputs.get("lora_name") or "").strip()
            lora_refs = []
            if lora_name and lora_name.casefold() != "none":
                lora_refs.append({"role": "lora", "name": lora_name})
            components.append(_component(
                node_id, "model", str(inputs.get("unet_name") or ""), staged=True, stage=2,
                binding={"kind": "input", "field": "device"},
                extra_weight_refs=lora_refs,
            ))
            continue
        if cls == "OmniH3StageDecode":
            components.append(_component(
                node_id, "vae", str(inputs.get("video_vae_name") or ""), staged=True, stage=3,
                binding={"kind": "input", "field": "video_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:video_vae"
            components[-1]["role"] = "video_vae"
            components.append(_component(
                node_id, "vae", str(inputs.get("audio_vae_name") or ""), staged=True, stage=4,
                binding={"kind": "input", "field": "audio_device"},
            ))
            components[-1]["component_id"] = f"{node_id}:audio_vae"
            components[-1]["role"] = "audio_vae"
            continue
        if cls == "OmniStageVAEDecode":
            components.append(_component(
                node_id, "vae", str(inputs.get("vae_name") or ""), staged=True, stage=2,
                binding={"kind": "input", "field": "device"},
            ))
            continue

        known: list[tuple[str, int, str, float]] = []
        if cls in {"UNETLoader", "DiffusionModelLoader"}:
            known = [("model", 0, _model_name(inputs, ("unet_name", "model_name")), 1.0)]
        elif cls in {"CLIPLoader", "DualCLIPLoader", "TripleCLIPLoader", "QuadrupleCLIPLoader"}:
            names = [
                value for key, value in inputs.items()
                if "clip_name" in str(key) and isinstance(value, str)
            ]
            known = [("clip", 0, " + ".join(names) or _model_name(inputs), 1.0)]
        elif cls == "VAELoader":
            known = [("vae", 0, _model_name(inputs, ("vae_name",)), 1.0)]
        elif cls in {"CheckpointLoader", "CheckpointLoaderSimple"}:
            name = _model_name(inputs, ("ckpt_name",))
            known = [("model", 0, name, 0.75), ("clip", 1, name, 0.15), ("vae", 2, name, 0.10)]
        else:
            info = object_info.get(cls) if isinstance(object_info.get(cls), dict) else {}
            outputs = info.get("output") if isinstance(info.get("output"), list) else []
            consumes_component = any(
                str(field).casefold() in {"model", "clip", "vae"}
                and isinstance(value, list)
                and len(value) >= 2
                for field, value in inputs.items()
            )
            # Adapter/LoRA nodes often include "Loader" in their class name but
            # transform an upstream MODEL instead of loading an independently
            # placeable component. Routing them as a second model can move the
            # final diffusion path onto the wrong GPU.
            if "loader" in cls.casefold() and not consumes_component:
                name = _model_name(inputs)
                for slot, output_type in enumerate(outputs):
                    role = str(output_type or "").casefold()
                    if role in _ROUTE_CLASS:
                        known.append((role, slot, name, 1.0))

        for role, slot, name, fraction in known:
            if role == "audio_encoder" and "whisper_large_v3" in name.casefold():
                # Comfy discards the checkpoint's decoder weights and loads
                # only the encoder used for conditioning.
                fraction = 0.42
            components.append(_component(
                node_id, role, name,
                staged=False,
                stage=None,
                binding={"kind": "route", "output_slot": slot},
                estimate_fraction=fraction,
                runtime_overhead_mb=(
                    humo_runtime_overhead
                    if role == "model" and "humo_" in name.casefold()
                    else 0
                ),
            ))

    if not components:
        warnings.append("No independently placeable MODEL, CLIP, or VAE loaders were found in this API graph.")
    return components, warnings


def _single_model_size_mb(name: str, role: str, model_root: Path) -> int | None:
    clean = str(name or "").strip()
    if not clean or " + " in clean or ".." in Path(clean).parts:
        return None
    for category in _CATEGORY_CANDIDATES.get(role, ()):
        candidate = model_root / category / clean
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return max(1, (candidate.stat().st_size + (1024 * 1024 - 1)) // (1024 * 1024))
        except OSError:
            continue
    return None


def _workflow_weight_footprint_mb(workflow: dict, model_root: Path) -> int:
    """Count unique installed weight files referenced anywhere in the graph.

    Placement components intentionally exclude LoRA and model-patch nodes
    because those transform an upstream MODEL rather than accepting their own
    route. They still map weight files into host memory, so ordinary graphs
    need them included in the host-residency gate.
    """
    try:
        categories = [item for item in model_root.iterdir() if item.is_dir()]
    except OSError:
        return 0
    paths: set[Path] = set()
    for raw_node in workflow.values():
        inputs = raw_node.get("inputs") if isinstance(raw_node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            if not isinstance(value, str):
                continue
            clean = value.replace("\\", "/").strip("/")
            if not clean or ".." in Path(clean).parts:
                continue
            if Path(clean).suffix.casefold() not in _WEIGHT_SUFFIXES:
                continue
            for category in categories:
                candidate = category / clean
                try:
                    if candidate.is_file() and not candidate.is_symlink():
                        paths.add(candidate.resolve())
                except OSError:
                    continue
    total_bytes = 0
    for path in paths:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue
    return (total_bytes + (1024 * 1024 - 1)) // (1024 * 1024)


def _estimate_component(component: dict, model_root: Path) -> int:
    role = str(component.get("role") or "")
    base_role = "vae" if role.endswith("vae") else role
    names = str(component.get("model_name") or "").split(" + ")
    sizes = [_single_model_size_mb(name, base_role, model_root) for name in names]
    known_sizes = [value for value in sizes if value is not None]
    if known_sizes and len(known_sizes) == len(names):
        disk_mb = sum(known_sizes) * float(component.get("estimate_fraction") or 1.0)
        multiplier = {
            "model": 1.12,
            "clip": 1.15,
            "vae": 1.35,
            "audio_encoder": 1.15,
        }.get(base_role, 1.2)
        overhead = {
            "model": 1024,
            "clip": 768,
            "vae": 512,
            "audio_encoder": 512,
        }.get(base_role, 512)
        estimated = max(256, int(disk_mb * multiplier + overhead))
    else:
        estimated = _DEFAULT_ESTIMATE_MB.get(base_role, 2048)
    for ref in component.get("extra_weight_refs") or []:
        if not isinstance(ref, dict):
            continue
        extra_role = str(ref.get("role") or "")
        extra_name = str(ref.get("name") or "")
        extra_size = _single_model_size_mb(extra_name, extra_role, model_root)
        estimated += extra_size if extra_size is not None else _DEFAULT_ESTIMATE_MB.get(extra_role, 512)
    return estimated + max(0, int(component.get("runtime_overhead_mb") or 0))


def _override_for(component: dict, overrides: dict[str, str]) -> str | None:
    for key in (
        component["component_id"],
        f"{component['node_id']}:{component['role']}",
        component["node_id"],
        component.get("model_name"),
    ):
        if key and key in overrides:
            return overrides[key]
    return None


def build_placement_plan(
    workflow: dict,
    raw_policy: dict | None,
    instance: Any,
    detected_devices: list[dict],
    model_root: Path,
    object_info: dict | None = None,
) -> dict:
    """Plan component placement without mutating the workflow or loading weights."""
    policy = normalize_policy(raw_policy)
    visible, warnings = instance_devices(instance, detected_devices, policy)
    blockers: list[str] = []
    components, discovery_warnings = discover_components(workflow, object_info)
    warnings.extend(discovery_warnings)

    selected: list[dict] = []
    missing: list[str] = []
    if policy["eligible_devices"]:
        for requested in policy["eligible_devices"]:
            found = _resolve_device(visible, requested)
            if found and found not in selected:
                selected.append(found)
            elif not found:
                missing.append(requested)
    else:
        selected = list(visible)
    if missing:
        blockers.append(
            "The running Comfy instance cannot see requested device(s): " + ", ".join(missing)
            + ". Restart it with those GPUs in its pool."
        )
    if not selected:
        blockers.append("No eligible GPU is visible to the selected Comfy instance.")

    requested_primary = _resolve_device(selected, policy.get("primary_device"))
    if policy.get("primary_device") and requested_primary is None:
        blockers.append(f"Requested primary device is unavailable: {policy['primary_device']}")
    primary = requested_primary or (selected[0] if selected else None)
    if requested_primary and requested_primary.get("target") != "primary":
        warnings.append(
            f"{requested_primary['name']} is usable, but it is auxiliary in this running instance; "
            "restart Comfy with it selected first to make it the instance primary."
        )

    instance_vram_mode = str(getattr(instance, "vram_mode", "normal") or "normal")
    startup_options = getattr(instance, "startup_options", {})
    startup_options = startup_options if isinstance(startup_options, dict) else {}
    try:
        reserve_vram_gb = float(startup_options.get("reserve_vram") or 0)
    except (TypeError, ValueError):
        reserve_vram_gb = 0
    humo_memory_safe = (
        instance_vram_mode == "none"
        or (instance_vram_mode == "low" and reserve_vram_gb >= 9.0)
    )
    if (
        primary
        and _uses_humo_distilled_lora(workflow)
        and int(primary.get("vram_total_mb") or 0) <= 24576
        and not humo_memory_safe
    ):
        blockers.append(
            "HuMo 17B with the distilled LightX2V LoRA has an observed full-load OOM "
            "on a 24 GiB primary GPU. Restart this Comfy instance with vram_mode=low "
            "and startup_options.reserve_vram>=9, or use vram_mode=none, before running "
            "this graph."
        )

    # CPU is an explicit mixed-placement target only.  Keep it out of the
    # automatic candidate set whenever GPUs are available so `allow_cpu` does
    # not silently move a large model off GPU, but make `component -> cpu`
    # overrides resolvable for small auxiliaries such as a VAE.
    routable = list(selected)
    if policy.get("allow_cpu") and not any(
        item.get("physical_id") == "cpu" for item in routable
    ):
        routable.append({
            "physical_id": "cpu", "uuid": None, "stable_id": "cpu",
            "logical_id": "cpu", "target": "cpu", "name": "CPU",
            "vram_total_mb": 0, "vram_free_mb": 0, "reserve_mb": 0,
            "usable_mb": _host_model_budget_mb(), "compute_pids": [],
            "reusable_instance_vram": False,
        })
    automatic_candidates = [
        item for item in selected if item.get("physical_id") != "cpu"
    ] or list(selected)

    resident: dict[str, int] = {item["stable_id"]: 0 for item in routable}
    staged_peak: dict[str, int] = {item["stable_id"]: 0 for item in routable}
    staged_by_stage: dict[str, dict[tuple[str, int], int]] = {
        item["stable_id"]: {} for item in routable
    }
    used: set[str] = set()
    assignments: list[dict] = []
    low_vram_offload = instance_vram_mode in {"low", "none"}
    offload_peak_by_device: dict[str, int] = {
        item["stable_id"]: 0 for item in routable
    }
    cpu_offload_budget_mb = _cpu_offload_budget_mb() if low_vram_offload else 0
    override_keys = {
        key for item in components
        for key in (item["component_id"], f"{item['node_id']}:{item['role']}", item["node_id"], item.get("model_name"))
        if key
    }
    for key in policy["overrides"].keys() - override_keys:
        blockers.append(f"Unknown placement override: {key}")

    # One input field can express only one device, even when two weights use it.
    groups: dict[tuple, list[dict]] = {}
    for item in components:
        binding = item["binding"]
        key = (item["node_id"], binding["field"]) if binding["kind"] == "input" else (item["component_id"],)
        groups.setdefault(key, []).append(item)
    ordered = sorted(groups.values(), key=lambda members: sum(_estimate_component(item, model_root) for item in members), reverse=True)

    def projected(device: dict, component: dict, estimate: int) -> int:
        stable = device["stable_id"]
        if component.get("staged"):
            stage = (component["node_id"], int(component.get("stage") or 1))
            stage_total = staged_by_stage[stable].get(stage, 0) + estimate
            return resident[stable] + max(staged_peak[stable], stage_total)
        return resident[stable] + estimate + staged_peak[stable]

    for members in ordered:
        component = members[0]
        estimate = sum(_estimate_component(item, model_root) for item in members)
        requests = {_override_for(item, policy["overrides"]) for item in members} - {None}
        targets = {
            (_resolve_device(routable, request) or {}).get("stable_id", request)
            for request in requests
        }
        if len(targets) > 1:
            blockers.append(f"Conflicting overrides for shared device field at node {component['node_id']}")
            continue
        requested = next(iter(requests), None)
        chosen: dict | None = None
        reason = "automatic best fit"
        if requested:
            chosen = _resolve_device(routable, requested)
            reason = "manual override"
            if chosen is None:
                blockers.append(f"{component['component_id']} targets unavailable device {requested}.")
        elif policy["mode"] == "single":
            chosen = primary
            reason = "single GPU policy"
        else:
            fitting = [
                item for item in automatic_candidates
                if projected(item, component, estimate) <= item["usable_mb"]
            ]
            if fitting:
                if component["role"] == "model":
                    chosen = max(fitting, key=lambda item: (item["usable_mb"], item["stable_id"]))
                    reason = "largest eligible GPU for diffusion model"
                else:
                    chosen = min(fitting, key=lambda item: (item["usable_mb"], item["stable_id"]))
                    reason = "smallest eligible GPU with sufficient headroom"
            elif automatic_candidates:
                chosen = max(
                    automatic_candidates,
                    key=lambda item: (item["usable_mb"], item["stable_id"]),
                )
                if low_vram_offload:
                    reason = "low-VRAM best fit with bounded CPU offload"
                else:
                    blockers.append(
                        f"Estimated {estimate} MiB for {component['component_id']} exceeds current GPU headroom."
                    )

        if chosen is None:
            continue
        estimate_after = projected(chosen, component, estimate)
        overflow_mb = max(0, estimate_after - chosen["usable_mb"])
        activation_overhead_mb = max(
            0, sum(int(item.get("runtime_overhead_mb") or 0) for item in members),
        )
        if overflow_mb and activation_overhead_mb:
            blockers.append(
                f"Activation estimate for {component['component_id']} exceeds "
                f"{chosen['name']} headroom ({estimate_after} MiB needed, "
                f"{chosen['usable_mb']} MiB usable). Activation workspace is not "
                "CPU-offloadable; reduce temporal/spatial volume or route this stage "
                "to a larger GPU."
            )
        elif overflow_mb and low_vram_offload and chosen["physical_id"] != "cpu":
            offload_peak_by_device[chosen["stable_id"]] = max(
                offload_peak_by_device[chosen["stable_id"]], overflow_mb
            )
        elif overflow_mb and (requested or policy["mode"] == "single" or chosen["physical_id"] == "cpu"):
            prefix = "Manual target" if requested else "Single-GPU target"
            blockers.append(
                f"{prefix} {chosen['name']} lacks estimated headroom for {component['component_id']} "
                f"({estimate_after} MiB needed, {chosen['usable_mb']} MiB usable)."
            )
        stable = chosen["stable_id"]
        if component.get("staged"):
            stage = (component["node_id"], int(component.get("stage") or 1))
            staged_by_stage[stable][stage] = (
                staged_by_stage[stable].get(stage, 0) + estimate
            )
            staged_peak[stable] = max(staged_by_stage[stable].values())
        else:
            resident[stable] += estimate
        used.add(stable)
        for member in members:
            assignments.append({
                **member,
                "estimated_peak_mb": _estimate_component(member, model_root),
                "estimated_cpu_offload_mb": overflow_mb if low_vram_offload and chosen["physical_id"] != "cpu" else 0,
                "device": {
                    key: chosen.get(key) for key in (
                        "physical_id", "stable_id", "uuid", "logical_id", "target", "name"
                    )
                },
                "reason": reason,
            })

    estimated_cpu_offload_mb = sum(offload_peak_by_device.values())
    if estimated_cpu_offload_mb > cpu_offload_budget_mb:
        blockers.append(
            f"Estimated CPU offload {estimated_cpu_offload_mb} MiB exceeds the current "
            f"workload-cgroup budget of {cpu_offload_budget_mb} MiB."
        )
    elif estimated_cpu_offload_mb:
        warnings.append(
            f"Low-VRAM placement expects up to {estimated_cpu_offload_mb} MiB of CPU "
            "offload; the estimate is bounded by the current workload cgroup."
        )

    staged_nodes = {item["node_id"] for item in components if item.get("staged")}
    ordinary = {key: node for key, node in workflow.items() if str(key) not in staged_nodes}
    resident_weights = _workflow_weight_footprint_mb(ordinary, model_root)
    staged_weights = max((
        _workflow_weight_footprint_mb({key: node}, model_root)
        for key, node in workflow.items() if str(key) in staged_nodes
    ), default=0)
    host_weight_footprint_mb = resident_weights + staged_weights
    host_model_budget_mb = _host_model_budget_mb()
    cpu_peak = resident.get("cpu", 0) + staged_peak.get("cpu", 0)
    if cpu_peak + estimated_cpu_offload_mb > host_model_budget_mb:
        blockers.append(
            f"CPU components and offload need {cpu_peak + estimated_cpu_offload_mb} MiB "
            f"but the shared host budget is {host_model_budget_mb} MiB."
        )
    if host_weight_footprint_mb > host_model_budget_mb:
        blockers.append(
            f"Installed weights resident during this graph total "
            f"{host_weight_footprint_mb} MiB, exceeding the current host model "
            f"mapping budget of {host_model_budget_mb} MiB. Use staged loaders, "
            "a smaller compatible weight stack, or increase the distro/cgroup RAM limit."
        )

    if policy["require_all"]:
        unused = [item["name"] for item in selected if item["stable_id"] not in used]
        if unused:
            blockers.append("require_all is enabled but no component was assigned to: " + ", ".join(unused))

    assigned_ids = {item["component_id"] for item in assignments}
    missing_assignments = [item["component_id"] for item in components if item["component_id"] not in assigned_ids]
    if missing_assignments:
        blockers.append("Components without a valid assignment: " + ", ".join(missing_assignments))
    if "cpu" in used:
        warnings.append(
            "One or more components are explicitly routed to CPU; expect slower execution "
            "and higher system-RAM use."
        )
    if any(not item.get("staged") for item in components):
        warnings.append(
            "Ordinary Comfy loaders can remain resident together; routing places components but does not "
            "provide the strict unload-between-stages behavior of Omni staged nodes."
        )

    device_results = []
    for item in routable:
        stable = item["stable_id"]
        if item.get("physical_id") == "cpu" and stable not in used:
            continue
        device_results.append({
            **item,
            "estimated_resident_mb": resident[stable],
            "estimated_staged_peak_mb": staged_peak[stable],
            "estimated_total_peak_mb": resident[stable] + staged_peak[stable],
            "used": stable in used,
        })
    status = "ready" if not blockers else (
        "restart-required" if any("Restart" in item or "running Comfy instance" in item for item in blockers)
        else "blocked"
    )
    return {
        "status": status,
        "valid": not blockers,
        "policy": policy,
        "instance_id": getattr(instance, "instance_id", None),
        "devices": device_results,
        "components": sorted(assignments, key=lambda item: (item.get("stage") or 9999, item["component_id"])),
        "warnings": warnings,
        "blockers": blockers,
        "summary": {
            "component_count": len(components),
            "assigned_count": len(assignments),
            "eligible_gpu_count": len(selected),
            "used_gpu_count": sum(
                item["stable_id"] in used
                for item in selected
                if item.get("physical_id") != "cpu"
            ),
            "staged": bool(components) and all(item.get("staged") for item in components),
            "estimated_cpu_offload_mb": estimated_cpu_offload_mb,
            "cpu_offload_budget_mb": cpu_offload_budget_mb,
            "host_weight_footprint_mb": host_weight_footprint_mb,
            "host_model_budget_mb": host_model_budget_mb,
        },
    }


def _new_node_id(workflow: dict) -> str:
    numeric = [int(str(key)) for key in workflow if str(key).isdigit()]
    if len(numeric) == len(workflow):
        return str((max(numeric) if numeric else 0) + 1)
    index = 1
    while f"omni_place_{index}" in workflow:
        index += 1
    return f"omni_place_{index}"


def _existing_route(workflow: dict, source_id: str, slot: int, route_class: str) -> str | None:
    expected = [source_id, slot]
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or node.get("class_type") != route_class:
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        if expected in inputs.values():
            return str(node_id)
    return None


def _rewire_links(value: Any, source_id: str, slot: int, replacement: list, skip_node: str) -> Any:
    if isinstance(value, list):
        if len(value) == 2 and str(value[0]) == source_id and value[1] == slot:
            return list(replacement)
        return [_rewire_links(item, source_id, slot, replacement, skip_node) for item in value]
    if isinstance(value, dict):
        return {
            key: item if str(key) == skip_node else _rewire_links(item, source_id, slot, replacement, skip_node)
            for key, item in value.items()
        }
    return value


def apply_placement_plan(workflow: dict, plan: dict) -> tuple[dict, dict]:
    """Apply a valid plan to a copy of an API-format graph."""
    if not plan.get("valid"):
        raise ValueError("cannot apply an invalid GPU placement plan")
    patched = copy.deepcopy(workflow)
    inserted: list[str] = []
    updated: list[str] = []
    bound_targets: dict[tuple[str, str], str] = {}
    for assignment in plan.get("components") or []:
        node_id = str(assignment["node_id"])
        target = str((assignment.get("device") or {}).get("target") or "default")
        binding = assignment.get("binding") or {}
        node = patched.get(node_id)
        if not isinstance(node, dict):
            raise ValueError(f"placement node disappeared from workflow: {node_id}")
        if binding.get("kind") == "input":
            key = (node_id, str(binding["field"]))
            if key in bound_targets and bound_targets[key] != target:
                raise ValueError(f"conflicting targets for node input {key}")
            bound_targets[key] = target
            node.setdefault("inputs", {})[str(binding["field"])] = target
            updated.append(node_id)
            continue
        if binding.get("kind") != "route":
            continue
        role = str(assignment.get("role") or "")
        route_class = _ROUTE_CLASS.get(role)
        input_name = _ROUTE_INPUT.get(role)
        if not route_class or not input_name:
            continue
        slot = int(binding.get("output_slot") or 0)
        route_id = _existing_route(patched, node_id, slot, route_class)
        if route_id is None:
            route_id = _new_node_id(patched)
            patched[route_id] = {
                "inputs": {input_name: [node_id, slot], "device": target},
                "class_type": route_class,
                "_meta": {"title": f"Omni placement: {assignment['component_id']}"},
            }
            inserted.append(route_id)
        else:
            patched[route_id].setdefault("inputs", {})["device"] = target
            updated.append(route_id)
        patched = _rewire_links(patched, node_id, slot, [route_id, 0], route_id)

    return patched, {
        "inserted_node_ids": inserted,
        "updated_node_ids": sorted(set(updated)),
        "inserted_count": len(inserted),
        "updated_count": len(set(updated)),
    }
