"""Stable Omni-owned wrappers around ComfyUI multi-GPU placement APIs.

The wrappers intentionally use capability checks at execution time. ComfyUI
may evolve its native node names, but Omni workflows keep these node IDs. A
runtime that cannot safely reload a component on another device raises a clear
error instead of mutating loader state and risking silent corruption.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import re

_AUX_RE = re.compile(r"^(?:aux|auxiliary):([1-9][0-9]*)$")
_GPU_RE = re.compile(r"^(?:cuda|gpu):([0-9]+)$")


def _configured_pool_size() -> int:
    try:
        value = json.loads(os.environ.get("OMNI_COMFY_GPU_POOL", "[]"))
        if isinstance(value, list) and value:
            return len(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        import torch
        return max(0, int(torch.cuda.device_count()))
    except Exception:
        return 0


def _device_options(*, include_cpu: bool = True) -> list[str]:
    options = ["default"]
    count = _configured_pool_size()
    if count:
        options.append("primary")
        options.extend(f"auxiliary:{index}" for index in range(1, count))
    if include_cpu:
        options.append("cpu")
    return options


def _resolve_device(option: str):
    import torch

    value = str(option or "default").strip().lower()
    if value == "default":
        return None
    if value == "cpu":
        return torch.device("cpu")
    if value == "primary":
        index = 0
    else:
        matched = _AUX_RE.fullmatch(value) or _GPU_RE.fullmatch(value)
        if not matched:
            raise RuntimeError(f"Unknown Omni GPU route: {option}")
        index = int(matched.group(1))
    count = int(torch.cuda.device_count())
    if index < 0 or index >= count:
        raise RuntimeError(
            f"Requested logical cuda:{index}, but this Comfy instance exposes {count} GPU(s)"
        )
    return torch.device(f"cuda:{index}")


def _remember_base_devices(patcher) -> None:
    model = patcher.model
    if not hasattr(model, "_omni_base_load_device"):
        model._omni_base_load_device = patcher.load_device
        model._omni_base_offload_device = patcher.offload_device


def _retarget_clip_reload_source(patcher, target):
    """Make a CLIP reload initialize off-device for a clean routed load."""
    if not getattr(patcher, "is_clip", False):
        return patcher
    cached = getattr(patcher, "cached_patcher_init", None)
    if not isinstance(cached, tuple) or len(cached) < 2:
        return patcher
    factory, factory_args, *factory_tail = cached
    if not isinstance(factory_args, (tuple, list)):
        return patcher
    try:
        parameter_names = list(inspect.signature(factory).parameters)
    except (TypeError, ValueError):
        return patcher
    options_name = (
        "te_model_options"
        if "te_model_options" in parameter_names
        else "model_options"
    )
    if options_name not in parameter_names:
        return patcher
    options_index = parameter_names.index(options_name)
    if options_index >= len(factory_args):
        return patcher
    options = factory_args[options_index]
    if not isinstance(options, dict):
        return patcher

    # Avoid Comfy eagerly registering the factory's temporary CLIP patcher.
    # If that patcher dies while its model is retained by the routed clone,
    # current_loaded_models keeps a dead entry and reports a memory leak.
    routed_options = dict(options)
    routed_options["load_device"] = target
    routed_options["offload_device"] = patcher.offload_device
    routed_options["initial_device"] = patcher.offload_device
    routed_args = list(factory_args)
    routed_args[options_index] = routed_options
    source = patcher.clone()
    source.cached_patcher_init = (
        factory,
        tuple(routed_args),
        *factory_tail,
    )
    return source


def _route_patcher(patcher, target):
    """Clone/reload a Comfy ModelPatcher onto *target* without mutating input."""
    _remember_base_devices(patcher)
    base_model = patcher.model
    base_load = base_model._omni_base_load_device
    base_offload = base_model._omni_base_offload_device
    target_load = base_load if target is None else target

    if getattr(target_load, "type", None) == "cpu":
        routed = patcher.clone()
        is_dynamic = getattr(routed, "is_dynamic", None)
        if callable(is_dynamic) and is_dynamic():
            try:
                routed = routed.clone(disable_dynamic=True)
            except (RuntimeError, TypeError) as exc:
                raise RuntimeError(
                    "This loader cannot create a safe CPU-routed clone"
                ) from exc
        routed.load_device = target_load
        routed.offload_device = target_load
        return routed

    # A no-op route must preserve the original patcher identity.  Cloning a
    # same-device patcher is unnecessary and can leave a downstream LoRA
    # clone outside the loader's normal unload/cache identity after /free.
    if patcher.load_device == target_load:
        return patcher
    else:
        reload_source = _retarget_clip_reload_source(patcher, target_load)
        deepclone = getattr(reload_source, "deepclone_multigpu", None)
        if not callable(deepclone):
            raise RuntimeError(
                "This Comfy loader does not expose safe multi-GPU reload support"
            )
        try:
            routed = deepclone(new_load_device=target_load)
        except (RuntimeError, TypeError, AttributeError) as exc:
            raise RuntimeError(
                "This model cannot be safely reloaded on the requested GPU"
            ) from exc
        if not hasattr(routed.model, "_omni_base_load_device"):
            routed.model._omni_base_load_device = base_load
            routed.model._omni_base_offload_device = base_offload

    routed.load_device = target_load
    routed.offload_device = base_offload
    register = getattr(routed, "register_load_device", None)
    if callable(register):
        register(target_load)
    return routed


def _route_audio_encoder_patcher(patcher, target):
    """Retarget a freshly loaded, CPU-offloaded audio encoder patcher."""
    _remember_base_devices(patcher)
    base_model = patcher.model
    base_load = base_model._omni_base_load_device
    base_offload = base_model._omni_base_offload_device
    target_load = base_load if target is None else target
    if patcher.load_device == target_load:
        return patcher
    if getattr(target_load, "type", None) == "cpu":
        return _route_patcher(patcher, target_load)

    # AudioEncoderLoader constructs a CoreModelPatcher around an offloaded,
    # immutable encoder. It has no diffusion deep-clone API, but cloning the
    # patcher before its first encode is sufficient: the graph transformer
    # rewires every consumer through this node, so the loader's original
    # wrapper is never executed concurrently on its default GPU.
    try:
        routed = patcher.clone()
    except (RuntimeError, TypeError, AttributeError) as exc:
        raise RuntimeError(
            "This audio encoder cannot be safely routed to another GPU"
        ) from exc
    routed.load_device = target_load
    routed.offload_device = base_offload
    register = getattr(routed, "register_load_device", None)
    if callable(register):
        register(target_load)
    return routed


class OmniRouteModel:
    """Place one diffusion model on the primary or an auxiliary GPU."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": ("MODEL",),
            "device": (_device_options(include_cpu=True),),
        }}

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "route"
    CATEGORY = "OmniBridge/multigpu"

    def route(self, model, device="default"):
        return (_route_patcher(model, _resolve_device(device)),)


class OmniRouteCLIP:
    """Place one CLIP/text encoder on the primary or an auxiliary GPU."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "clip": ("CLIP",),
            "device": (_device_options(include_cpu=True),),
        }}

    RETURN_TYPES = ("CLIP",)
    RETURN_NAMES = ("clip",)
    FUNCTION = "route"
    CATEGORY = "OmniBridge/multigpu"

    def route(self, clip, device="default"):
        routed = clip.clone()
        routed.patcher = _route_patcher(routed.patcher, _resolve_device(device))
        # CLIP.encode_from_tokens() executes through cond_stage_model rather
        # than through patcher.model.  A multi-GPU deep clone owns a distinct
        # model, so keep both references aligned; otherwise the original text
        # encoder remains live and encoding can silently run on its old GPU.
        routed.cond_stage_model = routed.patcher.model
        return (routed,)


class OmniRouteVAE:
    """Place one VAE on the primary or an auxiliary GPU."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "vae": ("VAE",),
            "device": (_device_options(include_cpu=True),),
        }}

    RETURN_TYPES = ("VAE",)
    RETURN_NAMES = ("vae",)
    FUNCTION = "route"
    CATEGORY = "OmniBridge/multigpu"

    def route(self, vae, device="default"):
        target = _resolve_device(device)
        routed = copy.copy(vae)
        routed.patcher = _route_patcher(vae.patcher, target)
        routed.first_stage_model = routed.patcher.model
        routed.device = routed.patcher.load_device
        return (routed,)


class OmniRouteAudioEncoder:
    """Place a Whisper/Wav2Vec audio encoder on a selected device."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "audio_encoder": ("AUDIO_ENCODER",),
            "device": (_device_options(include_cpu=True),),
        }}

    RETURN_TYPES = ("AUDIO_ENCODER",)
    RETURN_NAMES = ("audio_encoder",)
    FUNCTION = "route"
    CATEGORY = "OmniBridge/multigpu"

    def route(self, audio_encoder, device="default"):
        routed = copy.copy(audio_encoder)
        routed.patcher = _route_audio_encoder_patcher(
            audio_encoder.patcher,
            _resolve_device(device),
        )
        routed.model = routed.patcher.model
        routed.load_device = routed.patcher.load_device
        return (routed,)


class OmniMultiGPUWorkUnits:
    """Replicate a model to parallelize CFG work; this is not layer sharding."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "model": ("MODEL",),
            "max_gpus": ("INT", {
                "default": max(1, _configured_pool_size()),
                "min": 1,
                "max": 64,
                "step": 1,
            }),
        }}

    RETURN_TYPES = ("MODEL",)
    RETURN_NAMES = ("model",)
    FUNCTION = "configure"
    CATEGORY = "OmniBridge/multigpu"

    def configure(self, model, max_gpus=2):
        try:
            import comfy.multigpu
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError("This Comfy version has no multi-GPU work-unit API") from exc
        factory = getattr(comfy.multigpu, "create_multigpu_deepclones", None)
        if not callable(factory):
            raise RuntimeError("This Comfy version has no compatible multi-GPU clone API")
        visible = _configured_pool_size()
        if visible < 2 or int(max_gpus) < 2:
            logging.info("Omni MultiGPU Work Units: fewer than two GPUs selected; passing through")
            return (model,)
        return (factory(model, min(int(max_gpus), visible), reuse_loaded=True),)
