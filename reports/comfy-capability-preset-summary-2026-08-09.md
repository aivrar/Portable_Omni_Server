# Omni Studio local capability preset summary

Qualified on RTX 3090 (24 GB) plus RTX 3060 (12 GB) in the 32 GB Omni WSL
guest. These are regression baselines for this machine, not universal speed
claims. Analyze every graph against current GPU UUIDs before execution.

## Recommended presets

| Family | Starting preset | Qualified result / limitation |
|---|---|---|
| MiniMax H3 FL2V | ClipProj 4B on 3060; keyframe VAE, sampler, and decode staged on 3090; 352x608, 73 frames, 24 fps, 12 `res_multistep`/`simple` steps | First/last-frame video and generated speech succeeded. Start reference strength at visual `0.999`, audio `1.0`; visual `0.7` changed motion/expression while retaining identity and final pose, but audio also changed. |
| MiniMax H3 Spectrum | Same native-strength graph; video blend `0.5`, audio blend `0.0`, offline replay on, RAM history/archive | Visually close to native (`0.977253` SSIM) and cleaned up correctly. Approximate and not yet proven faster because safe replay is two-pass and cold I/O varied. |
| Krea 2 Turbo | INT8, 1024 square, 8 steps, CFG 1, Euler/simple, native zero-negative graph | Qualified; warm run is the practical default. Cold model I/O dominates. |
| Krea 2 Raw | INT8, 1024 square, 52 steps, CFG 3.5, resolution shift `0.90625`, separately encoded empty unconditional | Do not reuse Turbo's fixed shift or zeroed-positive negative semantics. |
| HiDream-O1 Dev | 1024 square T2I, 28 steps, `ModelNoiseScale=7.6`, LCM, CFG 1 | Qualified in 11.3 seconds warm. Native image edit requires four-megapixel latent sizing; 2048 single-reference edit succeeded. Multi-reference can create separate people instead of fusing identity. |
| Ideogram 4 INT8 | Conditional model + Qwen on 3090, unconditional on 3060, `OmniStageVAEDecode` on 3090; Euler, 20 steps, official scheduler/guider | 1024 completed in 609.664 seconds; 2048 in 944.141 seconds. 2048 improved exact spelling and removed filler gibberish, but neither guaranteed requested typography placement. Non-commercial license applies. |
| BiRefNet | Native `RemoveBackground` plus correctly inverted mask into `JoinImageWithAlpha` | Qualified on portrait/profile/full-body fixtures. Verify alpha and mask, not only RGB preview. |
| Wan Animate 2 | 480x832, 21 frames, 6 simple steps, LCM, CFG 1, CPU INT8 Wan cache | Matched human reference/driver completed warm in 88.19 seconds with stable identity and hands. Mismatched fox driver transferred crouched/animal motion. Stop the exact instance after the batch because `/free` did not hard-release this routed graph. |
| HuMo lip motion | 384x384, 25-49 frames; low VRAM, pinned memory off, `reserve_vram=10`, preferably cache none | Local portrait plus ACE-Step audio succeeded with visible mouth motion. Audio is reused, not regenerated. The global reserve can push auxiliary components off GPU. |
| ACE-Step singing | 2B + 0.6B LM, BF16, 8 Euler steps, CFG 1 | 15-second stereo song completed warm in 24.427 seconds. XL + 1.7B completed in 17.658 seconds but was less tempo-faithful and has very slow cold shard loads. |
| MOSS TTS | Record language, seed, frame cap, temperature, top-p/top-k, duration, rate, and channels | Keep TTS unloaded before heavy Comfy work; delete its exact worker for a zero-context GPU handoff. |

## Explicitly bounded or deferred

- LTX 2.3 first/last-frame: local blueprint is UI-only and its missing 29.53
  GB checkpoint plus 9.45 GB encoder exceed total guest RAM before runtime
  overhead. It was not downloaded because H3 already qualified first/last
  animation and no materialized, analyze-valid LTX API graph exists.
- CPU VAE encode/decode is a compatibility fallback, not a normal preset. The
  tested H3 encode and Ideogram decode paths were impractically slow and did
  not promptly honor interrupts inside the VAE call.
- `/free` is best-effort. Stop the exact Comfy instance when a routed graph
  retains material VRAM after cleanup.

## Operational entry points

- Canonical API guide: `docs/comfy-api.md`
- Agent API skill: `skills/omni-comfy-api/`
- Capability-testing skill: `skills/omni-comfy-testing/`
- Detailed evidence: `reports/comfy-full-capability-findings-2026-08-09.md`
  and `reports/minimax-h3-capability-probe-2026-08-09.md`
