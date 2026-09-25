"""Memory-bounded MiniMax H3 execution across an Omni GPU pool.

These nodes keep one Comfy workflow while deliberately separating its heavy
phases.  Loader nodes normally stay cached for the lifetime of a prompt, which
means the text encoder, diffusion model, and both VAEs can all retain backing
memory at once.  H3 exceeds a 32 GiB WSL guest that way even when the compute
itself fits in GPU VRAM.

The staged nodes load one component directly on its requested logical device,
produce CPU-resident intermediates, and then unload and destroy the component
before returning.  No Comfy source files are patched, so the contract remains
owned by OmniBridge across Comfy updates.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .omni_multigpu import _device_options, _resolve_device


def _to_cpu(value: Any) -> Any:
    """Recursively detach tensor-like workflow values from a loaded model."""
    if isinstance(value, Mapping):
        return type(value)((key, _to_cpu(item)) for key, item in value.items())
    if isinstance(value, tuple):
        return tuple(_to_cpu(item) for item in value)
    if isinstance(value, list):
        return [_to_cpu(item) for item in value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        try:
            return type(value)(_to_cpu(item) for item in value)
        except TypeError:
            pass
    detach = getattr(value, "detach", None)
    to = getattr(value, "to", None)
    if callable(detach) and callable(to):
        try:
            return detach().to("cpu")
        except (RuntimeError, TypeError, AttributeError):
            pass
    if callable(to):
        try:
            return to("cpu")
        except (RuntimeError, TypeError, AttributeError):
            pass
    return value


def _unload_patcher(patcher) -> None:
    if patcher is None:
        return
    import comfy.model_management as model_management

    unload = getattr(model_management, "unload_model_and_clones", None)
    if callable(unload):
        try:
            unload(patcher, all_devices=True)
            return
        except TypeError:
            unload(patcher)
            return
        except (RuntimeError, AttributeError):
            logging.exception("Omni staged unload failed; falling back to global unload")
    model_management.unload_all_models()


def _discard_file_cache(paths: Sequence[str]) -> None:
    advice = getattr(os, "POSIX_FADV_DONTNEED", None)
    fadvise = getattr(os, "posix_fadvise", None)
    if advice is None or not callable(fadvise):
        return
    for path in paths:
        if not path:
            continue
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                fadvise(fd, 0, 0, advice)
            finally:
                os.close(fd)
        except OSError:
            logging.debug("Could not discard staged model cache for %s", path, exc_info=True)


def _release_memory(paths: Sequence[str] = ()) -> None:
    """Release Python, allocator, CUDA, and clean file-cache references."""
    import comfy.model_management as model_management

    gc.collect()
    try:
        model_management.soft_empty_cache(force=True)
    except TypeError:
        model_management.soft_empty_cache()

    try:
        import torch

        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                with torch.cuda.device(index):
                    torch.cuda.empty_cache()
    except (RuntimeError, AttributeError):
        logging.debug("CUDA cache release was incomplete", exc_info=True)

    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass
    _discard_file_cache(paths)


def _target_options() -> list[str]:
    return _device_options(include_cpu=True)


def _apply_spectrum_h3(
    model,
    *,
    enabled: bool,
    blend_weight: float,
    audio_blend_weight: float,
    offline_smoothing_replay: bool,
):
    """Apply the installed Spectrum H3 wrapper with memory-safe defaults."""
    if not enabled:
        return model

    import nodes as comfy_nodes

    spectrum_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("SpectrumApplyMiniMaxH3")
    if spectrum_cls is None:
        raise RuntimeError(
            "Spectrum H3 was requested but SpectrumApplyMiniMaxH3 is not installed"
        )
    return spectrum_cls().apply(
        model,
        True,
        float(blend_weight),
        1,       # degree
        0.10,    # ridge_lambda
        2.0,     # window_size
        0.75,    # flex_window
        1,       # warmup_steps
        1,       # tail_actual_steps
        8,       # max_history
        False,   # debug
        history_storage="system_ram",
        bootstrap_first_forecast=True,
        anchor_residual_feedback=False,
        selective_rollback_correction=False,
        offline_smoothing_replay=bool(offline_smoothing_replay),
        audio_blend_weight=float(audio_blend_weight),
        offline_archive_storage="system_ram",
    )[0]


def _apply_h3_lora(model, lora_name: str, strength: float):
    """Apply one model-only H3 LoRA and retain its loader until sampling ends."""
    clean_name = str(lora_name or "").strip()
    if not clean_name or clean_name.casefold() == "none" or float(strength) == 0.0:
        return model, None, ""

    import folder_paths
    import nodes as comfy_nodes

    lora_path = folder_paths.get_full_path_or_raise("loras", clean_name)
    loader_cls = getattr(comfy_nodes, "LoraLoaderModelOnly", None)
    if loader_cls is None:
        loader_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("LoraLoaderModelOnly")
    if loader_cls is None:
        raise RuntimeError("ComfyUI LoraLoaderModelOnly is required for staged H3 Turbo")
    loader = loader_cls()
    patched = loader.load_lora_model_only(model, clean_name, float(strength))[0]
    return patched, loader, lora_path


def _apply_h3_sigma_shift(model, shift_video: float, shift_audio: float):
    """Use Comfy core's native H3 video/audio schedule patch."""
    try:
        from comfy_extras.nodes_minimax_h3 import MiniMaxH3SigmaShift
    except ImportError as exc:
        raise RuntimeError(
            "This ComfyUI core does not expose MiniMaxH3SigmaShift; update core "
            "before using staged H3 Turbo sigma controls"
        ) from exc
    return MiniMaxH3SigmaShift.execute(
        model,
        float(shift_video),
        float(shift_audio),
    )[0]


def _apply_h3_sage_attention(model, mode: str, allow_compile: bool):
    """Apply KJNodes SageAttention only to the staged H3 diffusion model."""
    clean_mode = str(mode or "disabled").strip()
    if clean_mode == "disabled":
        return model

    import nodes as comfy_nodes

    patch_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("PathchSageAttentionKJ")
    if patch_cls is None:
        raise RuntimeError(
            "H3 SageAttention was requested but KJNodes PathchSageAttentionKJ "
            "is not installed"
        )
    return patch_cls().patch(
        model,
        clean_mode,
        bool(allow_compile),
    )[0]


def _apply_h3_low_vram_attention(model, enabled: bool, head_chunks: int):
    """Apply KJNodes' exact-math H3 attention chunking when requested."""
    if not bool(enabled):
        return model

    import nodes as comfy_nodes

    patch_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("MiniMaxLowVRAMAttention")
    if patch_cls is None:
        raise RuntimeError(
            "H3 low-VRAM attention was requested but KJNodes "
            "MiniMaxLowVRAMAttention is not installed"
        )
    chunks = max(1, min(56, int(head_chunks)))
    return patch_cls.execute(model, chunks)[0]


def _model_options_for_device(device: str) -> dict[str, Any]:
    import torch

    target = _resolve_device(device)
    if target is None:
        return {}
    return {
        "load_device": target,
        "offload_device": torch.device("cpu"),
    }


def _load_disk_vae(vae_name: str, device: str):
    """Load one disk-backed VAE directly on its target without a base clone."""
    import comfy.sd
    import comfy.utils
    import folder_paths

    path = folder_paths.get_full_path_or_raise("vae", vae_name)
    state_dict, metadata = comfy.utils.load_torch_file(path, return_metadata=True)
    target = _resolve_device(device)
    vae = comfy.sd.VAE(sd=state_dict, metadata=metadata, device=target)
    vae.throw_exception_if_invalid()
    vae.patcher.cached_patcher_init = (
        comfy.sd.load_vae_patcher,
        (path, metadata, target),
    )
    logging.info("Omni staged VAE %s targeted to %s", vae_name, vae.patcher.load_device)
    return vae, path


class OmniH3StageConditioning:
    """Encode an H3 prompt, return CPU intermediates, then discard the encoder."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "clip_name": (folder_paths.get_filename_list("text_encoders"),),
            "prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
            "width": ("INT", {"default": 608, "min": 32, "max": 16384, "step": 32}),
            "height": ("INT", {"default": 352, "min": 32, "max": 16384, "step": 32}),
            "length": ("INT", {"default": 124, "min": 5, "max": 3600, "step": 17}),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("conditioning", "latent")
    FUNCTION = "condition"
    CATEGORY = "OmniBridge/multigpu/staged"
    DESCRIPTION = (
        "Text-only H3 conditioning. Defaults are the verified 24 GB baseline "
        "(608x352, 124 frames, ~5.2s). 768x448 at 124 frames OOMs this 3090."
    )

    def condition(self, clip_name, prompt, width, height, length, device="primary"):
        import comfy.sd
        import folder_paths
        from comfy_extras.nodes_minimax_h3 import MiniMaxH3ImageToVideo

        clip_path = folder_paths.get_full_path_or_raise("text_encoders", clip_name)
        model_options = _model_options_for_device(device)
        clip = None
        patcher = None
        try:
            clip = comfy.sd.load_clip(
                ckpt_paths=[clip_path],
                embedding_directory=folder_paths.get_folder_paths("embeddings"),
                clip_type=comfy.sd.CLIPType.MINIMAX,
                model_options=model_options,
            )
            patcher = clip.patcher
            logging.info("Omni staged H3 text encoder targeted to %s", patcher.load_device)
            result = MiniMaxH3ImageToVideo.execute(
                clip=clip,
                vae=None,
                prompt=prompt,
                width=int(width),
                height=int(height),
                length=int(length),
            )
            conditioning = _to_cpu(result[0])
            latent = _to_cpu(result[1])
            return conditioning, latent
        finally:
            _unload_patcher(patcher)
            patcher = None
            clip = None
            _release_memory((clip_path,))


class OmniH3StageFL2VConditioning:
    """Encode projected H3 first/last-frame conditioning, then unload both models."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {
            "required": {
                "clip_name": (folder_paths.get_filename_list("text_encoders"),),
                "projection": (folder_paths.get_filename_list("clip_projections"),),
                "video_vae_name": (folder_paths.get_filename_list("vae"),),
                "prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                "width": ("INT", {"default": 608, "min": 32, "max": 16384, "step": 32}),
                "height": ("INT", {"default": 352, "min": 32, "max": 16384, "step": 32}),
                "length": ("INT", {"default": 124, "min": 5, "max": 3600, "step": 17}),
                "clip_device": (_target_options(),),
                "vae_device": (_target_options(),),
            },
            "optional": {
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("conditioning", "latent")
    FUNCTION = "condition"
    CATEGORY = "OmniBridge/multigpu/staged"
    DESCRIPTION = (
        "Loads a ClipProj encoder and the H3 video VAE on independently selected "
        "devices, creates FL2V conditioning, moves results to CPU, and unloads "
        "both components before diffusion sampling."
    )

    def condition(
        self,
        clip_name,
        projection,
        video_vae_name,
        prompt,
        width,
        height,
        length,
        clip_device="auxiliary:1",
        vae_device="primary",
        first_frame=None,
        last_frame=None,
    ):
        import comfy.model_management as mm
        import folder_paths
        import nodes as comfy_nodes
        from comfy_extras.nodes_minimax_h3 import MiniMaxH3ImageToVideo

        loader_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("ClipProjLoader")
        free_cls = comfy_nodes.NODE_CLASS_MAPPINGS.get("ClipProjFree")
        if loader_cls is None or free_cls is None:
            raise RuntimeError(
                "ComfyUI-ClipProj with ClipProjLoader and ClipProjFree is required"
            )

        clip_path = folder_paths.get_full_path_or_raise("text_encoders", clip_name)
        projection_path = folder_paths.get_full_path_or_raise(
            "clip_projections", projection,
        )
        clip_target = _resolve_device(clip_device) or mm.get_torch_device()

        clip = None
        vae = None
        vae_patcher = None
        vae_path = None
        try:
            clip = loader_cls().load(
                clip_name=clip_name,
                type="auto",
                projection=projection,
                device=str(clip_target),
                mode="resident",
                unique_id=None,
            )[0]
            vae, vae_path = _load_disk_vae(video_vae_name, vae_device)
            vae_patcher = vae.patcher
            result = MiniMaxH3ImageToVideo.execute(
                clip=clip,
                vae=vae,
                prompt=prompt,
                width=int(width),
                height=int(height),
                length=int(length),
                first_frame=first_frame,
                last_frame=last_frame,
            )
            return _to_cpu(result[0]), _to_cpu(result[1])
        finally:
            try:
                free_cls().free("ClipProj encoders")
            except Exception:
                logging.exception("Failed to release the staged ClipProj encoder")
            clip = None
            _unload_patcher(vae_patcher)
            vae_patcher = None
            vae = None
            _release_memory((clip_path, projection_path, vae_path))


class OmniH3StageSampler:
    """Load, sample, and discard the H3 diffusion model in one workflow node."""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers
        import folder_paths

        return {"required": {
            "conditioning": ("CONDITIONING",),
            "latent": ("LATENT",),
            "unet_name": (folder_paths.get_filename_list("diffusion_models"),),
            "weight_dtype": (["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"],),
            "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            "sampler_name": (comfy.samplers.SAMPLER_NAMES,),
            "scheduler": (comfy.samplers.SCHEDULER_NAMES,),
            "steps": ("INT", {"default": 20, "min": 1, "max": 10000}),
            "denoise": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            "device": (_target_options(),),
        }, "optional": {
            "lora_name": (
                ["none"] + folder_paths.get_filename_list("loras"),
                {"default": "none"},
            ),
            "lora_strength": (
                "FLOAT", {"default": 1.0, "min": -100.0, "max": 100.0, "step": 0.01},
            ),
            "sigma_shift_video": (
                "FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01},
            ),
            "sigma_shift_audio": (
                "FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01},
            ),
            "low_vram_attention": (
                "BOOLEAN", {"default": False},
            ),
            "low_vram_head_chunks": (
                "INT", {"default": 4, "min": 1, "max": 56, "step": 1},
            ),
            "sage_attention": (
                [
                    "disabled",
                    "auto",
                    "sageattn_qk_int8_pv_fp16_cuda",
                    "sageattn_qk_int8_pv_fp16_triton",
                    "sageattn_qk_int8_pv_fp8_cuda",
                    "sageattn_qk_int8_pv_fp8_cuda++",
                ],
                {"default": "disabled"},
            ),
            "sage_allow_compile": ("BOOLEAN", {"default": False}),
            "spectrum_enabled": ("BOOLEAN", {"default": False}),
            "spectrum_blend_weight": (
                "FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01},
            ),
            "spectrum_audio_blend_weight": (
                "FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01},
            ),
            "spectrum_offline_smoothing_replay": ("BOOLEAN", {"default": True}),
        }}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "sample"
    CATEGORY = "OmniBridge/multigpu/staged"

    def sample(
        self,
        conditioning,
        latent,
        unet_name,
        weight_dtype,
        noise_seed,
        sampler_name,
        scheduler,
        steps,
        denoise,
        device="primary",
        lora_name="none",
        lora_strength=1.0,
        sigma_shift_video=12.0,
        sigma_shift_audio=3.0,
        low_vram_attention=False,
        low_vram_head_chunks=4,
        sage_attention="disabled",
        sage_allow_compile=False,
        spectrum_enabled=False,
        spectrum_blend_weight=0.5,
        spectrum_audio_blend_weight=0.0,
        spectrum_offline_smoothing_replay=True,
    ):
        import torch
        import comfy.sd
        import folder_paths
        from comfy_extras.nodes_custom_sampler import (
            BasicGuider,
            BasicScheduler,
            KSamplerSelect,
            RandomNoise,
            SamplerCustomAdvanced,
        )

        unet_path = folder_paths.get_full_path_or_raise("diffusion_models", unet_name)
        model_options = _model_options_for_device(device)
        if weight_dtype == "fp8_e4m3fn":
            model_options["dtype"] = torch.float8_e4m3fn
        elif weight_dtype == "fp8_e4m3fn_fast":
            model_options["dtype"] = torch.float8_e4m3fn
            model_options["fp8_optimizations"] = True
        elif weight_dtype == "fp8_e5m2":
            model_options["dtype"] = torch.float8_e5m2

        model = None
        patcher = None
        lora_loader = None
        lora_path = ""
        guider = None
        try:
            model = comfy.sd.load_diffusion_model(unet_path, model_options=model_options)
            model, lora_loader, lora_path = _apply_h3_lora(
                model,
                str(lora_name),
                float(lora_strength),
            )
            model = _apply_h3_sigma_shift(
                model,
                float(sigma_shift_video),
                float(sigma_shift_audio),
            )
            model = _apply_h3_low_vram_attention(
                model,
                bool(low_vram_attention),
                int(low_vram_head_chunks),
            )
            model = _apply_h3_sage_attention(
                model,
                str(sage_attention),
                bool(sage_allow_compile),
            )
            model = _apply_spectrum_h3(
                model,
                enabled=bool(spectrum_enabled),
                blend_weight=float(spectrum_blend_weight),
                audio_blend_weight=float(spectrum_audio_blend_weight),
                offline_smoothing_replay=bool(spectrum_offline_smoothing_replay),
            )
            patcher = model
            logging.info("Omni staged H3 diffusion model targeted to %s", model.load_device)
            noise = RandomNoise.execute(int(noise_seed))[0]
            guider = BasicGuider.execute(model, conditioning)[0]
            sampler = KSamplerSelect.execute(sampler_name)[0]
            sigmas = BasicScheduler.execute(model, scheduler, int(steps), float(denoise))[0]
            output = SamplerCustomAdvanced.execute(
                noise,
                guider,
                sampler,
                sigmas,
                latent,
            )[0]
            return (_to_cpu(output),)
        finally:
            guider = None
            model = None
            _unload_patcher(patcher)
            patcher = None
            lora_loader = None  # noqa: F841 - release the loader before cache cleanup
            _release_memory((unet_path, lora_path))


class OmniH3StageDecode:
    """Decode H3 video and audio sequentially on selected auxiliary devices."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "samples": ("LATENT",),
            "video_vae_name": (folder_paths.get_filename_list("vae"),),
            "audio_vae_name": (folder_paths.get_filename_list("vae"),),
            "video_device": (_target_options(),),
            "audio_device": (_target_options(),),
        }}

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "decode"
    CATEGORY = "OmniBridge/multigpu/staged"

    @staticmethod
    def _decode_video(samples, vae_name, device):
        import nodes

        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(vae_name, device)
            patcher = vae.patcher
            return _to_cpu(nodes.VAEDecode().decode(vae=vae, samples=samples)[0])
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))

    @staticmethod
    def _decode_audio(samples, vae_name, device):
        from comfy_extras.nodes_audio import VAEDecodeAudio

        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(vae_name, device)
            patcher = vae.patcher
            return _to_cpu(VAEDecodeAudio.execute(vae=vae, samples=samples)[0])
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))

    def decode(
        self,
        samples,
        video_vae_name,
        audio_vae_name,
        video_device="auxiliary:1",
        audio_device="auxiliary:1",
    ):
        images = self._decode_video(samples, video_vae_name, video_device)
        audio = self._decode_audio(samples, audio_vae_name, audio_device)
        return images, audio
