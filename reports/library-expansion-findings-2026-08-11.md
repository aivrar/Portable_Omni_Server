# Omni Studio library expansion findings — 2026-08-11

## Scope and safety

Omni Studio stayed visibly open while the authenticated API drove small,
sequential tests. The RTX 3090 was the primary heavy-work GPU and the RTX 3060
remained auxiliary/display-capable. Every heavyweight worker was deleted before
the next family. No broad process scan, CPU stress test, or speculative large
download was used.

## ACE-Step

- Euler, Heun, and DPM++ same-seed 12-second generation all passed. Recorded
  inference times were 36.428, 22.604, and 18.456 seconds respectively.
- ACE 2B SFT and Turbo Continuous installed, cold-loaded, generated bounded
  outputs, and cleaned up.
- ScragVAE is compatible and generated successfully. The official
  Stable-Audio-format ACE VAE expects a different weight layout, while HOT-Step
  is GGUF-only; both are current format blockers rather than retry candidates.
- Chinese New Year, lofi, raga, and acoustic fingerstyle LoRAs attach,
  generate, detach, and clean up. The legacy Chinese Rap repository lacks the
  PEFT adapter contract and fails deterministically.
- A direct 240-second DPM++ instrumental completed in 113.984 seconds at 48
  kHz. Analysis measured about 104 BPM against a requested 126 BPM, so 360/600
  second direct escalation was gated; use composition for dependable long-form
  structure.
- The 4B LM plus 2B core exhausted practical 24 GB residency and failed while
  moving the LM to CUDA. The exact worker was deleted; do not blindly retry.

## Stable Audio Lab

Short persisted generation passed for Foundation-1 diffusers, SAO
Instrumental, Audialab EDM, Nekochu Music, Infinite Pianos, and Vocal Textures.
All native community variants reported the expected 44.1 kHz path and 120-second
declared ceiling.

The Tuned-100k VAE repository intentionally omits `model_config.json` and states
that its config is the stock Stable Audio 2.0 autoencoder. Omni's loader was
fixed to use that explicit profile, choose the fully tuned checkpoint, preserve
the native pretransform wrapper, and satisfy diffusers' `hop_length` and latent
distribution contracts. After the fix:

- 10-second conditioned generation passed (`0c6aaadb586f40d4bc4b85c889e61dc2`).
- VAE reconstruction passed at 9.9846 seconds with diff RMS `0.02044373`
  (`2c4fb702644c4e1ea6cbb6acafe20d63`).
- Four-second unconditional generation passed
  (`d373447e4618416493cc61fa8818660d`).
- `larger-clap-music` scored the matching vocal texture at `0.3722278`.

## Standalone Omni workers

- Qwen2.5-Omni 3B base loaded on the 3090 and accurately described the real
  portrait fixture in 18.1 seconds.
- Qwen2.5-Omni 7B GPTQ-Int4 loaded on the 3090 and returned the exact requested
  deterministic text in 17.6 seconds.
- MiniCPM-o 2.6 base loaded on the 3090 and correctly identified the jacket and
  background colors in 15.8 seconds.
- Cold starts took roughly 4–8 minutes under concurrent host load. One spawn
  request was kept alive; none was duplicated.
- Installed Qwen3-Omni Instruct is invalid at an estimated 61,440 MB versus a
  32,312 MB combined GPU budget. Installed Nemotron BF16 is invalid at 63,488
  MB versus 32,307 MB. These are hard stops. Nemotron NVFP4 analyzes as fitting
  the 3090 alone but is not installed or transport-qualified.
- Moshi remains blocked by the absent full-duplex API transport. AnyGPT remains
  text-only in source with the previously documented unbounded cold-load issue.

## Comfy and optimized-weight discovery

One Comfy instance started with the 3090 primary, 3060 auxiliary, cache disabled,
and automatic dynamic/offload options. The fully staged MiniMax-H3 ClipProj FL2V
graph analyzed ready with all five models and seven runtime nodes installed. Its
23,422 MiB sampler stage maps to the 3090, its 6,518 MiB text stage maps to the
3060, and staged unloading makes the plan valid.

The live registry found current Krea Turbo GGUF families, QuantStack Wan Animate
GGUF variants (about 6.46 GB at Q2), Unsloth LTX-2.3 GGUF variants (about 7.94
GB at Q2), and official/community MiniMax-H3 quant stacks. These are discovery
leads only: no candidate was downloaded without an installed compatible loader
and coherent API graph.

Live Krea and Wan analysis exposed a gateway consistency bug: model/node
readiness was reported as runnable even when `placement_plan.valid` was false.
The ordinary Krea graph mapped 18,110 MiB and Wan mapped 24,458 MiB against a
current 16,659 MiB host-model budget. Omni now returns
`readiness=needs-placement` and `ready_to_run=false` for any invalid placement.
The analyze-only campaign queued no workflow, and the exact Comfy instance was
stopped.

## Code and regression evidence

Changed Omni-owned files:

- `server/audio_lab_loaders.py`
- `server/config.py`
- `server/routers/workflows.py`
- focused regression tests and the relevant API/domain guides and skills

The combined focused suite passed 49 Audio Lab, Comfy placement, multi-GPU, and
workflow-requirement tests. Subjective music/image/video quality remains a
human review task; API compatibility, artifact integrity, placement validity,
and cleanup are the claims made here.
