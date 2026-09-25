"""GPU-targeted VAE decoding that releases its own VAE after use."""

from __future__ import annotations

from .omni_staged_h3 import (
    _load_disk_vae,
    _release_memory,
    _target_options,
    _to_cpu,
    _unload_patcher,
)


class OmniStageVAEDecode:
    """Decode with one named VAE, then unload it; upstream models may remain resident."""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {"required": {
            "samples": ("LATENT",),
            "vae_name": (folder_paths.get_filename_list("vae"),),
            "device": (_target_options(),),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "decode"
    CATEGORY = "OmniBridge/multigpu/staged"

    def decode(self, samples, vae_name, device="primary"):
        # Do not call unload_all_models() here. Krea's routed CLIP clone can
        # stay referenced on the auxiliary GPU and that global unload blocks
        # the prompt server. After sampling, the 3090 is already free enough
        # to take the VAE.
        _release_memory(())

        vae = None
        patcher = None
        vae_path = ""
        try:
            vae, vae_path = _load_disk_vae(vae_name, device)
            patcher = vae.patcher
            import nodes as comfy_nodes

            # Use Comfy's VAEDecode so Flux/Krea packed latents are moved and
            # unpacked the same way as the qualified non-staged graph.
            return (_to_cpu(comfy_nodes.VAEDecode().decode(vae=vae, samples=samples)[0]),)
        finally:
            vae = None
            _unload_patcher(patcher)
            patcher = None
            _release_memory((vae_path,))
