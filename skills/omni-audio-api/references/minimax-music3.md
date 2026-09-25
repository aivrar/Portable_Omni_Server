# MiniMax Music 3

Use the standalone `minimax_music3` worker. Do not route this model through
ComfyUI or treat it as an ACE-Step checkpoint.

## Safe sequence

1. Read `GET /api/minimax_music3/status` and
   `GET /api/minimax_music3/state?autospawn=false`.
2. If absent, start `official-diffusers` through
   `POST /api/setup/install-variant`; poll its returned job with
   `GET /api/jobs/{job_id}`.
3. Read `GET /api/devices`, select a current `cuda:N`, and call
   `POST /api/minimax_music3/load` with an explicit CPU-offload budget. The
   route runs weight-free placement analysis and refuses `valid:false`.
   Prefer BF16 and bounded CPU offload on a 24 GB GPU.
4. Call `POST /api/minimax_music3/generate` once with both a structured music
   description and lyrics. The call is synchronous; allow a long timeout.
5. Verify the WAV and adjacent `manifest.json` in the Media library.
6. Call `POST /api/minimax_music3/unload` after use. If generation must stop,
   call `/cancel`; it retires the exact worker because the upstream pipeline
   has no reliable cooperative interruption point.

## Input contract

- `prompt`: describe genre, BPM, key, emotional progression, vocals, and
  arrangement. Explicitly describe vocal gender/timbre when vocals matter.
- `lyrics`: required even for instrumentals. Put `[intro]`, `[verse]`,
  `[pre-chorus]`, `[chorus]`, `[post-chorus]`, `[bridge]`, `[instrumental]`, `[solo]`, and
  `[outro]` tags on their own lines. Text placed on a tag line is dropped.
- For an instrumental, use a nonempty form such as
  `[intro]\n(instrumental)`.
- `duration_s`: 1 through 360. It is an upper bound; the autoregressive stage
  may emit its stop token earlier.
- `seed`: nonnegative signed 64-bit integer.
- Combined tokenized caption and lyrics: at most 5,000 tokens; streaming is
  unsupported.

The engine returns native 44.1 kHz stereo PCM-24 WAV. It does not currently
expose reference audio, voice cloning, TTS voice selection, streaming, or a
verified LoRA attachment contract.

The official modular index declares the slow `Qwen2Tokenizer`, while its
snapshot ships a fast `tokenizer.json` without slow vocab/merges files. Omni's
loader must register `Qwen2TokenizerFast` from the managed local tokenizer
directory before `load_components`; otherwise Diffusers leaves the component
`None` and generation fails in the text-encoder block. Do not remove this
compatibility step during runtime updates.

Qualified 2026-08-13 baseline on the RTX 3090 with BF16 and a 16 GB bounded
CPU-offload plan: warm-cache load 473 seconds; 60.07 seconds of vocal music
generated in 718.273 seconds. Treat timings as hardware/runtime observations,
not service guarantees.

## Discovery and compatibility

Use `/api/search/hf?q=MiniMax%20Music3&kind=model&family=minimax_music3` and
repeat with `kind=lora`. Treat the returned compatibility label as a gate:
only `official` is currently runtime-compatible. Do not install mirrors,
hardware-specific AOTI supplements, test fixtures, Comfy packages, or future
LoRAs as if they were drop-in variants.

The managed installer pins the official repository revision and downloads the
Diffusers component layout only. It deliberately excludes duplicate SGLang
weights. MiniMax Music 3 uses a custom community license; retain the visible
`MiniMax-Music3` attribution and review its commercial revenue, acceptable-use,
and safeguard terms before deployment.
