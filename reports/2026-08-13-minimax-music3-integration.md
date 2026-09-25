# MiniMax Music 3 standalone integration review

## Decision

Integrate MiniMax Music 3 as its own Omni audio worker using the official
Diffusers modular pipeline. Do not route it through ComfyUI and do not treat it
as an ACE-Step model. Pin the official model and runtime revisions so upstream
updates cannot silently change the contract.

## Official runtime facts

- Model: <https://huggingface.co/MiniMaxAI/MiniMax-Music3>
- Source: <https://github.com/MiniMax-AI/MiniMax-Music3>
- Diffusers integration pinned at:
  <https://github.com/huggingface/diffusers/blob/dafe3733fcfdbf3c48915fe77be3aef65b5d6a2d/docs/source/en/api/pipelines/minimax_music3.md>
- Architecture: 8B Qwen3-based global language model, 0.6B depth decoder,
  2.4B flow-matching transformer, and Flow-VAE/vocoder.
- Native Diffusers output: 44.1 kHz stereo.
- Inputs: nonempty music description, nonempty lyrics, duration upper bound,
  and seed. The documented Diffusers call returns `audios`.
- Structure tags must occupy their own lyrics lines. Text placed on a leading
  tag line is discarded by the input contract.
- Official marketing says songs up to five minutes. The underlying source
  admits at most 9,000 semantic frames at 25 Hz, so Omni validates a 360-second
  technical ceiling while describing duration as an upper bound.
- The autoregressive stage dominates runtime. The flow stage uses reference
  classifier-free guidance 1.7; the first stable Omni contract keeps upstream
  quality defaults rather than exposing unqualified tuning knobs.

## Install layout

The official repository contains both SGLang and Diffusers layouts and totals
about 53.4 GiB. Omni downloads only these Diffusers paths:

- root license/config/readme/modular index;
- tokenizer;
- language model;
- RVQ depth decoder;
- condition encoder;
- transformer;
- scheduler;
- vocoder.

This is expected to use about 28 GiB. It deliberately excludes `qwen_7B`,
`flowmatching_vae.pth`, `dav.pth`, and other duplicate SGLang files. The model
snapshot and the Diffusers package commit each use an atomic completion marker.

The shared Omni environment has Diffusers 0.38 while Music 3 landed after that
release. The worker therefore injects a model-specific Diffusers override at
commit `dafe3733fcfdbf3c48915fe77be3aef65b5d6a2d` without replacing shared
Torch or Transformers packages.

## GPU and memory behavior

The official full-precision Diffusers configuration needs a 24 GB-class GPU.
Omni defaults to BF16 plus the modular pipeline's `ComponentsManager` dynamic
CPU offload for the 3090. CPU offload is
single-GPU execution; it is not pooled 3090+3060 VRAM. The first contract does
not advertise multi-GPU component placement because the Music 3 modular
pipeline has not been qualified with an explicit device map in this app.

Upstream leaf/group offload can reach smaller GPUs but is materially slower
and needs a current host/cgroup admission check. It remains a future qualified
mode rather than an unchecked switch. SGLang-Omni was rejected for this
integration because its current Torch 2.11/CUDA 13/Transformers 5/SGLang/UCX
stack would be invasive to the shared app environment.

## Community scan

Hugging Face search on 2026-08-13 found:

- `MiniMaxAI/MiniMax-Music3`: official and loadable by this worker.
- `TechnoBaptist/MiniMax-Music3`: same-size unverified mirror, not an
  optimization.
- `diffusers/MiniMax-Music3-aoti`: small compiled supplement for RTX 6000 PRO,
  not a standalone model and not suitable for the detected 3090/3060 pair.
- `diffusers-internal-dev/tiny-minimax-music3`: internal test fixture, not a
  quality model.
- `Comfy-Org/MiniMax-Music-3`: packaging for another runtime and intentionally
  outside this standalone integration.
- `Gluttony10/MiniMax-Music3-INT8-CONVROT`: newly indexed placeholder at scan
  time; repository SHA `3d7135abfef5ddd1455b6033354fe2ce8b5779d1`
  contained only `.gitattributes` and no weights, config, code, or model card.
- Later live API search also found MLX 8-bit, MLX-Serve, W4A8, GGUF, encoder,
  and additional mirror candidates. None currently satisfies the standalone
  Diffusers component contract. W4A8 is the most promising NVIDIA follow-up;
  it needs isolated kernel dependencies, component mapping, license review,
  quality comparison, and peak-memory qualification before registration. MLX
  targets Apple Silicon. GGUF would require a component-aware loader and is
  not a whole-pipeline drop-in. The encoder package is incomplete by design.

PEFT searches for `MiniMax Music3`, `MiniMax-Music3`, and `minimax_music3`
returned no LoRAs. Omni discovery labels future LoRA results as discovery-only
until component targets and an attachment contract are verified. It must not
install a random PEFT repository as a working Music 3 adapter.

The official GitHub repository provides a music-caption-rewriter skill and
structured caption examples rather than another standalone inference workflow.
Their useful input guidance has been distilled into Omni's existing audio
skill; no Comfy workflow is required.

## API and persistence boundary

The gateway exposes status, cheap state, load, generation, cancel, unload,
history, and file retrieval. Installation uses the existing typed
`/api/setup/install-variant` route. Existing `/api/search/hf` gains an optional
`family=minimax_music3` compatibility classifier instead of adding a second
search API.

The worker writes PCM-24 WAV directly beneath
`outputs/omni/minimax_music3/<job_id>/`. The gateway validates the result and
writes `manifest.json`. This avoids carrying roughly 60 MB of six-minute PCM
as roughly 80 MB of base64 through both worker and gateway memory. Cancelling
terminates the exact worker because upstream generation has no qualified
cooperative interruption point.

## Remaining live qualification

- Completed the selective model download through API job `2f6a3a6e`: all 25
  selected files installed atomically in about 7.5 minutes.
- The first runtime install exposed a shared `diffusers<0.40` constraint
  conflict. The isolated installer was corrected to remain `--no-deps` but
  omit the shared-package constraint; retry job `dc3a64ad` installed pinned
  Diffusers `0.40.0.dev0` successfully without redownloading weights.
- Live status now reports weights installed, runtime ready, and no worker
  running. The override metadata and `ComponentsManager` source are present.
- A deliberately oversized 24,576 MB CPU-offload load request returned HTTP
  409 and spawned zero workers. Weight-free analysis accepted a bounded
  17,000 MB CPU budget on physical `cuda:1` (RTX 3090): estimated VRAM 22,528
  MB against a 23,297 MB post-reserve GPU budget and 17,408 MB safe app-cgroup
  headroom. The load route now carries this analyzed plan into worker spawn.
- The first live load exposed an upstream modular-tokenizer mismatch: the
  model index declares slow `Qwen2Tokenizer`, but the official repository
  ships only `tokenizer.json`, `tokenizer_config.json`, and a chat template.
  Diffusers left the tokenizer component `None`, causing generation to fail in
  `MiniMaxMusic3TextEncoderStep`. Omni now registers local
  `Qwen2TokenizerFast` before `load_components`; focused tests cover this.
- Corrected API qualification on physical RTX 3090 (`cuda:1`, worker-local
  `cuda:0`) passed. First load took 646 seconds; corrected warm-cache reload
  took 473 seconds. A 60-second psychedelic minimal-techno vocal song with
  seed `130826` completed in 718.273 seconds as job
  `e848e4d257194d75ab4a81732b7e018c`.
- Media API probing verified a 15,894,572-byte, 60.0700-second, stereo,
  44.1 kHz PCM-24 WAV with integrity `ok`, adjacent readable manifest, and
  byte-range playback support. API unload and exact-worker deletion restored
  the 3090 to 24,326 MB free with zero Omni workers/compute PIDs.
- Verify actual load on the current 3090 only when a heavyweight qualification
  run is intended; project policy forbids loading weights merely to test API
  wiring.
- Generate a short instrumental and a short vocal sample before attempting a
  one-, five-, or six-minute run.
- Confirm actual 44.1 kHz stereo PCM-24 output, media playback, elapsed time,
  peak VRAM, host RAM, early stop behavior, unload, and exact-worker cancel.
- Only after those passes, evaluate sequential/group offload, the official
  SGLang-Omni two-GPU split (GPU 0 Global/RVQ, GPU 1 Flow/DAV), or a real
  multi-GPU device map as separately named experimental strategies.

The MiniMax-Music3 Community License requires visible attribution in a
commercial product and includes commercial-revenue authorization,
acceptable-use, and safeguard provisions. This report is technical guidance,
not legal advice.
