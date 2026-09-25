"""OmniDescribe — IMAGE → text via Omni Studio gateway."""

from ..client import chat_text, comfy_image_to_b64, VISION_MODELS


class OmniDescribe:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "model": (VISION_MODELS, {"default": "qwen_omni_7b"}),
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "Describe this image in detail.",
                }),
            },
            "optional": {
                "max_new_tokens": ("INT", {"default": 512, "min": 1, "max": 4096}),
                "temperature": ("FLOAT", {"default": 0.4, "min": 0.0, "max": 2.0, "step": 0.05}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("description",)
    FUNCTION = "run"
    CATEGORY = "OmniBridge"

    def run(self, image, model, prompt, max_new_tokens=512, temperature=0.4):
        img_b64 = comfy_image_to_b64(image)
        text = chat_text(
            model, prompt,
            image_b64=img_b64,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
        )
        return (text,)
