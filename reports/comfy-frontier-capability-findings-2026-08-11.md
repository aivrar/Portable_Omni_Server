# Comfy frontier capability findings - 2026-08-11

This is the live execution ledger for the API-first capability plan. Secrets
are intentionally excluded. Tests are sequential; a case is not marked
qualified until its output and cleanup are verified.

## Runtime baseline

- Gateway: started through `Omni_Studio.exe`; API bridge reachable on
  `127.0.0.1:9200`.
- Comfy instance: `comfy-cuda1-8188`, PID 9234, ready after initialization.
- Startup: primary host `cuda:1` (RTX 3090), visible pool `cuda:1,cuda:0`
  (RTX 3060 auxiliary), normal VRAM mode, automatic attention, 1 GB reserve,
  `cache_policy=none`, `mmap_mode=torch`, async offload automatic.
- Device baseline before Comfy: RTX 3060 free 10,405 / 12,288 MB; RTX 3090
  free 24,326 / 24,576 MB; no compute PIDs or workers.
- Host baseline before Comfy: `MemAvailable=30,774,072 kB`,
  `Cached=7,285,800 kB`, `SReclaimable=1,639,240 kB`,
  `SwapFree=8,340,804 kB`.
- After Comfy initialization: `MemAvailable=30,262,396 kB`,
  `Cached=7,832,912 kB`, `SReclaimable=1,650,112 kB`; this is a small
  reclaimable-cache increase, not a leak signal.
- Comfy core: installed, clean, current commit
  `9eaba63e1a9f2b27701cf0a0694aeed777da42f5`; no core update available.
- Manager: installed but current commit does not match its pinned ref; this is
  recorded as a maintenance finding, not changed during the baseline.
- Installed custom nodes: ComfyUI-ClipProj, ComfyUI-Manager,
  ComfyUI-Spectrum-MiniMax-H3, and Omni bridge.
- ACE-Step assets: core and runtime ready; `ace-1.5`, `ace-base`, both 0.6B
  and 1.7B LMs, and community acoustic/raspy LoRA packs installed. No audio
  worker is resident.

The first `POST /api/comfy/start` client call exceeded its 120-second client
timeout while the instance was still starting. A read-only instance check
showed the same exact PID progressing to `ready`; the request was not retried.

## Case ledger

Cases will be added below as they complete. Each entry records analysis,
execution, output metadata, resource telemetry, and cleanup.

## KREA-T0 - Turbo 512px/4-step smoke (cancelled for safety)

- Analysis: `ready_to_run=true`, `placement_plan.valid=true`; model on the
  3090 primary, Qwen encoder and VAE on the 3060 auxiliary; all three model
  files and nine node classes installed.
- Prompt ID: `45eaab54-42e8-4841-927c-2354b462a65d`.
- The run remained in Comfy's queue for more than one minute without reaching
  completion. The prior qualified machine record shows cold Krea loading can
  take several minutes because checkpoint page-in dominates; this was not
  allowed to become a long-running probe.
- Action: sent the allowlisted Comfy `/interrupt`, confirmed an empty queue,
  then sent `/free` with `unload_models=true` and `free_memory=true`.
- `/free` restored VRAM but left roughly 13.6 GB Linux file cache from the
  memory-mapped checkpoint. Stopping the exact instance released the workload
  cgroup: cached pages fell to 7.36 GB and `MemAvailable` rose to 30.67 GB;
  both GPUs returned to their no-worker baseline. This is expected reclaimable
  file cache, not an Omni leak.
- Classification: `environment/performance` cold checkpoint page-in; no
  output was declared successful. The multi-GPU analysis/routing path itself
  passed.

## MOSS-TTS-T0 - worker lifecycle and short TTS gate (post-reboot)

- The documented hyphenated path `/api/tts/moss-tts` returned `400 Unknown
  model: moss-tts`. The current typed route/config contract uses `moss_tts`,
  so `/api/tts/moss_tts` is the valid path. This is a stale documentation or
  alias defect; no source change was made during this run.
- Worker analysis for `moss_tts`, variant `local-v1.5`, single placement on
  the 3090 stable UUID returned `ready_to_spawn=true`, a valid plan,
  16,384 MB estimated VRAM, and healthy host pressure.
- The exact worker `moss_tts-1` spawned, but remained `loading` after its
  three checkpoint shards reached 100%. It never reported readiness or GPU
  allocation; targeted RSS reached about 2.5 GB and CPU stayed around 7-9%.
- The exact worker was deleted after the bounded load window. `/api/workers`
  returned an empty list; both GPUs returned to baseline free VRAM and host
  `MemAvailable` returned above 30 GB. No cache leak was observed.
- Classification: custom-worker loader/readiness stall. API analyze,
  placement, spawn, kill, and cleanup paths worked; no TTS audio output was
  generated in this attempt.

## ACE-STEP-T0 - model load gate (post-reboot)

- A single ACE worker was started on the 3090 with the installed `ace-1.5`
  core and `ace-lm-0.6b`, BF16, no CPU offload, no int8, and no compile.
- `POST /api/ace_step/load` exceeded the 120-second bounded client window.
  The worker log showed checkpoint/runtime initialization and then stopped at
  the generation-service load attempt; authoritative state remained
  `model_loaded=false` and `lm_loaded=false`.
- Only about 1.3 GB VRAM was resident and host pressure stayed healthy. The
  exact `ace_step-1` worker was deleted rather than retried.
- `/api/workers` was empty afterward and host cache/memory returned to the
  pre-load baseline. Existing checked-in 1-15 second ACE WAV fixtures document
  the earlier warm-path capability; this post-reboot cold load is classified
  as an environment/loader stall, not a placement failure.

## API analysis gates after Comfy restart

- Comfy was restarted with the same cache-safe two-GPU startup body. The
  instance became ready after about 140 seconds; no generation was queued in
  this phase.
- Krea Turbo 512px/4-step analysis passed with `ready_to_run=true`, a valid
  plan, and component placement across both GPUs (15,436 MB diffusion model on
  the 3090; 6,518 MB text encoder and 840 MB VAE on the 3060). All three model
  files and all nine runtime node classes were installed. Its previous cold
  generation remains cancelled for the safety stop rule.
- Krea Raw analysis found no missing files or node classes and a valid
  two-GPU placement, but correctly returned `needs-review` rather than
  `ready_to_run`. The graph uses Turbo's fixed 1.15 flow shift and
  `ConditioningZeroOut`; the analyzer warns Raw needs its resolution-derived
  flow shift and an independently encoded empty-string unconditional branch.
  This is a workflow-correctness issue, not a model-placement failure.
- Wan Animate 2 (21-frame smoke) passed analysis with no missing models or
  nodes, a valid two-GPU plan, 18,811 MB estimated peak on the primary and
  8,995 MB on the auxiliary, and `ready_to_run=true`. Existing checked-in
  480x832 videos document successful execution and cleanup.
- MiniMax-H3 ClipProj FL2V strength smoke passed analysis with no missing
  models/nodes, a valid two-GPU plan, and `ready_to_run=true`; its prior
  staged 73-frame runs are the qualified execution record in the H3 report.
- LTX-2.3 templates are discoverable through `/api/workflows/search` and
  `/api/workflow-templates/{id}`, but the current templates are UI graphs
  (`api_runnable=false`, `requires_api_export=true`). No saved API-format LTX
  graph is available to pass to `/api/workflows/analyze`; this is an API
  coverage gap, not a failed LTX inference. The template documents the 22B
  FP8 checkpoint, Gemma text/audio stack, distilled LoRA, and 2x spatial
  upscaler requirements.
- The output route was checked against an existing H3 MP4 with a byte-range
  GET (`kind=output`): HTTP 200, `video/mp4`, 259,783 bytes, and
  `accept-ranges: bytes`. The media-library serving path is therefore healthy
  for these Comfy outputs.

## Final cleanup state

- The exact Comfy instance `comfy-cuda1-8188` was stopped after analysis. The
  gateway process remains available, but `/api/comfy/instances` and
  `/api/workers` are empty.
- Final device telemetry: RTX 3060 free 10,448 / 12,288 MB; RTX 3090 free
  24,326 / 24,576 MB. Final host telemetry:
  `MemAvailable=30,666,368 kB`, `Cached=7,391,740 kB`,
  `SReclaimable=1,642,304 kB`, `SwapFree=8,340,804 kB`.
- No global cache drop was used. The larger cache observed during mapped-model
  activity fell back after the exact worker/Comfy stops, confirming ordinary
  reclaimable file cache rather than a persistent Omni leak.

## Continuation ledger (same session, bounded post-restart execution)

The earlier final-cleanup snapshot above was superseded while the remaining
plan was executed. All new probes below were sequential and bounded; no
heavyweight generation was queued solely to test API wiring.

### Audio loader gates and long-form composition

- ACE-Step `ace-xl-turbo`/1.7B was given one bounded load attempt on the 3090.
  The worker remained at `Loading checkpoint shards: 0% (0/4)` for roughly four
  minutes, at about 6.6 GB RSS and low CPU. The client timed out at 240 seconds;
  the exact worker was deleted and `/api/workers` returned empty. This is a
  cold-loader/page-in stall after reboot, not an API or placement failure.
- Stable Audio Open Small (`sao-open-small` with `larger-clap-general`) was
  given one bounded load attempt on the 3090. CLAP became ready, while the
  Stable Audio model remained unset; the bounded request timed out and the
  exact worker was deleted. The log recorded the expected `flash_attn` fallback
  rather than a crash. Prior qualified Open Small and Open 1.0 generations
  remain the capability record in `reports/audio-music-capability-findings-2026-08-10.md`.
- MOSS-SFX was analyzed successfully (valid one-GPU plan, 12,288 MB estimate),
  then given one short bounded worker-load window. It stayed in `loading` with
  stable memory and was deleted exactly; no new audio was declared.
- Existing successful outputs were revalidated through the metadata API:
  ACE 60 s and 120 s stereo WAVs, MOSS TTS 12.945 s, MOSS SFX 8 s mono, Stable
  Audio 2.972 s, and a 172 s composed instrumental. A five-slice composition
  gate also completed through `POST /api/outputs/audio/compose`: the output is
  exactly 60 s, 48 kHz, 16-bit. It is a repeated existing TTS slice for API
  duration testing, not a claim of natural 60-second TTS generation.

### Community extension install/rollback and API coverage

- The catalog install route was exercised for LatentSync (`hay86/ComfyUI_LatentSync`).
  Manager reported an install-script failure; the recovery snapshot restored the
  tree. The exact failed extension was then uninstalled through
  `/api/comfy/extensions/manage`, and status verified that no LatentSync node
  remained. No source change or partial install was left behind.
- The catalog install route succeeded for `infinitetalk-native-sampler`.
  The extension is enabled at version 1.1.0 and its node search returns both
  `InfiniteTalkAutoSampler` and `InfiniteTalkAutoSamplerAdvanced`. The official
  `video_wan2_1_infinitetalk.json` template is discoverable, but it is a UI
  graph (`api_runnable=false`, `needs-api-export`); the API correctly refuses it
  until Comfy exports an API-format graph. This is a documented coverage gap,
  not an inference failure.
- The external Hugging Face model-registry query for `lightx2v` returned HTTP
  502 from the upstream search path. The local Manager catalog remained
  available; its `ltx2` query returned three LTX-2.3 control/LoRA entries. The
  installed-model index confirmed the local `lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors` file. The 502 is recorded as upstream
  connectivity, not treated as an empty registry.

### Comfy API round-trip and post-install analyses

- A real image was uploaded with `/api/comfy/{instance}/upload/image`, then a
  two-node LoadImage -> SaveImage API graph was analyzed and run. The prompt
  completed in 0.79 s; `/api/outputs` and `/api/outputs/metadata` returned a
  480x832 PNG with intact Comfy prompt metadata. This verifies upload, queue,
  execution, output discovery, and metadata serving as one API path.
- After the InfiniteTalk install, Krea Turbo and Wan Animate analyses still
  reported zero missing models/nodes and valid placement. H3 analysis correctly
  reported a blocked warm plan because its 23,422 MiB model plus 6,518 MiB clip
  cannot fit the current free headroom while the instance owns VRAM; no run was
  attempted. The plan warning explicitly distinguishes component routing from
  true staged unload-between-stage behavior.
- The targeted contract suite passed: 59 tests in 11.534 s (`comfy` placement,
  multi-GPU, workflow requirements, audio composition, ACE sources/staging,
  MOSS TTS, and MOSS SFX contracts).

### Final post-continuation cleanup

- The exact `comfy-cuda1-8188` instance was stopped after the round-trip smoke;
  `/api/comfy/instances` and `/api/workers` are both empty. The gateway remains
  reachable for the next controlled operation.
- Final device read: RTX 3060 free 10,471 / 12,288 MiB and RTX 3090 free
  24,326 / 24,576 MiB, with no worker or Comfy compute PIDs.
- Final WSL read: `MemAvailable=31,283,948 kB`, `Cached=7,417,356 kB`,
  `SReclaimable=1,642,840 kB`, `SwapFree=8,346,436 kB`. No global cache drop
  was used; the reclaimable cache returned to the normal post-workload range.

### Installed-model inventory snapshot

- Installed Wan entries include `wan_animate_2_int8_convrot.safetensors` and
  the Wan 2.1 VAE files.
- Installed Krea entries include both Raw and Turbo INT8 checkpoints plus the
  Darkbrush and Style Reference LoRAs.
- Installed MiniMax-H3 entries include the INT8 FL2VA model, audio/video VAEs,
  and the Qwen3-VL 32B NVFP4/AWQ encoder. No InfiniteTalk-specific model file
  is installed, which is another reason its UI template was not queued.

## Comprehensive continuation: Krea 2 Raw/Turbo frontier

- `KREA-R-smoke`: corrected Raw INT8, 1024x1024, seed 20260809, 16 steps,
  CFG 3.5, Euler/simple, resolution shift 0.90625, empty-string unconditional.
  Prompt `333a048b-09db-436d-a33b-21035620f209` completed in 465.098 s and
  produced `capability_tests/Krea2_Raw_Corrected_Smoke_00002_.png` (integrity
  OK, embedded prompt metadata). The image was coherent but introduced a
  strong unrequested inset/window-frame composition.
- `KREA-R1`: the matched 52-step Raw quality control completed as prompt
  `cd73550e-843d-48a5-b5dc-50337d3ea723` in 557.780 s. Facial detail,
  hand/lantern structure, and background coherence improved, but the inset
  composition remained; it is a fixed-seed model interpretation rather than
  only a 16-step under-sampling defect.
- The existing matched Turbo 1024 control (same prompt and seed) was inspected
  and avoids the inset artifact with stronger natural full-frame composition.
  This confirms Turbo as the routine prompt-following winner for this case.
- `KREA-S1`: Turbo INT8 at 1536x1024, eight steps, same seed completed as
  prompt `7ce5ae9b-744b-4d98-b723-493afc8ae03e` in 418.604 s. The output
  `capability_tests/Krea2_Turbo_1536x1024_00001_.png` passed visual inspection:
  coherent hands/lantern, rain/window detail, full-frame composition, and no
  inset artifact.
- `KREA-S2`: Turbo INT8 at 2048x2048 completed as prompt
  `0f841126-38cd-48b9-9e92-5037f9670ed0` in 398.437 s. Peak observed free VRAM
  was about 6.56 GiB on the 3090 and 5.15 GiB on the 3060. The PNG is valid and
  highly detailed, but it duplicated the requested lantern; retain 2048 as a
  frontier/detail mode with increased semantic risk, not the balanced default.
- Existing Krea style-reference media was visually rechecked and shows strong
  reference transfer. Existing 1280-square Turbo media plus the new 1536 and
  2048 cases complete the useful scale ladder without a redundant Cartesian
  sweep.
- `/free` returned OK after the family. Device free memory returned to
  10,366 MiB (3060) and 23,936 MiB (3090); host `MemAvailable` returned to
  28.28 GiB. The remaining 22.36 GiB Linux cache is file-backed/reclaimable
  checkpoint data and will be cleared by the exact distro teardown requested
  at the end of the exercise.

## Comprehensive continuation: Wan Animate duration frontier

- The existing matched-human and mismatched-driver 21-frame outputs remain the
  positive/negative controls at 480x832, six LCM steps, CFG 1, CPU INT8 Wan
  cache, and LightX2V LoRA. They already establish identity transfer and the
  failure mode when driver pose/subject class do not match.
- `WAN-A2-49f-native-canvas`: extending only `WanAnimate2ToVideo.length` from
  21 to 49 at 480x832 produced a CUDA OOM in `SamplerCustom` after 637 s
  (prompt `f40d13e7-2b65-42f6-91b4-e719ead268f8`). Static placement had been
  valid and reported no missing dependencies. Immediately before failure both
  GPUs still showed about 6.8 GiB free, demonstrating that the current planner
  does not account for this graph's spatial-temporal activation allocation.
  The identical case was not retried.
- `WAN-A2-49f-lowres`: with the same model, seed, driver, six steps, LoRA, and
  49 frames, reducing only the canvas to 384x640 completed as prompt
  `4b0fd447-3ae0-4d3b-b9c6-8abb9973c89c` in 510.390 s. The output
  `capability_tests/WanAnimate2_49f_CPUCache_384x640_00001_.mp4` is a valid
  384x640 H.264 file, 49 frames/24 fps, 2.041667 s, 190,604 bytes.
- Contact-sheet inspection found stable identity, clothing, neutral studio,
  and continuous crouching/forward motion across early/middle/late frames.
  The lower canvas softens detail and the arms are less faithful to the brass
  mechanical reference description. Recommended tiers are therefore 21 frames
  at 480x832 for balanced quality and 49 frames at 384x640 for duration.
- The 49-frame A/B shows the OOM is driven by the combined spatial-temporal
  activation rather than frame count alone. An 81-frame attempt at the native
  canvas is rejected by the stop-on-first-boundary rule; it would not add a
  safe operating point after the 49-frame native-canvas failure.
- `/free` returned the instance to 10,327 MiB free on the 3060 and 23,928 MiB
  on the 3090 after the successful lower-resolution run.

## Community Wan wrapper qualification and InfiniteTalk preparation

- The live Manager catalog identifies Kijai's `ComfyUI-WanVideoWrapper` as
  `repo-4327e958e064a3f9` and advertises 148 classes spanning block swap,
  Tea/Easy/Mag cache, Sage/radial attention, Wan Animate, and newer video
  families. Upstream HEAD was resolved to
  `088128b224242e110d3906c6750e9a3a348a659b` for this qualification.
- A guarded Manager install was attempted through the registry contract. Job
  `659f773d` failed its required live-node verification because
  `FantasyPortraitFaceDetector`, `FantasyPortraitModelLoader`,
  `LandmarksToImage`, `WanVideoAddFantasyPortrait`, and
  `WanVideoTextImageEncode` were absent from `/object_info`. The recovery
  snapshot was restored, and the exact disabled failed install was removed by
  verified uninstall job `6ce37e9c`; it was not left to confuse later agents.
- Native Comfy supplies `WanInfiniteTalkToVideo`; the app's installed
  `infinitetalk-native-sampler` extension supplies the compact
  `InfiniteTalkAutoSampler`/advanced nodes. The official single-speaker patch,
  Wan 2.1 I2V FP8 base, and wav2vec2 audio encoder were installed through the
  model asset API and verified in the installed registry.
- The authenticated Comfy image-upload proxy also accepts WAV input. A
  persisted MOSS-TTS voice was uploaded as
  `omni-capability-tests/2026-08-09T17-50-53.wav`, so native InfiniteTalk can
  consume local TTS without a paid API or a filesystem-only workflow step.
- `infinitetalk_single_81f_tts_api.json` preflighted as a 14-node API graph:
  all six dependencies and all runtime classes are installed, and the two-GPU
  plan is valid. It assigns the 14B diffusion model plus wav2vec2 to the RTX
  3090 and UMT5 plus the Wan VAE to the RTX 3060.
- The first execution exposed an upstream helper trap before producing a
  qualified artifact. `InfiniteTalkAutoSampler.length=81` is a chunk size, not
  an output cap. The 12.95-second TTS source at 25 fps becomes about 324 output
  frames and five diffusion passes because extensions advance by
  `length-motion_frame_count` (72) frames. The helper held the Comfy request
  loop long enough that normal status/interrupt calls became unavailable, so
  only the exact Omni distro was terminated; no output is claimed from this
  attempt.
- The project testing skill now requires agents to compute the pass count
  before queueing and to trim one-pass 81-frame/25-fps smoke audio to at most
  3.24 seconds (3.0 seconds preferred). The skill validates successfully.

## LTX-2.3 official capacity gate and optimized-weight search

- The current official `video_ltx2_3_flf2v` package template is a UI subgraph,
  not directly API-runnable. Its native LTX-2.3 requirements were materialized
  as `ltx23_flf2v_requirements_api.json`, which the analyzer resolves to the
  22B distilled FP8 checkpoint and Gemma 3 12B FP4 mixed encoder. Both files
  are absent; no heavyweight download was started.
- The app's live Hugging Face registry resolved immutable revisions and exact
  sizes: official `ltx-2.3-22b-distilled-fp8.safetensors` is 29,531,884,062
  bytes at revision `1d756cd27fa11c0896c4dfee093cd1bf36c7f7a1`, while
  `gemma_3_12B_it_fp4_mixed.safetensors` is 9,447,702,218 bytes at revision
  `bd5f9c87fcb0360ae7112f9784562670894d9492`. Their 38.98 GB combined file
  footprint already exceeds the distro's 32 GB RAM budget before runtime
  overhead and cannot reside on either GPU; the official graph is therefore a
  hard capacity stop on this host.
- The same registry found Kijai's smaller transformer-only choices at revision
  `6d980fde0d330f2fed6ff8dfdfddb06d88a004e5`, including a 21,505,993,064-byte
  INT8 ConvRot dev transformer and a 24,052,755,264-byte MXFP8 distilled
  transformer. These are not drop-in replacements for the official combined
  checkpoint: they require a compatible split-model workflow plus the
  remaining VAE/audio/connector stack. Since the guarded Kijai wrapper install
  failed live-node qualification, they are recorded as candidates rather than
  downloaded or falsely declared runnable.

## Runtime persistence and corrected InfiniteTalk capacity result

- Omni and Comfy mutable state now persists inside the distro under
  `/var/lib/omni_studio`, while the canonical public paths beneath
  `/opt/omni_studio` remain guarded symlinks. A normal desktop-app restart
  preserved the 179 GB Comfy model tree, 72 audio outputs, and an uploaded
  input sentinel. Windows UNC does not follow these absolute Linux symlinks;
  API/inside-distro verification is authoritative.
- Cold setup was repaired so persistence links exist before fast-start,
  cache-only clone destinations are accepted, Audio Lab dependency groups do
  not trigger unbounded resolver backtracking, and TorchInductor setup uses one
  compile worker. The focused startup-layout regression suite passes.
- Audio composition initially failed when the canonical output root was a
  symlink because `Path.relative_to()` compared unresolved roots. The gateway
  now resolves both paths through `_relative_to_output_root`; the regression
  suite passes and composition `2b810172f37e46ba9006bb7f894c9048`
  produced an exact 3.0-second, 48 kHz PCM16 InfiniteTalk fixture.
- The corrected InfiniteTalk graph analyzed ready with all six models and all
  14 node classes installed. It queued through `/api/workflows/run` with a
  valid two-GPU plan, and the helper confirmed `audio=3.00s`,
  `total_frames=75`, `1 passes`.
- This one-pass run exposed a second hard resource boundary before output. The
  static plan estimated 19.5 GB resident on the RTX 3090 and 9.0 GB on the RTX
  3060, but did not bound simultaneous host/offload residency. During the run
  both GPUs were populated (about 20.1 GB and 5.2 GB) while WSL reached about
  32.4 GB working set at its 32 GB limit and exhausted socket-buffer capacity
  (`WSL 0x80072747`). Normal interrupt and stop APIs could no longer obtain a
  socket, so only `linbox-Omni_Studio` was terminated to protect the host.
  No artifact is claimed. The exact graph is a hard capacity failure on a
  32 GB distro even though `placement_plan.valid` was true; planner host-memory
  accounting needs a future fix before retrying this model stack.

## Host-capacity fix and live registry alternative scan

- The workflow placement analyzer now counts every unique installed weight
  referenced by an ordinary graph, including LoRAs and model patches that are
  intentionally not independent routing components. It compares that mapped
  footprint with reclaim-aware cgroup/host availability while retaining a
  4 GiB reserve. Genuine all-staged graphs bypass the aggregate gate because
  their contract unloads between stages.
- Forty-three focused placement, multigpu, and workflow-requirement tests pass.
  Live analysis now blocks the InfiniteTalk graph at 28,312 MiB referenced
  weights versus a current 16,680 MiB mapping budget; the Comfy queue remained
  empty. The fully staged H3 ClipProj graph still analyzes ready/valid with
  zero missing models/nodes and no host aggregate blocker.
- The live Hugging Face registry API successfully found candidates absent from
  the tested workflows and returned immutable revisions, exact sizes,
  categories, and install URLs. Besides Kijai, notable current contributors
  include `vantagewithai`, `AlperKTS`, `wikeeyang`, and `realrebelai` for Krea
  2; `QuantStack` and `befox` for Wan Animate GGUF; `unsloth` for LTX-2.3 GGUF;
  and `Abiray` plus `DeepBeepMeep` for MiniMax-H3 quantized stacks.
- Examples of useful capacity leads are Krea Turbo GGUFs from about 4.66 GB,
  Wikeeyang Krea Turbo HD int4 around 6.82 GB, QuantStack Wan Animate GGUFs
  from about 6.01 GB, and Abiray/DeepBeepMeep H3 component choices. These are
  discovery results, not drop-in qualification: each still requires a
  compatible loader/workflow, license review, and analyze-only placement gate.
- InfiniteTalk search found the official MeiGen-AI stack and only low-usage
  mirrors/fine-tunes; no community candidate was promoted over the official
  patch without compatibility evidence.
- Output metadata probing was also made symlink-safe across file records,
  sidecar provenance, canonical metadata paths, and pruning. Fifteen focused
  output tests pass and the live MOSS WAV metadata route now succeeds through
  the `/opt` to `/var/lib` persistence link.

## Final verification and teardown

- The combined focused suite passed 63 placement, workflow, output, audio
  composition, and TTS lifecycle tests (one POSIX-only symlink case skipped on
  Windows), plus all five startup-layout regression tests. Both project agent
  skills validate successfully.
- Comfy `/free` completed, the exact instance stopped, and final API reads
  showed zero Comfy instances and zero model workers. Immediately before
  shutdown, the distro reported 29,768 MiB available RAM with 8,685 MiB
  reclaimable cache. Persisted state was synced, the desktop process closed,
  and only `linbox-Omni_Studio` was terminated. Its remaining page cache is
  therefore released; the unrelated running distro was not touched.

## LTX-2.5 launch-day qualification

- Core Comfy was updated to commit `27bca654eb9a70237d93f56a6ea336ab55f8925d`
  (ComfyUI 0.32.0). The installed package catalog contains only LTX-2.5 paid
  API templates; it does not yet ship local 2.5 examples.
- The nine current official local workflows were imported from
  `Lightricks/ComfyUI-LTXVideo/example_workflows/2.5` and tagged
  `ltx-2.5`, `official`, and `ui-template`. They cover T2V/I2V single and
  two-stage, T2A, ingredients, inpaint, outpaint, motion tracking, union
  control, and V2V IC-LoRA. They are UI/subgraph documents and require API
  export or an equivalent API-format graph before execution.
- The official custom extension commit
  `ac4d99839020b983e956a8ab67ec38aec1b6e65a` is enabled and live-qualified.
  `LowVRAMCheckpointLoader`, `LowVRAMAudioVAELoader`,
  `LTXVAddGuideAdvanced`, and `LTXVTiledVAEDecode` were verified through live
  object info. The Manager catalog mapped 70 classes; 69 loaded and only the
  obsolete `LTXAddImageGuide` name was absent.
- Catalog installs previously treated one stale mapped class as total package
  failure. Catalog-origin checks now tolerate bounded mapping drift only when
  at least 80% of mapped classes are live, report every missing name, and keep
  direct explicit-class checks strict. Snapshot recovery also inspects Manager
  log evidence and no longer reports `restored: true` when the background
  restore logged an error. Eighteen focused extension/discovery tests pass.
- The official repository revision
  `28dac7acdc1f78a70e98687db261a949754f8941` exposes a 21.50 GB INT8 ConvRot
  distilled transformer, 18.72 GB NVFP4 transformer, 15.37 GB INT8 ConvRot
  Gemma-4 projection encoder, 1.47 GB video VAE, 0.36 GB audio VAE, 1.00 GB
  spatial and 0.26 GB temporal latent upscalers, and an 8.90 GB distilled
  LoRA. Community GGUFs range from about 9.74 GB Q3_K_S through 22.75 GB Q8.
- On this RTX 3090 + RTX 3060 Ampere host, the official INT8 ConvRot path is
  the first compatibility choice. Comfy reports PyTorch 2.7.1+cu128 and warns
  that its optimized fused CUDA operations require CUDA 13.0 or newer;
  NVFP4 is therefore not promoted as the speed choice in the current runtime.
- The first official Xet install job `9eb94b66` transferred no data and failed
  with HTTP 403 because `Lightricks/LTX-2.5` is gated and the configured
  Hugging Face identity lacks access. No mirror was used to bypass the license.
  Actual inference, quality, duration, resolution, LoRA, and upscaler sweeps
  remain blocked until the operator accepts the official license for that
  configured Hugging Face account.
- Media Library now persists an `autoplay media` browsing option. Its lightbox
  keeps playable media on the left and automatically loads bounded readable
  JSON/text, sibling manifest model/parameter provenance, media facts, and
  related playable siblings in the right-hand panel. The existing metadata
  route was extended; no new endpoint was added. Live verification against an
  Audio Lab manifest returned parsed JSON and its related WAV.

### LTX-2.5 staged local execution follow-up

- After base-repository access was accepted, the official INT8 ConvRot Gemma,
  distilled transformer, video/audio VAEs, spatial/temporal latent upscalers,
  and duration head installed successfully. A community W4A8 mixed distilled
  transformer from `tsolful/LTX_2.5_INT4_W4A8_ConvRot` also installed and is
  natively recognized as `asym_w4a8_int8`; the tested Q3 GGUF was
  architecture-incompatible and is not promoted.
- Ordinary LTX graphs exceeded the distro host-mapping budget. Persisting
  Gemma conditioning with upstream `LTXVSaveConditioning`/load nodes made the
  graph fit but produced colored noise. Source inspection found that the saver
  retains the conditioning tensor and attention mask but drops other LTX 2.5
  conditioning options.
- Omni now provides a lossless fully staged LTX path:
  `OmniLTXStageConditioning`, `OmniLTXStageEmptyAVLatent`,
  `OmniLTXStageSampler`, and `OmniLTXStageAVDecode`. The live planner assigned
  Gemma and W4A8 sampling to the RTX 3090 and audio-latent setup plus video and
  audio decode to the RTX 3060. All components were marked staged with
  distinct unload boundaries; host aggregate gating was therefore accurate.
- The qualification graph at 512x320, 49 frames, 24 fps, CFG 1, and the
  official eight-sigma distilled schedule completed in 15:58. Sampling took
  41 seconds after cold weight preparation. Output
  `video/LTX25_W4A8_lossless_staged_512x320_49f_00001_.mp4` is coherent (not
  noise), H.264 at 24 fps with 48 kHz stereo AAC and synchronized 2.04/2.01
  second video/audio streams.
- The installed `ltx-2.5-video-vae-bf16.safetensors` is the new
  `NADiffusionDecoder`, so the successful baseline already exercised the 2.5
  diffusion decoder. The runtime used its non-NATTEN fallback because the
  optimized backend is not installed/available on this CUDA 12.8 stack.
- Official DFR is a separate detail-fidelity pipeline, not a sampler flag. It
  combines generated keyframes and spatial detailing and can add temporal
  refinement rounds. The pixel-detailing IC-LoRA repository remains separately
  gated: retry job `7309aaa0` returned HTTP 403 and explicitly said the current
  account is not on that repository's authorized list. Base LTX-2.5 access is
  therefore insufficient for that optional asset.
- The official two-stage Comfy recipe uses spatial latent x2 followed by a
  three-step dev-transformer refinement at CFG 1, `euler_ancestral`, and
  sigmas `0.85, 0.7250, 0.4219, 0.0`. The 15.4 GB W4A8 mixed dev transformer
  from the same `tsolful` repository installed and loaded successfully.
- `OmniLTXStageSpatialRefine` now implements that stage without overlapping
  heavy residency: the RTX 3060 decodes the low-resolution guide and performs
  latent spatial x2, then the RTX 3090 runs the dev refinement. The first
  qualification exposed a 5D/4D guide-shape mismatch when calling the VAE
  object directly; using Comfy's `VAEDecodeTiled` wrapper fixed the frame
  flattening without modifying upstream source.
- The qualified two-stage graph produced
  `video/LTX25_W4A8_two_stage_1024x640_49f_00001_.mp4`: 1024x640 H.264 at
  24 fps plus 48 kHz stereo AAC, with 2.04/2.01 second synchronized streams.
  Total cold execution was 22:28; the first sampling pass took 29 seconds and
  the three-step refinement took 18 seconds after loading. First, midpoint,
  and final frames retained subject identity, framing, lighting, and street
  geometry. The x2 result was coherent and sharper than the baseline, with
  minor hand deformation but no endpoint collapse.
- The official distilled LoRA and W4A8 dev transformer are installed. The
  Q3 GGUF remains excluded because its architecture did not load as LTX 2.5.
- `GET /api/workflows/search` against live template contents timed out on a
  narrow phrase during this exercise. Direct saved-workflow retrieval and
  live node search remained responsive; optimize/index template content search
  before treating it as a low-latency discovery primitive.
- The native duration node requires simultaneous transformer, conditioning,
  and duration-head residency. Omni added `OmniLTXStageDurationPredictor` so
  those weights form one explicit stage and unload immediately afterward; its
  model-discovery mapping classifies the head under `model_patches`. The
  focused placement/readiness suite now covers this path.
- Live qualification succeeded for prompt id
  `a12f2ff9-8364-4967-84e8-5e22f137840d`. The three-shot prompt produced a
  raw duration prediction of 3.767 seconds and a valid snapped length of 89
  frames at 24 fps. Cold execution took 10:28. The queue finished empty and
  the staged node returned the RTX 3090 to zero model residency.
- LTX 2.5 multi-shot behavior is prompt-driven rather than a separate sampler
  input. A qualification prompt should describe chronological shots and cuts
  in one literal paragraph, then validate actual transitions and identity
  continuity rather than assuming prompt compliance.
- The saved multi-GPU qualification graph
  `ltx25_w4a8_multishot_512x320_121f_api.json` completed successfully in
  13:32 (prompt id `f8677627-bb1b-49ea-aaa9-018745b703fb`). Sampling took 38
  seconds for eight steps. The output is H.264 512x320 at 24 fps for 5.0417
  seconds with stereo 48 kHz AAC for 5.01 seconds. Audio is not silent (about
  -27.2 dB peak and -45.1 dB RMS), though it is intentionally quiet ambience.
- Four temporal samples confirmed three distinct requested compositions in
  order: wide alley establishing shot, medium side view at a red sign, and
  frontal close-up. Robot identity, wet alley, rain, and neon palette persist
  across cuts. There is some scale/proportion drift between the distant and
  close views, but no duplication, malformed transition, or endpoint collapse.
  Live logs verified the full placement sequence: RTX 3090 Gemma, RTX 3060
  audio-latent setup, RTX 3090 W4A8 sampling, then RTX 3060 video/audio decode.
- The live custom-node prompt enhancer is available but defaults to separate
  Hugging Face Llama 3.2 3B and Florence-2 repositories. It remains
  unqualified because that loader bypasses Omni's managed Comfy model
  inventory. The official Python `res_2s` HQ sampler also has no matching node
  in the current live Comfy catalog, so the successful two-stage Euler run
  must not be labeled as the official HQ variant.
- The runtime's PyTorch 2.7.1+cu128 causes two independent optimization gaps.
  Comfy-Kitchen detects its Triton/CUDA capabilities but disables them and
  recommends cu130. Separately, a matching older NATTEN wheel exists for the
  Torch 2.7/CUDA 12.8 generation and could accelerate only the diffusion-video
  decoder. Do not conflate those upgrades, and do not install the newest
  NATTEN blindly because current releases require PyTorch 2.8 or newer.

### LTX-2.5 power-recovery and decoder qualification

- After the 2026-08-12 power restart, the interrupted 1024x640x121 output was
  absent, so the saved workflow was analyzed again and rerun from the start.
  Prompt `f3e0172f-c698-4b0a-ad99-dd03d59e8e6d` completed successfully in
  38:29. The 5.0417-second result is H.264 1024x640 at 24 fps plus stereo
  48 kHz AAC. Four sampled moments retained robot identity, rainy-alley
  geometry, three requested compositions, and clean cuts.
- Live logs verified staged placement after reboot: Gemma and both W4A8
  transformer passes on the RTX 3090; audio latent, guide decode/spatial
  upscale, and ordinary final decode on the RTX 3060. Each heavyweight stage
  unloaded before the next device assignment.
- The official conv VAE was discovered from the exact pinned
  `Lightricks/LTX-2.5` repository and installed with the asset API through
  Hugging Face Hub + Xet. Cached-latent A/B timings for the same
  1024x640x121 result were: diffusion/t64 on 3060, 157.42 seconds; conv/t64 on
  3060, 73.50 seconds; diffusion/t2048 on 3060, OOM after 137.70 seconds;
  diffusion/t2048 on 3090, 129.51 seconds; conv/t2048 on 3090, 61.98 seconds;
  conv/t64 on 3090, 60.58 seconds. Conv was slightly smoother in fine texture
  but preserved composition, motion, identity, and timing.
- The qualified fast policy is therefore conv VAE with 512 spatial tiles and
  temporal size 64 on the largest free GPU after transformer unload, followed
  by audio decode on the auxiliary GPU. It is saved as
  `ltx25_w4a8_multishot_two_stage_1024x640_121f_fastdecode_api.json` with a
  UUID-based placement policy. Diffusion VAE/t64 remains the fidelity option.
  Temporal 2048 is not a safe default, and static weight estimates did not
  predict its activation OOM on 12 GB.
- A separately named low-VRAM candidate from
  `Winnougan/ltx-2.5-w4a8-convrot-int4-convrot-Winnougan-Blessing` was
  installed through the asset API and tested without replacing qualified
  weights. Its W4A8 ConvRot Gemma loaded completely at 10065.76 MB versus
  about 14613.55 MB for the official INT8 ConvRot encoder; its distilled
  transformer loaded completely at 11919.34 MB, roughly 3 GB below the
  previously qualified W4A8 transformer. The same-seed 512x320x49 staged run
  completed in 19:51 cold, with eight-step sampling in 36 seconds versus 41
  seconds for the earlier baseline. Output
  `video/LTX25_Winnougan_W4A8_512x320_49f_00001_.mp4` is coherent, H.264 at
  24 fps with synchronized 48 kHz stereo AAC; sampled frames preserve robot
  identity, alley geometry, reflections, and motion. Audio measured -41.5 dB
  mean and -24.7 dB peak. Keep the pair as a qualified low-VRAM option, but
  do not treat it as a full two-stage replacement: the repository provides no
  matching dev transformer, and the current loader prints a very large list
  of unexpected `.comfy_quant` helper tensors even though mixed-precision
  operations are detected and the main weights load and execute correctly.
- The high-quality hybrid graph
  `ltx25_hybrid_lowvram_multishot_two_stage_1024x640_121f_api.json` combines
  that smaller Gemma/distilled first pass with the qualified tsolful W4A8 dev
  refinement and conv final decoder. Its API preflight was ready with zero
  missing models or nodes and a valid eight-component, two-GPU staged plan.
  Prompt `4b0445d8-82b3-45c1-8388-71bed4e497c1` completed in 26:27 cold,
  compared with 38:29 for the earlier larger-weight recovery run. The first
  eight-step pass took 38 seconds and three-step dev refinement took 34
  seconds. Output
  `video/LTX25_HybridLowVRAM_multishot_two_stage_1024x640_121f_00001_.mp4`
  is coherent H.264 at 1024x640/24 fps for 5.0417 seconds with synchronized
  stereo 48 kHz AAC for 5.01 seconds. The requested wide, side-tracking, and
  close-up shots occur in order while preserving robot and alley identity.
  Audio is present but quiet (-50.8 dB mean, -35.1 dB peak). The saved graph
  is tagged `qualified`.
- A fresh registry API scan also found the official BF16 and INT8 ConvRot
  dev/distilled weights, an NVFP4 distilled weight, both latent upscalers, the
  distilled LoRA, duration head, both video decoders, and audio VAE. NVFP4 is
  not promoted on the RTX 3090/3060 because this Comfy-Kitchen runtime lists
  it as emulated while `asym_w4a8_int8` is native. No credible LTX 2.5-specific
  style or motion LoRA surfaced; search results containing 2.3 adapters remain
  unqualified rather than being assumed forward-compatible. The separately
  gated official pixel-spatial IC-LoRA was not obtained through a third-party
  mirror because doing so would bypass the publisher's access control.
- Source inspection of native `LTXVAddGuide` exposed a multi-GPU lifecycle
  gap: ordinary image/video guide conditioning could not guarantee that its
  video VAE unloaded before the 22B sampler. Omni now provides
  `OmniLTXStageGuide`, preserving native frame index, reference strength,
  optional attention mask, and optional IC-LoRA parameters while making each
  guide VAE a distinct staged placement component. The focused suite passes
  56 tests, including actual VAE-release behavior and planner stage/binding
  coverage. Comfy must reload the custom-node package before live I2V use.
- The first live I2V attempt correctly exposed that a combined AV latent is a
  `NestedTensor`; native guide encoding requires a plain video latent. The
  staged guide now separates video/audio, guides only video, and recombines
  the untouched audio latent. A corrected run then completed coherently at
  320x512x49: it preserved the reference woman's face, curls, denim jacket,
  black jeans, boots, studio background, and lighting while generating the
  requested natural wave. Eight-step sampling took 29 seconds. That run also
  proved appended native guide tokens must be removed after sampling: without
  the crop, video was 2.375 seconds while audio remained 2.01 seconds. The
  staged sampler now splits AV output, applies native `LTXVCropGuides` only to
  video, and recombines unchanged audio. The focused suite passes 58 tests.
- Final prompt `46835351-cf6d-4c7b-8631-753f37bf1461` confirmed the complete
  fix. Output
  `video/LTX25_W4A8_staged_I2V_portrait_320x512_49f_00002_.mp4` contains
  exactly 49 H.264 frames at 320x512 and 24 fps (2.0417 seconds), plus stereo
  48 kHz AAC lasting 2.01 seconds. The reference woman's face, hair, clothes,
  proportions, background, and lighting remain stable while she performs the
  requested wave. Eight-step reference-conditioned sampling took 29 seconds.
  The saved workflow
  `ltx25_w4a8_staged_i2v_portrait_320x512_49f_api.json` is now tagged
  `qualified` with its UUID-based two-GPU policy.
- Native chained multi-keyframe guidance also passed. Workflow
  `ltx25_w4a8_staged_flf2v_portrait_320x512_49f_api.json` uses separate first
  and last images at frame indices `0` and `-1`, with two sequential guide
  stages on the RTX 3060 and W4A8 sampling on the RTX 3090. Both guide VAEs
  loaded and unloaded independently before the transformer stage. Prompt
  `ced0b32a-eff4-47b5-a327-64af1c8065bb` completed successfully; eight-step
  sampling took 30 seconds. Output
  `video/LTX25_W4A8_staged_FLF2V_portrait_320x512_49f_00001_.mp4` contains
  exactly 49 frames at 320x512/24 fps (2.0417 seconds) and synchronized stereo
  AAC at 48 kHz (2.01 seconds). Visual samples transition from the supplied
  neutral stance to the supplied raised-hand pose while preserving identity,
  clothing, studio background, and lighting. The saved workflow is tagged
  `qualified`.
