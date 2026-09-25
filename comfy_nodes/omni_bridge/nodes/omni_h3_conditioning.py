"""Stable MiniMax H3 conditioning controls owned by OmniBridge."""


class OmniH3ReferenceStrength:
    """Expose MiniMax H3's native visual/audio conditioning noise values."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "visual_reference_strength": (
                    "FLOAT",
                    {
                        "default": 0.999,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.001,
                        "tooltip": (
                            "Native minimax_visual_cond_noise_aug value. Lower values add "
                            "more noise to image/video references; 0.7 is a conservative "
                            "motion-variation test value and 0.999 is the model default."
                        ),
                    },
                ),
                "audio_reference_strength": (
                    "FLOAT",
                    {
                        "default": 1.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.001,
                        "tooltip": (
                            "Native minimax_audio_cond_noise_aug value. Keep 1.0 to copy "
                            "reference audio conditioning without added noise."
                        ),
                    },
                ),
            }
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "apply"
    CATEGORY = "OmniBridge/conditioning/minimax"
    DESCRIPTION = (
        "Sets MiniMax H3's native visual and audio reference-conditioning strength "
        "values without modifying ComfyUI core."
    )

    def apply(self, conditioning, visual_reference_strength=0.999,
              audio_reference_strength=1.0):
        import node_helpers

        values = {
            "minimax_visual_cond_noise_aug": float(visual_reference_strength),
            "minimax_audio_cond_noise_aug": float(audio_reference_strength),
        }
        return (node_helpers.conditioning_set_values(conditioning, values),)
