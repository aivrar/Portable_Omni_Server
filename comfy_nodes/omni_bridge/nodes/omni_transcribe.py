"""OmniTranscribe — AUDIO → text via Omni Studio gateway."""

from ..client import comfy_audio_to_b64_wav, stt_audio, AUDIO_IN_MODELS


class OmniTranscribe:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio": ("AUDIO",),
                "model": (AUDIO_IN_MODELS, {"default": "qwen_omni_7b"}),
            },
            "optional": {
                "prompt": ("STRING", {
                    "multiline": False,
                    "default": "Transcribe this audio.",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("transcript",)
    FUNCTION = "run"
    CATEGORY = "OmniBridge"

    def run(self, audio, model, prompt="Transcribe this audio."):
        audio_b64 = comfy_audio_to_b64_wav(audio)
        text = stt_audio(model, audio_b64, prompt=prompt)
        return (text,)
