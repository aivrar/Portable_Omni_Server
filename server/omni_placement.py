"""No-weight placement planning for standalone Omni model workers."""

from __future__ import annotations

import re
import os
from typing import Any

from config import ACE_STEP_MODELS, ACE_STEP_LMS, OMNI_MODEL_SETUP, get_variant


SHARDABLE_MODELS = {
    "qwen_omni_3b",
    "qwen_omni_7b",
    "anygpt",
    "qwen3_omni",
    "nemotron_nano_omni",
}
# One process, independently placed components (not Hugging Face layer shards).
# ACE-Step: DiT on the primary GPU, 5Hz LM on the first auxiliary GPU.
COMPONENT_SPLIT_MODELS = {
    "ace_step",
}
PLACEMENT_MODES = {"single", "auto", "manual"}


def ace_component_requirements(variant: str | None, lm_variant: str | None = None,
                               precision: str = "bf16") -> dict[str, int]:
    """Catalog admission estimates; DiT and LM cannot borrow each other's VRAM."""
    variant = variant or next(key for key, item in ACE_STEP_MODELS.items() if item.get("default"))
    info = ACE_STEP_MODELS.get(variant)
    if not info:
        raise ValueError(f"No ACE-Step capacity estimate for model {variant}")
    lm_variant = lm_variant or info.get("default_lm")
    lm = ACE_STEP_LMS.get(lm_variant) if lm_variant else None
    if lm_variant and not lm:
        raise ValueError(f"No ACE-Step capacity estimate for LM {lm_variant}")
    scale = 2 if precision == "fp32" else 1
    return {"dit": int(info["vram_gb"] * 1024 * scale),
            "lm": int(lm["vram_gb"] * 1024 * scale) if lm else 0}


def estimate_model_vram_mb(model: str, variant: str | None = None, precision: str | None = None) -> int | None:
    """Return the conservative documented VRAM estimate, without loading code."""
    variant_meta = get_variant(model, variant)
    model_meta = OMNI_MODEL_SETUP.get(model, {})
    raw = (variant_meta or {}).get("vram") or model_meta.get("vram")
    if not raw:
        return None
    values: list[int] = []
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(GB|MB)", str(raw), re.I):
        value = float(number)
        values.append(int(value * 1024) if unit.upper() == "GB" else int(value))
    # Catalog budgets describe the usual half-precision load. Scaling the full
    # estimate is deliberately conservative for FP32 activation/headroom too.
    return max(values) * (2 if precision == "fp32" else 1) if values else None


def _placement_dict(placement: Any) -> dict:
    if placement is None:
        return {}
    if isinstance(placement, dict):
        return dict(placement)
    if hasattr(placement, "model_dump"):
        return placement.model_dump(exclude_none=True)
    if hasattr(placement, "dict"):
        return placement.dict(exclude_none=True)
    raise TypeError("placement must be a mapping or typed request model")


def _cuda_records(devices: list[dict]) -> list[dict]:
    return [dict(item) for item in devices if str(item.get("id", "")).startswith("cuda:")]


def _resolve_record(identifier: str | None, records: list[dict]) -> dict | None:
    if identifier is None:
        return None
    wanted = str(identifier)
    for record in records:
        aliases = {
            str(record.get("id") or ""),
            str(record.get("uuid") or ""),
            str(record.get("stable_id") or ""),
        }
        if wanted in aliases:
            return record
    return None


def _resolve_keyed_mb(
    values: dict[str, int], records: list[dict], blockers: list[str], label: str,
) -> dict[str, int]:
    resolved: dict[str, int] = {}
    for key, raw_value in values.items():
        record = _resolve_record(str(key), records)
        if not record:
            blockers.append(f"Unknown {label} device: {key}")
            continue
        resolved[str(record["id"])] = max(0, int(raw_value))
    return resolved


def analyze_worker_placement(
    *,
    model: str,
    variant: str | None,
    legacy_device: str | None,
    placement: Any,
    devices: list[dict],
    managed_pids: set[int] | None = None,
    memory_state: dict | None = None,
    precision: str | None = None,
) -> dict:
    """Plan one worker against a current device snapshot without loading weights."""
    spec = _placement_dict(placement)
    mode = str(spec.get("mode") or "single").lower()
    blockers: list[str] = []
    warnings: list[str] = []
    if mode not in PLACEMENT_MODES:
        blockers.append(f"Unknown placement mode: {mode}")

    records = _cuda_records(devices)
    record_by_id = {str(record["id"]): record for record in records}
    requested = list(spec.get("eligible_devices") or [])
    primary_requested = spec.get("primary_device") or legacy_device
    if precision not in {None, "fp16", "bf16", "fp32"}:
        blockers.append("precision must be fp16, bf16, or fp32")
    effective_precision = precision or ("fp32" if primary_requested == "cpu" else "bf16")
    estimated_mb = estimate_model_vram_mb(model, variant, effective_precision)
    component_mb = {}
    if model == "ace_step":
        try:
            component_mb = ace_component_requirements(variant, precision=effective_precision)
            estimated_mb = sum(component_mb.values())
        except ValueError as exc:
            blockers.append(str(exc))
    reserve_mb = max(0, int(spec.get("reserve_mb", 1024)))
    require_all = bool(spec.get("require_all", False))
    allow_cpu = bool(spec.get("allow_cpu", False))
    cpu_memory_mb = max(0, int(spec.get("cpu_memory_mb", 0) or 0))
    memory = dict(memory_state or {})
    if not memory:
        limit_mb = max(1024, int(os.environ.get("OMNI_MEMORY_LIMIT_GB", "24")) * 1024)
        reserve_system_mb = max(1024, int(os.environ.get("OMNI_WORKER_CPU_RESERVE_MB", "4096")))
        memory = {
            "limit_mb": limit_mb,
            "usage_mb": 0,
            "reclaimable_cache_mb": 0,
            "reserve_mb": reserve_system_mb,
            "available_worker_mb": max(0, limit_mb - reserve_system_mb),
        }
    available_cpu_mb = max(0, int(memory.get("available_worker_mb", 0) or 0))
    for alert in memory.get("pressure_alerts") or []:
        warnings.append(f"Current system pressure: {alert}")
    if cpu_memory_mb > available_cpu_mb:
        blockers.append(
            f"CPU budget {cpu_memory_mb} MB exceeds the current safe app-cgroup "
            f"budget of {available_cpu_mb} MB"
        )

    if mode == "single" and str(primary_requested or "").lower() == "cpu":
        if cpu_memory_mb <= 0:
            blockers.append("CPU placement requires an explicit positive cpu_memory_mb budget")
        elif estimated_mb is not None and cpu_memory_mb < estimated_mb:
            blockers.append(
                f"Estimated requirement is {estimated_mb} MB but the CPU budget is "
                f"{cpu_memory_mb} MB"
            )
        warnings.append(
            "CPU placement is explicit and may consume substantial system RAM; "
            "worker deletion is required after use"
        )
        return {
            "valid": not blockers,
            "model": model,
            "variant": variant,
            "mode": mode,
            "estimated_vram_mb": estimated_mb,
            "primary_device": "cpu",
            "gpu_pool": [],
            "gpu_uuids": [],
            "gpu_device_map": {},
            "budgets_mb": {},
            "logical_max_memory_mb": {"cpu": cpu_memory_mb} if cpu_memory_mb else {},
            "hf_device_map": "cpu",
            "precision": effective_precision,
            "allow_cpu": True,
            "cpu_memory_mb": cpu_memory_mb,
            "memory_state": memory,
            "reserve_mb": reserve_mb,
            "external_compute_pids": {},
            "blockers": blockers,
            "warnings": warnings,
        }

    eligible: list[dict] = []
    if requested:
        for identifier in requested:
            record = _resolve_record(str(identifier), records)
            if not record:
                blockers.append(f"Unknown eligible GPU: {identifier}")
            elif record["id"] not in {item["id"] for item in eligible}:
                eligible.append(record)
    else:
        eligible = list(records)

    primary = _resolve_record(str(primary_requested), records) if primary_requested else None
    if primary_requested and not primary:
        blockers.append(f"Unknown primary GPU: {primary_requested}")
    if primary and primary["id"] not in {item["id"] for item in eligible}:
        blockers.append("primary_device must be included in eligible_devices")
    if not eligible:
        blockers.append("No eligible CUDA GPUs are currently detected")

    device_reserve = _resolve_keyed_mb(
        dict(spec.get("device_reserve_mb") or {}), records, blockers, "reserve",
    )
    memory_caps = _resolve_keyed_mb(
        dict(spec.get("max_memory_mb") or {}), records, blockers, "max-memory",
    )
    budgets: dict[str, int] = {}
    for record in eligible:
        device_id = str(record["id"])
        free_mb = max(0, int(record.get("vram_free_mb", 0) or 0))
        budget = max(0, free_mb - device_reserve.get(device_id, reserve_mb))
        if device_id in memory_caps:
            budget = min(budget, memory_caps[device_id])
        budgets[device_id] = budget

    if not primary and eligible:
        primary = max(eligible, key=lambda item: budgets.get(str(item["id"]), 0))

    selected: list[dict] = []
    if mode == "single":
        if primary:
            selected = [primary]
    elif mode == "manual":
        selected = ([primary] if primary else []) + [item for item in eligible if not primary or item["id"] != primary["id"]]
        if not spec.get("device_map"):
            blockers.append("manual placement requires a non-empty device_map")
    elif mode == "auto" and eligible:
        remaining = sorted(
            (item for item in eligible if not primary or item["id"] != primary["id"]),
            key=lambda item: budgets.get(str(item["id"]), 0),
            reverse=True,
        )
        if primary:
            selected.append(primary)
        if require_all or estimated_mb is None:
            selected.extend(remaining)
        else:
            for item in remaining:
                if sum(budgets.get(str(chosen["id"]), 0) for chosen in selected) >= estimated_mb:
                    break
                selected.append(item)

    if (
        mode != "single"
        and len(selected) > 1
        and model not in SHARDABLE_MODELS
        and model not in COMPONENT_SPLIT_MODELS
    ):
        blockers.append(f"{model} does not support standalone multi-GPU sharding")
    if mode != "single" and len(selected) > 1 and model in COMPONENT_SPLIT_MODELS:
        warnings.append(
            f"{model} places independent components on the selected GPUs; "
            "this is not Hugging Face layer sharding"
        )
    if model in COMPONENT_SPLIT_MODELS:
        if mode == "manual":
            blockers.append("ACE-Step uses DiT/LM device selection, not a manual layer device_map")
        if len(selected) > 2:
            blockers.append("ACE-Step uses at most two GPUs: primary DiT and auxiliary LM")
        component_devices = {}
        if selected and component_mb:
            component_devices = {"dit": str(selected[0]["id"]),
                                 "lm": str(selected[min(1, len(selected) - 1)]["id"])}
            per_device = {}
            for component, amount in component_mb.items():
                target = component_devices[component]
                per_device[target] = per_device.get(target, 0) + amount
            for target, amount in per_device.items():
                if budgets[target] < amount:
                    blockers.append(f"ACE-Step components on {target} require {amount} MB; current GPU budget is {budgets[target]} MB")
    if (
        mode == "auto"
        and len(selected) > 1
        and model in {"qwen_omni_3b", "qwen_omni_7b"}
        and "gptq" in str(variant or "").lower()
    ):
        blockers.append(
            "The current Qwen GPTQ backend cannot safely auto-dispatch quantized "
            "qweight tensors across multiple GPUs; use the base checkpoint with "
            "automatic placement or run the GPTQ checkpoint on one GPU"
        )

    gpu_budget_mb = sum(budgets.get(str(item["id"]), 0) for item in selected)
    total_budget_mb = gpu_budget_mb + (cpu_memory_mb if allow_cpu else 0)
    if estimated_mb is None:
        warnings.append("No documented VRAM estimate is available; capacity cannot be proven")
    elif total_budget_mb < estimated_mb:
        blockers.append(
            f"Estimated requirement is {estimated_mb} MB but the selected budget is "
            f"{total_budget_mb} MB"
        )
    if allow_cpu and cpu_memory_mb <= 0:
        blockers.append("allow_cpu requires an explicit positive cpu_memory_mb budget")

    if mode == "auto" and len(selected) > 1 and model in SHARDABLE_MODELS:
        warnings.append(
            "Automatic layer/expert sharding is loader-supported but remains "
            "model- and checkpoint-dependent; run one bounded verification first"
        )

    physical_ids = [str(item["id"]) for item in selected]
    physical_to_logical = {
        device_id: f"cuda:{index}" for index, device_id in enumerate(physical_ids)
    }
    logical_max_memory = {
        physical_to_logical[device_id]: budgets.get(device_id, 0)
        for device_id in physical_ids
    }
    if allow_cpu and cpu_memory_mb:
        logical_max_memory["cpu"] = cpu_memory_mb

    hf_device_map: str | dict = "cuda:0" if mode == "single" else "auto"
    if mode == "manual":
        hf_device_map = {}
        for module_name, target in dict(spec.get("device_map") or {}).items():
            resolved_target: str | None = None
            if isinstance(target, int):
                candidate = f"cuda:{target}"
                if candidate in physical_to_logical.values():
                    resolved_target = candidate
            elif str(target).lower() == "cpu":
                if allow_cpu:
                    resolved_target = "cpu"
                else:
                    blockers.append(f"Manual module {module_name} targets CPU without allow_cpu")
            else:
                record = _resolve_record(str(target), records)
                if record and str(record["id"]) in physical_to_logical:
                    resolved_target = physical_to_logical[str(record["id"])]
            if resolved_target is None:
                blockers.append(f"Invalid manual target for module {module_name}: {target}")
            else:
                hf_device_map[str(module_name)] = resolved_target
        warnings.append(
            "Manual module coverage is validated by the model loader; use exact Hugging Face module names"
        )
        used_targets = set(hf_device_map.values())
        used_budget = sum(value for target, value in logical_max_memory.items() if target in used_targets)
        if estimated_mb is not None and used_budget < estimated_mb:
            blockers.append(f"Manual targets provide {used_budget} MB for an estimated {estimated_mb} MB; unused eligible GPUs do not count")
        if require_all:
            unused = set(physical_to_logical.values()) - used_targets
            if unused:
                blockers.append("require_all has unused manual targets: " + ", ".join(sorted(unused)))

    managed = managed_pids or set()
    external: dict[str, list[int]] = {}
    for device_id in physical_ids:
        pids = sorted({
            int(pid) for pid in record_by_id[device_id].get("compute_pids", [])
            if int(pid) not in managed
        })
        if pids:
            external[device_id] = pids
            warnings.append(
                f"{device_id} currently has non-Omni compute PID(s) {pids}; "
                "free VRAM may change before or during load"
            )

    return {
        "valid": not blockers,
        "model": model,
        "variant": variant,
        "mode": mode,
        "estimated_vram_mb": estimated_mb,
        "component_requirements_mb": component_mb,
        "precision": effective_precision,
        "primary_device": physical_ids[0] if physical_ids else None,
        "gpu_pool": physical_ids,
        "gpu_uuids": [str(item.get("uuid") or "") for item in selected],
        "gpu_device_map": physical_to_logical,
        "budgets_mb": {device_id: budgets.get(device_id, 0) for device_id in physical_ids},
        "logical_max_memory_mb": logical_max_memory,
        "hf_device_map": hf_device_map,
        "allow_cpu": allow_cpu,
        "cpu_memory_mb": cpu_memory_mb,
        "memory_state": memory,
        "reserve_mb": reserve_mb,
        "external_compute_pids": external,
        "blockers": blockers,
        "warnings": warnings,
    }
