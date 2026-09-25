"""Split and rejoin MiniMax H3 NestedTensor video/audio latents.

Official H3 latents are ``NestedTensor((video, audio))``. Third-party
upscalers (and LTX AV split/concat helpers) expect a plain video tensor.
These nodes keep that conversion on OmniBridge instead of patching Comfy
core or the upscaler pack.
"""

from __future__ import annotations


def _nested_parts(samples):
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif hasattr(samples, "tensors"):
        parts = list(samples.tensors)
    else:
        raise ValueError(
            "Expected a MiniMax H3 NestedTensor AV latent. "
            "Do not send this through LTX AV split/concat nodes."
        )
    if len(parts) < 2:
        raise ValueError("H3 AV latent must contain video and audio tensors")
    return parts[0], parts[1]


def _as_latent(samples, source: dict | None = None) -> dict:
    latent = dict(source or {})
    latent["samples"] = samples
    return latent


class OmniH3SplitAVLatent:
    """Unpack an official H3 NestedTensor into video and audio latents."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"latent": ("LATENT",)}}

    RETURN_TYPES = ("LATENT", "LATENT")
    RETURN_NAMES = ("video_latent", "audio_latent")
    FUNCTION = "split"
    CATEGORY = "OmniBridge/conditioning/minimax"
    DESCRIPTION = (
        "Splits MiniMax H3 NestedTensor latents into a plain video latent "
        "for third-party upscalers and the untouched audio latent."
    )

    def split(self, latent):
        video, audio = _nested_parts(latent["samples"])
        return _as_latent(video, latent), _as_latent(audio, latent)


class OmniH3JoinAVLatent:
    """Repack separately processed H3 video/audio latents for native decode."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "video_latent": ("LATENT",),
            "audio_latent": ("LATENT",),
        }}

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("latent",)
    FUNCTION = "join"
    CATEGORY = "OmniBridge/conditioning/minimax"
    DESCRIPTION = (
        "Rebuilds the official H3 NestedTensor after a video-only latent "
        "upscale so Omni H3 staged decode still sees video plus audio."
    )

    def join(self, video_latent, audio_latent):
        from comfy.nested_tensor import NestedTensor

        packed = NestedTensor((video_latent["samples"], audio_latent["samples"]))
        return (_as_latent(packed, video_latent),)
