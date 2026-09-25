# Local model-family baseline matrix

These are starting points, not universal winners. Preserve a fixed prompt/input
and change one variable at a time.

## MiniMax H3

- Use local FL2V/Ref2VA graphs only; paid API templates are out of scope.
- Baseline the installed INT8 diffusion model with the paired video/audio VAEs.
  For FL2V on constrained host RAM, the verified ClipProj path uses
  `qwen3vl_4b_fp8_scaled.safetensors` plus
  `h3_qwen3vl_4b_tap24.safetensors`; it loaded about 4.88 GB instead of the
  native 32B encoder's roughly 15.7 GB conditioning footprint.
- Use `OmniH3StageFL2VConditioning` to place ClipProj on a selected auxiliary
  GPU and keyframe VAE encoding on the primary, then unload both before
  `OmniH3StageSampler`. Keep video and audio decode in the later staged node.
  Do not use CPU keyframe VAE as the normal fallback: it was safe but too slow
  and did not respond promptly to an interrupt while inside the VAE call.
- Reference trust is a conditioning value: default image/video `0.999`, audio
  `1.0`. Test `0.9`, `0.7`, then `0.5`; FL2V last-frame control can degrade near
  `0.5`, so `0.7` is the practical lower exploratory point.
- Keep seed fixed when preserving an FL2V voice/intonation and varying motion,
  but do not assume audio remains identical. At fixed seed `424242`, visual
  strength `0.7` preserved identity, clothing, background, and the requested
  final wave while changing expression, hand angle, body pose, and decoded
  audio. It measured -17.7 dB mean versus -21.0 dB at `0.999`.
- Verified short reference preset: 352x608, 73 frames at 24 fps, 12
  `res_multistep`/`simple` steps. Native sampling was 73-74 seconds after model
  load; the first cold end-to-end run took 38:17 because storage page-in,
  especially the Qwen file, dominated wall time.
- Spectrum is optional and approximate. Start through the staged sampler with
  video blend `0.5`, audio blend `0.0`, offline smoothing replay enabled, and
  both retained histories in system RAM. Compare at identical seed and inputs;
  never use VRAM archive storage on a constrained auxiliary GPU. This preset
  completed in 980.294 seconds end-to-end after a Comfy restart, produced
  352x608/73-frame output, and cleaned up the diffusion stage. Relative to the
  fixed-seed native `0.999` output, video SSIM was `0.977253`: likeness,
  composition, and the final wave were visually close, but decoded audio still
  differed and measured -19.4 dB mean. Do not claim a speedup from this one
  run because safe offline replay uses two passes and cold storage I/O varied.
- Turbo LoRAs belong inside `OmniH3StageSampler`, not in an ordinary upstream
  `LoraLoaderModelOnly` graph. Set `lora_name` and `lora_strength` on the
  staged sampler so the adapter is loaded and released with the diffusion
  stage. The planner counts the LoRA in that stage's peak estimate.
- The staged sampler exposes native H3 `sigma_shift_video` and
  `sigma_shift_audio`. Preserve the native `12/3` baseline. The initial
  larryvrh/drbaph EMA Turbo candidate uses strength `1.0`, 8 steps,
  `res_multistep`/`simple`, and shifts `12/6`; do not change any additional
  axis in that comparison.
- Treat the third-party H3 Turbo evaluation at repository revision
  `f13bf725c1a0cdbf913cafe8264eaca42e01bb4b` as a candidate generator, not a
  local performance claim. It used one RTX 5080, one seed, 960x544, 124 frames,
  and a different Comfy/Torch stack. Its useful stress categories are
  sustained singing, overlapping speech, code-switching, off-screen sound,
  silence-to-transient range, hard cuts, reflections, hands, metronomic A/V
  timing, and dense polyphony.
- Qualification order is native fixed-seed baseline, EMA Turbo without an
  attention override, the identical Turbo graph with SageAttention, then
  optional Spectrum as a separate draft-only axis. The global Sage startup
  option is only valid when an exact compatible `sageattention` wheel imports
  successfully. Prefer the exact `sage_attention` choice returned by live
  object-info (the 2026-08-12 CUDA test used
  `sageattn_qk_int8_pv_fp16_cuda`) rather than an informal shorthand. This
  applies KJNodes' patch only to the staged H3 diffusion model. Record whether
  Sage is global or a per-model patch; they are different configurations.
- On the 2026-08-12 Torch 2.7.1+cu128 install, the 352x608/49-frame native
  20-step control completed, but the drbaph/larryvrh EMA ckpt500 LoRA at eight
  steps OOMed on the 24 GB RTX 3090 both without Sage and with the exact
  per-model Sage CUDA kernel. Peak residency reached about 23.7 GiB; do not
  treat installed files plus `ready` analysis as a qualified Turbo path.
- A second run with `vram_mode=low` and `reserve_vram=4` correctly reduced
  usable weight memory from about 22.0 GiB to 18.4 GiB, but LoRA
  patching/offload made the Omni distro and both HTTP services unresponsive
  with WSL socket-buffer exhaustion. It was aborted by gracefully closing only
  Omni Studio. Do not repeat that interactive configuration on this runtime.
  Requalify after a compatible Torch/Comfy update or a proven lower-memory LoRA
  patching implementation. The external RTX 5080 used Torch 2.12.1+cu130, so
  its fit and speed cannot be transferred to this environment.
- Record cold load, sampling, and decode separately. A same-seed result can
  still be a different take after changes to steps, sampler, LoRA, Spectrum,
  or Comfy's H3 audio transport, so compare voice character and A/V behavior
  rather than assuming frame identity.

## Krea 2 OSS Turbo and Raw

- Shared files: `qwen3vl_4b_fp8_scaled.safetensors` in `text_encoders` and
  `qwen_image_vae.safetensors` in `vae`.
- Turbo INT8: `krea2_turbo_int8_convrot.safetensors`; baseline 8 steps, CFG
  1.0 in native Comfy's zero-negative graph, Euler/simple, 1024 square.
- Raw INT8: `krea2_raw_int8_convrot.safetensors`; upstream baseline 52 steps,
  CFG 3.5, 1024 square. Raw is a base/post-training model and is not expected to
  beat Turbo on routine prompt-following per unit time.
- Raw must not inherit Turbo's fixed flow shift or zeroed-positive negative
  conditioning. At 1024, use the official resolution-derived shift `0.90625`
  and a separately encoded empty-string unconditional branch. Omni analysis
  returns `needs-review` for the known-bad semantics.
- Local 3090+3060 routed reference: Turbo at 1024 square measured 552.227
  seconds from a clean cold state and 23.080 seconds warm with a new seed. The
  8 sampling steps were about 20-31 seconds; cold weight I/O dominates. Treat
  these as this machine's regression baseline, not a portable speed claim.
- Current quality recommendation: Turbo 1536x1024 completed in 418.604 seconds
  and is the best balanced qualified output. Turbo 2048 square completed in
  398.437 seconds with strong detail but duplicated a lantern, so treat 2048
  as a frontier composition check rather than an automatic quality default.
- Corrected Raw at 1024 succeeded at both 16 and 52 steps (465.098 and 557.780
  seconds). Extra steps improved detail but did not remove the inset-image
  composition artifact; do not spend 52 steps expecting that semantic issue
  to disappear.
- Optional style reference and style LoRAs are separate tests after base T2I.
- Top-level analyze readiness must agree with `placement_plan.valid`. An
  invalid host-mapping or GPU plan reports `readiness=needs-placement` and
  `ready_to_run=false`; never queue from model/node readiness alone. On the
  current 21 GiB workload cgroup, the ordinary Krea Turbo smoke maps about
  18.1 GiB against a roughly 16.7 GiB host model budget and is blocked until
  staged or given more host capacity.

## Ideogram 4 local

- Requires both conditional and unconditional INT8 diffusion files, the
  Qwen3-VL 8B FP8 text encoder, and the declared VAE.
- Use structured JSON captions for the primary alignment test; compare one
  plain-text prompt separately. Include typography and native 2K only after a
  smaller smoke.
- On a 3090+3060, route the conditional model and Qwen to the 3090 and the
  unconditional model to the 3060, then replace ordinary `VAELoader` plus
  `VAEDecode` with `OmniStageVAEDecode` targeting the 3090. This unloads both
  diffusion models before Flux2 VAE decode and avoids the impractical CPU VAE
  fallback.
- Qualified official-setting results: 1024 square completed in 609.664 seconds
  cold; native 2048 completed in 944.141 seconds with the same prompt and 20
  steps. The 2048 output made all four requested text strings legible and
  removed 1024's gibberish filler, but both chose vertical editorial text
  placement instead of the requested horizontal layout. Use 1024 for a smoke
  or faster concept pass and 2048 when exact spelling matters more than time;
  neither resolution guarantees requested typography placement.
- Both runs released the dual diffusion stage before decode and returned to
  about 0.9 GB on the display 3060 and 0.4 GB on the 3090 after completion.
- Record the non-commercial license constraint in findings.

## HiDream-O1-Image

- Dev FP8 plus Gemma4 E4B FP8 is the lower-cost baseline: 28 steps.
- Full FP8 uses 50 steps and is the preferred editing/quality comparison.
- Begin at 1024; test 1536/2048 only after memory cleanup is confirmed.

## BiRefNet

- `birefnet.safetensors` belongs in `background_removal`.
- Test neutral portrait, profile/fine hair, and full body. Verify actual RGBA
  alpha range and inspect the mask, not merely the RGB preview.
- `RemoveBackground` outputs a foreground mask; the native
  `JoinImageWithAlpha` path expects the matching inversion used by the official
  template.

## Wan Animate 2

- Treat current Wan Animate 2 as direct reference-image plus driving-video
  animation; do not substitute the older Wan 2.2 preprocessing workflow.
- Baseline with the official INT8 diffusion model, UMT5 FP8 text encoder, CLIP
  vision H, Wan VAE, and optional four-step LightX2V LoRA.
- Qualified short preset on 3090+3060: 480x832, 21 frames, CPU INT8 Wan cache,
  `ModelSamplingSD3(shift=5)`, simple scheduler at six steps, LCM, CFG 1, and
  24 fps output. It measured 643.75 seconds cold and 88.19 seconds warm.
- Extending that shape to 49 frames OOMed in `SamplerCustom` after 637 seconds
  even though static placement was valid. Reducing the duration case to
  384x640/49 frames succeeded in 510.390 seconds and produced 2.04 seconds at
  24 fps. Prefer 480x832/21 frames for detail and 384x640/49 for duration.
- Match subject class, full/half-body crop, and camera framing between reference
  and driver. A fox-running driver transferred crouched quadruped motion and
  limb semantics to a human; a matched human wave driver preserved identity,
  hand structure, clothing, and background.
- CPU cache avoids the template's documented VRAM spike. Full 832x480/81-frame
  BF16 cache is approximately 12.5 GB RAM; shorter windows and INT8 reduce it.
- Current placement underestimates observed primary residency (18.8 GB plan
  versus about 20.3 GB live) and does not list CLIP Vision as its own component.
- `/free` did not release the routed Wan graph in qualification. Stop the exact
  instance for a dependable hard release before loading another model family.
- The same host-mapping gate blocks the ordinary installed Wan smoke at about
  24.5 GiB referenced weights versus the current roughly 16.7 GiB budget.
  QuantStack GGUF discovery currently finds Q2 through Q8 variants, but those
  require an exact GGUF-compatible graph before download or qualification.

## InfiniteTalk native single-speaker

- Dependencies: Wan 2.1 I2V 14B 480p FP8, the single-speaker InfiniteTalk
  model patch, wav2vec2 Chinese base FP16, UMT5 XXL FP8, Wan 2.1 VAE, and the
  optional LightX2V I2V distilled LoRA.
- The authenticated Comfy `upload/image` route accepts local WAV files as
  `LoadAudio` inputs; keep using the API rather than copying ad hoc files.
- `InfiniteTalkAutoSampler.length` is the per-pass generation window. It does
  not truncate the audio. Compute the pass count with the formula in
  `capability-runbook.md` and trim one-pass smoke audio to the window first.
- The initial two-GPU plan placed the 14B model and wav2vec2 on the 3090, with
  UMT5 and the VAE on the 3060. Treat that as planned placement until a bounded
  output passes visual/audio inspection; the first untrimmed run expanded to
  five passes and was stopped.
- A corrected 3.0-second fixture computed exactly 75 frames and one pass, but
  simultaneous mapped/offload state exhausted the 32 GB distro before output.
  Live analysis now blocks this ordinary graph at 28,312 MiB referenced
  weights versus a 16,680 MiB current host mapping budget. No output is
  qualified on this host until a staged implementation or smaller compatible
  stack exists.

## HuMo local audio-driven video

- Dependencies: HuMo 17B FP8 in `diffusion_models`, Whisper Large V3 FP16 in
  `audio_encoders`, UMT5 XXL FP8 in `text_encoders`, Wan 2.1 VAE, and the
  LightX2V 14B distilled LoRA.
- Qualified input path: rights-safe portrait plus local ACE-Step WAV through
  `WanHuMoImageToVideo`. The output reuses the supplied audio; HuMo does not
  synthesize a replacement soundtrack in this graph.
- On the tested 24 GB 3090 primary, normal mode and low-VRAM without an
  explicit reserve both full-load the 16.27 GB base model and OOM while
  applying the LoRA/first convolution. Resolution reduction alone does not
  fix this boundary.
- Verified start: `vram_mode=low`, `disable_pinned_memory=true`, and startup
  options `reserve_vram=10`, `cache_policy=ram_8`,
  `disable_smart_memory=true`, `async_offload=disabled`. Analyze blocks a
  24 GB HuMo plan unless low mode has at least a 9 GB startup reserve (or the
  instance is `none`).
- The reserve is global: it also limits a 12 GB auxiliary and can force UMT5
  and VAE off GPU despite correct placement. This is safe but slower.
- Qualified outputs: 384x384/25 frames/1.0 second took 12:01 cold; a warm
  384x384/49-frame override took 301.57 seconds and produced 1.96 seconds.
  Both were H.264/AAC at 25 fps with stable identity and visible mouth motion.
- `cache_policy=ram_8` retained about 14.4 GB process RSS after `/free`.
  Prefer `cache_policy=none` when automatic RAM release matters more than warm
  latency, or stop the exact instance after a HuMo batch.
- Whisper files include unused decoder weights. Planner estimates use the
  encoder fraction, and Comfy's unexpected decoder-key warnings are expected.
- The official LoRA reports unmatched HuMo image-specific keys. Do not hide
  that upstream compatibility warning even when output succeeds.

## LTX 2.3 first/last frame

- The distilled FP8 checkpoint is about 29.53 GB and the Gemma 3 12B FP4 mixed
  encoder is 9.45 GB. On a 32 GB WSL host, their approximately 38.98 GB total
  before activation/offload overhead is a separate large-host stress test, not
  a smoke test.
- Kijai's transformer-only LTX-2.3 INT8 ConvRot candidate is about 21.51 GB,
  but it is not a drop-in replacement for the official combined checkpoint.
  Require a live-qualified split-model wrapper plus its VAE, audio, connector,
  and text stack before treating the smaller transformer as runnable.
- The live registry also finds Unsloth LTX-2.3 GGUF variants, but these have the
  same compatibility burden: discovery and smaller file size do not prove a
  drop-in Comfy graph. Do not download one until its exact loader and remaining
  connector/VAE/text dependencies analyze as a coherent API workflow.
- Require proven unload behavior and monitor host memory in small checks.
- Do not download merely to prove first/last animation when staged H3 FL2V has
  already qualified that feature. Require a materialized API graph and a valid
  placement plan first; then start with the shortest supported
  duration/resolution and matched frames.

## MOSS TTS and ACE-Step

- MOSS TTS fixtures should record language, seed, frame cap, temperature,
  top-p/top-k, generation time, duration, rate/channels, and Media API path.
- Unload MOSS before launching Comfy-heavy image/video inference.
- ACE-Step speech/singing tests should retain lyrics, language, seed, duration,
  LM variant, audio format, and any reference audio. Begin short before long
  singing or lip-sync chains.
- Qualified ACE 2B baseline: `ace-1.5` + `ace-lm-0.6b`, BF16, no CPU
  offload, eight Euler steps, CFG 1. A 15-second original song took 24.427
  seconds warm and produced stereo 48 kHz PCM-16.
- Qualified XL comparison: `ace-xl-turbo` + `ace-lm-1.7b` used roughly 19 GB
  of the 3090 and generated the same clip in 17.658 seconds, but cold four-shard
  page-in took about ten minutes and tempo adherence was worse in this seed.
- Keep one load request alive or poll state without retrying. Updated Omni
  builds serialize model+LM transactions and deduplicate identical retries;
  older builds can interleave a retry between those two stages.
- `unload all` releases weights; delete the exact ACE worker when a zero-context
  handoff to Comfy is required.
