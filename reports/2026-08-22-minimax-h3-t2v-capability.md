# MiniMax H3 T2V capability campaign — 2026-08-22

Status: completed to the current safe runtime boundary

## Scope

- Local MiniMax H3 text-to-audio-video through Omni Studio and ComfyUI.
- Native duration, frame-rate, resolution, aspect-ratio, quality, speed, LoRA,
  reference, acceleration, and multi-device boundaries.
- Target: at least one useful 10+ second clip, with retained repeatable API
  workflows and verified media facts.

## Baseline

- Gateway/bridge: healthy.
- Comfy: `comfy-cuda1-8188`, ready, normal VRAM mode, empty queue.
- Physical RTX 3090: `cuda:1`, UUID
  `GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a`, 24,576 MB total, 23,821 MB
  free at baseline. It is Comfy logical `cuda:0` / `primary`.
- Physical RTX 3060: `cuda:0`, UUID
  `GPU-80c24f57-c604-b55c-5433-5d248304eec9`, 12,288 MB total, 8,612 MB
  free at baseline. It is Comfy logical `cuda:1` / `auxiliary:1`.
- No standalone workers. Both reported compute PID 174850, the selected Comfy
  instance.
- WSL MemAvailable: about 17.1 GiB. Workload cgroup usage was about 19.7 GiB
  against a 21 GiB limit, so only fully staged H3 graphs are admissible.

## Installed stack

- Comfy commit: `c67885b14556cf3e4e061862925282d403d09862`, clean.
- H3 diffusion: `minimax_h3_fl2va_pruned_int8_convrot.safetensors`, 19,998.9 MB.
- Native T2VA encoder: `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`,
  14,960.4 MB.
- Video VAE: `minimax_h3_video_vae_fp16.safetensors`, 4,966.6 MB.
- Audio VAE: `minimax_h3_audio_vae_fp32.safetensors`, 577.2 MB.
- Installed LoRAs: `h3-realism-people-t2v-i2v-r2v.safetensors` (125.2 MB)
  and `minimax_h3_turbo_4step_ema_ckpt500_pruned_comfyui.safetensors`
  (591.6 MB).
- Installed H3 extras: Omni staged nodes, Spectrum H3, SageAttention choices,
  ClipProj, and a 658.6 MB learned latent upscaler.

## Confirmed contracts and research

- Native output is 24 fps.
- Valid duration grid is `17k+5` frames: 124 ≈ 5 s, 243 ≈ 10 s,
  362 ≈ 15 s. Official open-model ceiling is 15 seconds.
- Canvas dimensions must be multiples of 32. Standard grids include
  832x480, 1280x736, and 1920x1088; 720 high is invalid because its latent
  height is odd.
- Official H3 supports text-only T2VA through the FL2VA checkpoint, first/last
  frames, and a separate Ref2VA checkpoint for multimodal references.
- The available Ulysses Comfy node replicates the full roughly 21 GB DiT on
  every participating GPU. It cannot use this heterogeneous 24 GB + 12 GB pair.
  Omni staged component execution remains the correct strategy.
- New Turbo v4 evidence recommends eight Euler/Beta steps at strength 1.0 and
  reports roughly 1.9x speed on 5-second 1344x768 clips. Longer and higher
  resolution cases remain explicitly experimental.

## Test ledger

| Case | Workflow | Status | Settings | Result |
|---|---|---|---|---|
| Native 32B conditioning | `minimax_h3_t2v_124f_base_api.json` before low-memory revision | failed | 608x352, 124f, 24 fps, native 32B encoder | Comfy exited -9 immediately after the 14,960 MB encoder fully loaded; workload cgroup reached its 21 GiB ceiling. No output. |
| Base smoke, 4B ClipProj | `minimax_h3_t2v_124f_base_api.json` | passed | 608x352, 124f, 24 fps, 12 steps, native shifts 12/3, no LoRA | `base_608x352_124f_12step_00001_.mp4`; 5.167 s, 124 frames, H.264, stereo 32 kHz AAC. Total cold run 19:06; sampling 1:52. Contact sheet shows one stable dancer and coherent scene. |
| Native 10-second duration | `minimax_h3_t2v_243f_base_api.json` | failed before sampling | 608x352, 243f, 24 fps, 12 steps, two timed shots, no LoRA | Prompt `8a6c4bf8-941d-439f-ad16-f751cbdb3dca`; the second consecutive cold DiT load stalled at about 19.7 GB GPU residency while the Omni distro stopped accepting commands. Omni-only recovery was required; TQ remained untouched. No output. |
| Turbo v4 control | `minimax_h3_t2v_124f_turbo_v4_api.json` | failed at first denoising forward | 608x352, 124f, 24 fps, 8 steps, Euler/Beta, shifts 12/6, LoRA 1.0 | Prompt `231a2acd-03f9-4351-ab8e-f158def2cfd9`; QKV/int8 eager conversion OOM with 23,904 MiB reserved and 22,008 MiB allocated at peak. No output. |
| Turbo v4 + exact-math low-VRAM attention | `minimax_h3_t2v_124f_turbo_v4_api.json` after low-VRAM revision | interrupted before sampling | Same settings plus four attention head groups | First retry was killed by a duplicate Omni supervisor discovered after recovery. After the supervisor topology was repaired, a clean Comfy cold start read at roughly 0.4 MiB/s and exceeded the gateway's 10-minute startup deadline. The low-VRAM sampling path therefore remains unqualified. |

Initial analysis with a 1,024 MB 3090 planner reserve blocked correctly:
the staged INT8 sampler estimate is 23,422 MiB against 22,797 MiB usable.
The policy was reduced to the previously qualified H3 reserve of 64 MB on the
3090 while retaining 1,024 MB on the 3060. No weights were loaded by analysis.

The first live queue reached a complete 14,960.20 MB text-encoder load, then
the Comfy process was killed with exit code -9. The workload cgroup reported
`memory.max_usage_in_bytes=22550667264` against its approximately 21 GiB cap;
GPU memory recovered fully and the gateway removed the dead instance. This is
an environment/host-mapping capacity boundary, not a video-resolution OOM.

The revised control uses `OmniH3StageFL2VConditioning` without first/last
frames, the installed 4B Qwen3-VL encoder, and the H3 tap-24 projection. The
official FL2VA model defines no-image conditioning as T2VA. This preserves a
genuine text-only test while reducing conditioning weights by about 10 GB.

The revised control completed without OOM. Its diffusion model loaded fully at
19,996.14 MB on the 3090, leaving about 923 MiB free during sampling. Sampling
averaged 9.39 seconds per step. The staged teardown temporarily grew process
RSS while moving the patcher off GPU, then garbage collection reduced it before
the video and audio VAEs were loaded sequentially. Objective audio level was
-23.4 dB mean / -9.3 dB peak.

The pinned Turbo v4 EMA adapter was installed from revision
`4728c77ec8c0a32b9ec62a128f6c118372f5fa1f`. With the normal 64 MiB 3090
reserve, analysis hard-blocked the graph at 24,014 MiB estimated versus
24,001 MiB usable. A separate limit-test policy with zero 3090 reserve made
the plan valid at 24,014 MiB estimated versus 24,065 MiB usable; the 3060
retains its 1,024 MiB reserve.

The Turbo graph reached its first denoising forward, where the eager INT8 QKV
path OOMed. KJNodes' installed `MiniMaxLowVRAMAttention` is designed for this
exact transient and preserves the math by splitting attention heads. Omni's
staged H3 sampler now exposes `low_vram_attention` and
`low_vram_head_chunks`; 66 targeted H3, placement, multi-GPU, and workflow
tests pass. Runtime sampling could not be qualified because subsequent fully
cold Comfy imports fell to roughly 0.4 MiB/s and exceeded the gateway's
10-minute startup deadline. The cgroup OOM-kill counter did not increase.

Final runtime state: one app-owned watchdog, one bridge, one gateway, no Comfy
instance, no queued job, and both GPUs free of Omni model allocations. The TQ
distro was not stopped or modified.
