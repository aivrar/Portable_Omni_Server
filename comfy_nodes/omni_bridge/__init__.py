"""OmniBridge — call the Omni Studio gateway from ComfyUI workflows.

This package is auto-installed by Omni Studio's setup.sh into
``$COMFYUI_DIR/custom_nodes/omni_bridge/`` on every setup run, so the
nodes stay in lockstep with the gateway version.

The nodes call back to the local Omni Studio gateway (``127.0.0.1:8200``
by default — overridable via env) using the per-instance session token
read from ``/opt/omni_studio/cache/runtime/api_token`` or fetched live
from ``GET /api/session`` (which is loopback-exempt from auth).
"""

from .nodes.omni_chat import OmniChat
from .nodes.omni_describe import OmniDescribe
from .nodes.omni_transcribe import OmniTranscribe
from .nodes.omni_tts import OmniTTS
from .nodes.omni_h3_conditioning import OmniH3ReferenceStrength
from .nodes.omni_h3_latent import OmniH3JoinAVLatent, OmniH3SplitAVLatent
from .nodes.omni_multigpu import (
    OmniMultiGPUWorkUnits,
    OmniRouteAudioEncoder,
    OmniRouteCLIP,
    OmniRouteModel,
    OmniRouteVAE,
)
from .nodes.omni_staged_h3 import (
    OmniH3StageFL2VConditioning,
    OmniH3StageConditioning,
    OmniH3StageDecode,
    OmniH3StageSampler,
)
from .nodes.omni_staged_vae import OmniStageVAEDecode
from .nodes.omni_staged_ltx import (
    OmniLTXStageAVDecode,
    OmniLTXStageConditioning,
    OmniLTXStageDurationPredictor,
    OmniLTXStageEmptyAVLatent,
    OmniLTXStageGuide,
    OmniLTXStageSampler,
    OmniLTXStageSpatialRefine,
)

NODE_CLASS_MAPPINGS = {
    "OmniChat":       OmniChat,
    "OmniDescribe":   OmniDescribe,
    "OmniTranscribe": OmniTranscribe,
    "OmniTTS":        OmniTTS,
    "OmniRouteModel": OmniRouteModel,
    "OmniRouteCLIP": OmniRouteCLIP,
    "OmniRouteVAE": OmniRouteVAE,
    "OmniRouteAudioEncoder": OmniRouteAudioEncoder,
    "OmniMultiGPUWorkUnits": OmniMultiGPUWorkUnits,
    "OmniH3StageConditioning": OmniH3StageConditioning,
    "OmniH3StageFL2VConditioning": OmniH3StageFL2VConditioning,
    "OmniH3StageSampler": OmniH3StageSampler,
    "OmniH3StageDecode": OmniH3StageDecode,
    "OmniH3ReferenceStrength": OmniH3ReferenceStrength,
    "OmniH3SplitAVLatent": OmniH3SplitAVLatent,
    "OmniH3JoinAVLatent": OmniH3JoinAVLatent,
    "OmniStageVAEDecode": OmniStageVAEDecode,
    "OmniLTXStageConditioning": OmniLTXStageConditioning,
    "OmniLTXStageDurationPredictor": OmniLTXStageDurationPredictor,
    "OmniLTXStageEmptyAVLatent": OmniLTXStageEmptyAVLatent,
    "OmniLTXStageGuide": OmniLTXStageGuide,
    "OmniLTXStageSampler": OmniLTXStageSampler,
    "OmniLTXStageSpatialRefine": OmniLTXStageSpatialRefine,
    "OmniLTXStageAVDecode": OmniLTXStageAVDecode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "OmniRouteModel": "Omni Route Model to GPU",
    "OmniRouteCLIP": "Omni Route Text Encoder to GPU",
    "OmniRouteVAE": "Omni Route VAE to GPU",
    "OmniRouteAudioEncoder": "Omni Route Audio Encoder to GPU",
    "OmniMultiGPUWorkUnits": "Omni MultiGPU CFG Work Units",
    "OmniH3StageConditioning": "Omni H3 Staged Conditioning",
    "OmniH3StageFL2VConditioning": "Omni H3 Staged FL2V Conditioning",
    "OmniH3StageSampler": "Omni H3 Staged Sampler",
    "OmniH3StageDecode": "Omni H3 Staged AV Decode",
    "OmniH3ReferenceStrength": "Omni H3 Reference Strength",
    "OmniH3SplitAVLatent": "Omni H3 Split AV Latent",
    "OmniH3JoinAVLatent": "Omni H3 Join AV Latent",
    "OmniStageVAEDecode": "Omni Staged VAE Decode",
    "OmniLTXStageConditioning": "Omni LTX Staged Conditioning",
    "OmniLTXStageDurationPredictor": "Omni LTX Staged Duration Predictor",
    "OmniLTXStageEmptyAVLatent": "Omni LTX Staged Empty AV Latent",
    "OmniLTXStageGuide": "Omni LTX Staged Guide",
    "OmniLTXStageSampler": "Omni LTX Staged Sampler",
    "OmniLTXStageSpatialRefine": "Omni LTX Staged Spatial Refine",
    "OmniLTXStageAVDecode": "Omni LTX Staged AV Decode",
    "OmniChat":       "Omni Chat (text → text)",
    "OmniDescribe":   "Omni Describe (image → text)",
    "OmniTranscribe": "Omni Transcribe (audio → text)",
    "OmniTTS":        "Omni TTS (text → audio)",
}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
