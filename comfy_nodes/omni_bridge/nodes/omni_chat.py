"""OmniChat — text → text via Omni Studio gateway."""

from ..client import chat_text, TEXT_MODELS


class OmniChat:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (TEXT_MODELS, {"default": "qwen_omni_3b"}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "max_new_tokens": ("INT", {"default": 512, "min": 1, "max": 4096}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "top_p": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.05}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "run"
    CATEGORY = "OmniBridge"

    def run(self, model, prompt, max_new_tokens=512, temperature=0.7, top_p=0.9):
        text = chat_text(
            model, prompt,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        return (text,)
