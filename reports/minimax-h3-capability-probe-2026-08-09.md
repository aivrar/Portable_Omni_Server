# MiniMax H3 local capability and resource probe

Date: 2026-08-09

Status: completed probe pass

## Scope

- Local ComfyUI MiniMax H3 only; no paid/API MiniMax services.
- Probe resolution, trained-range duration, output encoding, GPU placement,
  peak resource use, runtime, failure limits, and source-level traps.
- No application or ComfyUI source files will be changed during this probe.

## Hardware and placement

- Primary: NVIDIA GeForce RTX 3090, 24,576 MiB,
  `GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a`.
- Auxiliary: NVIDIA GeForce RTX 3060, 12,288 MiB,
  `GPU-80c24f57-c604-b55c-5433-5d248304eec9`.
- Instance: `comfy-cuda1-8188` with physical `cuda:1` mapped to logical
  `cuda:0`, and physical `cuda:0` mapped to logical `cuda:1`.
- Staged assignment: text encoder and diffusion sampler on the RTX 3090;
  video and audio VAEs on the RTX 3060.

## Known baseline

- 608x352, 124 frames, 24 fps, 20 steps.
- Successful 5.167-second H.264/AAC output.
- Total prompt runtime: 22:37.
- Diffusion sampling: about 3:02, approximately 9.1 seconds per step.
- Peak observed primary allocation: about 23,566 MiB.
- Stage unload reduced the primary allocation to about 462 MiB before decode.
- Video and audio decode were verified on the auxiliary GPU.

## Probe matrix

| Probe | Resolution | Frames | Seconds | Steps | Save | State |
|---|---:|---:|---:|---:|---|---|
| Baseline | 608x352 | 124 | 5.167 | 20 | H.264 default | passed |
| R1-normal | 768x448 | 124 | 5.167 | 20 | H.264 CRF 18 | failed: primary GPU OOM |
| R1-offload | 768x448 | 124 | 5.167 | 20 | H.264 CRF 18 | failed: unsafe pinned-offload stall |
| Q1 | 768x448 | 73 | 3.042 | 30 | H.264 CRF 15 | passed |
| L1 | 448x256 | 243 | 10.125 | 20 | H.264 CRF 18 | passed |
| L2 | 352x224 | 362 | 15.083 | 20 | H.264 CRF 18 | passed technically; visible chromatic artifacts |

## Early observations

- Cold Comfy startup is I/O-bound for several minutes. The process spent time
  in uninterruptible I/O while importing the runtime and custom nodes.
- Automatic startup selected PyTorch attention, cudaMallocAsync, two-stream
  asynchronous weight offload, and enabled a 23,909 MiB pinned-memory budget.
  Actual resident RAM must be measured; the configured pinned ceiling alone
  does not prove that amount is committed.
- During startup, Linux reported about 1.6-3.5 GiB used and 28-30 GiB available
  RAM even while the Comfy process showed a large resident/map count. Much of
  that apparent footprint was file-backed page cache or mapped model data, not
  a persistent anonymous-RAM leak.

## Results

### R1-normal: 768x448, 124 frames

- Prompt ID: `e1dd212e-deb8-4e06-9d80-9a45f9e3da0b`.
- Static analysis reported `ready_to_run: true`, valid placement, no blockers,
  and estimated 23,422 MiB on the RTX 3090 plus 7,217 MiB on the RTX 3060.
- Runtime failed with `torch.OutOfMemoryError: Allocation on device` in
  `OmniH3StageSampler` before sampling step 1 completed.
- The failing path was the MiniMax DiT feed-forward network's quantized linear
  operation, ending in `torch.cat(scaled_parts, dim=0)` in comfy-kitchen.
- The RTX 3090 reached about 24,108 MiB. The full diffusion model had loaded
  about 19,996 MiB, leaving insufficient space for the larger activation.
- Total prompt time before failure was about 14:42, dominated by cold text and
  diffusion-model reads.
- Staged cleanup worked: primary allocation fell to about 460 MiB immediately,
  the queue cleared, and Comfy remained healthy.
- No media output was produced.

### Planner limitation exposed by R1

The planner returned the same model-memory estimate independent of resolution
and duration. It therefore did not account for sequence-length-dependent
activation memory and accepted a workflow that failed at runtime. Placement
validity currently proves that model assignments are structurally possible; it
does not prove the selected canvas and frame count will fit.

### R1-offload: 768x448 with 4 GiB VRAM reserve

- Prompt ID: `429df24e-af3a-42e8-aecd-34217373d2fc`.
- The reserve correctly prevented a full diffusion-model load and held primary
  VRAM at about 16,358 MiB instead of the previous 24,108 MiB crash point.
- When offloaded weights began using the asynchronous host-transfer path, the
  log emitted repeated `Pin error` warnings. Comfy's queue endpoint, the
  managed stop operation, and WSL filesystem reads then stopped responding.
- At the safety cutoff, Windows reported about 25.7 GiB WSL working set and
  43 GiB private/committed memory. This is materially different from the benign
  file-cache growth observed during normal cold loading.
- The exact managed stop timed out. Terminating only the
  `linbox-Omni_Studio` distro released the stuck process; primary VRAM fell from
  16.3 GiB to effectively zero and WSL working set later returned to about
  4.2 GiB after the app/gateway restarted.
- This combination (`reserve_vram: 4`, pinned memory enabled, async offload,
  WSL) is unsafe on the tested host and should not be used as an automatic
  fallback without a bounded watchdog and a verified non-pinned path.

### Q1: 768x448, 73 frames, 30 steps

- Prompt ID: `c71c5ee3-2ee7-46f2-8be6-01a832ef876d`; cached save retry:
  `ff989a7c-d748-41e9-bf53-74a83844b0a8`.
- Sampling completed in 4:14, averaging 8.48 seconds per step. Primary VRAM
  stayed flat at about 22,958 MiB during sampling and unloaded to 462 MiB.
- The original prompt spent 21:57 through decode but failed only at `SaveVideo`
  because the API prompt used the wrong dynamic-combo serialization. Correcting
  only that final input allowed cached upstream output to save in 4.66 seconds.
- Verified output: 768x448, 24 fps, 3.042 seconds, H.264 plus stereo 32 kHz AAC,
  1,527,366 bytes, approximately 4.02 Mb/s.
- Midpoint visual inspection found a coherent fox, credible snow contact,
  consistent forest geometry, and visibly stronger fine detail than the lower
  resolution long-duration run.
- This duration is below the source-documented trained range. Execution accepts
  it, but the analyzer gives no warning that it is an out-of-training-range
  tradeoff.

### L1: 448x256, 243 frames, 20 steps

- Prompt ID: `9ef7c49f-6069-474f-a0a4-5458366c2ffb`.
- Sampling completed in 3:21, averaging 10.09 seconds per step. Peak primary
  VRAM was about 23,822 MiB and unloaded to 462 MiB before decode.
- Total prompt runtime was 22:08. The inter-stage CPU/cleanup transition again
  took several minutes and process RSS temporarily reached about 12.8 GiB,
  while Linux still had more than 20 GiB available.
- Verified output: 448x256, 24 fps, exactly 10.125 seconds, H.264 plus stereo
  32 kHz AAC, 1,428,649 bytes, approximately 1.13 Mb/s.
- Full-file decode verification reported no errors. Audio was non-silent with
  mean level -30.1 dB and maximum -15.0 dB.
- Early, midpoint, and late frames retained a consistent fox and plausible tree
  occlusions. Fine fur, paw, and snow detail was visibly softer at 448x256.

### L2: 352x224, 362 frames, 20 steps

- The first prompt, `72b22564-59a7-4ba1-a2d2-fac8b5bc6b3c`, was externally
  interrupted at step 7/20 by a host power outage. The replacement prompt,
  `d1be7c3e-0686-460b-843c-7fbb4d05729f`, completed successfully after restart.
- Sampling completed in 3:33, averaging 10.70 seconds per step. Peak primary
  VRAM was about 23,465 MiB and unloaded to about 457 MiB before decode.
- Total resumed-prompt runtime was 26:10. The longer time relative to L1 was
  dominated by fully cold post-reboot reads, not sampling.
- During the staged handoff, the RTX 3060 reached about 6,828 MiB and 99%
  utilization for video VAE decode while the RTX 3090 remained unloaded. The
  installed video and audio VAEs reported 4,966 MiB and 577 MiB loaded.
- Verified output: 352x224, 24 fps, 15.083333 seconds, H.264 plus stereo 32 kHz
  AAC, 1,997,684 bytes, approximately 1.06 Mb/s.
- Full-file decode verification reported no errors. Audio was non-silent with
  mean level -30.9 dB and maximum -16.6 dB.
- Visual inspection found the fox remained recognizable across early, middle,
  and late frames, but strong unnatural rainbow/chromatic bloom appeared in
  the snow and around the fox, especially at the midpoint. This is a technical
  capability success but a practical quality failure for this seed and setup.
- The result shows that being inside the documented trained frame range does
  not guarantee acceptable output at a very small canvas. Maximum duration and
  minimum practical visual quality must be treated as separate limits.

### API-format dynamic input trap

`SaveVideo.codec` is a Comfy V3 dynamic combo. API prompts must flatten it as
`"codec": "h264"`, `"codec.encoding": "re-encode"`, and
`"codec.encoding.crf": 15.0`. A nested UI-style object is accepted by Omni's
workflow analyzer but does not match any dynamic option; Comfy silently drops
the required codec input and later raises `SaveVideo.execute() missing ...
'codec'`. This should be validated before expensive execution.

## Source findings

### MiniMax H3 architecture and scaling

- The installed local model has 50 DiT blocks, hidden width 5,376, 56 attention
  heads with 128 dimensions per head, and feed-forward width 14,336.
- Text, audio, and video are packed into one sequence. Attention is performed
  over that full sequence without a mask, and batch size is restricted to one.
- At 608x352 and 124 frames the video portion is roughly 7,733 packed rows.
  At 768x448 it is 12,432 rows: 1.61x as many video tokens and approximately
  2.58x the dense-attention work before adding text/audio.
- Approximate resolution-only scaling relative to 608x352 at the same duration:
  896x512 is 2.14x the video tokens and about 4.6x attention work; 1024x576 is
  2.76x tokens and about 7.6x attention work; 1344x768 is 4.82x tokens and
  about 23x attention work. These are scaling indicators, not runtime promises.
- Longer clips scale the temporal token count as well. Duration and resolution
  should therefore be increased separately; raising both compounds memory and
  time sharply.

### Loading, offload, and host-memory traps

- The staged node explicitly garbage-collects, empties CUDA caches, trims the
  allocator, and advises Linux to evict model-file pages after stages. This is
  effective at releasing GPU/RAM pressure.
- The tradeoff is substantial repeated disk I/O: later stages and each new run
  can cold-read tens of gigabytes of weights. The observed 14-minute pre-sample
  failure is consistent with that release-versus-reload policy.
- ComfyUI-Manager refreshed remote registry/cache data during startup. That is
  unrelated to generation and adds network/I/O work to a cold launch.
- The installed environment sets deprecated `TRANSFORMERS_CACHE`; Transformers
  warns that `HF_HOME` should be used before version 5 removes the old setting.
- Intermediates are moved to CPU between stages. Video is decoded before audio,
  so decoded video can occupy host RAM while the audio VAE loads and runs.
- Native H3 reference images, keyframes, reference video, and reference audio
  all add packed rows used throughout sampling. They can increase resource use
  beyond a text-only run even at the same output dimensions.

### Current staged-workflow capability limits

- Core local H3 supports text-to-video-with-audio, first/last-frame video, and
  reference-conditioned video/audio paths. The current Omni staged conditioning
  node exposes only prompt, width, height, and length, so it presently exercises
  the text-only path.
- Output length is aligned to the model's `17k + 5` frame cadence at 24 fps.
  The source describes roughly 124-362 frames as the trained range. Higher
  values are accepted up to 3,600 but are explicitly outside the tested range.
- The local latent always contains video and audio. There is no staged input to
  disable audio generation, although the audio stream can be replaced or
  processed afterward.
- H.264 CRF affects compression quality and file size, not generated visual
  detail. Lower CRF preserves more of the generated frames. Ten-bit output is
  available but has broader playback-compatibility risk.

## Conclusions and operating recommendations

1. The best verified balanced setting from this pass is 448x256, 243 frames,
   20 steps at 24 fps: about 10.1 seconds, valid generated audio, and coherent
   motion, with softer detail as the expected compromise.
2. The best verified spatial-detail setting is 768x448, 73 frames, 30 steps:
   about 3.0 seconds. It fits because temporal tokens were reduced, but 73
   frames are below the documented training range, so behavior is not assured.
3. 608x352, 124 frames, 20 steps remains the safer trained-range baseline for
   approximately 5.2-second clips.
4. Do not combine 768x448 with 124 frames under normal full loading on this
   24 GiB primary GPU; it OOMed before completing sampling step 1.
5. Do not automatically retry OOMs using a 4 GiB VRAM reserve with pinned
   memory on this WSL host. The tested configuration entered a pin-error loop,
   made Comfy and WSL control paths unresponsive, and consumed unsafe committed
   host memory.
6. Treat 352x224 at 362 frames as a capability ceiling, not a recommended
   quality preset. It produced a valid 15.1-second file but obvious chromatic
   artifacts. A separate upscale/interpolation or clip-extension workflow would
   be preferable to shrinking H3 generation further.
7. Increasing steps from 20 to 30 increased sampling work roughly linearly and
   did not increase steady-state VRAM. This pass did not isolate steps in a
   same-resolution A/B, so it does not prove that 30 steps alone improves visual
   quality enough to justify the extra 50% sampling time.
8. Use CRF 15-18 when preserving generated frames matters, but do not present
   CRF as a generation-quality control. It only controls H.264 re-encoding.
9. Any automatic preset selector should estimate packed spatial-temporal tokens
   and activation headroom, not only static model size. It should also flag
   frame counts outside 124-362 and validate Comfy V3 dynamic-combo fields.

## Produced media

- `video/probes/Omni_H3_probe_r768x448_f73_s30_crf15_00001_.mp4`
- `video/probes/Omni_H3_probe_r448x256_f243_s20_crf18_00001_.mp4`
- `video/probes/Omni_H3_probe_r352x224_f362_s20_crf18_00001_.mp4`
- `capability_tests/H3_ClipProj_FL2V_Strength0999_FullStaged_00001_.mp4`
- `capability_tests/H3_ClipProj_FL2V_Strength0700_FullStaged_00001_.mp4`
- `capability_tests/H3_ClipProj_FL2V_Spectrum050_FullStaged_00001_.mp4`

All three files are beneath `/opt/omni_studio/output/comfyui`, so Omni's media
library can discover and play them. No application, custom-node, or ComfyUI
source file was modified during this probe; only probe JSON, extracted QA frames,
generated media, and this report were written.

## External follow-up: reference control, ClipProj, and Spectrum

The supplied links and description cover three separate mechanisms. ClipProj is
a text-encoder substitution, Spectrum is an approximate sampling accelerator,
and the hidden reference-strength behavior is already implemented in native
ComfyUI H3 code. They should not be treated as one extension or one feature.

### Native visual/audio reference noise control

- The reported control is real. Installed ComfyUI core defines visual and audio
  conditioning noise-augmentation defaults of `0.999` and `1.0` respectively.
  Its exact conditioning keys are `minimax_visual_cond_noise_aug` and
  `minimax_audio_cond_noise_aug`.
- This is not model fine-tuning. It is an inference-time conditioning-timestep
  and noise-mixing control. For a visual value `a`, native code mixes the
  encoded reference latent as `a * reference + (1 - a) * seeded_noise`.
- Lowering visual strength can therefore change composition, expression, and
  motion details while keeping the generation seed fixed. This was reproduced
  locally at seed `424242`: `0.7` preserved identity, clothing, background,
  and the requested final wave while changing facial expression, hand angle,
  and body pose relative to `0.999`.
- The cited practical range of `0.7` through `0.999` is a sensible conservative
  FL2VA test range. The report that final-frame adherence weakens near `0.5` is
  consistent with the implementation: at `0.5`, half the final-frame latent is
  replaced with noise; at `0.3`, noise is the dominant component.
- Keep audio at `1.0` for reference-audio preservation unless deliberately
  testing audio variation. Because H3 jointly packs audio and visual tokens,
  changing visual conditioning can still indirectly perturb generated audio.
  The two fixed-seed outputs had different decoded audio hashes; mean level was
  `-21.0 dB` at visual `0.999` and `-17.7 dB` at `0.7`.
- Native H3 nodes do not expose these keys in their UI. Omni now exposes them
  through `OmniH3ReferenceStrength` without modifying Comfy core. The new
  `OmniH3StageFL2VConditioning` provides a reference-aware staged path: ClipProj
  ran on the 12 GB auxiliary, keyframe VAE encoding ran on the 24 GB primary,
  and both were explicitly unloaded before diffusion sampling.
- Reducing this value does not reduce memory or token count. Reference rows are
  still packed into the model input and processed throughout denoising.

Relevant installed sources:

- `/opt/omni_studio/comfyui/comfy/ldm/minimax/model.py`
- `/opt/omni_studio/comfyui/comfy/model_base.py`
- `/opt/omni_studio/comfyui/comfy_extras/nodes_minimax_h3.py`

Upstream source: <https://github.com/Comfy-Org/ComfyUI/blob/master/comfy/ldm/minimax/model.py>

### ClipProj-MiniMax-H3

- The [ClipProj matrices](https://huggingface.co/NicoLab28/ClipProj-MiniMax-H3)
  and [ComfyUI-ClipProj node](https://github.com/nicolab28/ComfyUI-ClipProj)
  are the highest-value performance lead in the supplied material for this
  installation. They replace the roughly 15.7 GB Qwen3-VL-32B text encoder
  with Qwen3-VL-4B or 8B plus a learned linear projection into H3's expected
  embedding space.
- The project's reported 4B footprints are about 4.5 GB for int8, 5.2 GB for
  FP8, or 8.3 GB for BF16. That directly targets the slow, approximately
  14,960 MiB text-encoder cold load observed in this probe. It should improve
  prompt-stage VRAM, disk I/O, and cold-start time, but it does not shrink the
  H3 diffusion transformer, latent video/audio, or VAE stages. It therefore
  does not by itself solve the 768x448-by-124-frame sampling OOM.
- The project recommends 4B for text prompting and 8B for image/reference work.
  Its documented tradeoffs include weaker factual/proper-name knowledge,
  possible non-English speech degradation, and projected vision embeddings
  being outside the projection's training distribution.
- REF2VA is reported to require resident encoder mode; the dynamic path can
  fail in Comfy's vision tower with quantized encoders. The matrices should be
  obtained as `.safetensors`; avoid the older `.pt`/pickle format. The current
  matrices must also be used because the first release had an attention-sink
  bug.
- The node can select a GPU, which aligns with Omni's multi-GPU objective.
  `ComfyUI-ClipProj` is now installed at commit
  `ca9b325e83cb02cb5e652570569c6f3f20fee342`, and its five expected live node
  classes were verified. The 4B projection matrix is installed in the new
  `clip_projections` category. The staged FL2V integration uses the extension's
  resident vision path and explicit free operation without changing its source.
- The verified 4B encoder load was about 4.88 GB. A 352x608, 73-frame,
  12-step fixed-seed FL2V run completed successfully with stable likeness,
  first/last-frame control, generated speech, and staged cleanup. Native
  sampling took 73-74 seconds; the first cold end-to-end run took 38:17 because
  model page-in, especially the Qwen file, dominated wall time.
- Before adoption, compare the same prompt and seed using native 32B versus
  projected 4B/8B, paying particular attention to spoken text, proper nouns,
  non-English speech, reference likeness, load time, peak VRAM, and host RAM.

### Spectrum MiniMax H3

- [Spectrum MiniMax H3](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3)
  is an approximate transformer-evaluation skipping method. It forecasts
  selected post-transformer hidden features with Chebyshev ridge regression;
  reconstruction and output heads still run. It is not lossless and may change
  pose, motion, gaze, timing, action, or anatomy even at the same seed.
- It can target T2VA, FL2VA, and REF2VA and may shorten the sampling portion of
  a run. It will not reduce the model cold-loading time that dominated the
  measured end-to-end runs, so its total benefit is likely secondary until
  text/model reload costs are reduced.
- Do not use the linked v0.2.1 as the starting point. The newer
  [v0.2.5 release](https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3/releases/tag/v0.2.5)
  separates bounded causal history from the full offline archive and defaults
  the archive to system RAM. Its release notes say earlier behavior could
  retain roughly 4.3 GiB on the GPU and cause allocator pressure, slowdown, or
  OOM on a 12 GB card. It also adds Turbo sampler support.
- For this system, any experiment should use v0.2.5 or newer with both
  `history_storage=system_ram` and `offline_archive_storage=system_ram`, retain
  `offline_smoothing_replay=true`, start with video blend `0.5` and audio blend
  `0.0`, and perform exact-seed native-versus-Spectrum visual/audio comparison.
  GPU history/archive storage is unsuitable given the narrow sampling VRAM
  headroom already observed.
- Spectrum v0.2.5 is now installed and its live Apply class was verified. Its
  repository is not in the current Manager catalog, so Manager rejected the
  direct repo operation under its security policy; the exact tagged repository
  was installed through the existing asset-node installer and verified after
  one restart.
- `OmniH3StageSampler` now applies the extension at its internal MODEL seam when
  explicitly enabled. The control is default-off and exposes video blend,
  audio blend, and offline replay while fixing both retained stores to system
  RAM. This preserves staged model cleanup and does not patch extension or
  Comfy core source.
- The fixed-seed safe preset completed successfully in `980.294` seconds total
  after a Comfy restart. It produced 352x608, 73 frames, 24 fps, and retained
  close visual agreement with the native `0.999` run (`0.977253` whole-video
  SSIM); identity, composition, and the final wave remained visually stable.
  The decoded audio hash changed despite audio blend `0.0`, with mean level
  `-19.4 dB` versus native `-21.0 dB`. The 3090 reached about 22.9 GB during
  sampling and returned to about 399 MB before decode; the auxiliary retained
  only its small Comfy context after ClipProj cleanup.
- This run does not establish a speedup. Safe offline smoothing replay uses two
  passes, while cold model page-in varied materially between runs. Compare
  sampling-only telemetry over repeated warm runs before advertising a
  performance ratio.

### Recommended evaluation order

1. Use the verified staged ClipProj 4B FL2V path as the constrained-host
   baseline. It materially reduces conditioning residency but does not shrink
   the diffusion or VAE stages.
2. Begin reference work at visual `0.999`/audio `1.0`; try visual `0.7` at the
   same seed when motion, composition, or expression needs freedom. Review both
   video and audio because the audio result can still change.
3. Evaluate Spectrum last and optionally from the native-strength baseline. It
   adds approximation risk and system-RAM history while leaving cold model
   loading intact. Do not combine a new Spectrum setting and a new reference
   strength in the same comparison.

Omni-owned source was extended for projection storage, authenticated Comfy
image upload, native H3 reference controls, staged ClipProj FL2V, and optional
staged Spectrum application. Comfy core and third-party extension source were
not modified.
