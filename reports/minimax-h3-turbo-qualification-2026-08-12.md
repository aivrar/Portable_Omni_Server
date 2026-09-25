# MiniMax H3 Turbo qualification — 2026-08-12

## Scope

- Local MiniMax H3 FL2V through Omni's authenticated Comfy API.
- Fixed seed `424242`, retained first/last-frame fixtures, 352x608, 49 frames,
  24 fps, generated native audio.
- Compare the proven staged native path with a pinned EMA Turbo candidate and
  then the identical Turbo graph under SageAttention when the binary runtime
  is verified.
- Keep Spectrum separate from Turbo/Sage so approximation effects are not
  conflated.

## External candidate source

The H3 Turbo comparison project at
<https://jo-nike.github.io/h3-turbo-eval/> was inspected at repository commit
`f13bf725c1a0cdbf913cafe8264eaca42e01bb4b`. It is useful for candidate
selection and stress-scene design, but its RTX 5080 timings and single-seed
quality conclusions are not treated as local results.

## Baseline before mutation

- Gateway connected; Comfy instance `comfy-cuda1-8188` ready.
- Physical RTX 3090 `cuda:1` is Comfy logical `cuda:0`/`primary`.
- Physical RTX 3060 `cuda:0` is Comfy logical `cuda:1`/`auxiliary:1`.
- Free VRAM: about 23,902 MiB on the 3090 and 10,660 MiB on the 3060.
- No standalone workers; Comfy queue empty.
- WSL memory: 32,101 MiB total, 27,443 MiB available, 2,005 MiB cache.
- Existing staged native H3 graph analyzed `ready`, `valid`, fully staged, and
  assigned both GPUs. The plan expected at most 544 MiB bounded CPU offload.
- Installed: H3 INT8 diffusion model, paired video/audio VAEs, ClipProj 4B
  path, and Spectrum. No H3 Turbo LoRA or SageAttention runtime was installed.

## Omni changes

- `OmniH3StageSampler` now accepts a model-only LoRA, LoRA strength, and native
  H3 video/audio sigma shifts while preserving staged unload behavior.
- Placement accounts for the LoRA in the diffusion stage's peak estimate.
- Added a pinned, reusable 49-frame EMA ckpt500 Turbo API fixture.
- Fixed the bundled `outputs list` CLI contract: storage `kind` and
  `media_kind` are now separate, and the command exposes existing subdirectory,
  prefix, prompt-ID, and bounded probe filters. No endpoint was added.
- Focused regressions: 63 H3/placement/workflow tests and 8 CLI tests passed.

## Live results

### Installation and API verification

- Installed the pinned drbaph conversion of larryvrh's EMA ckpt500 LoRA at
  Hugging Face revision `498f1e2ca02e10a598f21267739f30073f68eb10` through
  Omni's Xet-backed model download API. The persisted file is 591.6 MiB.
- Installed KJNodes through the pinned Omni extension-catalog record. Its
  post-install inventory/object-info verification found all 249 nodes,
  including `PathchSageAttentionKJ`.
- Installed the exact prebuilt SageAttention 2.2.0 wheel for the current
  CUDA 12.8 / Torch 2.7 runtime; no source build was attempted.
- The live `OmniH3StageSampler` contract exposes LoRA, H3 sigma-shift, Sage,
  and Spectrum controls. The reusable Turbo fixture analyzes `ready` with no
  warnings and is accepted by Comfy when it uses the exact installed Sage
  kernel name, `sageattn_qk_int8_pv_fp16_cuda`.
- The analyzed staged plan uses both GPUs: conditioning/video work is placed
  on the RTX 3060 and the H3 diffusion stage on the RTX 3090. Analysis did not
  queue work or load weights.

### Native control: pass

- Prompt ID: `72b78865-d779-4af9-a750-faf561617ee6`.
- Native 20-step `res_multistep`, sigma shift 12/3, fixed seed `424242`.
- Completed successfully in 1,041.6 seconds end to end. The sampler itself
  took about 99 seconds (about 4.95 seconds/step); cold model and conditioning
  loads dominated elapsed time.
- Persisted media:
  `capability_tests/H3_Native_20step_Shift12_3_FullStaged_00001_.mp4`.
- Media integrity passed: H.264/yuv420p, 352x608, 226,767 bytes, 2.333 seconds.
  The container duration is longer than the nominal 49/24 = 2.042 seconds and
  should be investigated separately as an encode/audio-duration observation.
- The staged cleanup released both model residency and GPU VRAM after output.
- The corrected CLI queried this exact artifact through `/api/outputs` using
  `kind=output`, `media_kind=video`, the `capability_tests` subdirectory,
  `H3_Native` prefix, and bounded metadata probing.

### Turbo without Sage: fail (bounded capacity)

- Prompt ID: `dd138dce-5dfe-480c-9a36-dffa3857f897`.
- EMA ckpt500 at strength 1.0, 8-step `res_multistep`, sigma shift 12/6.
- Reached the H3 stage, then failed in a comfy-kitchen INT8 linear temporary
  allocation. Peak allocated memory was about 21.8 GiB and peak reserved
  memory reached essentially the full 23.9 GiB device budget.
- No media was persisted. Comfy's OOM handler unloaded the model.

### Turbo with per-model Sage: fail on this runtime

- Prompt ID: `683b2844-227b-46df-97d6-f19ddf691f1b`.
- Live logs confirmed the exact per-model Sage CUDA kernel was active.
- It still OOMed during H3 loading/patching after VRAM climbed to about
  23.7 GiB. This rules out ordinary attention tensors as the sole cause and
  points to LoRA patch/materialization headroom against the INT8 base.
- No media was persisted.

### Forced-offload experiment: unsafe / aborted

- Comfy was restarted through the typed API with its same two-GPU pool,
  `vram_mode=low`, cache disabled, and `startup_options.reserve_vram=4`.
- Prompt ID: `69da8016-8a6c-4178-bd53-3da2bec3f61b`.
- Comfy honored the option: usable weight memory fell from about 22.0 GiB to
  18.4 GiB, and RTX 3090 residency stopped at about 20.4 GiB with roughly
  4.0 GiB free.
- During LoRA patching/offload, both gateway and Comfy HTTP sockets became
  unreachable and WSL could no longer spawn even a tiny command, reporting
  insufficient socket buffer space. The run was therefore treated as unsafe,
  not as a successful render. Gracefully closing the Windows Omni app released
  both GPUs immediately; no unrelated distro was accessed or stopped.
- Omni Studio was restarted visibly. Its bridge returned connected and the
  Omni distro came back with zero Comfy instances and no model residency.

## Qualification decision

- **Qualified:** native staged MiniMax H3 FL2V on the 3090+3060 pool; API
  analysis/queueing; pinned LoRA installation; exact per-model Sage patch
  selection; staged cleanup; persisted native media.
- **Not qualified on the current runtime:** larryvrh/drbaph EMA ckpt500 Turbo
  inference. Do not advertise it as locally runnable merely because analysis
  is `ready` or because the model files are installed.
- The external evaluation used ComfyUI `344b4398` and Torch 2.12.1+cu130 on an
  RTX 5080. This install uses a newer Comfy revision but Torch 2.7.1+cu128.
  Candidate-selection results transfer; its timing and memory behavior do not.
- Before another Turbo render, use a separate post-update cohort with a
  compatible newer Torch/Comfy runtime or a tested lower-memory patching path.
  Do not repeat the 4 GiB-reserve run interactively on this stack.

## Final state

- The Windows Omni Studio UI and lightweight gateway are running.
- No Comfy instance or standalone worker remains loaded.
- RTX 3090 inference residency returned to zero and RTX 3060 shows only normal
  desktop use. No global cache drop was performed.
- The Omni distro reports 31,262 MiB available, only 315 MiB buff/cache, and
  no workers. There was no reclaimable cache pressure to clear.
