"""OmniTTS — text → AUDIO via Omni Studio gateway."""

from ..client import tts_speak, TTS_MODELS


class OmniTTS:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": (TTS_MODELS, {"default": "qwen_omni_7b"}),
                "text": ("STRING", {"multiline": True, "default": ""}),
            },
            "optional": {
                "voice": ("STRING", {"multiline": False, "default": ""}),
                "speed": ("FLOAT", {"default": 1.0, "min": 0.25, "max": 4.0, "step": 0.05}),
            },
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("audio",)
    FUNCTION = "run"
    CATEGORY = "OmniBridge"

    def run(self, model, text, voice="", speed=1.0):
        if not text.strip():
            raise RuntimeError("OmniTTS: empty text")
        audio = tts_speak(model, text, voice=voice or None, speed=speed)
        # Note: the Omni Studio audio router writes a copy to
        # OUTPUT_DIR/omni/tts/<model>/, so the audio is already archived
        # in the Media tab. The AUDIO type returned here is what
        # downstream nodes (e.g. SaveAudio) consume.
        return (audio,)
