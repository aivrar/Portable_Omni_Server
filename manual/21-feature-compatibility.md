# Feature compatibility

Use this guide to choose engines, input types and GPUs for your task.
Each model family has its own supported modalities, runtime dependencies
and memory requirements. Read it alongside the individual workspace pages.
Installation and worker readiness describe different states from successful
generation; the sections below explain the requirements for each task.

## Speech workspaces

The **Voice & TTS** nav item and Home card are labelled **Soon**. There is
no engine picker and no Generate control. The [MOSS](14-moss.md) page has
speech controls; see the decoder compatibility notes below before using them.
Do not wait on this tab during a session.

## MOSS-TTS decoder compatibility

Earlier MOSS-TTS runs saved 48 kHz WAVs. On the tested build, a fresh worker
loaded but inference failed in the upstream decoder with a CUDA driver error
and WSL `dxgresource` exhaustion. Omni retires that worker after the failure.
Do not keep restarting it as a capability probe. MOSS-SoundEffect remains a
separate, qualified path. Previously saved speech or a recorded vocal can
still be used as ACE-Step source audio.

## Moshi has no usable batch or stream path

Moshi weights may be installable for a future duplex transport. The current
API:

- batch infer raises **501**
- Moshi is absent from the stream-handler registry
- Chat hides Moshi
- Testing hides Moshi from batch load

Loading VRAM cannot make Chat or `/api/chat/moshi` work. Do not spawn Moshi
as a capability probe; it wastes 16 GB class VRAM and still 501s.

## Qwen input and output types

Qwen2.5-Omni **text** is verified (3B; 7B GPTQ-int4 text/image on 24 GB).
The worker **does not wire `audio` or `video`**. Those fields return **501**
instead of ignoring the file. Native Talker/TTS is **not registered** on
`/api/tts`. Official upstream speech does not make Omni's route work.

Chat may still *show* capability hints derived from model descriptions.
Trust this page and the disabled-attachment behavior when it matches; if
an attach button is enabled and the server 501s, the server is the source
of truth.

Qwen3-Omni 30B: audio/video also 501, and the installed BF16-class variants
**hard-stop before spawn** on a typical 24+12 GB host (`valid: false`).

## MiniCPM-o: understanding yes, speech and video no

Verified on a 24 GB GPU: text, small-image description, 16 kHz audio
understanding (one modality per request, audio ≤ 60 s).

Unavailable:

- **Video** — explicitly rejected
- **Combined image+audio** — 400
- **Native TTS** (`/api/tts/minicpm_o`) — decoder incompatibility
  (`get_mask_sizes`). Do not use it.

## AnyGPT is not a live multimodal worker

The worker implements **text only** and ignores speech/music/image fields.
Cold load has stayed CPU-side for many minutes, retained ~16 GB RAM, and
missed the bridge window. Delete the worker if you started one by mistake.
Do not autospawn AnyGPT to "see if it works now."

## ACE specialized adapters

Music sub-tabs **Lyric→Vocal** and **Text→Samples** depend on official
adapters that are **unreleased**. The registry **refuses false installs**.
CLI `ace-step lyric2vocal` / `text2samples` will not become working because
you passed `--prompt`.

If a future status row shows those adapters installed with `enables_mode`,
revisit this paragraph. Until then: Generate with lyrics, Vocal→BGM, Audio
Lab, and MOSS-SFX.

The legacy Chinese Rap LoRA pack is PEFT-incompatible and rejected even if
files are on disk.

ACE extend/complete can **rewrite** audio you hoped to keep. Use composition
for stable assembly.

## Music 3 task scope

Official Diffusers subset only. No verified LoRA attach, no voice cloning,
no streaming, no reference audio. Lyrics are required even for
instrumentals. Load plans that are `valid: false` are refusals, not hints.

## Oversized 30B Omni variants

Qwen3-Omni ~60 GB and Nemotron BF16 ~62 GB exceed a 24+12 GB safe budget.
Analysis returns an invalid plan and spawn **hard-stops** before process
creation. Nemotron NVFP4 (~21 GB) is the candidate that should prefer the
24 GB card alone; it is **not live-verified** here. Do not download 60 GB
checkpoints expecting a miracle device map.

GPTQ Qwen uses `single` placement; automatic multi-GPU placement is unavailable.

## Comfy: analysis is not generation

A green requirement check means files and nodes look present. A valid
placement plan means the **estimate** fits current free VRAM. It does not
mean the graph will finish at 4K, 121 frames, or `temporal_size=2048`.
Staged LTX decode has OOMed a 12 GB card on aggressive temporal settings.
Analyze-before-run, then start small.

`prompt_id` is not a PNG. `valid: false` is a hard stop.

Core update and Manager `update_all` are different buttons. Do not skip
verification of each.

## Jobs versus output ids

Polling `/api/jobs` for ACE/Audio Lab/Music 3 generate is wrong. Poll only
when the response says the operation is `running` (installs, compose,
maintenance).

## Tokens and security

Never print `X-Omni-Token`, `omni-cli session show`, or a newly created
bearer key in logs, tickets, or git. Loopback does not mean "safe to paste
in Discord."

## Related pages

- [Voice & TTS](10-voice-and-tts.md)
- [Chat](04-chat.md)
- [Music](12-music.md)
- [GPU placement](19-gpu-placement.md)
