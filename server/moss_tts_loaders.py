"""MOSS-TTS and MOSS-SoundEffect worker helpers.

The upstream MOSS repository carries two incompatible runtime stacks for the
parts we support here:

* MOSS-TTS Local v1.5: Qwen3-4B speech generation plus Audio-Tokenizer-v2.
* MOSS-SoundEffect v2.0: a separate DiT pipeline with its own pinned deps.

Omni launches those workers from model-specific venvs, but keeps the source,
weights, Hugging Face cache, and output layout inside the normal Omni runtime.
"""

from __future__ import annotations

import base64
import inspect
import io
import os
import sys
from pathlib import Path
from typing import Any

from config import (
    MODELS_DIR,
    MOSS_REPO_DIR,
    MOSS_TTS_CODEC_WEIGHTS_DIR,
    MOSS_TTS_WEIGHTS_DIR,
    MOSS_SFX_WEIGHTS_DIR,
    OUTPUT_DIR,
)


_WEIGHT_EXTS = (".safetensors", ".bin", ".pt", ".pth", ".ckpt")


def _has_weights(path: Path) -> bool:
    if not path.exists():
        return False
    return any(
        p.is_file() and p.suffix.lower() in _WEIGHT_EXTS
        for p in path.rglob("*")
    )


def _require_source(kind: str) -> None:
    if not MOSS_REPO_DIR.exists():
        raise FileNotFoundError(
            f"MOSS source checkout is missing at {MOSS_REPO_DIR}. "
            f"Install {kind} from the Models tab first."
        )


def _dtype_name(precision: str | None, device: str) -> str:
    normalized = str(precision or "").strip().lower()
    if normalized in {"fp32", "float32"}:
        return "float32"
    if normalized in {"fp16", "float16", "half"}:
        return "float16"
    if normalized in {"bf16", "bfloat16"}:
        return "bfloat16"
    return "bfloat16" if str(device).startswith("cuda") else "float32"


def _torch_dtype(precision: str | None, device: str):
    import torch

    name = _dtype_name(precision, device)
    if name == "float16":
        return torch.float16
    if name == "bfloat16":
        return torch.bfloat16
    return torch.float32


def _audio_to_wav_base64(waveform: Any, sample_rate: int) -> str:
    import numpy as np
    import soundfile as sf

    try:
        import torch
        if torch.is_tensor(waveform):
            waveform = waveform.detach().cpu().to(torch.float32).numpy()
    except Exception:
        pass

    arr = np.asarray(waveform, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim == 1:
        pass
    elif arr.ndim == 2:
        # Torch audio convention is [channels, samples]; soundfile wants
        # [samples, channels]. If the first dim looks like channels, transpose.
        if arr.shape[0] <= 8 and arr.shape[1] > arr.shape[0]:
            arr = arr.T
    else:
        raise ValueError(f"Unsupported audio tensor shape: {arr.shape}")
    arr = np.clip(arr, -1.0, 1.0)

    buf = io.BytesIO()
    sf.write(buf, arr, int(sample_rate), format="WAV", subtype="PCM_16")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _load_runtime_with_fixed_tokenizer(streaming_module, **kwargs):
    """Load upstream MOSS without leaking tokenizer-only kwargs to its codec.

    MOSS' custom processor forwards unknown processor kwargs to both
    ``AutoTokenizer`` and ``AutoModel``. Transformers' optional
    ``fix_mistral_regex`` flag is not accepted by MossAudioTokenizerModel and
    MOSS-TTS uses a Qwen3 tokenizer here, so explicitly remove the flag if a
    caller or future Transformers default supplies it.
    """
    original_processor = streaming_module.AutoProcessor

    class _FixedAutoProcessor:
        @staticmethod
        def from_pretrained(*args, **processor_kwargs):
            processor_kwargs.pop("fix_mistral_regex", None)
            # The MOSS processor forwards its kwargs to both AutoTokenizer and
            # the audio-tokenizer AutoModel. Apply the regex repair only at the
            # tokenizer call so the codec model never receives this unsupported
            # constructor argument.
            from transformers import AutoTokenizer

            original_loader = AutoTokenizer.from_pretrained
            original_descriptor = inspect.getattr_static(
                AutoTokenizer, "from_pretrained"
            )

            def _fixed_tokenizer_loader(cls, *loader_args, **loader_kwargs):
                loader_kwargs.setdefault("fix_mistral_regex", True)
                return original_loader(*loader_args, **loader_kwargs)

            AutoTokenizer.from_pretrained = classmethod(_fixed_tokenizer_loader)
            try:
                return original_processor.from_pretrained(*args, **processor_kwargs)
            finally:
                AutoTokenizer.from_pretrained = original_descriptor

    streaming_module.AutoProcessor = _FixedAutoProcessor
    try:
        return streaming_module.load_runtime(**kwargs)
    finally:
        streaming_module.AutoProcessor = original_processor


def load_moss_tts(device: str, precision: str | None = None) -> dict:
    """Load MOSS-TTS Local v1.5 and its paired Audio-Tokenizer-v2."""
    _require_source("MOSS-TTS")
    streaming_dir = MOSS_REPO_DIR / "moss_tts_local_v1.5"
    streaming_file = streaming_dir / "streaming.py"
    if not streaming_file.exists():
        raise FileNotFoundError(f"MOSS-TTS streaming helper missing: {streaming_file}")
    for path in (streaming_dir, MOSS_REPO_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    model_dir = MODELS_DIR / "omni" / MOSS_TTS_WEIGHTS_DIR
    codec_dir = MODELS_DIR / "omni" / MOSS_TTS_CODEC_WEIGHTS_DIR
    if not _has_weights(model_dir):
        raise FileNotFoundError(f"MOSS-TTS weights are missing or incomplete: {model_dir}")
    if not _has_weights(codec_dir):
        raise FileNotFoundError(
            f"MOSS Audio-Tokenizer-v2 weights are missing or incomplete: {codec_dir}"
        )

    import torch
    import streaming

    torch.backends.cuda.enable_cudnn_sdp(False)
    torch.backends.cuda.enable_flash_sdp(True)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
    torch.backends.cuda.enable_math_sdp(True)

    dtype = _dtype_name(precision, device)
    codec_weight_dtype = os.environ.get("OMNI_MOSS_CODEC_WEIGHT_DTYPE", "fp32")
    codec_compute_dtype = os.environ.get("OMNI_MOSS_CODEC_COMPUTE_DTYPE", "bf16")
    attn_impl = os.environ.get("OMNI_MOSS_ATTN_IMPL", "auto")
    warmup = os.environ.get("OMNI_MOSS_TTS_WARMUP", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }

    runtime = _load_runtime_with_fixed_tokenizer(
        streaming,
        model_dir=str(model_dir),
        codec_dir=str(codec_dir),
        device=device,
        tts_device=device,
        codec_device=device,
        dtype=dtype,
        attn_implementation=attn_impl,
        codec_weight_dtype=codec_weight_dtype,
        codec_compute_dtype=codec_compute_dtype,
        warmup=warmup,
    )
    return {
        "kind": "moss_tts",
        "runtime": runtime,
        "model_dir": str(model_dir),
        "codec_dir": str(codec_dir),
        "sample_rate": int(runtime.sample_rate),
    }


def infer_moss_tts(model_obj: dict, params: dict) -> dict:
    runtime = model_obj["runtime"]
    text = (params.get("text") or "").strip()
    if not text:
        raise ValueError("MOSS-TTS text must not be empty")

    model_params = params.get("model_params") or {}
    voice = params.get("voice") or model_params.get("voice") or ""
    language = model_params.get("language") or ""
    if not language and voice:
        # Let the existing OpenAI-style "voice" slot be useful without adding
        # a separate UI field. Non-language voices are harmlessly omitted.
        voice_norm = str(voice).strip().lower()
        known = {
            "english": "English",
            "chinese": "Chinese",
            "french": "French",
            "japanese": "Japanese",
            "korean": "Korean",
            "spanish": "Spanish",
            "german": "German",
        }
        language = known.get(voice_norm, "")

    try:
        max_new_frames = int(model_params.get("max_new_frames") or os.environ.get("OMNI_MOSS_TTS_MAX_NEW_FRAMES", "2048"))
    except ValueError:
        max_new_frames = 2048
    max_new_frames = max(1, min(max_new_frames, 7500))
    seed = model_params.get("seed")
    seed = None if seed in (None, "", -1) else int(seed)

    from streaming import StreamingRequest, synthesize_stream

    output_dir = OUTPUT_DIR / "omni" / "moss_tts_streaming"
    request = StreamingRequest(
        text=text,
        mode="continuation",
        language=str(language or ""),
        max_new_frames=max_new_frames,
        seed=seed,
        temperature=float(model_params.get("temperature", 1.7)),
        top_p=float(model_params.get("top_p", 0.8)),
        top_k=int(model_params.get("top_k", 25)),
        repetition_penalty=float(model_params.get("repetition_penalty", 1.0)),
        codec_chunk_frames=int(model_params.get("codec_chunk_frames", 0)),
    )

    final_event = None
    for event in synthesize_stream(runtime, request, output_dir=output_dir):
        if event.type == "result":
            final_event = event
    if final_event is None:
        raise RuntimeError("MOSS-TTS did not produce a final audio result")

    waveform = final_event.data["waveform"]
    sample_rate = int(final_event.data.get("sample_rate") or runtime.sample_rate)
    return {
        "audio_base64": _audio_to_wav_base64(waveform, sample_rate),
        "format": "wav",
        "sample_rate": sample_rate,
        "metadata": final_event.data.get("metadata") or {},
    }


def load_moss_sfx(device: str, precision: str | None = None) -> dict:
    """Load MOSS-SoundEffect v2.0."""
    _require_source("MOSS-SoundEffect")
    if str(MOSS_REPO_DIR) not in sys.path:
        sys.path.insert(0, str(MOSS_REPO_DIR))
    model_dir = MODELS_DIR / "omni" / MOSS_SFX_WEIGHTS_DIR
    if not _has_weights(model_dir):
        raise FileNotFoundError(f"MOSS-SoundEffect weights are missing or incomplete: {model_dir}")

    from moss_soundeffect_v2 import MossSoundEffectPipeline

    dtype = _torch_dtype(precision, device)
    pipe = MossSoundEffectPipeline.from_pretrained(
        str(model_dir),
        torch_dtype=dtype,
        device=device,
    )
    return {
        "kind": "moss_sfx",
        "pipe": pipe,
        "model_dir": str(model_dir),
        "sample_rate": int(getattr(pipe, "sample_rate", 48000)),
    }


def infer_moss_sfx(model_obj: dict, params: dict) -> dict:
    pipe = model_obj["pipe"]
    model_params = params.get("model_params") or {}
    prompt = (
        model_params.get("prompt")
        or params.get("text")
        or ""
    ).strip()
    if not prompt:
        raise ValueError("MOSS-SoundEffect prompt must not be empty")

    seconds = round(float(model_params.get("seconds", model_params.get("duration_s", 10.0))), 1)
    seconds = max(0.1, min(seconds, 30.0))
    steps = max(1, min(int(model_params.get("steps", 100)), 200))
    cfg_scale = float(model_params.get("cfg_scale", 4.0))
    sigma_shift = float(model_params.get("sigma_shift", 5.0))
    seed = int(model_params.get("seed", 0))
    negative_prompt = str(model_params.get("negative_prompt") or "")

    audio = pipe(
        prompt=prompt,
        seconds=seconds,
        num_inference_steps=steps,
        cfg_scale=cfg_scale,
        sigma_shift=sigma_shift,
        seed=seed,
        negative_prompt=negative_prompt,
    )
    sample_rate = int(getattr(pipe, "sample_rate", 48000))
    return {
        "audio_base64": _audio_to_wav_base64(audio, sample_rate),
        "format": "wav",
        "sample_rate": sample_rate,
        "metadata": {
            "prompt": prompt,
            "seconds": seconds,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sigma_shift": sigma_shift,
            "seed": seed,
        },
    }
