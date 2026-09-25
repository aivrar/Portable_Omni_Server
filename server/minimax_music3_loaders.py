"""MiniMax Music 3 loader and inference helpers for its dedicated worker.

The worker uses the official Diffusers modular pipeline.  It writes audio
directly into Omni's managed output tree so multi-minute stereo WAV files are
never expanded into base64 inside gateway memory.
"""

from __future__ import annotations

import gc
import os
import re
import threading
import time
from pathlib import Path

from config import (
    MINIMAX_MUSIC3_MAX_DURATION_S,
    MINIMAX_MUSIC3_MAX_TOKENS,
    MINIMAX_MUSIC3_MODELS,
    MINIMAX_MUSIC3_OUTPUT_KIND,
    MINIMAX_MUSIC3_SAMPLE_RATE,
    OUTPUT_DIR,
    is_minimax_music3_model_installed,
    minimax_music3_model_path,
)

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def init_state(device: str) -> dict:
    return {
        "lock": threading.RLock(),
        "device": device,
        "pipeline": None,
        "components_manager": None,
        "model_variant": None,
        "load_kwargs": None,
        "sampling_rate": MINIMAX_MUSIC3_SAMPLE_RATE,
    }


def _clear_cuda() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
    except Exception:
        pass


def current_state(state: dict) -> dict:
    return {
        "model_loaded": state.get("pipeline") is not None,
        "model_variant": state.get("model_variant"),
        "device": state.get("device"),
        "load_kwargs": state.get("load_kwargs"),
        "sample_rate": state.get("sampling_rate", MINIMAX_MUSIC3_SAMPLE_RATE),
        "max_duration_s": MINIMAX_MUSIC3_MAX_DURATION_S,
    }


def unload(state: dict) -> dict:
    with state["lock"]:
        state["pipeline"] = None
        state["components_manager"] = None
        state["model_variant"] = None
        state["load_kwargs"] = None
        _clear_cuda()
        return current_state(state)


def load_model(
    state: dict,
    model_variant: str,
    *,
    bf16: bool = True,
    cpu_offload: bool = True,
) -> dict:
    if model_variant not in MINIMAX_MUSIC3_MODELS:
        raise ValueError(f"Unknown MiniMax Music 3 variant: {model_variant}")
    if not is_minimax_music3_model_installed(model_variant):
        raise FileNotFoundError(
            f"MiniMax Music 3 variant '{model_variant}' is not installed"
        )

    with state["lock"]:
        requested = {"bf16": bool(bf16), "cpu_offload": bool(cpu_offload)}
        if (
            state.get("pipeline") is not None
            and state.get("model_variant") == model_variant
            and state.get("load_kwargs") == requested
        ):
            return {**current_state(state), "unchanged": True}

        state["pipeline"] = None
        state["components_manager"] = None
        state["model_variant"] = None
        state["load_kwargs"] = None
        _clear_cuda()

        import torch
        from transformers import AutoTokenizer
        from diffusers import ComponentsManager, ModularPipeline

        if not torch.cuda.is_available() or not str(state["device"]).startswith("cuda"):
            raise RuntimeError("MiniMax Music 3 requires an NVIDIA CUDA device")

        dtype = torch.bfloat16 if bf16 else torch.float32
        model_path = minimax_music3_model_path(model_variant)
        components_manager = None
        if cpu_offload:
            components_manager = ComponentsManager()
            components_manager.enable_auto_cpu_offload(device=state["device"])
        pipe = ModularPipeline.from_pretrained(
            str(model_path),
            components_manager=components_manager,
        )
        # The official modular index declares the slow Qwen2Tokenizer class,
        # while the repository intentionally ships tokenizer.json only.  The
        # slow class therefore receives vocab_file=None and Diffusers silently
        # leaves the tokenizer component empty.  Register the supported fast
        # tokenizer from the managed local snapshot before load_components so
        # the modular loader skips the broken slow-tokenizer construction.
        tokenizer = AutoTokenizer.from_pretrained(
            str(model_path / "tokenizer"),
            use_fast=True,
            local_files_only=True,
        )
        pipe.update_components(tokenizer=tokenizer)
        pipe.load_components(dtype=dtype)
        if not cpu_offload:
            # Pinned Diffusers override imports huggingface_hub.get_cached_repo_tree
            # (absent from 0.36). Fall back to the already-loaded device map.
            try:
                pipe.to(state["device"])
            except ImportError:
                pass

        state["pipeline"] = pipe
        state["components_manager"] = components_manager
        state["model_variant"] = model_variant
        state["load_kwargs"] = requested
        state["sampling_rate"] = int(
            getattr(pipe, "sampling_rate", MINIMAX_MUSIC3_SAMPLE_RATE)
        )
        return current_state(state)


def _output_path(job_id: str) -> Path:
    if not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError("Invalid job_id")
    root = OUTPUT_DIR / "omni" / MINIMAX_MUSIC3_OUTPUT_KIND
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    return job_dir / "01.wav"


def infer_generate(state: dict, req: dict) -> dict:
    pipe = state.get("pipeline")
    if pipe is None:
        raise RuntimeError("No MiniMax Music 3 model is loaded")

    prompt = str(req.get("prompt") or "").strip()
    lyrics = str(req.get("lyrics") or "").strip()
    if not prompt or not lyrics:
        raise ValueError("Both prompt and lyrics are required")

    duration_s = float(req.get("duration_s", 60.0))
    if not 1.0 <= duration_s <= MINIMAX_MUSIC3_MAX_DURATION_S:
        raise ValueError(
            f"duration_s must be between 1 and {MINIMAX_MUSIC3_MAX_DURATION_S:g}"
        )
    seed = int(req.get("seed", 0))
    job_id = str(req.get("job_id") or "")

    tokenizer = getattr(pipe, "tokenizer", None)
    if tokenizer is not None:
        tokenized = tokenizer(
            prompt + "\n" + lyrics,
            add_special_tokens=False,
            truncation=False,
        )
        token_ids = tokenized.get("input_ids") if isinstance(tokenized, dict) else None
        if token_ids is not None and len(token_ids) > MINIMAX_MUSIC3_MAX_TOKENS:
            raise ValueError(
                f"Combined prompt and lyrics exceed {MINIMAX_MUSIC3_MAX_TOKENS} tokens"
            )

    import soundfile as sf
    import torch

    started = time.monotonic()
    # CPU generator: offload parks weights on RAM; a CUDA Generator opens
    # another WSL dxgresource and is what pushed long runs into FD exhaustion.
    previous_hub = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    with state["lock"]:
        try:
            generator = torch.Generator().manual_seed(seed)
            audio = pipe(
                prompt=prompt,
                lyrics=lyrics,
                audio_duration=duration_s,
                generator=generator,
                output="audios",
            )[0]
            if hasattr(audio, "detach"):
                audio = audio.detach().float().cpu().numpy()

            output_path = _output_path(job_id)
            # Official output is channels-first; preserve already interleaved arrays.
            if getattr(audio, "ndim", 0) == 2 and audio.shape[0] <= 8:
                audio = audio.T
            sample_rate = int(state.get("sampling_rate") or MINIMAX_MUSIC3_SAMPLE_RATE)
            sf.write(str(output_path), audio, sample_rate, subtype="PCM_24")
        finally:
            if previous_hub is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous_hub
            _clear_cuda()

    return {
        "job_id": job_id,
        "filename": output_path.name,
        "sample_rate": sample_rate,
        "duration_s": round(len(audio) / sample_rate, 3),
        "seed": seed,
        "elapsed_s": round(time.monotonic() - started, 3),
    }
