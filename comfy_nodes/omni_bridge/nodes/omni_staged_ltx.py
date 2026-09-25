"""Lossless staged prompt encoding for LTX audio-video workflows."""

from __future__ import annotations

from .omni_staged_h3 import (
    _load_disk_vae,
    _model_options_for_device,
    _release_memory,
    _target_options,
    _to_cpu,
    _unload_patcher,
)
from .omni_multigpu import _resolve_device


_DISTILLED_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"


class OmniLTXStageConditioning:
    """Encode both LTX prompts, retain every option, then unload the encoder."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "clip_name": (folder_paths.get_filename_list("text_encoders"),),
            "positive_prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
            "negative_prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
            "frame_rate": ("FLOAT", {"default": 24.0, "min": 0.0, "max": 1000.0, "step": 0.01}),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING")
    RETURN_NAMES = ("positive", "negative")
    FUNCTION = "condition"
    CATEGORY = "OmniBridge/multigpu/staged"

    def condition(
        self,
        clip_name,
        positive_prompt,
        negative_prompt,
        frame_rate=24.0,
        device="primary",
    ):
        import comfy.sd
        import folder_paths
        import node_helpers

        clip_path = folder_paths.get_full_path_or_raise("text_encoders", clip_name)
        clip = None
        patcher = None
        try:
            clip = comfy.sd.load_clip(
                ckpt_paths=[clip_path],
                embedding_directory=folder_paths.get_folder_paths("embeddings"),
                clip_type=comfy.sd.CLIPType.LTXV,
                model_options=_model_options_for_device(device),
            )
            patcher = clip.patcher
            positive = clip.encode_from_tokens_scheduled(clip.tokenize(positive_prompt))
            negative = clip.encode_from_tokens_scheduled(clip.tokenize(negative_prompt))
            values = {"frame_rate": float(frame_rate)}
            positive = node_helpers.conditioning_set_values(positive, values)
            negative = node_helpers.conditioning_set_values(negative, values)
            return _to_cpu(positive), _to_cpu(negative)
        finally:
            _unload_patcher(patcher)
            patcher = None
            clip = None
            _release_memory((clip_path,))


class OmniLTXStageDurationPredictor:
    """Predict a prompt's native shot length, then unload model and head."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "positive": ("CONDITIONING",),
            "unet_name": (folder_paths.get_filename_list("diffusion_models"),),
            "duration_head_name": (folder_paths.get_filename_list("model_patches"),),
            "weight_dtype": (["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"],),
            "frame_rate": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.01}),
            "min_seconds": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 120.0, "step": 0.1}),
            "max_seconds": ("FLOAT", {"default": 20.0, "min": 0.5, "max": 120.0, "step": 0.1}),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("INT", "FLOAT")
    RETURN_NAMES = ("num_frames", "seconds")
    FUNCTION = "predict"
    CATEGORY = "OmniBridge/multigpu/staged"

    def predict(self, positive, unet_name, duration_head_name, weight_dtype, frame_rate, min_seconds, max_seconds, device="primary"):
        import torch
        import comfy.ldm.lightricks.duration_head
        import comfy.model_management
        import comfy.sd
        import folder_paths
        from comfy_extras.nodes_model_patch import ModelPatchLoader

        unet_path = folder_paths.get_full_path_or_raise("diffusion_models", unet_name)
        head_path = folder_paths.get_full_path_or_raise("model_patches", duration_head_name)
        model_options = _model_options_for_device(device)
        if weight_dtype == "fp8_e4m3fn":
            model_options["dtype"] = torch.float8_e4m3fn
        elif weight_dtype == "fp8_e4m3fn_fast":
            model_options["dtype"] = torch.float8_e4m3fn
            model_options["fp8_optimizations"] = True
        elif weight_dtype == "fp8_e5m2":
            model_options["dtype"] = torch.float8_e5m2

        model = None
        duration_head = None
        try:
            model = comfy.sd.load_diffusion_model(unet_path, model_options=model_options)
            duration_head = ModelPatchLoader().load_model_patch(duration_head_name)[0]
            duration_head.load_device = _resolve_device(device)
            dm = model.model.diffusion_model
            head = duration_head.model
            if not isinstance(head, comfy.ldm.lightricks.duration_head.DurationHead):
                raise ValueError(f"{duration_head_name} is not an LTX duration head")

            context = positive[0][0]
            meta = positive[0][1]
            if context.shape[0] != 1:
                context = context[:1]
            comfy.model_management.load_models_gpu([model, duration_head])
            head = head.to(model.load_device)
            with torch.no_grad():
                context = context.to(
                    device=model.load_device,
                    dtype=model.model.get_dtype_inference(),
                )
                processed = dm.preprocess_text_embeds(
                    context,
                    unprocessed=meta.get("unprocessed_ltxav_embeds", False),
                )
                video_tokens = processed[..., :dm.cross_attention_dim].float()
                audio_tokens = processed[..., dm.cross_attention_dim:].float()
                seconds = float(head(video_tokens, audio_tokens)[0])
            frames = comfy.ldm.lightricks.duration_head.seconds_to_num_frames(
                seconds, float(frame_rate), float(min_seconds), float(max_seconds),
            )
            return int(frames), seconds
        finally:
            _unload_patcher(duration_head)
            duration_head = None
            _unload_patcher(model)
            model = None
            _release_memory((head_path, unet_path))


class OmniLTXStageEmptyAVLatent:
    """Build the LTX video/audio latent, then discard the audio VAE."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "audio_vae_name": (folder_paths.get_filename_list("vae"),),
            "width": ("INT", {"default": 960, "min": 64, "max": 16384, "step": 32}),
            "height": ("INT", {"default": 544, "min": 64, "max": 16384, "step": 32}),
            "length": ("INT", {"default": 121, "min": 1, "max": 16384, "step": 8}),
            "frame_rate": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 1000.0, "step": 0.01}),
            "batch_size": ("INT", {"default": 1, "min": 1, "max": 4096}),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "create"
    CATEGORY = "OmniBridge/multigpu/staged"

    def create(self, audio_vae_name, width, height, length, frame_rate, batch_size=1, device="auxiliary:1"):
        from comfy_extras.nodes_lt import EmptyLTXVLatentVideo, LTXVConcatAVLatent
        from comfy_extras.nodes_lt_audio import LTXVEmptyLatentAudio

        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(audio_vae_name, device)
            patcher = vae.patcher
            video = EmptyLTXVLatentVideo.execute(
                int(width), int(height), int(length), int(batch_size),
            )[0]
            audio = LTXVEmptyLatentAudio.execute(
                int(length), float(frame_rate), int(batch_size), vae,
            )[0]
            return (_to_cpu(LTXVConcatAVLatent.execute(video, audio)[0]),)
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))


class OmniLTXStageGuide:
    """Encode one LTX image/video guide, then unload its video VAE."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {
            "required": {
                "positive": ("CONDITIONING",),
                "negative": ("CONDITIONING",),
                "latent": ("LATENT",),
                "image": ("IMAGE",),
                "video_vae_name": (folder_paths.get_filename_list("vae"),),
                "frame_idx": ("INT", {"default": 0, "min": -9999, "max": 9999}),
                "strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.01}),
                "device": (_target_options(),),
            },
            "optional": {
                "attention_mask": ("MASK",),
                "iclora_parameters": ("IC_LORA_PARAMETERS",),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "negative", "latent")
    FUNCTION = "guide"
    CATEGORY = "OmniBridge/multigpu/staged"

    def guide(
        self,
        positive,
        negative,
        latent,
        image,
        video_vae_name,
        frame_idx=0,
        strength=1.0,
        device="auxiliary:1",
        attention_mask=None,
        iclora_parameters=None,
    ):
        from comfy_extras.nodes_lt import (
            LTXVAddGuide,
            LTXVConcatAVLatent,
            LTXVSeparateAVLatent,
        )

        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(video_vae_name, device)
            patcher = vae.patcher
            video_latent = latent
            audio_latent = None
            samples = latent.get("samples") if isinstance(latent, dict) else None
            if getattr(samples, "is_nested", False):
                video_latent, audio_latent = LTXVSeparateAVLatent.execute(latent)

            result = LTXVAddGuide.execute(
                positive,
                negative,
                vae,
                video_latent,
                image,
                int(frame_idx),
                float(strength),
                attention_mask,
                iclora_parameters,
            )
            guided_latent = result[2]
            if audio_latent is not None:
                guided_latent = LTXVConcatAVLatent.execute(guided_latent, audio_latent)[0]
            return _to_cpu(result[0]), _to_cpu(result[1]), _to_cpu(guided_latent)
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))


class OmniLTXStageSampler:
    """Load, sample, and discard one LTX diffusion model."""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers
        import folder_paths

        return {"required": {
            "positive": ("CONDITIONING",),
            "negative": ("CONDITIONING",),
            "latent": ("LATENT",),
            "unet_name": (folder_paths.get_filename_list("diffusion_models"),),
            "weight_dtype": (["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"],),
            "noise_seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1}),
            "sampler_name": (comfy.samplers.SAMPLER_NAMES,),
            "sigmas": ("STRING", {"default": _DISTILLED_SIGMAS}),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "sample"
    CATEGORY = "OmniBridge/multigpu/staged"

    @staticmethod
    def _crop_guides(result, positive, negative):
        """Remove appended video guide tokens without shortening AV audio."""
        from comfy_extras.nodes_lt import (
            LTXVConcatAVLatent,
            LTXVCropGuides,
            LTXVSeparateAVLatent,
        )

        video_latent = result
        audio_latent = None
        samples = result.get("samples") if isinstance(result, dict) else None
        if getattr(samples, "is_nested", False):
            video_latent, audio_latent = LTXVSeparateAVLatent.execute(result)
        cropped = LTXVCropGuides.execute(positive, negative, video_latent)[2]
        if audio_latent is not None:
            cropped = LTXVConcatAVLatent.execute(cropped, audio_latent)[0]
        return cropped

    def sample(self, positive, negative, latent, unet_name, weight_dtype, noise_seed, cfg, sampler_name, sigmas, device="primary"):
        import torch
        import comfy.sd
        import folder_paths
        from comfy_extras.nodes_custom_sampler import (
            CFGGuider, KSamplerSelect, ManualSigmas, RandomNoise, SamplerCustomAdvanced,
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
        guider = None
        try:
            model = comfy.sd.load_diffusion_model(unet_path, model_options=model_options)
            patcher = model
            noise = RandomNoise.execute(int(noise_seed))[0]
            guider = CFGGuider.execute(model, positive, negative, float(cfg))[0]
            sampler = KSamplerSelect.execute(sampler_name)[0]
            schedule = ManualSigmas.execute(sigmas)[0]
            result = SamplerCustomAdvanced.execute(noise, guider, sampler, schedule, latent)[0]
            result = self._crop_guides(result, positive, negative)
            return (_to_cpu(result),)
        finally:
            guider = None
            model = None
            _unload_patcher(patcher)
            patcher = None
            _release_memory((unet_path,))


class OmniLTXStageSpatialRefine:
    """Upscale/re-guide an LTX AV latent, then refine it with a dev model."""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers
        import folder_paths

        return {"required": {
            "samples": ("LATENT",),
            "positive": ("CONDITIONING",),
            "negative": ("CONDITIONING",),
            "upscale_model_name": (folder_paths.get_filename_list("latent_upscale_models"),),
            "video_vae_name": (folder_paths.get_filename_list("vae"),),
            "unet_name": (folder_paths.get_filename_list("diffusion_models"),),
            "weight_dtype": (["default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"],),
            "noise_seed": ("INT", {"default": 42, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
            "cfg": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 100.0, "step": 0.1}),
            "sampler_name": (comfy.samplers.SAMPLER_NAMES,),
            "sigmas": ("STRING", {"default": "0.85, 0.7250, 0.4219, 0.0"}),
            "upscale_device": (_target_options(),),
            "model_device": (_target_options(),),
        }}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("samples",)
    FUNCTION = "refine"
    CATEGORY = "OmniBridge/multigpu/staged"

    @staticmethod
    def _load_upscaler(model_name, device):
        import json
        import torch
        import comfy.model_management as mm
        import comfy.model_patcher
        import comfy.ops
        import comfy.utils
        import folder_paths
        from comfy.ldm.lightricks.latent_upsampler import LatentUpsampler

        path = folder_paths.get_full_path_or_raise("latent_upscale_models", model_name)
        sd, metadata = comfy.utils.load_torch_file(path, safe_load=True, return_metadata=True)
        if "post_upsample_res_blocks.0.conv2.bias" not in sd:
            raise RuntimeError(f"{model_name} is not an LTX latent spatial upscaler")
        config = json.loads(metadata["config"])
        model = LatentUpsampler.from_config(
            config, operations=comfy.ops.disable_weight_init,
        ).to(dtype=mm.vae_dtype(allowed_dtypes=[torch.bfloat16, torch.float32]))
        mm.archive_model_dtypes(model)
        patcher = comfy.model_patcher.CoreModelPatcher(
            model, load_device=_resolve_device(device), offload_device=torch.device("cpu"),
        )
        model.load_state_dict(sd, assign=patcher.is_dynamic())
        return patcher, path

    def refine(self, samples, positive, negative, upscale_model_name, video_vae_name, unet_name, weight_dtype, noise_seed, cfg, sampler_name, sigmas, upscale_device="auxiliary:1", model_device="primary"):
        import nodes
        import torch
        import comfy.sd
        import folder_paths
        from comfy_extras.nodes_custom_sampler import (
            CFGGuider, KSamplerSelect, ManualSigmas, RandomNoise, SamplerCustomAdvanced,
        )
        from comfy_extras.nodes_lt import (
            LTXVConcatAVLatent, LTXVImgToVideoInplace, LTXVSeparateAVLatent,
        )
        from comfy_extras.nodes_lt_upsampler import LTXVLatentUpsampler

        video, audio = LTXVSeparateAVLatent.execute(samples)[0:2]
        vae = None
        vae_patcher = None
        upscaler = None
        vae_path = ""
        upscaler_path = ""
        try:
            vae, vae_path = _load_disk_vae(video_vae_name, upscale_device)
            vae_patcher = vae.patcher
            upscaler, upscaler_path = self._load_upscaler(upscale_model_name, upscale_device)
            images = nodes.VAEDecodeTiled().decode(
                vae=vae, samples=video, tile_size=512, overlap=64,
                temporal_size=64, temporal_overlap=8,
            )[0]
            upscaled = LTXVLatentUpsampler.execute(video, upscaler, vae)[0]
            guided = LTXVImgToVideoInplace.execute(
                vae=vae, image=images, latent=upscaled, strength=1.0, bypass=False,
            )[0]
            refined_input = _to_cpu(LTXVConcatAVLatent.execute(guided, audio)[0])
        finally:
            _unload_patcher(upscaler)
            upscaler = None
            _unload_patcher(vae_patcher)
            vae_patcher = None
            vae = None
            _release_memory((upscaler_path, vae_path))

        unet_path = folder_paths.get_full_path_or_raise("diffusion_models", unet_name)
        model_options = _model_options_for_device(model_device)
        if weight_dtype == "fp8_e4m3fn":
            model_options["dtype"] = torch.float8_e4m3fn
        elif weight_dtype == "fp8_e4m3fn_fast":
            model_options["dtype"] = torch.float8_e4m3fn
            model_options["fp8_optimizations"] = True
        elif weight_dtype == "fp8_e5m2":
            model_options["dtype"] = torch.float8_e5m2
        model = None
        patcher = None
        guider = None
        try:
            model = comfy.sd.load_diffusion_model(unet_path, model_options=model_options)
            patcher = model
            guider = CFGGuider.execute(model, positive, negative, float(cfg))[0]
            output = SamplerCustomAdvanced.execute(
                RandomNoise.execute(int(noise_seed))[0], guider,
                KSamplerSelect.execute(sampler_name)[0], ManualSigmas.execute(sigmas)[0],
                refined_input,
            )[0]
            return (_to_cpu(output),)
        finally:
            guider = None
            model = None
            _unload_patcher(patcher)
            patcher = None
            _release_memory((unet_path,))


class OmniLTXStageAVDecode:
    """Decode LTX video and audio sequentially, unloading each VAE."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "samples": ("LATENT",),
            "video_vae_name": (folder_paths.get_filename_list("vae"),),
            "audio_vae_name": (folder_paths.get_filename_list("vae"),),
            "video_device": (_target_options(),),
            "audio_device": (_target_options(),),
            "tile_size": ("INT", {"default": 512, "min": 64, "max": 4096, "step": 32}),
            "overlap": ("INT", {"default": 64, "min": 0, "max": 4096, "step": 32}),
            "temporal_size": ("INT", {"default": 64, "min": 8, "max": 4096, "step": 4}),
            "temporal_overlap": ("INT", {"default": 8, "min": 4, "max": 4096, "step": 4}),
        }}

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "decode"
    CATEGORY = "OmniBridge/multigpu/staged"

    @staticmethod
    def _separate(samples):
        from comfy_extras.nodes_lt import LTXVSeparateAVLatent
        result = LTXVSeparateAVLatent.execute(samples)
        return result[0], result[1]

    @staticmethod
    def _decode_video(samples, vae_name, device, tile_size, overlap, temporal_size, temporal_overlap):
        import nodes

        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(vae_name, device)
            patcher = vae.patcher
            images = nodes.VAEDecodeTiled().decode(
                vae=vae, samples=samples, tile_size=int(tile_size), overlap=int(overlap),
                temporal_size=int(temporal_size), temporal_overlap=int(temporal_overlap),
            )[0]
            return _to_cpu(images)
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))

    @staticmethod
    def _decode_audio(samples, vae_name, device):
        vae = None
        patcher = None
        path = ""
        try:
            vae, path = _load_disk_vae(vae_name, device)
            patcher = vae.patcher
            latent = samples["samples"]
            if latent.is_nested:
                latent = latent.unbind()[-1]
            waveform = vae.decode(latent).movedim(-1, 1).to(latent.device)
            return _to_cpu({
                "waveform": waveform,
                "sample_rate": int(vae.first_stage_model.output_sample_rate),
            })
        finally:
            _unload_patcher(patcher)
            patcher = None
            vae = None
            _release_memory((path,))

    def decode(self, samples, video_vae_name, audio_vae_name, video_device="auxiliary:1", audio_device="auxiliary:1", tile_size=512, overlap=64, temporal_size=64, temporal_overlap=8):
        video, audio = self._separate(samples)
        images = self._decode_video(
            video, video_vae_name, video_device, tile_size, overlap,
            temporal_size, temporal_overlap,
        )
        waveform = self._decode_audio(audio, audio_vae_name, audio_device)
        return images, waveform
