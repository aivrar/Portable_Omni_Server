---
name: omni-audio-api
description: Operate, inspect, test, document, or modify Omni Studio's local audio APIs. Use for ACE-Step and MiniMax Music 3 song generation, Stable Audio and Audio Lab tools, MOSS-TTS speech, MOSS-SoundEffect and Stable Audio sound effects, audio model or LoRA discovery and installation, GPU-specific audio workers, output-library playback, long-form crossfade composition, cleanup, and troubleshooting inside the Omni_Studio app.
---

# Omni Audio API

Use Omni's authenticated API for local music, audio, and speech work. Keep
model and worker lifecycle explicit so an inference request never silently
loads a large model on the wrong GPU.

## Establish context

1. Treat the directory two levels above this skill as the Omni Studio app root.
2. Read the app-root `AGENTS.md`.
3. Read the relevant sections of `docs/audio-api.md` completely before acting.
4. Inspect the current typed route model before using an unfamiliar field.
   When the gateway was deliberately started with `OMNI_ENABLE_DOCS=1`, its
   `GET /openapi.json` response is also authoritative.
5. Never print, log, or persist `X-Omni-Token`.

## Route the task

- For installation, model selection, LoRAs, or deletion, read **Asset status
  and installation** and the relevant engine section in `docs/audio-api.md`.
- For ACE-Step songs, lyrics, covers, edits, continuation, Vocal-to-BGM, or
  LoRA packs, read **ACE-Step music**.
- For MiniMax Music 3 installation, structured captions, lyrics, long songs,
  community variants, or LoRA discovery, read
  [`references/minimax-music3.md`](references/minimax-music3.md) and the
  **MiniMax Music 3** section of `docs/audio-api.md`.
- For Stable Audio generation, ranking, audio-to-audio, inpaint, CLAP, or VAE
  work, read **Stable Audio Lab**.
- For sound effects, read **Sound effects with two engines** and test MOSS-SFX
  and Stable Audio as separate pipelines.
- For MOSS-TTS or feeding speech into ACE-Step, read **Local TTS and
  cross-tool audio**.
- For long tracks and the media library, read **Long-form composition and
  outputs**.
- For GPU choice, unloading, failure recovery, or live testing, read
  **Devices, workers, and cleanup** and **Verified behavior and limitations**.
- For cross-engine scheduling or deciding whether multiple workers can coexist,
  also read `../omni-gpu-orchestration/SKILL.md`.

## Use the operating sequence

1. Call `GET /api/devices`, the relevant install-status route, and the
   engine's cheap state route with `autospawn=false`.
2. Install only missing assets. Poll the returned `job_id` through
   `GET /api/jobs/{job_id}` until `done`, `error`, or `cancelled`.
3. Select an explicit `cuda:N` from the current device response when GPU
   placement matters. Do not assume the largest or secondary GPU is free.
4. Load the selected model explicitly and verify the returned state before
   inference. Treat model load time as separate from generation time.
5. Make one inference request at a time. ACE-Step and Audio Lab generation
   calls are synchronous even though their persisted result contains a
   `job_id`; use an adequate client timeout and do not retry blindly.
   While such a call is active its worker reports `busy`. If the server-side
   inference timeout fires, Omni retires that exact worker; query state before
   deciding whether a retry must first reload the model. A worker-side TTS 5xx
   also retires that exact worker because CUDA/descriptor state may be poisoned.
6. Verify the returned output URL, manifest, duration, sample rate, and media
   library entry. Listen to results when judging music quality or continuity.
7. Detach LoRAs, unload model components, and delete the exact worker when the
   user does not want it resident.

## Use the bundled client

Use `scripts/omni_audio_api.py` for token-safe JSON calls, binary TTS output,
job polling, and multimodal file embedding. It reads `OMNI_API_TOKEN`,
`--token-file`, or the normal local runtime token file and never prints the
token. Use repeatable `--base64-field image=PATH`, `audio=PATH`, or
`video=PATH` arguments when a typed route expects raw base64.

```text
python skills/omni-audio-api/scripts/omni_audio_api.py request GET /api/devices
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/ace_step/load --body load.json --timeout 900
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/tts/moss_tts --body tts.json --output speech.wav --timeout 900
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/chat/qwen_omni_3b?autospawn=false --body chat.json --base64-field image=frame.png --timeout 900
python skills/omni-audio-api/scripts/omni_audio_api.py wait-job JOB_ID
```

The client sends mutations directly. Before a `DELETE`, resolve and report the
exact model, LoRA, output, or worker target; asset and output deletion is
immediate.

## Guard model and worker state

- Use `GET /api/ace_step/status` and `GET /api/audio_lab/status` for installed
  assets. Use `/state` for runtime-loaded components.
- Do not confuse a persisted inference `job_id` with a background job. Poll
  `/api/jobs/{job_id}` only for requests that explicitly return
  `status: "running"`, such as installs and compositions.
- Keep ACE-Step and Stable Audio worker families independent. CLAP ranking for
  ACE-Step may reuse a running Audio Lab worker.
- Keep MiniMax Music 3 independent from ComfyUI and ACE-Step. Its worker writes
  multi-minute WAV output directly to managed storage; never add a base64
  gateway hop. Cancelling terminates the exact Music 3 worker and unloads it.
- Use the exact worker ID from state or `GET /api/workers` when deleting a
  worker. Avoid `kill-all` unless the user asked to stop every model service.
- Do not infer modality support from typed fields or installed weights. Qwen
  2.5 Omni text/image is verified, but its current Omni worker explicitly
  rejects unwired audio/video with 501 and has no native TTS handler. Check
  `docs/capability-confidence.md` before any general multimodal worker test.
- MiniCPM-o 2.6 text, image, and audio understanding are live-verified on the
  3090. Use one modality per chat request; audio is mono 16 kHz with a 60-second
  limit. Video is explicitly unsupported. Its existing TTS route is still
  blocked in the native decoder at `get_mask_sizes`, so do not advertise
  MiniCPM speech output as working.
- MOSS-TTS currently loads on the 3090 but a fresh inference failed in the
  upstream decoder with `CUDA driver error: unknown error` and exhausted 1,024
  WSL `dxgresource` descriptors. Do not retry that worker. Confirm the gateway
  retired it, delete the exact ID if an older runtime left it present, and use
  an existing persisted TTS fixture or another verified engine.
- Do not autospawn Moshi merely to probe it: the batch handler is a hard 501
  and the current stream registry has no full-duplex audio transport. AnyGPT's
  worker is text-only in source, and its cold load exceeded the bridge window
  while retaining about 16 GB CPU RAM. Treat both as blocked API capabilities
  until their transport/loader designs are addressed.
- Qwen3-Omni and Nemotron 30B CUDA loaders enforce variant capacity before
  heavy import/loading. Never bypass an insufficient-VRAM error. The installed
  60/62 GB variants do not fit either GPU; Nemotron NVFP4 (~21 GB) is the only
  configured 30B candidate expected to fit the 3090 and is not yet verified.
- Current ACE qualification adds `dpmpp` and Heun scheduler passes; 2B SFT and
  Turbo Continuous cores; Chinese New Year, lofi, raga, and acoustic LoRAs;
  and a 240-second direct generation. The 4B LM exceeded practical 24 GB
  residency, and the old Chinese Rap package is not a PEFT-compatible adapter.
- For Stable Audio, verified native community variants include SAO
  Instrumental, Audialab EDM, Nekochu Music, Infinite Pianos, and Vocal
  Textures. The Tuned-100k VAE now works through its declared stock Stable
  Audio 2.0 autoencoder profile; generation, VAE reconstruction, unconditional
  generation, and music-CLAP scoring all pass. Do not infer that every
  installed registry package is loader-compatible.
- For standalone Qwen/MiniCPM/Nemotron/AnyGPT/Moshi worker pools, Hugging Face
  layer sharding, or CPU offload, switch to `skills/omni-model-api/SKILL.md` and
  `docs/omni-model-api.md`. Do not confuse that policy with ACE-Step/Audio Lab
  component workers.
- Cancel first only when inference is active. Then unload and delete the exact
  worker to release both GPU memory and CPU-side model caches.
- Never assess GPU cleanup from process RAM alone. Recheck `/api/workers` and
  current device memory after deletion.

## Verify proportionally

- Compile changed Python with `python -m py_compile`.
- Run focused tests for the touched feature. The core cross-tool suite is:
  `tests.test_ace_step_staging`, `tests.test_audio_compose`,
  `tests.test_ace_step_audio_sources`, and `tests.test_moss_tts_contract`.
- Keep live checks small and sequential. Do not load a model merely to test
  JSON wiring; use typed-model/unit tests for that.
- For a live inference, use one short sample before long songs or ranked fanout.
- Record source-level findings in the dated report without treating subjective
  listening quality as numerically verified.

Report the selected physical GPU and `cuda:N`, model/LM/LoRA state, request
settings, returned identifier and output URL, media facts, cleanup result, and
any timing contamination from unrelated host load.
