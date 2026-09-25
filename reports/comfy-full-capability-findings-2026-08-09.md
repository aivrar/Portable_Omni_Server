# Omni Studio full capability probe findings

Date started: 2026-08-09

This is the running findings log for the execution plan in
`reports/comfy-full-capability-test-plan-2026-08-09.md`. Findings are appended
as they are reproduced. ComfyUI/custom-node/model source remains read-only;
Omni-owned fixes will be identified separately and verified with targeted
tests.

## Baseline state

- Gateway health: ready at the normal loopback API.
- Comfy instance: `comfy-cuda1-8188`, RTX 3090 physical `cuda:1` primary and
  RTX 3060 physical `cuda:0` auxiliary.
- In-process logical mapping: physical 3090 is logical `cuda:0`; physical 3060
  is logical `cuda:1`.
- Comfy reports both GPUs and zero model VRAM at ready state.
- Comfy startup reports PyTorch `2.7.1+cu128`, PyTorch attention,
  `cudaMallocAsync`, async weight offload, and pinned memory enabled.
- Comfy emits a warning that PyTorch with CUDA 13.0 or newer is required for
  optimized CUDA operations. `comfy_kitchen` eager is available; Triton and
  CUDA backends are detected but disabled. Treat int8-convrot correctness and
  performance as test targets rather than assuming the optimized CUDA kernel
  path is active.
- The deprecated `TRANSFORMERS_CACHE` warning persists. Migrate the runtime
  environment to `HF_HOME` before Transformers v5 removes the old variable.

## Installed Comfy assets at baseline

Only the MiniMax H3 stack is installed beneath the native Comfy model tree:

| Category | File | Approximate size |
|---|---|---:|
| diffusion_models | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | 19,999 MiB |
| text_encoders | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | 14,960 MiB |
| vae | `minimax_h3_video_vae_fp16.safetensors` | 4,967 MiB |
| vae | `minimax_h3_audio_vae_fp32.safetensors` | 577 MiB |

The current Comfy model root is `/opt/omni_studio/comfyui/models`; legacy
Windows-side storage links were not counted by the API and are not accepted as
installed dependencies.

## MiniMax H3 staged FL2V qualification

- Installed and verified `ComfyUI-ClipProj` at commit
  `ca9b325e83cb02cb5e652570569c6f3f20fee342`, its 4B H3 projection matrix,
  and Spectrum MiniMax H3 v0.2.5. Both repositories were absent from the
  current Manager catalog, so exact-revision installation used the existing
  asset-node API after Manager refused the uncatalogued repository.
- Added the fundamental `clip_projections` Comfy model category and mapped a
  workflow `projection` input to it. The projection now lives beneath
  `/opt/omni_studio/comfyui/models/clip_projections`.
- Added `OmniH3StageFL2VConditioning`: ClipProj 4B runs on the 3060, reference
  video-VAE encode runs on the 3090, both return CPU intermediates, and both
  unload before the H3 diffusion stage. A CPU VAE fallback remained responsive
  at the process level but was impractically slow and did not cooperate with a
  Comfy interrupt inside the encode call.
- Added `OmniH3ReferenceStrength` using Comfy's existing native keys
  `minimax_visual_cond_noise_aug` and `minimax_audio_cond_noise_aug`; Comfy core
  was not patched.
- Fixed-seed comparisons at 352x608, 73 frames, 12 steps succeeded at visual
  strengths `0.999` and `0.7`. The lower value retained identity, outfit,
  background, and final-frame wave while changing expression, hand angle, and
  body pose. Audio also changed despite audio strength remaining `1.0`; its
  mean level moved from `-21.0 dB` to `-17.7 dB`.
- Native sampling measured 73-74 seconds, but the first complete cold run took
  38:17. Storage page-in, especially the 4B Qwen encoder at roughly 2.2-3.5
  MB/s, dominated cold wall time. This is an I/O bottleneck, not a GPU compute
  or memory leak.
- Added default-off Spectrum controls at the staged sampler's internal MODEL
  seam. The first safe preset keeps video blend `0.5`, audio blend `0.0`,
  offline smoothing replay enabled, and both retained stores in system RAM.
  Third-party extension source remains unmodified. Its fixed-seed run completed
  successfully in 980.294 seconds total, produced the expected 352x608/73-frame
  MP4, and cleaned the diffusion stage before decode. Whole-video SSIM against
  native `0.999` was `0.977253`; visual identity/composition remained close,
  but audio differed and measured `-19.4 dB` mean. This is not yet a speedup
  claim because offline replay is two-pass and cold storage I/O varied.

## Workflow/template API findings

### Cold template-search latency and event-loop work

`GET /api/workflows/search` discovered about 600 workflow/template files. The
first Krea search exceeded a 15-second caller timeout, while warm searches took
about two seconds.

Omni-owned source evidence:

- `server/routers/workflows.py::search_workflows` is an async route but calls
  `_template_files()` synchronously and then reads/parses every candidate in
  the event-loop thread.
- `server/routers/assets.py::_installed_template_files` enumerates all Python
  distributions, walks each matching distribution manifest, resolves every
  JSON path, and checks the filesystem on each call.
- The search route then opens and parses each JSON document again for every
  query.

Impact: a cold search can delay unrelated lightweight gateway calls and causes
ordinary clients with conservative timeouts to report failure even though the
search may finish server-side.

Recommended Omni fix: cache a parsed searchable template index with explicit
invalidation based on mutable workflow-root state and installed template
package name/version manifests. Move cold rebuild work to a thread. Extend the
existing search route; do not add a duplicate endpoint.

### UI-only official templates block API execution

All newly selected official templates are local-inference but UI-format. Their
analysis correctly returns `needs-api-export` even when every native node is
installed. Exact examples:

- `image_krea2_turbo_t2i_int8`
- `image_ideogram4_t2i_int8`
- `image_hidream_o1_dev`
- `utility_birefnet_remove_background`
- `video_wan_animate2`
- `video_ltx2_3_flf2v`

This is currently the largest API usability gap. Search/fetch/analyze/download
are API-complete, but an agent cannot queue the official template until it is
materialized as API-format. Embedded subgraphs make a generic converter more
complex than rewriting top-level node dictionaries.

Required investigation: determine whether the installed Comfy frontend exposes
a supported graph-to-prompt service usable headlessly. If not, add a general
Omni materialization path only after it can preserve embedded subgraphs,
frontend widget semantics, and validation. Do not create per-model conversion
routes.

PowerShell's `ConvertTo-Json` did not safely round-trip the HiDream template
body and produced an HTTP body-parse error; Python's standard JSON round-trip
worked. Durable automation should use the bundled Python client/harness rather
than shell-specific JSON serialization for arbitrary Comfy UI documents.

## Exact dependency inventory

### Krea 2 OSS

The current Comfy repository revision inspected by API is
`Comfy-Org/Krea-2@952f49d49653cb42e7d6cf7cbfad74738073ec7d`.

- Raw BF16: 26.28 GB
- Raw FP8 scaled: 13.14 GB
- Raw int8 convrot: 13.49 GB
- Turbo BF16: 26.28 GB
- Turbo FP8 scaled: 13.14 GB
- Turbo int8 convrot: 13.49 GB
- Turbo NVFP4: 7.67 GB
- Qwen3-VL 4B FP8 text encoder: 5.24 GB
- Qwen image VAE: 0.254 GB
- Individual official style LoRAs: about 0.46-0.47 GB

The official Raw card says it is a base checkpoint intended mainly for
finetuning/post-training and is not the recommended normal inference model.
Its published reference inference setting is 52 steps at CFG 3.5 and 1024x1024.
Turbo's published setting is 8 steps, CFG 0, `mu=1.15`, demonstrated at both
1024 and 2048 square. The installed official Comfy Turbo template uses an int8
convrot model. Krea uses its community license and associated safeguard terms.

### Ideogram 4

The exact Comfy repository revision is
`Comfy-Org/Ideogram-4@9d0e686d42c1b1e575f0de15104d68e9157f59a0`.

- Conditional int8 model: 9.58 GB
- Unconditional int8 model: 9.58 GB
- Qwen3-VL 8B FP8 text encoder: 10.59 GB
- Flux2 VAE: 0.336 GB

Official discussion and the template confirm that conditional and
unconditional diffusion models are both expected. Ideogram 4 is a 9.3B
single-stream flow-matching model trained on structured JSON captions, supports
native 2K output, and is licensed under a non-commercial agreement. Plain-text
prompts work but are explicitly less aligned than exhaustive structured JSON;
prompt-refinement behavior must therefore be isolated in tests.

### HiDream-O1-Image

The exact Comfy checkpoint revision is
`Comfy-Org/HiDream-O1-Image@54d16b20496bbd1bdfa6f79ec1ad2d6f0bfd2dcc`.

- Dev FP8 checkpoint: 8.07 GB
- Full FP8 checkpoint: 8.07 GB
- Gemma4 E4B instruction FP8 text encoder: 9.06 GB

Upstream describes the Dev model as a 28-step distilled path and the full
model as a 50-step path, with full preferred for editing. It supports T2I,
editing, subject-driven personalization, and up to 2048x2048. The model license
is MIT. The native Comfy Dev template has no missing node classes.

### BiRefNet

- `birefnet.safetensors`: 444 MB
- optional `lucida.safetensors`: 885 MB

The official BiRefNet utility template uses only native installed nodes and the
single BiRefNet file. It is the lowest-cost feature and should be the first new
Comfy capability smoke-tested after API materialization is resolved.

### Wan Animate 2

The exact Comfy repository revision is
`Comfy-Org/Wan-Animate-2@9bd151d80e72c0840fc976bba8490f2d8bdc7a50`.

Selected official template dependencies:

- int8 convrot diffusion model: 16.65 GB
- UMT5 XXL FP8 text encoder: 6.74 GB
- CLIP vision H: 1.26 GB
- Wan VAE: 0.254 GB
- LightX2V 480p four-step LoRA: 0.738 GB

Total files are approximately 25.6 GB. The new native model directly consumes a
driving video, unlike older Wan Animate preprocessing pipelines. This makes it
the clearest current multi-GPU component-placement test: keep the diffusion
model on the 3090 and consider text/vision components on the 3060 only after a
valid analysis plan confirms node routing semantics.

### LTX 2.3 first/last-frame video

- Distilled FP8 checkpoint: 29.53 GB
- Gemma 3 12B FP4 mixed text encoder: 9.45 GB

The exact local blueprint remains UI/subgraph format and analysis requires API
export; neither dependency is installed. The approximately 38.98 GB pair
exceeds total WSL guest RAM before activation/offload overhead, and the 29.53
GB checkpoint alone exceeds the 3090. First/last-frame behavior is already
qualified through staged H3 FL2V. Therefore LTX was not downloaded or executed
in this pass: there is no valid API placement plan to authorize that distinct
large-model stress test, and it is not needed to prove the requested feature.
Revisit only on a larger host or after a separately materialized, analyze-valid
staged LTX API graph exists; keep pinned memory disabled for that probe.

## Test asset state

The following rights-safe generated assets are retained in
`test_assets/comfy-capability/` and uploaded through
`POST /api/comfy/comfy-cuda1-8188/proxy/upload/image` to the Comfy input subfolder
`omni-capability-tests`:

- `portrait-neutral.png`
- `portrait-profile.png`
- `fullbody-neutral.png`

The image-generation tool also retains immutable originals under its automatic
Codex output directory. Operational workflows use the app/Comfy copies above.

## Local TTS startup finding

MOSS-TTS worker `moss_tts-1` was requested through `POST /api/workers/spawn` on
the physical RTX 3090. The client timed out at 50 seconds while the registry
correctly retained a single `loading` worker; no duplicate was launched.

The worker log immediately emitted a Transformers warning that the tokenizer at
`/opt/omni_studio/models/omni/moss-tts-local-v1.5` uses the known incorrect
Mistral regex and should be loaded with `fix_mistral_regex=True`. The warning
states that leaving it unchanged can cause incorrect tokenization.

Classification: likely Omni loader integration unless the current
Transformers tokenizer API cannot accept the flag. Inspect
`server/moss_tts_loaders.py` and worker loading code after startup completes.
This is a credible pronunciation/text fidelity bug and should be fixed on the
Omni side if reproducible, then covered by a focused loader test.

## BiRefNet execution results

Installed `birefnet.safetensors` through the Omni asset-install API at
`/opt/omni_studio/comfyui/models/background_removal/birefnet.safetensors` and
saved an API-format seven-node workflow as `birefnet_capability_api.json`.
Analysis against the live Comfy instance reported `ready`, with no missing node
classes or model files.

Three API-queued inputs completed successfully:

- neutral portrait, cold model load: approximately 52.8 seconds
- profile portrait, warm cache: approximately 1.05 seconds
- neutral full body, warm cache: approximately 0.96 seconds

The saved RGBA portrait is 1024x1536 with a full 0-255 alpha range; about
41.63% of its pixels are fully transparent and 53.72% are fully opaque. Visual
inspection of the profile and full-body masks shows clean body boundaries and
good retention of fine curly-hair structure. The workflow's `InvertMask`
before `JoinImageWithAlpha` is intentional: `RemoveBackground` produces a
foreground mask, while `JoinImageWithAlpha` internally inverts its mask input.

API observation: the output registry and byte-serving routes correctly expose
the files, but image metadata currently reports `dimensions: null`. Adding
dimensions, channel count, and alpha presence to the existing output metadata
path would improve API-side verification without creating a new endpoint.

## MOSS-TTS execution results

The worker eventually reached `ready` and reported 17.26 GB of VRAM in use on
the RTX 3090. Two useful fixtures were generated through
`POST /api/tts/moss_tts` and appeared under the Media API's `kind=omni` root:

- fixed-seed neutral speech: 22.35 seconds generation, 6.24 seconds audio
- expressive speech: 24.99 seconds generation, 8.64 seconds audio

Both files are stereo 48 kHz PCM-16 WAVs. The neutral file measured roughly
-23.1 dB mean and -9.5 dB peak with FFmpeg's volume detector. Workspace copies
are stored in `test_assets/comfy-capability/` for later lip-sync testing.

The `speed` contract defect is empirically confirmed. Repeating the neutral
request with identical model parameters and `speed: 2.0` still produced exactly
6.24 seconds of audio, the same duration as `speed: 1.0`. The gateway forwards
the value, but `infer_moss_tts` never consumes it. The generated bytes were not
identical despite the fixed seed, which also means deterministic replay should
not be assumed without a separate reproducibility test.

After generation, `DELETE /api/workers/moss_tts-1` cleanly removed the worker.
The RTX 3090 returned to about 23.97 GB free while the lightweight Comfy process
remained running, confirming successful model unload and no persistent VRAM
leak from this run.

Omni-side fixes prepared in source:

- MOSS loading now injects `fix_mistral_regex=True` without modifying the
  upstream MOSS checkout, then restores the upstream processor class.
- Non-default MOSS `speed` values are applied with a bounded, pitch-preserving
  FFmpeg `atempo` chain covering the route's full 0.25-4.0 range.
- Three focused regression tests cover tokenizer injection/restoration, speed
  filter construction, and an actual WAV duration change.

## Additional prepared image fixtures

Created, retained under `test_assets/comfy-capability/`, and uploaded to the
Comfy input subfolder `omni-capability-tests`:

- `style-reference.png`: an original mixed-media visual-style target
- `first-frame-fullbody.png`: matched animation start frame
- `last-frame-wave.png`: same subject/scene with a changed wave/step pose

## API reliability improvements prepared

- Workflow search parsing now has a size/mtime-invalidated in-memory index and
  the package/filesystem scan runs in a worker thread, preventing cold template
  searches from blocking the FastAPI event loop.
- The existing output-metadata route now probes one explicitly requested media
  file for dimensions, alpha/channel information, duration, codec, and audio
  shape. PNG and WAV use bounded standard-library reads; other image/video/audio
  formats use a 15-second-bounded `ffprobe` call. Bulk listing remains cheap.
- Focused cache and artifact-metadata regression tests pass, as do the required
  placement, multi-GPU, and workflow-requirements suites.

## Gateway/Comfy lifecycle defect

A lightweight gateway restart stopped the still-running Comfy process and the
new gateway returned an empty instance registry. Two behaviors combined:

- graceful gateway shutdown calls `comfy_manager.stop_all()`;
- startup previously treated every record owned by the former gateway PID as
  an orphan and killed it rather than re-adopting it.

Omni now persists the instance's device, GPU pool/map, VRAM mode, precision,
preview mode, pinned-memory choice, and structured startup options in its PID
record. On startup it validates the live PID command line and port, probes
`/system_stats`, registers the same process, transfers record ownership to the
new gateway, and restores current VRAM state. Full Omni shutdown retains its
existing stop-all behavior. Development/lightweight restarts can now use an
unexpected-process restart path and recover Comfy instead of terminating it.

Live verification succeeded: after an abrupt gateway-only restart, Comfy kept
PID `15408`; the replacement gateway re-registered `comfy-cuda1-8188` as
`ready` with physical `cuda:1` primary, physical `cuda:0` auxiliary, and the
same logical device map.

## Krea 2 installation and first live plan

Installed through the Xet-backed asset API at pinned Comfy-Org/Krea-2 revision
`952f49d49653cb42e7d6cf7cbfad74738073ec7d`:

- `diffusion_models/krea2_turbo_int8_convrot.safetensors`
- `diffusion_models/krea2_raw_int8_convrot.safetensors`
- `text_encoders/qwen3vl_4b_fp8_scaled.safetensors`
- `vae/qwen_image_vae.safetensors`
- `loras/krea2_style_reference.safetensors`
- `loras/krea2_darkbrush.safetensors`

Saved separate API graphs for Turbo (8 steps, CFG 1.0) and Raw (52 steps,
CFG 3.5) with the same 1024-square prompt and seed. Turbo readiness is `ready`
with zero missing models/nodes. Its valid automatic plan assigns the INT8
diffusion model to the physical RTX 3090 and the Qwen encoder plus VAE to the
physical RTX 3060. Queue submission inserted three `OmniRoute*` nodes and
returned prompt `bfb38aaa-3aeb-4d86-adc9-5a8de031202c` with no node errors.

During cold routed loading, Comfy logged two warnings that
`Krea2TEModel_` remained referenced when its original patcher was unloaded,
forcing a full garbage collection and labeling it a potential memory leak.
This appears during the `OmniRouteCLIP` deep-clone/reload path and may be an
execution-cache reference rather than unrecoverable memory. Classify it only
after the run's explicit `/free` request and before/after VRAM check.

The cold Turbo run completed successfully in 466.756 seconds and saved
`capability_tests/Krea2_Turbo_INT8_00001_.png`. The output is a 1024x1024
RGB PNG (1,228,480 bytes) whose embedded prompt contains the three inserted
`OmniRoute*` nodes. Visual inspection found a coherent subject, correct
lantern/wardrobe/blue-hour lighting, natural hands, and no obvious INT8
quantization artifact.

The warning was confirmed as an Omni-owned routing defect. Comfy's
`CLIP.clone()` keeps both `patcher` and `cond_stage_model`, while
`OmniRouteCLIP.route()` replaced only `patcher`; all encoding methods execute
through `cond_stage_model`. The old text encoder therefore remained strongly
referenced on its original device. An explicit `/free` immediately after the
run left about 6,898 MiB allocated on the physical RTX 3060 and 13,504 MiB on
the physical RTX 3090. Fixed the bundled node by aligning
`routed.cond_stage_model = routed.patcher.model` after routing, deployed the
single changed node file, and restarted only the Comfy instance for a clean
repeat.

The controlled repeat showed that aligning `cond_stage_model` corrected the
observed placement (the physical RTX 3060 held about 6.9 GiB for the routed
encoder while the RTX 3090 held the diffusion allocation), but the warning
still appeared. A second narrow source trace found the remaining cause in
`ModelPatcher.deepclone_multigpu()`: its cached CLIP factory can eagerly load
and register a temporary patcher, then the method retains that model in the
routed clone while the temporary patcher dies. Comfy consequently keeps a
`LoadedModel` entry whose patcher weakref is dead but whose real model is still
referenced. The Omni bridge now prepares CLIP factory options with the selected
`load_device` and CPU `initial_device`/`offload_device`, preventing that
temporary eager registration. This second change requires another clean Comfy
restart and live unload verification before it is considered resolved.

The pre-second-fix control completed successfully in 464.372 seconds and saved
`Krea2_Turbo_INT8_00002_.png`. Its SHA-256 matched the first image exactly,
confirming deterministic output for the fixed graph and seed and showing that
the `cond_stage_model` alignment did not change image semantics. The warning
recurred and `/free` still left about 6,955 MiB on the physical RTX 3060 and
13,504 MiB on the RTX 3090, providing a clean negative control for the factory
retargeting change.

Even with the dead-entry fix, ordinary post-loader routing has a cold-I/O
cost: the base loader reads/builds the component and
`deepclone_multigpu()` invokes its cached factory to produce a pristine model
for the selected GPU. During the final verification run the Comfy process had
read roughly 14 GiB before sampling. This is not a leak, but it is a material
startup bottleneck. A future optimization should use an Omni-owned routed
loader/factory path that selects the final device before the first weight load,
while retaining current route nodes as the compatibility fallback. Do not
patch third-party loaders ad hoc.

A seed-only warm rerun initially failed with HTTP 409 because placement used
current free VRAM and added the complete workflow estimate again, double-
counting components already resident in the selected Comfy process. The device
detector now records NVIDIA compute PIDs, and placement treats existing VRAM as
reusable only when every compute context on a selected GPU belongs to the
target Comfy PID. If ownership is unknown or any other compute PID is present,
the planner remains conservative and uses current-free headroom. No endpoint
was added. After a gateway-only restart (Comfy PID 20814 and loaded models were
preserved), the same analysis became valid with no blockers and the existing
parameterized run API applied the seed override successfully.

The warm seed run completed successfully in 23.080 seconds, versus 552.227
seconds for the clean cold run. Visual output remained coherent and changed
composition naturally with the new seed. `/free` returned both GPUs to their
baseline allocations (about 1,497 MiB on the physical RTX 3060 and 391 MiB on
the RTX 3090), and the current Comfy log contained zero memory-leak warnings.
The `/api/devices` result initially concealed this successful cleanup because
device telemetry was cached for 30 seconds. Reduced the existing detector
cache TTL to two seconds so placement and cleanup checks do not act on stale
VRAM; no API route was added.

PyTorch also warned that the active `cudaMallocAsync` backend ignores
`max_split_size_mb`, `roundup_power2_divisions`, and
`garbage_collect_threshold`. Treat those allocator settings as ineffective in
this launch mode; tuning them would create false confidence rather than improve
Krea throughput. The measured sampling path used native INT8/ConvRot operations
where available and PyTorch attention.

### Krea 2 Raw invalid control and corrected recipe

The first Raw graph completed in 671.942 seconds at 1024 square (52 steps,
CFG 3.5) and saved `Krea2_Raw_INT8_00001_.png`, but the output was invalid:
a small recognizable scene was buried in severe full-frame chromatic noise and
border artifacts. Preserve this as a negative control; do not present it as
model quality.

The official Raw model card confirms 52 steps and CFG 3.5 but explicitly says
Raw is a base checkpoint that is not recommended for routine inference:
https://huggingface.co/krea/Krea-2-Raw. The official sampler additionally uses
an independently encoded empty-string unconditional branch and a
resolution-derived flow shift (`y1=0.5` at 256, `y2=1.15` at 1280). At 1024
that interpolation is `0.90625`. Current Comfy Krea support hardcodes shift
`1.15`, matching Turbo's fixed schedule, and the original hand-authored graph
used `ConditioningZeroOut` instead of a true empty prompt.

Saved `krea2_raw_int8_corrected_smoke_api.json` through the API with native
`ModelSamplingFlux(max_shift=0.90625, base_shift=0.5)`, an empty
`CLIPTextEncode` negative branch, and a 16-step smoke gate. Its analysis is
ready with zero missing dependencies and a valid warm-residency-aware plan.

The corrected 16-step warm smoke completed in 89.391 seconds. It removed the
destructive chromatic noise and produced a recognizable, coherent subject, so
the schedule/unconditional diagnosis is confirmed. It remained under-refined
and placed the scene inside a conspicuous black inset frame. Saved and queued a
separate corrected 52-step graph with the same seed to determine whether that
artifact is short-denoise behavior or an inherent Raw composition tendency.

The corrected 52-step warm pass completed in 285.181 seconds. It was fully
denoised, coherent, and substantially more detailed, but retained the same
large inset-frame composition. This establishes the frame as a Raw/seed
tendency rather than incomplete denoising. Given the official recommendation
and measured quality/time tradeoff, stop further Raw inference variants: use
Raw for training/post-training research and Turbo for production generation.
An explicit `/free` returned both GPUs to baseline (approximately 1.7 GiB on
the display-attached 3060 and 0.6 GiB on the 3090 through the device API).

Extended the existing analyzer with a Krea Raw semantic guard. A graph using a
Raw filename but lacking a compatible resolution-derived `ModelSamplingFlux`
shift or an independently encoded empty negative branch now reports
`needs-review` instead of `ready`, preventing a repeat of the 11-minute noisy
control. Focused offline regression cases were added but not executed in this
session because bundled Python test runners were explicitly avoided.

### Krea 2 Turbo image style reference

The local official template ID is
`image_krea2_turbo_int8_image_style_reference`. Fetching it by ID alone returned
404 even though search found it; qualifying the request with its returned
`source=package-template` and `package=comfyui-workflow-templates-json`
resolved it. Updated the API guide/runbook to preserve those provenance fields.

The template is UI-format with an embedded subgraph. Reconstructed its native
local execution graph in API format using `TextEncodeQwenImageEditPlus`,
`FluxKontextMultiReferenceLatentMethod`, the style-reference LoRA,
`ModelSamplingFlux`, `CFGGuider`, `BasicScheduler`, and
`SamplerCustomAdvanced`. Saved `krea2_turbo_style_reference_api.json` through
the API with the uploaded `omni-capability-tests/style-reference.png` fixture.
Analysis reports ready, zero missing models/nodes, and a valid two-GPU plan.
The first queued prompt (`a79ebfa3-bc8c-4b60-8d29-648ade807508`) was
interrupted before sampling after plan inspection exposed a placement bug:
the generic loader detector treated `LoraLoaderModelOnly` as a second,
independently placeable diffusion model because its class name contains
`Loader` and its output is `MODEL`. That could route the final LoRA-patched
diffusion path away from the base model. Updated discovery so loader-like
nodes that consume an upstream `MODEL`, `CLIP`, or `VAE` are treated as
transform nodes rather than independent components. Added a focused source
regression case without running the test suite, deployed only the placement
module, and restarted only the lightweight gateway. The original Comfy PID
remained ready.

Re-analysis then produced exactly three valid components: Krea base model on
the RTX 3090, Qwen text encoder on the RTX 3060, and VAE on the RTX 3060. The
style LoRA remained attached to the base-model path. Prompt
`abb79cfe-2f43-4bcd-8927-c8e63cb8a4f4` completed successfully in 171.42
seconds including cold loads; the eight sampling steps took about 43 seconds.
Output `capability_tests/Krea2_Turbo_StyleReference_00001_.png` is coherent
and high quality: clean fox anatomy and scene structure with strong transfer
of the fixture's navy/vermilion/cream/gold palette, textured paper, ink
brushwork, and geometric abstraction. No destructive noise, inset-frame, or
visible routing defect was present. Live device telemetry confirmed the same
Comfy PID held compute contexts on both GPUs during execution.

The saved-workflow convenience parameter `seed` did not match this graph's
`RandomNoise.noise_seed` input and was correctly reported in
`patch_report.unmatched`; the saved seed was therefore used. Subsequent runs
must use the exact `11.noise_seed` parameter and require it in
`patch_report.applied`.

A warm 1536-square stress variant used exact overrides for
`9.width/height`, `14.width/height`, and `11.noise_seed`. All five overrides
were reported applied, none unmatched, and the recalculated two-GPU plan was
valid. Prompt `72572782-d412-4f9a-a655-152cd3858b2d` succeeded in 422.92
seconds as `capability_tests/Krea2_Turbo_StyleReference_00002_.png`. The image
is visually strong, with more detailed anatomy and clean large-form style
transfer, but the runtime path is not dependable enough to recommend as a
default on this machine.

During the final decode/offload section, Comfy emitted repeated `Pin error`
warnings, WSL working set reached approximately 27.4 GiB, and both the Omni
gateway and direct Comfy HTTP control plane became unresponsive for several
minutes even though GPU utilization had fallen nearly idle. WSL recovered
without a restart, then Comfy reported partial unloading of the 4999.47 MB
text encoder and successful completion. Treat 1024 square as the dependable
default for this style-reference graph. Treat 1536 as an opt-in stress setting
with a clear responsiveness warning; do not queue other work alongside it.
The instance currently has pinned-memory support enabled, so the structured
`disable_pinned_memory` startup option is a plausible future mitigation test,
but this run does not establish that disabling it will improve total runtime.
Model cache was explicitly released after inspection.

Two `/free` requests after the 1536 style run released the auxiliary text/VAE
path but left approximately 13.3 GiB resident on the primary RTX 3090 under
the Comfy PID. The amount matched the diffusion path. Inspection found that
`OmniRouteModel` cloned a patcher even when the requested device already
equaled its current load device; the downstream style LoRA then cloned that
redundant identity again. Changed same-device routing to a true pass-through
so Comfy retains the loader's normal cache/unload identity. Added a focused
source regression case without running the test suite. This custom-node fix
requires the next Comfy restart and still needs post-run `/free` verification.

The pressure-affected instance disappeared from the manager's live registry
after it finished, so its stop call correctly returned 404 and the instance
list was empty. Starting the replacement through the API preserved
`device=cuda:1`, `gpu_pool=[cuda:1,cuda:0]`, and enabled the structured
`disable_pinned_memory=true` mitigation. On this cold start, ComfyUI-Manager's
prestartup hook alone reported 48.0 seconds. The Python process subsequently
spent extended periods in the kernel wait channel `wait_on_page_bit_common`,
showing that the remaining startup delay was distro VHD page-in latency rather
than CPU spin. Treat Manager prestartup and cold VHD page-in as independent
startup bottlenecks when measuring lifecycle responsiveness.

Post-restart verification with the same 1024 style graph succeeded as prompt
`62a28360-e413-4a7c-8a23-710e570c2c13` and output
`Krea2_Turbo_StyleReference_00003_.png`. The run took 555.51 seconds from a
fully cold distro/model state; the sampler itself took 48 seconds. No `Pin
error` warnings occurred with `disable_pinned_memory=true`, and the control
plane remained responsive, but the cold total was much worse than the earlier
171.42-second run. Because the distro was simultaneously repaging after the
pressure event, this single comparison cannot attribute all extra latency to
pinned-memory policy.

The post-run cleanup hypothesis was not confirmed. Both the authenticated
Omni proxy `/free` call and a direct native Comfy `/free` call used the exact
current core payload (`unload_models=true`, `free_memory=true`), yet the RTX
3060 retained about 6.2 GiB and the RTX 3090 about 15.5 GiB. Local Comfy source
shows those flags should wake the prompt worker, call `unload_all_models()`,
and reset the executor cache; no unload or leak error was logged. Therefore
the remaining defect is downstream model tracking/unloading for this routed
Krea+LoRA graph, not gateway body forwarding. Do not claim `/free` is a
dependable hard-release operation for this graph. The existing instance stop
API remains the dependable fallback: stopping the exact instance returned the
3090 to 0 MiB and the display-attached 3060 to approximately 0.7 GiB within
two seconds. External Comfy source was inspected only and not modified.

### HiDream and Ideogram dependency materialization

With Comfy stopped and both GPUs at baseline, the targeted HiDream Dev
requirements API reported exactly two missing, Xet-capable artifacts beneath
the distro's native Comfy tree:

- `checkpoints/hidream_o1_image_dev_fp8_scaled.safetensors`
- `text_encoders/gemma4_e4b_it_fp8_scaled.safetensors`

Installed the Dev checkpoint as isolated template-install job `b1eda4e9`; the
job completed successfully and the subsequent requirement check reported only
the Gemma encoder missing. Encoder job `bf58cf5c` was then started separately
so each large artifact can be verified independently.

The qualified HiDream template is UI-format and local. Its native Dev path
uses `CheckpointLoaderSimple`, `ModelNoiseScale=7.6`, 28 normal scheduler
steps, `SamplerLCM` values `1, 1, 2.5`, `SamplerCustom` CFG 1, and
`EmptyHiDreamO1LatentImage`. The template defaults to 2048 square but the first
capability run should use a lower smoke resolution. Its optional `Prompt
Enhancement` subgraph contains the separate Gemma loader; direct T2I can be
reconstructed without hosted API nodes.

The qualified Ideogram INT8 template is also UI-format/local and contains a
27-node `Text to Image (Ideogram v4)` subgraph plus a caption-template
subgraph. Exact model widgets are `ideogram4_int8_convrot.safetensors`,
`ideogram4_unconditional_int8_convrot.safetensors`,
`qwen3vl_8b_fp8_scaled.safetensors`, and `flux2-vae.safetensors`. Its official
defaults are Euler, 20 Ideogram scheduler steps, 1024 square, scheduler values
`0.5` and `1.75`, dual-model guider 7, and CFG override values `3, 0.7, 1`.
Use structured JSON captions for the typography qualification rather than
silently relying on plain-text prompt refinement.

HiDream encoder job `bf58cf5c` completed successfully. A final targeted
requirements check for `image_hidream_o1_dev` reports one template, zero
missing, zero downloadable missing, and zero undownloadable dependencies.

Ideogram dependencies were installed sequentially with no overlapping jobs:

- conditional INT8 diffusion: job `11c50202`
- unconditional INT8 diffusion: job `21c79783`
- Qwen3-VL 8B FP8 text encoder: job `824d157e`
- Flux2 VAE: job `42c210a5`

Every job completed on its first Xet attempt and reported the exact target
under `/opt/omni_studio/comfyui/models/<category>`. The final targeted
requirements check for `image_ideogram4_t2i_int8` reports one template, zero
missing, zero downloadable missing, and zero undownloadable dependencies.
Large Xet writes can briefly delay gateway status calls even though the job
continues correctly; avoid stacking retries or starting a duplicate install.

### HiDream-O1 Dev live qualification

Saved `hidream_o1_dev_768_capability_api.json` as a local-only API graph with
the official Dev settings: checkpoint loader, `ModelNoiseScale=7.6`, 28 normal
steps, `SamplerLCM(s_noise=1, s_noise_end=1, noise_clip_std=2.5)`, CFG 1, and
the HiDream latent node. It persists auto placement by stable UUID. Analysis
reported ready, no missing models/classes, and a valid plan that placed the
diffusion model on the RTX 3090 and checkpoint CLIP/VAE outputs on the RTX
3060.

Prompt `28840b44-ce68-4ae9-a77f-d90135416ad6` produced
`HiDream_O1_Dev_768_00001_.png` successfully in 155.38 seconds from cold; the
28-step sampler itself took about nine seconds. The image is coherent with
good hands, face, greenhouse depth, and lighting, but underplayed the requested
clockwork concept. A warm 1024 override with a stronger prompt completed in
11.3 seconds as `HiDream_O1_Dev_768_00002_.png`. It cleanly rendered brass
mechanical forearms, tools, greenhouse context, and credible anatomy. For Dev
T2I on this hardware, 1024 square/28 steps is the practical quality default;
768 is useful only as a cold smoke gate.

Saved `hidream_o1_dev_single_edit_1024_api.json` with one local portrait
reference. The graph was technically valid and prompt
`43a27083-0620-4147-a180-c5e075feb981` executed in 23.4 seconds, but its
1024-square output was destructive high-frequency noise. Official-template
link inspection found the missing semantic: image-edit mode derives the output
latent dimensions from a Lanczos-scaled four-megapixel copy of the reference,
while passing the original image to `HiDreamO1ReferenceImages`. For a square
reference, the intended latent is 2048 square, not 1024.

An analyze-only 2048 override remained ready and placement-valid. The guarded
official-size run `b1965bb0-cd69-4f98-b6db-32b457f894bb` completed in 75.6
seconds as `HiDream_O1_Dev_SingleEdit_2048_00001_.png`, with healthy VRAM
headroom and a responsive control plane. It strongly preserved face, hair,
and teal clothing from the reference while cleanly applying a greenhouse,
workshop apron, gears, and coherent brass mechanical arms/hands. Treat four
megapixels as a semantic requirement for the native single-image edit path,
not merely an optional quality increase.

Saved `hidream_o1_dev_multi_reference_2048_api.json` with front and profile
views of the same local subject. Analysis was ready/valid and prompt
`8aa1a2c1-b8f4-40c0-8d96-c97b18352041` completed in 82.9 seconds. Identity
family, clothing palette, and scene quality were strong, but the model rendered
the two views as two separate people despite an explicit same-person prompt.
Multiple reference slots therefore must not be described as automatic
multi-view identity fusion; they can be interpreted as distinct subjects.

### Ideogram 4 INT8 placement and CPU-decode limit

Saved `ideogram4_int8_typography_1024_api.json` from the qualified local
template with both INT8 diffusion models, Qwen3-VL 8B FP8, Flux2 VAE,
`CFGOverride(cfg=3, start=0.7, end=1)`, `DualModelGuider(cfg=7)`, Euler, and
`Ideogram4Scheduler(steps=20, width=1024, height=1024, mu=0.5, std=1.75)`.
The fixture requests four exact poster text lines and a robotic-fox subject.

The first automatic analysis correctly hard-stopped: with the normal 1 GiB
reserve the two diffusion components and Qwen encoder could not coexist with
the VAE on the two GPUs. This exposed an Omni planner gap: `allow_cpu=true`
did not actually expose CPU to an explicit override on a mixed-GPU instance.
`server/comfy_placement.py` now allows CPU only when an explicit component
override requests it; automatic placement remains GPU-only, CPU is reported
only when used, it emits a performance warning, and it does not inflate the
used-GPU count. A source-only regression was added to
`tests/test_comfy_placement.py`; no broad test runner was invoked.

The persisted manual policy used stable GPU placement for the conditional
model plus text encoder on the RTX 3090, the unconditional model on the RTX
3060, and only `4:vae` on CPU, with `reserve_mb=0`. Re-analysis was ready and
valid with no blockers. Prompt `6e039826-6a90-465b-89a7-f3466e47d58a`
loaded both approximately 9.14 GB diffusion weights successfully and finished
all 20 sampling steps in about 128 seconds (about 6.4 seconds/step).

The CPU Flux2 VAE decode then remained active for well over ten minutes while
using roughly one CPU core, despite six-core affinity and all Omni math-thread
environment limits being set to six. It produced no decode error but never
reached `SaveImage` within a practical run budget. Comfy accepted a global
interrupt but its VAE decode did not poll the interrupt flag, so an exact
instance stop was required. That stop immediately restored the RTX 3060 to
about 11.4 GB free and the RTX 3090 to about 24.3 GB free with no compute PID.

Result: the graph, two-GPU placement, model loading, conditioning, and sampler
are qualified, but CPU VAE is a compatibility fallback rather than a usable
default on this host. A dependable Ideogram preset needs either more aggregate
GPU headroom or a staged execution boundary that unloads a diffusion model
before GPU VAE decode. Ordinary routing cannot guarantee that mid-graph
unload. Do not advertise this exact two-GPU preset as end-to-end qualified.

#### Resolved with staged GPU VAE decode

Omni now provides `OmniStageVAEDecode`, a generic node that explicitly unloads
upstream graph models before constructing a named VAE on the selected GPU, then
unloads the VAE after decode. Placement analysis follows the node's `samples`
ancestry: upstream models/encoders form stage 1 and the VAE forms stage 2.
Planner accounting was also corrected so components sharing a stage add
together instead of using only the largest component.

Saved `ideogram4_int8_typography_1024_staged_api.json` with a UUID-based manual
policy: conditional diffusion plus Qwen on the 3090, unconditional diffusion
on the 3060, and Flux2 VAE on the 3090 after the boundary. Analysis returned
ready/valid, fully staged, with estimated peaks of 23,641 MiB and 11,260 MiB.

Prompt `762a953b-2e9c-4b5f-aad5-01bf6b9836e1` completed end-to-end in 609.664
seconds and produced `Ideogram4_INT8_Typography_1024_Staged_00001_.png`. The
robotic-fox poster was coherent and all four primary phrases appeared, but it
rotated the typography, added misspelled filler, and did not follow requested
text bounding boxes exactly.

A same-prompt native-2048 parameterized run applied all five intended patches
with none unmatched. Prompt `c8e2f913-37fa-47a5-8f9c-4576e3b28f96` completed
in 944.141 seconds as
`Ideogram4_INT8_Typography_2048_Staged_00001_.png`. All four requested strings
were legible and the 1024 filler gibberish disappeared, but the model retained
a vertical editorial layout instead of the requested horizontal placements.
After each run, the dual diffusion stage unloaded before decode and GPU memory
returned to roughly 0.9 GiB on the display 3060 and 0.4 GiB on the 3090.

Result: the staged preset is now end-to-end qualified. Use 1024 for smoke and
concept work; use 2048 when spelling fidelity justifies the additional time.
Neither setting guarantees exact typography layout. The non-commercial model
license remains an external deployment constraint.

### Wan Animate 2 native template and dependencies

The current package template is `video_wan_animate2.json` / template id
`video_wan_animate2` from `comfyui-workflow-templates-json` 0.1.37. It is a
local workflow but is stored in UI/subgraph format, so direct API execution
requires an API-format export or an equivalent explicit graph. Its native
motion-transfer subgraph declares 832x480, 81 frames, six simple-scheduler
steps, LCM sampling, a 21-frame context with eight-frame overlap, and the
WanAnimate2 GPU INT8 cache. The template note says longer videos currently
require manually duplicating and chaining the motion-transfer subgraph.

Background template-install job `7adc1c01` installed all five declared assets
sequentially through Hugging Face/Xet, with every file succeeding on attempt
one:

- `diffusion_models/wan_animate_2_int8_convrot.safetensors`
- `loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors`
- `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`
- `clip_vision/clip_vision_h.safetensors`
- `vae/Wan2_1_VAE_bf16.safetensors`

The post-install exact-template scan reports one template, five declared
models, zero missing, zero downloadable missing, and zero undownloadable.

### Wan Animate 2 live qualification

Saved `wan_animate2_21f_smoke_api.json` through Omni's workflow API with a
stable-UUID auto policy. The API graph reproduces the native loaders,
conditioning, CLIP Vision branches, `WanAnimate2ToVideo`, INT8 cache,
`ModelSamplingSD3(shift=5)`, six-step simple scheduler, LCM sampler, latent
trim, VAE decode, and native `CreateVideo`/`SaveVideo`. The smoke uses one
21-frame context at 480x832 and keeps the Wan cache on CPU as recommended by
the template note. The live node contract estimates the full 832x480/81-frame
cache at about 12.5 GB RAM in BF16; INT8 and a single 21-frame window sharply
reduce that pressure.

Analysis was `ready`, `ready_to_run=true`, and placement-valid with an empty
queue. The planner put the Wan diffusion component on the RTX 3090 (18,811 MB
estimate) and UMT5 plus VAE on the RTX 3060 (8,995 MB combined estimate). Live
loading reached roughly 20.3 GB on the 3090 before sampling, so the current
model estimate understates this graph's observed residency/overhead by at
least about 1.5 GB. CLIP Vision is not represented as its own placement-plan
component and loaded approximately 1.21 GB on the primary during preparation.

The first prompt `3e863f53-6bf7-4f5a-8aba-ab285285d7db` used an existing
73-frame H3 fox-running clip as the driver. It succeeded cold in 643.75 seconds
and saved `WanAnimate2_21f_CPUCache_480x832_00001_.mp4`: H.264/YUV420p,
480x832, 21 frames at 24 fps, 0.875 seconds. Frames were sharp, temporally
coherent, and anatomically plausible, but the human inherited a crouched
running gait and brass mechanical legs. The driver was a fox while the
reference was a standing full-body human. This empirically confirms the native
template's warning that reference/driver framing mismatch is the leading
quality failure, and it must remain a rejected boundary result rather than a
model-quality preset.

Created `human-wave-driver-21f.mp4` from the rights-safe matched neutral/wave
fixtures, verified it as 480x832/21 frames/24 fps, and uploaded it through the
existing Comfy upload proxy. A same-seed parameterized run changed only the
driver, accurate character/pose text, and output name. Its analyze gate stayed
ready and valid; all four run overrides were applied with none unmatched.
Prompt `cb61cea6-790f-40a0-aee3-b7479127aeeb` completed warm in 88.19 seconds
as `WanAnimate2_21f_MatchedHuman_480x832_00001_.mp4`. Sampled frames preserve
the subject's face, curly hair, teal jacket, cream shirt, black trousers,
boots, full-body framing, five-finger raised hand, and stable gray background.
This is the qualified short local motion-transfer preset.

Both cold and warm paths emitted repeated `Pin error` warnings. Narrow source
inspection found the literal path in external Comfy
`comfy/model_management.py`: `pin_memory()` calls `cudaHostRegister`, warns on
a nonzero result, and clears the async CUDA error. On Linux/WSL the configured
maximum is derived from RAM plus swap and can substantially exceed the memory
that WSL/NVIDIA will actually register. This is a Comfy resource-policy trap,
not evidence of a failed sample. Keep pinned memory enabled as the current
general default until a bounded Wan-specific A/B proves otherwise; the earlier
Krea no-pin test had a severe cold-load penalty. External source was not
modified.

Finally, Comfy's `/free` accepted the request but left both routed Wan model
sets resident (only about 5.4 GB free on the 3060 and 4.2 GB on the 3090).
Stopping the exact instance restored approximately 11.3 GB and 24.3 GB free
with no compute PID. Wan therefore joins Krea routed graphs in requiring the
exact-instance stop for dependable hard release on this build.

### ACE-Step local singing qualification and API fixes

`GET /api/ace_step/status` reported a complete local stack: official
`ace-1.5` 2B core bundle, XL Turbo, 0.6B and 1.7B planners, shared VAE/text
core, and no missing runtime dependency. Comfy was stopped before ACE load so
the worker had exclusive GPU headroom.

The first `ace-1.5` + 0.6B LM load exposed an offline-readiness defect. Status
reported the model installed, but the worker invoked upstream's downloader for
28 files from `ACE-Step/Ace-Step1.5` into the managed checkpoints directory.
External source inspection showed upstream `check_main_model_exists()` always
requires four weighted component directories: 2B turbo DiT, shared Qwen text
encoder, official VAE, and bundled 1.7B LM. Omni's staging linked only the DiT
and text encoder and passed the VAE separately; it did not stage the mandatory
official VAE and 1.7B LM directories before `initialize_service()`.

Omni source now stages all four required components from managed installs and
validates that each source contains model weights. A focused source-only
regression covers the complete native checkpoint layout. This prevents a
future offline-ready load from turning into an implicit full-repo download.
Only syntax compilation was run; no broad test runner was invoked.

The 2B model reached ready with about 10.3 GB reported VRAM use. Job
`427d82953fc04eb0bcbed328a6209a15` generated an original 15-second sung
indie-electronic clip in 24.427 seconds using eight Euler steps, CFG 1, seed
20260821, explicit original lyrics, BF16, and overlapped decode. The WAV is
stereo 48 kHz PCM-16 and exactly 15.000 seconds; independent volume detection
measured about -19.1 dB mean and -1.0 dB peak. ACE analysis measured 106.13
BPM against the requested 104, F-sharp with low key confidence, -21.7 dB RMS,
and 0.843 peak amplitude. The API-downloaded fixture is
`ace-step-indie-sing-15s.wav`.

The jobs summary returned `prompt: null` even though the manifest correctly
stored `params.prompt`. The summary route now falls back to that canonical
field, preserving prompt discoverability without changing the endpoint.

XL Turbo exposed a second API reliability issue. A timed-out/retried load can
continue inside the worker, and pre-fix gateway calls performed `load_model`
and `load_lm` as separate unguarded operations. A second request could acquire
the worker lock between them, free the just-loaded DiT, make the first LM swap
fail, and trigger another approximately ten-minute four-shard load. Observed
XL shard passes took 9:38 and 9:51, roughly 144-158 seconds per shard. Omni
source now wraps the entire model+LM load transaction in a gateway lock,
exposes `model_load_kwargs` in worker state, and rechecks state inside the lock
so an identical retry becomes a no-op rather than another model load.

After the final XL load and a 46-second managed 1.7B LM initialization, the
stack left about 5.1 GB free on the 3090. Same-seed job
`047233e090c84ea38c0c62bc2c8763bc` generated the identical 15-second test in
17.658 seconds, faster than 2B warm inference. Its audio analysis measured 125
BPM despite the same requested 104, key C with low confidence, -21.29 dB RMS,
and 0.8704 peak. XL is therefore faster warm in this sample but not more
tempo-faithful. The API-downloaded fixture is
`ace-step-xl-indie-sing-15s.wav`.

`POST /api/ace_step/unload {component: all}` cleared model and LM weights,
leaving only the worker CUDA context. Deleting exact worker `ace_step-1`
restored approximately 24.3 GB free on the 3090 and removed all compute PIDs.

### HuMo local audio-driven portrait and lip-motion qualification

The official `video_humo` template resolved to five local dependencies. Xet
job `04a40fe1` installed `humo_17B_fp8_e4m3fn.safetensors`,
`whisper_large_v3_fp16.safetensors`, and `wan_2.1_vae.safetensors`; the
LightX2V distilled LoRA and UMT5 FP8 encoder were already present. The final
exact dependency scan reported five installed models and no missing,
downloadable, or unresolved item.

The template is a 20-node local graph: image plus audio are encoded by
`WanHuMoImageToVideo`, sampled through the 17B HuMo/Wan model, decoded with the
Wan VAE, and muxed with the input audio. Its live contract accepts width,
height, frame length in steps of four, batch size, optional audio conditioning,
and optional reference image. The rights-safe neutral portrait and a locally
generated ACE-Step singing clip were uploaded through the existing Comfy API.

Preflight exposed two Omni-owned coverage gaps before a successful run:

- `AudioEncoderLoader.audio_encoder_name` was treated as an unknown weight
  field. It is now classified under `audio_encoders`, so installed Whisper
  resolves cleanly instead of forcing `needs-review`.
- Whisper was absent from GPU planning. Placement now discovers the
  `AUDIO_ENCODER` component and inserts `OmniRouteAudioEncoder`. Because
  Comfy's audio encoder uses `CoreModelPatcher`, it cannot use the diffusion
  deep-clone API. The new route performs an encoder-specific patcher clone
  before first encode, updates the model/load-device references together, and
  fails visibly if cloning is unavailable. Live verification loaded Whisper
  on logical `cuda:1` (the physical RTX 3060), not the primary 3090.

Whisper's checkpoint contains decoder weights that Comfy intentionally does
not consume; live load reported about 1.215 GB despite the roughly 2.9 GB file
and emitted expected missing mel-buffer/unexpected decoder-key warnings. The
planner now applies a 0.42 usable fraction plus headroom rather than charging
the full decoder-inclusive file to VRAM.

Three bounded failure profiles established the memory boundary:

- Prompt `a55cafba-10f1-47a5-937b-b4457b8228b3`, 480x480/49 frames in normal
  mode, OOMed during the first Wan convolution.
- Prompt `05cf34ee-9754-4350-95c8-270372405cfd`, reduced to 384x384/25 frames,
  failed at the same point. Resolution was not the controlling factor.
- Prompt `3324ee08-a020-44ef-8566-c39e25ee46a5` used `vram_mode=low`, but
  Comfy still saw about 22.1 GB usable and fully loaded 16.267 GB of HuMo
  weights. Distilled-LoRA patching again filled the card and OOMed.

The decisive setting is a large startup VRAM reserve, not low-VRAM mode by
itself. On the tested 24 GB primary, the verified start profile is:

```json
{
  "vram_mode": "low",
  "disable_pinned_memory": true,
  "startup_options": {
    "reserve_vram": 10,
    "cache_policy": "ram_8",
    "disable_smart_memory": true,
    "async_offload": "disabled"
  }
}
```

With this profile, HuMo loaded about 12.108 GB, offloaded 4.159 GB, and used
218 low-VRAM patches instead of full-loading. Prompt
`57de08a8-39fa-4512-9554-39608dce3f4a` then completed the 384x384, 25-frame,
one-second audio-driven video in 12:01 cold; its six sampling steps took about
41 seconds. Output `HuMo_ACE_Sing_25f_384_00001_.mp4` is H.264/AAC, 25 fps,
stereo 48 kHz, and exactly 1.000 second. Five-frame visual QA showed stable
identity, clothing, hair, and background with distinct mouth shapes and subtle
head motion.

A parameterized warm run applied all three requested overrides (audio,
length, and filename) with none unmatched. Prompt
`bda0a919-f156-4e5e-ab9a-d01df84867ed` completed 384x384/49 frames in 301.57
seconds; sampling took about 43 seconds. Output
`HuMo_ACE_Sing_49f_384_LowVRAM_00001_.mp4` is H.264/AAC, 49 frames at 25 fps,
stereo 48 kHz, and 1.960 seconds. Visual QA retained the subject and scene
while adding blinking, smiling, head tilt, and mouth articulation.

The startup reserve is global across visible GPUs. A 10 GB reserve therefore
also leaves the 12 GB auxiliary with too little budget for UMT5 or VAE, causing
those components to be mostly offloaded even though placement targets the
3060. This is the remaining optimization gap: Comfy has one global reserve
flag, while Omni's placement policy has per-device reserves only for planning.
Do not claim the auxiliary is fully utilized under this profile.

The planner now blocks HuMo+distilled-LoRA on a 24 GB primary unless the live
instance uses `vram_mode=low` with `startup_options.reserve_vram >= 9`, or
`vram_mode=none`. This converts the repeatedly observed OOM into a preflight
hard stop. The saved workflow metadata records the verified 10 GB recipe.

Resource accounting showed why Linux cache must not be confused with a leak.
During model mmap, `buff/cache` rose to about 11.5 GB while available memory
remained about 16.2 GB and swap stayed near zero; those pages are reclaimable
and accelerate subsequent loads. After generation, `/free` released the large
GPU weights but the `ram_8` node cache left the live Comfy process at roughly
14.4 GB RSS. Kernel cache dropping cannot release that process-owned graph
cache. Use `cache_policy=none` when automatic post-prompt RAM release is more
important than warm-run latency, or restart the exact Comfy instance after a
HuMo batch. The instance was deliberately left running because the user asked
not to stop it.

The external Comfy source was not modified. It also reports numerous
`lora key not loaded` warnings for HuMo image-specific blocks when applying the
official template's LightX2V LoRA; successful output proves the compatible
subset runs, but the unmatched-key count should remain a recorded upstream
compatibility warning rather than being silently hidden.

### BiRefNet and Krea 2 Turbo live API qualification (2026-08-10)

After a cold app restart, the saved-workflow requirements route reported the
BiRefNet, Krea 2 Turbo/RAW, HiDream-O1, Ideogram 4, and Wan Animate 2 graphs
ready with no missing models or live node classes. Each workflow was checked
individually; no broad model scan or inference batch was queued.

The BiRefNet graph uses the local `birefnet.safetensors` loader and a rights-safe
portrait uploaded through the authenticated Comfy image proxy. Prompt
`57b80c43-86ae-4add-b0b0-44a96222a663` completed successfully in 59.76 seconds,
with about 419 MB reported model residency. It produced both
`capability_tests/BiRefNet_portrait_neutral_00002_.png` and
`capability_tests/BiRefNet_portrait_neutral_mask_00002_.png`. This verifies the
saved-run, upload, custom-node, queue/history, and persisted-output paths.

The Krea 2 Turbo INT8 graph contains a 1024x1024, eight-step Euler sample. Its
live placement plan routed the estimated 15,436 MB diffusion component to the
physical RTX 3090 and the estimated 6,518 MB text encoder plus 840 MB VAE to
the physical RTX 3060. During real inference, the 3060 settled near 7.2 GB used
while the 3090 progressively loaded the diffusion checkpoint. Comfy reported
4,999.47 MB for the text encoder and 12,866.82 MB for Krea2. No foreign compute
PID was present during this run.

Prompt `39a2a000-2b1b-4f5b-b513-02c2b47c78d7` completed successfully and
produced `capability_tests/Krea2_Turbo_INT8_00005_.png`. The eight denoising
steps took about 23 seconds; the cold end-to-end run took 476.83 seconds because
the 5 GB and 13.5 GB checkpoint files had to be read and materialized. Visual
QA found a coherent 1024x1024 result with strong face, hand, lantern, glass,
lighting, and background geometry and no obvious INT8 artifact. This is the
first verified real cross-GPU Comfy generation for the current placement
implementation, not merely an analyze-only graph.

A controlled warm 1280x1280 rerun applied `5.width`, `5.height`, and `6.seed`
successfully and had a valid placement plan. Under the current 24 GiB Omni
cgroup limit and concurrent system load, however, the active prompt made the
gateway repeatedly time out/reset and WSL stopped scheduling even exact,
single-process checks promptly. The graceful API stop was unavailable; an
exact signal was sent only to the previously verified managed Comfy PID. The
bridge was already unavailable, and the Omni distro subsequently stopped once
its remaining processes exited. This is evidence of an unsafe host-memory and
gateway-liveness profile, but not by itself proof of a kernel OOM kill. Do not
use 1280x1280 as the default on this configuration until the workflow has a
stricter RAM preflight or a lower-cache/offload startup profile. Keep the
verified default at 1024x1024.

Restarting the existing Omni application restored the dedicated distro and
gateway without touching the running TTS Server or TQ Server distros. After
recovery there were zero Comfy instances and zero standalone workers. The
Omni cgroup held about 118 MB total: about 72 MB RSS, 39 MB reclaimable cache,
and 0.25 MB swap. No large model cache remained to clear.

### Control-plane isolation and Ideogram 4 boundary (2026-08-10)

The Krea 1280 failure exposed that the gateway and model workloads inherited
the same 24 GiB cgroup ceiling. Omni now creates a 21 GiB
`/omni_studio/workloads` child while leaving 3 GiB reserved for the bridge and
gateway in the parent. Every Comfy and standalone model-worker PID is moved
into the child immediately after spawn. A missing cgroup remains a visible
degraded mode; if the child exists but rejects placement, the heavyweight
spawn fails instead of silently running unbounded. Kernel-level verification
placed Comfy PID 128 in the child while gateway PID 64 remained in the parent.

The placement planner no longer treats all VRAM owned by the selected Comfy
PID as reusable. A same-PID allocation may be irreducible CUDA context memory,
a routed deep clone, or another graph's weights. Plans now use current free
VRAM. This would have blocked the unsafe warm Krea 1280 rerun and correctly
changed the normal-VRAM Ideogram plan from valid to blocked: its manually
routed 11,260 MiB component targeted a 3060 with only about 10,337 MiB free.

An explicit low-VRAM instance can now report a bounded CPU-offload deficit.
The plan is valid only when the estimated deficit fits the current,
reclaim-aware workload-cgroup budget. Ideogram 4 reported approximately
1.57 GiB estimated CPU offload against 18.78 GiB available, and its run-time
recalculation reported 852 MiB. Normal-VRAM mode never assumes offload will
rescue an oversized assignment.

Prompt `de2e5a89-55ca-498d-91b0-3c283b1756a9` began the staged Ideogram 4 INT8
typography graph. The 10.1 GB Qwen3-VL encoder loaded and was released from the
3090 before the first Ideogram diffusion component materially loaded on the
3060, proving that the cache-none/staged path reduces simultaneous GPU
residency. The 3060 nevertheless reached about 10.4 GB used during sustained
checkpoint materialization. A no-op 20-second wait was then delayed for more
than a minute by system scheduling pressure. The exact managed Comfy instance
was stopped through the responsive API before sampling. No Ideogram output is
claimed, and this 3090+3060 profile is not qualified under concurrent host I/O.

After the abort, the empty workload cgroup retained about 18.2 GiB of
reclaimable checkpoint page cache with zero RSS and zero swap. Writing only
that empty cgroup's `memory.force_empty` reduced its charge to zero immediately.
Omni now performs this scoped cleanup automatically after the final Comfy or
standalone model worker exits; it never uses a system-wide `drop_caches`.
Live activation was verified with a weight-free Comfy startup aborted during
external I/O wait: the stop path logged `552 MiB -> 0 MiB`, and the workload
cgroup subsequently reported 24 KiB total, 4 KiB cache, zero RSS, and zero
swap.

The gateway's normal `SIGTERM` lifespan deliberately stops workers and Comfy.
For source-only refreshes with no gateway-owned jobs and an empty Comfy queue,
future agents must verify the exact gateway PID command line and use an exact
`SIGKILL`; the bridge watchdog restarts the gateway and it adopts durable
worker/Comfy records. This preservation path was live-verified with Comfy PID
501. Normal application shutdown continues to use graceful cleanup.

### Next bounded image/video profiles (static inspection)

HiDream-O1 Dev is a compact 10-node graph with one 8.07 GB FP8 checkpoint,
768x768 latent, 28 normal-scheduler steps, LCM sampling, CFG 1, and a single
PNG output. `CheckpointLoaderSimple` cold-loads the combined checkpoint before
any routed outputs can move, so multi-GPU routing does not reduce its initial
loader peak. The file fits the 3090 safely; 768x768 is the next bounded live
profile. Do not raise resolution on the first qualification run.

Wan Animate 2's saved smoke graph is already constrained to 480x832, 21 frames,
six LCM steps, 24 fps, one batch, and an INT8 CPU cache. Its installed weights
are approximately 16.65 GB diffusion, 6.74 GB UMT5, 1.26 GB CLIP Vision,
0.74 GB distilled LoRA, and 0.25 GB VAE. Component placement is plausible with
diffusion on the 3090 and UMT5/vision/VAE on the 3060, but the CPU cache and
sustained checkpoint I/O make this riskier than HiDream under the current
concurrent host load. The next live attempt must keep 21 frames, cache-none,
low VRAM, disabled pinned memory, one empty queue, and the 21 GiB workload cap;
do not increase length or resolution until that exact smoke run completes.
