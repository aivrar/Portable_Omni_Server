# Omni Studio comprehensive after-operation report

Date: 2026-08-14  
Campaign period covered: 2026-08-09 through 2026-08-13  
Scope: Omni Studio API, ComfyUI, GPU orchestration, local audio, standalone
multimodal workers, media/UI behavior, persistence, lifecycle, cleanup, tests,
skills, documentation, and repository hygiene.

## 1. Purpose and evidence standard

This is the consolidated handoff for the multi-day Omni Studio capability and
reliability campaign. It is intended to let a future user or agent understand:

- what was requested and why;
- what was implemented in Omni-owned source;
- what was exercised through the authenticated API;
- which local models and workflows produced real media;
- how the RTX 3090 and RTX 3060 were used together;
- which settings were safe and effective;
- which experiments failed and must not be repeated blindly;
- how model, worker, Comfy, RAM, cache, and output state are separated;
- what documentation, fixtures, workflows, reports, and skills now preserve
  that knowledge;
- what remains incomplete or unqualified.

The evidence terms used throughout are:

- **Verified:** a bounded live request completed, its actual result was
  retrieved and inspected or objectively probed, and cleanup was confirmed.
- **Qualified with limits:** the tested path works, but a quality, license,
  scale, environment, or repeatability boundary remains explicit.
- **Blocked:** analysis or a bounded live run established a hard incompatibility
  or capacity limit. The blocker is the correct result and must not be bypassed.
- **Deferred:** discovery or source inspection found a plausible path, but no
  live claim is made.

Installation, process readiness, model residency, queue acceptance, persisted
output, and subjective media quality are separate states. A downloaded file or
a Comfy `prompt_id` is never treated here as proof of a successful capability.

## 2. Executive outcome

The campaign substantially changed Omni Studio from a collection of local
model launchers into an API-driven, resource-aware media system with durable
operating guidance.

Major outcomes:

1. The complete gateway surface was inventoried: 208 registered route entries,
   198 OpenAPI paths, 211 HTTP operations, one WebSocket route, and 74 typed
   schemas at the time of the source audit.
2. Comfy lifecycle, core updates, Manager/custom-node updates, model discovery,
   Xet-backed installation, exact deletion, workflow analysis, queueing,
   outputs, and cleanup were made explicit and documented as separate API
   operations.
3. Comfy models were isolated beneath the distro's canonical tree at
   `/opt/omni_studio/comfyui/models/<category>` and made persistent without
   recreating an obsolete Windows-side model store.
4. Multi-GPU operation was implemented as several distinct mechanisms instead
   of one misleading switch:
   - ordinary Comfy component placement;
   - unload-separated Comfy staged execution;
   - standalone Hugging Face layer/module sharding for compatible models;
   - separate model workers pinned to different GPUs;
   - explicit, bounded CPU offload.
5. Host-memory admission, workload cgroup isolation, current-free-VRAM
   planning, activation warnings, and scoped checkpoint-cache reclamation were
   added after real pressure failures exposed the need.
6. Real local image, video, speech, music, and sound-effect outputs were
   produced and inspected across MiniMax H3, Krea 2, HiDream-O1, Ideogram 4,
   BiRefNet, Wan Animate 2, HuMo, LTX 2.5, ACE-Step, Stable Audio, MOSS, and
   MiniMax Music 3.
7. Standalone Qwen and MiniCPM understanding paths were repaired and verified;
   oversized or unwired model paths now fail explicitly instead of silently
   ignoring modalities or beginning impossible loads.
8. The Media Library was repaired for range-aware video playback and enhanced
   with autoplay browsing, readable JSON/text/manifest display beside media,
   provenance, related-file discovery, richer metadata, and integrity status.
9. App shutdown/restart, gateway adoption, frontend recovery, worker timeout
   retirement, install atomicity, and cleanup races received focused fixes.
10. Project-local skills, canonical API guides, reusable API clients, saved
    workflows, test assets, and dated reports now allow future agents to resume
    without reconstructing the work from chat history.

The system is not universally production-complete. Several paths remain
blocked or deferred: H3 Turbo LoRA on the present Torch/CUDA stack,
InfiniteTalk on a 32 GB WSL guest, LTX 2.3's official stack, Moshi transport,
AnyGPT's current loader/API design, Qwen audio/video/Talker wiring, MiniCPM TTS,
large Qwen3/Nemotron checkpoints, and comprehensive native audio/video
enhancement.

## 3. Hardware and runtime baseline

The tested machine consistently exposed:

| Physical GPU | VRAM | Stable UUID | Typical host CUDA index |
|---|---:|---|---|
| NVIDIA RTX 3090 | 24,576 MiB | `GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a` | `cuda:1` |
| NVIDIA RTX 3060 | 12,288 MiB | `GPU-80c24f57-c604-b55c-5433-5d248304eec9` | `cuda:0` |

The 3060 was also display-attached, so roughly 0.7-1.8 GiB of normal
desktop/driver allocation was often present even when Omni was empty. Current
free VRAM, not nominal total VRAM, became the planner input.

For a primary-first Comfy instance started with `[cuda:1, cuda:0]`:

- physical RTX 3090 became Comfy logical `cuda:0` / `primary`;
- physical RTX 3060 became Comfy logical `cuda:1` / `auxiliary:1`.

Logical CUDA indices are process-local and must never be persisted as physical
identity. Saved policies use GPU UUIDs and resolve them through
`GET /api/devices` after every boot.

The WSL guest normally exposed about 32 GiB RAM and 8 GiB swap. Swap was never
treated as model capacity. Later isolation work placed heavyweight Comfy/model
workloads in a 21 GiB child cgroup under the app's 24 GiB parent, reserving
3 GiB for the gateway and bridge.

## 4. API and control-plane accomplishments

### 4.1 Complete API inventory

The gateway API was enumerated from typed source and FastAPI schema and written
to `docs/api-capability-inventory.md`. It covers:

- session/bootstrap authentication and scoped API keys;
- health, configuration, capabilities, system state, logs, metrics, and
  maintenance;
- devices, jobs, workers, placement analysis, native and OpenAI-compatible
  chat;
- persistent chat sessions;
- Comfy lifecycle, updates, proxy, uploads, previews, nodes, workflows,
  templates, and assets;
- ACE-Step, Stable Audio Lab, MOSS TTS/SFX, MiniMax Music 3, STT, and TTS;
- generic model/LoRA/Hugging Face installation;
- media listing, streaming, metadata, provenance, collections, composition,
  zipping, pruning, and deletion.

The normal client surface is the loopback Windows bridge at
`http://127.0.0.1:9200`. `/api/*`, `/v1/*`, `/metrics`, media query-token
exceptions, and optional OpenAPI exposure were documented precisely. Tokens
were not written into reports, source, fixtures, or examples.

One source-level warning remains: FastAPI emits a duplicate operation ID for
the five-method Comfy proxy decorator. Normal routing works, but strict client
generators may eventually need unique operation IDs.

### 4.2 Job semantics were clarified

The campaign established a durable distinction:

- installation, update, zip, composition, and maintenance operations that
  return `status: running` are polled through `/api/jobs/{job_id}`;
- ACE-Step, Audio Lab, and MiniMax Music 3 inference calls are synchronous even
  though they persist output under job-shaped IDs;
- a client timeout is unknown state, not automatic failure;
- agents query the exact job, worker, instance, or queue before retrying.

This prevents duplicated cold loads and duplicate long-running inference.

### 4.3 App, bridge, and gateway lifecycle

The following were implemented or hardened:

- idempotent broad shutdown cleanup;
- exact bridge process-group handling;
- an explicit gateway restart deadline;
- a bridge startup/reload page instead of a dead embedded browser;
- gateway-only restart recovery in roughly five seconds during live tests;
- API-only full shutdown that closed only the matching `Omni_Studio.exe`;
- bridge proxy timeouts longer than ACE-Step's 1,800-second synchronous window;
- system-browser opening moved off the event loop;
- durable Comfy/worker PID records and validated re-adoption after an abrupt
  source-only gateway refresh;
- preservation of the intended rule that normal graceful gateway shutdown
  stops workers and Comfy.

The special gateway-preservation method is deliberately narrow: verify no
gateway-owned jobs, verify the exact gateway PID command line, then kill only
that PID abruptly so the watchdog can restart and adopt durable records. It is
not the normal app-shutdown method.

### 4.4 Frontend backend recovery

The UI now:

- clears stale credentials after a 401;
- reacquires `/api/session`;
- retries backend recovery at 1, 2, 4, and 8 seconds;
- reloads configuration, jobs, LoRAs, and devices after reconnect.

Source and focused tests passed. A full visual fault-injection test remained
blocked when no controllable browser session was available.

## 5. Comfy lifecycle, updates, assets, and workflows

### 5.1 Core update and Manager update-all are separate

An important early correction was that Comfy core update and Manager's
`update_all` are not the same operation.

The full sequence is:

1. update Comfy core through `/api/comfy/installation/update`;
2. wait for completion and qualification;
3. update Manager/custom nodes through `/api/comfy/extensions/manage` with
   `action: update_all`;
4. wait for restart/readiness;
5. verify core commit, Manager state, and expected live node classes.

Core updates are no longer declared successful from Git completion alone.
Qualification checks exact commit, clean checkout, and template discovery; a
live-runtime qualification additionally checks readiness, `/system_stats`,
`/object_info`, and core node classes.

Core was later updated for the LTX 2.5 launch-day work to recorded commit
`27bca654eb9a70237d93f56a6ea336ab55f8925d` / ComfyUI 0.32.0. Current
runtime commit must be re-read when the app is next started; this report does
not assume the historical commit is still current.

### 5.2 Independent, persistent Comfy storage

Comfy-specific weights now live under:

`/opt/omni_studio/comfyui/models/<category>`

This remains the canonical API path even when persistent backing is implemented
with guarded symlinks beneath `/var/lib/omni_studio`. Models, workflows,
outputs, Comfy inputs, and Comfy user data survived normal application restart.
Windows UNC does not reliably traverse absolute Linux symlinks; the APIs or
inside-distro checks are authoritative.

Direct Comfy model downloads and deletion do not require Comfy to be running.
Manager model and custom-node operations do require a ready instance.

### 5.3 Asset discovery, Xet installation, and exact deletion

The asset system now supports:

- installed-filesystem search;
- configured registry search;
- Manager model search;
- Hugging Face repository/file search;
- Xet-backed exact-file download into a declared Comfy category;
- exact URL installation;
- uploads;
- workflow/template dependency resolution;
- custom-node catalog search and exact inspection;
- immediate exact-file deletion with `recoverable: false`.

Large downloads were submitted one at a time, polled to terminal state, and
verified at their final category/name. Snapshot installs gained stable sibling
staging, partial/empty-file and missing-shard detection, atomic completion
markers, and transactional publish/rollback.

The new `clip_projections` category was added for learned projection matrices;
H3 ClipProj files are no longer misfiled under `clip` or `text_encoders`.

### 5.4 Custom-node installation and rollback

Verified extension work included:

- `ComfyUI-ClipProj` at commit
  `ca9b325e83cb02cb5e652570569c6f3f20fee342`;
- Spectrum MiniMax H3 v0.2.5 through the exact asset-node installer because it
  was absent from Manager's catalog;
- KJNodes with 249 live classes, including the Sage patch node;
- `infinitetalk-native-sampler` 1.1.0 with both AutoSampler classes;
- `ComfyUI-LTXVideo` at recorded commit
  `ac4d99839020b983e956a8ab67ec38aec1b6e65a`, including live low-VRAM,
  guide, and tiled-decode classes.

Negative extension tests were cleaned up:

- LatentSync's install script failed; the snapshot was restored and the exact
  failed extension uninstalled.
- Kijai's Wan wrapper failed required live-node verification; recovery ran and
  the disabled failed install was removed.

Catalog-origin verification now tolerates bounded stale class mappings only
when at least 80% of mapped classes are live, reports every missing name, and
keeps explicit expected-class checks strict. Snapshot recovery no longer
claims success when Manager's own logs report an error.

### 5.5 Workflow and template APIs

The app now has durable patterns for:

- searching saved workflows and installed package templates;
- fetching templates with their `source` and `package` provenance;
- probing requirements and dependencies;
- analyzing API-format graphs without loading weights;
- saving/importing API graphs;
- storing GPU placement separately as workflow metadata;
- parameterized run overrides with an applied/unmatched patch report;
- queueing and verifying output/history.

Official UI/subgraph JSON is inspectable but is not directly queueable. Krea,
Ideogram, HiDream, BiRefNet, Wan, InfiniteTalk, and official LTX examples had
to be materialized as API-format graphs before execution. A generic conversion
route was not invented because embedded subgraphs and frontend widgets require
semantic preservation.

Cold template search originally parsed roughly 600 documents in the event-loop
thread. A size/mtime-invalidated in-memory index and worker-thread rebuild were
added. A later narrow live phrase still timed out in one LTX session, so search
latency remains worth monitoring.

## 6. GPU orchestration and memory-safety architecture

### 6.1 The five placement strategies

The central architectural achievement was keeping these strategies distinct:

| Strategy | Meaning | Verified use |
|---|---|---|
| Single GPU | One complete compatible workload on one card | Audio workers, GPTQ Qwen, MiniCPM, Music 3 |
| Comfy component placement | Independent MODEL/CLIP/VAE/audio-encoder objects routed among visible GPUs | Krea, Wan, HiDream, ordinary graphs |
| Comfy staged execution | Heavy stages load sequentially, return CPU intermediates, and unload before the next stage | H3, Ideogram VAE decode, LTX 2.5 |
| Standalone layer/module sharding | One compatible checkpoint divided by bounded Hugging Face device map | Qwen 3B base live test |
| Parallel workers | Independent engines pinned to different GPUs | ACE/Audio Lab/Comfy scheduling, used sequentially during qualification |

CPU offload is a sixth, explicit resource policy. It is never an implied spare
device and never uses swap as budget.

### 6.2 Comfy placement contract

Comfy start accepts a physical primary and `gpu_pool`. Workflow placement uses:

- `single` for a primary-only graph;
- `auto` for deterministic best-fit among eligible UUIDs;
- `manual` for exact component locks followed by placement of the remainder;
- `require_all` only when every selected GPU must actually receive a component;
- per-device reserve/caps;
- explicit CPU overrides only when `allow_cpu` is true.

The analyzer returns component IDs such as `12:model`, `7:clip`,
`19:video_vae`, or `19:audio_vae`. The server recalculates and transforms a
copy of the exact graph again at run time; stale client plans are not trusted.

Ordinary `OmniRouteModel`, `OmniRouteCLIP`, `OmniRouteVAE`, and
`OmniRouteAudioEncoder` placement may keep components resident together. It
does not shard a single diffusion transformer and does not promise unload
boundaries.

### 6.3 Staged Comfy execution

Staged nodes were built because ordinary routing could not safely fit several
real graphs:

- `OmniH3StageFL2VConditioning`
- `OmniH3ReferenceStrength`
- enhanced `OmniH3StageSampler`
- generic `OmniStageVAEDecode`
- `OmniLTXStageConditioning`
- `OmniLTXStageEmptyAVLatent`
- `OmniLTXStageGuide`
- `OmniLTXStageSampler`
- `OmniLTXStageSpatialRefine`
- `OmniLTXStageDurationPredictor`
- `OmniLTXStageAVDecode`

Stage accounting adds every component that coexists within one stage and then
takes the maximum across unload-separated stages. A graph is allowed to bypass
the aggregate host-mapping gate only when all heavy paths are genuinely staged.

### 6.4 Standalone model sharding

`POST /api/workers/analyze` and the nested spawn placement policy now support:

- stable UUID resolution;
- primary-first visible GPU pools;
- `single`, `auto`, and expert `manual` modes;
- global/per-device reserve and max-memory bounds;
- physical-to-worker-logical CUDA mapping;
- external compute PID warnings;
- bounded CPU targets;
- host/cgroup pressure state.

Qwen 3B base completed a real two-GPU load and exact `OK.` inference with
approximately 4,963 MiB on the 3090 and 5,606 MiB on the 3060. Exact worker
deletion released both cards and all workload cgroup usage.

Qwen GPTQ auto-sharding failed in Accelerate while moving meta-device
quantized weights. The planner now blocks GPTQ multi-GPU before spawn and
directs clients to its verified single-3090 path. Multi-GPU compatibility is
checkpoint-specific, not inferred from nominal model size.

### 6.5 Host-memory admission and cgroup isolation

Several workflows fit both GPUs yet exhausted WSL host mapping, socket buffers,
or control-plane responsiveness. The planner now also enforces:

`host_weight_footprint_mb <= host_model_budget_mb`

for ordinary Comfy graphs. The footprint includes unique LoRAs and model
patches even when they are not independently routable. This converted
InfiniteTalk from a misleading valid plan into a correct hard blocker:
28,312 MiB referenced weights versus roughly 16,680 MiB current host budget.

Low/none VRAM plans can expose an estimated CPU-offload deficit only when it
fits the current child-cgroup allowance. Normal VRAM mode does not assume
offload will rescue an oversized assignment.

The app now keeps heavyweight workloads beneath `/omni_studio/workloads`
while reserving parent headroom for the gateway. Workload placement and scoped
cache release share one lock and check both `tasks` and `cgroup.procs`, closing
a start-versus-release race.

### 6.6 Cache and leak interpretation

The campaign repeatedly distinguished:

- process RSS / anonymous model RAM;
- Comfy graph/node cache;
- CUDA residency;
- Linux file-backed checkpoint cache;
- swap and host pressure.

Reclaimable `buff/cache` is not a live model leak. Global `drop_caches` was not
used. After the final heavyweight workload exits, Omni releases only the empty
workload cgroup's charged page cache through `memory.force_empty`. Process-owned
Comfy `ram_8` graph cache requires `cache_policy=none` or stopping the exact
instance; kernel cache dropping cannot free it.

## 7. Comfy image and video capability results

### 7.1 MiniMax H3 local FL2V

Initial native staged baseline:

- 608x352, 124 frames, 24 fps, 20 steps;
- 5.167-second H.264/AAC output;
- about 23,566 MiB primary peak;
- diffusion unloaded before video/audio decode on the auxiliary GPU.

Boundary ladder:

| Case | Result |
|---|---|
| 768x448, 124 frames, 20 steps | OOM before sampling step 1; about 24,108 MiB primary use |
| Same with 4 GiB reserve and pinned async offload | Unsafe pin-error/control-plane stall; do not repeat |
| 768x448, 73 frames, 30 steps | Passed; 3.042 s; best spatial-detail test but below trained frame range |
| 448x256, 243 frames, 20 steps | Passed; 10.125 s; best balanced long setting from that probe |
| 352x224, 362 frames, 20 steps | Container passed; visible chromatic artifacts; capability ceiling only |

The planner originally estimated only static model size. H3 established the
need for spatial-temporal activation reasoning: resolution and duration
compound token count and dense attention cost.

#### ClipProj and reference strength

The 32B H3 text encoder's roughly 15.7 GB conditioning footprint was reduced by
the verified Qwen3-VL 4B ClipProj path to about 4.88 GB. In the staged graph,
ClipProj ran on the 3060, keyframe VAE encoding ran on the 3090, and both
unloaded before diffusion.

`OmniH3ReferenceStrength` exposes native H3 conditioning values without
patching Comfy core. At fixed seed `424242`, visual strength `0.7` preserved
identity, clothing, background, and final pose while changing expression,
hand angle, and motion compared with `0.999`. Audio changed too even though
audio strength stayed `1.0`.

Qualified constrained reference preset:

- 352x608, 73 frames, 24 fps;
- 12 `res_multistep`/`simple` steps;
- native sampling about 73-74 seconds after load;
- first cold end-to-end run 38:17 due primarily to storage page-in.

#### Spectrum

Spectrum v0.2.5 was integrated as optional/default-off approximation at the
staged sampler seam. Safe first settings were video blend 0.5, audio blend 0,
offline smoothing replay enabled, with history/archive in system RAM.

The fixed-seed run completed in 980.294 seconds and produced close visual
agreement with native output (`SSIM 0.977253`), but its audio differed. No
speedup is claimed because cold I/O varied and safe replay used two passes.

#### H3 Turbo and Sage

The pinned EMA ckpt500 Turbo LoRA, KJNodes, and exact SageAttention wheel were
installed and exposed through the staged sampler. Native 20-step control
passed. Turbo at eight steps with shifts 12/6 OOMed near full 3090 capacity
during INT8 LoRA patch/materialization both without Sage and with the exact
per-model Sage CUDA kernel.

A low-VRAM 4 GiB-reserve retry made gateway/Comfy sockets and WSL process
creation fail from socket-buffer pressure. It was aborted by closing only Omni
Studio. H3 Turbo is **not qualified on the current Torch 2.7.1/CUDA 12.8
runtime** and that interactive configuration must not be repeated.

### 7.2 BiRefNet

`birefnet.safetensors` was installed under `background_removal`, and an
API-format RGBA workflow was saved.

- cold neutral portrait: about 52.8 seconds;
- warm profile: about 1.05 seconds;
- warm full body: about 0.96 seconds;
- 1024x1536 output with full alpha range and good fine-hair retention.

The foreground-mask inversion before `JoinImageWithAlpha` is intentional.
Upload, saved workflow, queue/history, mask output, alpha metadata, and media
retrieval paths were verified.

### 7.3 Krea 2 OSS Turbo and Raw

Installed assets included Turbo/Raw INT8 ConvRot diffusion models, Qwen3-VL 4B
FP8 encoder, Qwen image VAE, and style LoRAs.

Turbo routing placed the diffusion model on the 3090 and text/VAE on the 3060.
Cold runs were dominated by checkpoint I/O; a clean cold 1024 run took roughly
8-9 minutes while a warm new-seed run took 23.080 seconds.

Omni fixed two CLIP routing defects:

- `cond_stage_model` now follows the routed patcher model;
- cached factory loading no longer leaves a dead temporary patcher entry.

Device telemetry TTL was reduced from 30 seconds to two seconds so placement
and cleanup checks do not rely on stale VRAM.

Qualified Turbo results:

| Resolution | Result |
|---|---|
| 1024x1024 | Dependable baseline, coherent, no obvious INT8 artifact |
| 1536x1024 | Best balanced quality result; 418.604 s in the frontier run |
| 2048x2048 | Technically strong and detailed, but duplicated a lantern; frontier semantic-risk mode |

The style-reference graph transferred palette, paper texture, ink, and
geometric abstraction successfully at 1024. A 1536-square run created pin
errors and control-plane pressure; it is not the default.

Raw's first graph used Turbo's fixed shift and zeroed conditioning and produced
chromatic noise. The corrected 1024 recipe uses resolution shift `0.90625` and
an independently encoded empty unconditional branch. Both 16- and 52-step
runs became coherent, but retained an inset-frame composition. Raw is retained
for post-training research; Turbo is the routine inference recommendation. A
semantic analyzer guard now marks known-bad Raw graphs `needs-review`.

### 7.4 HiDream-O1-Image

Dev FP8 and Gemma4 E4B FP8 assets were installed through sequential Xet jobs.

Qualified Dev behavior:

- 768 square / 28 steps: useful cold smoke, 155.38 seconds;
- warm 1024 square / 28 steps: practical T2I quality default, 11.3 seconds;
- naive 1024 reference edit: destructive noise;
- official semantic four-megapixel 2048-square edit: passed in 75.6 seconds
  with strong identity and edit preservation;
- 2048 multi-reference: good style/identity family, but two supplied views
  were interpreted as two people rather than automatic multi-view fusion.

Four megapixels is a semantic requirement of the native edit path, not merely
an optional upscale.

### 7.5 Ideogram 4

Both conditional and unconditional INT8 diffusion models, Qwen3-VL 8B FP8,
and Flux2 VAE were installed. Ordinary two-GPU placement fit sampling only by
putting VAE on CPU, but CPU decode ran for more than ten minutes and ignored
interrupt until exact-instance stop.

`OmniStageVAEDecode` resolved the problem by unloading the dual diffusion stage
before GPU VAE decode.

- 1024 staged: completed in 609.664 seconds; primary phrases present, but
  rotated layout and filler misspellings;
- native 2048 staged: completed in 944.141 seconds; all four requested strings
  legible and filler removed, but requested horizontal layout still not obeyed.

Use 1024 for smoke/concepts and 2048 where spelling fidelity justifies the
time. The model's non-commercial license remains a deployment constraint.

### 7.6 Wan Animate 2

The official INT8 diffusion, UMT5 FP8, CLIP Vision H, Wan VAE, and LightX2V
LoRA were installed. The API graph used CPU INT8 Wan cache, six LCM/simple
steps, CFG 1, and 24 fps.

Results:

- 480x832, 21 frames, mismatched fox driver: passed technically but transferred
  crouched quadruped motion to the human; retained as a negative control;
- 480x832, 21 frames, matched human-wave driver: qualified in 88.19 seconds
  warm with stable identity, clothing, background, and hand motion;
- 480x832, 49 frames: OOM during sampling despite valid static placement;
- 384x640, 49 frames: passed in 510.390 seconds as 2.0417-second H.264 video.

Recommended tiers are 480x832/21 frames for detail and 384x640/49 frames for
duration. `/free` did not reliably unload routed Wan models; exact-instance
stop is the dependable hard-release boundary.

### 7.7 HuMo audio-driven portrait

HuMo 17B FP8, Whisper Large V3 FP16, UMT5, Wan VAE, and LightX2V LoRA were
resolved. Omni added `audio_encoders` classification and
`OmniRouteAudioEncoder`; Whisper was verified on the 3060.

Normal mode, low mode without reserve, and resolution reduction all OOMed
during LoRA/first-convolution materialization. The verified startup profile is:

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

Qualified outputs:

- 384x384, 25 frames, 1.0 second: 12:01 cold;
- 384x384, 49 frames, 1.96 seconds: 301.57 seconds warm.

Both retained identity and showed visible mouth/head motion using an ACE song.
The 10 GiB reserve is global and also constrains the 12 GiB auxiliary, so this
profile does not fully exploit the 3060. `ram_8` retained about 14.4 GiB
process RSS; use cache-none or stop the instance after a HuMo batch when
automatic RAM release matters.

### 7.8 InfiniteTalk

Native sampler classes and the official single-speaker model stack were
installed. The first attempt exposed that `length=81` is a per-pass chunk, not
an output cap. A 12.95-second TTS file expanded to about 324 frames and five
diffusion passes.

The skill now requires:

`total_frames = ceil(audio_duration_s * framerate)`  
`extend_frames = length - motion_frame_count`  
`passes = 1 + max(0, ceil((total_frames - length) / extend_frames))`

A corrected 3.0-second, 75-frame, one-pass fixture still exhausted the 32 GiB
guest while both GPUs were populated. After the host-footprint fix, live
analysis blocks the graph before queue. No InfiniteTalk output is claimed on
this host.

### 7.9 LTX 2.3

The official FP8 checkpoint plus Gemma encoder total roughly 38.98 GB before
activation/offload and exceed both GPU and WSL budgets. Kijai INT8 and Unsloth
GGUF candidates were discovered but are not drop-in replacements without a
compatible split-model wrapper and the full remaining stack. No speculative
download or false runnable claim was made.

### 7.10 LTX 2.5

LTX 2.5 received the deepest staged-video qualification.

Installed or inspected assets included official INT8 ConvRot Gemma,
distilled/dev transformers, video/audio VAEs, spatial/temporal upscalers,
duration head, distilled LoRA, the fast convolutional VAE, community W4A8
transformers, and official UI examples. Gated repositories were respected;
mirrors were not used to bypass access control.

Ordinary graphs exceeded host mapping. Upstream save/load conditioning dropped
important options and produced colored noise. Omni's lossless staged path
preserved full conditioning and produced coherent synchronized video/audio.

Qualified results included:

- 512x320, 49 frames, 24 fps, W4A8 distilled, CFG 1, official eight-sigma
  schedule: coherent 2.04-second H.264 plus 48 kHz AAC;
- two-stage spatial x2 and three-step dev refinement to 1024x640: coherent and
  sharper with minor hand deformation;
- duration predictor: raw 3.767 seconds snapped to 89 valid frames;
- 512x320, 121-frame prompt-driven three-shot clip: 5.0417 seconds with three
  ordered compositions and synchronized quiet ambience;
- 1024x640, 121-frame two-stage recovery run after power loss: completed in
  38:29;
- Winnougan lower-residency W4A8 Gemma/distilled pair: coherent 512x320 output;
- hybrid low-VRAM first pass plus tsolful dev refinement: coherent 1024x640,
  121 frames in 26:27, saved as a qualified graph;
- staged I2V 320x512/49 frames: identity-preserving wave with synchronized
  audio;
- staged first/last-frame 320x512/49 frames: successful neutral-to-raised-hand
  transition with two independent guide VAE stages.

Decoder A/B on the same cached 1024x640x121 latent:

| Decoder/profile | Device | Time/result |
|---|---|---|
| Diffusion VAE, temporal 64 | RTX 3060 | 157.42 s |
| Conv VAE, temporal 64 | RTX 3060 | 73.50 s |
| Diffusion VAE, temporal 2048 | RTX 3060 | OOM |
| Diffusion VAE, temporal 2048 | RTX 3090 | 129.51 s |
| Conv VAE, temporal 2048 | RTX 3090 | 61.98 s |
| Conv VAE, temporal 64 | RTX 3090 | 60.58 s |

Fast policy: convolutional VAE, spatial tile 512, temporal tile 64 on the
largest freed GPU after transformer unload. Fidelity policy: diffusion VAE,
temporal 64, with higher time and memory cost. Temporal 2048 is not a safe
default.

Remaining LTX gaps:

- official DFR is a separate pipeline, not a sampler flag;
- the pixel-spatial IC-LoRA remained separately gated;
- the official Python `res_2s` HQ sampler was not exposed by live nodes;
- prompt enhancer loaders bypass managed inventory and remain unqualified;
- NVFP4 was not promoted on Ampere while W4A8 was natively recognized.

## 8. Local audio capability results

### 8.1 ACE-Step

Verified baseline configuration:

- `ace-1.5` 2B core;
- `ace-lm-0.6b`;
- BF16, no CPU offload, no int8, no compile;
- eight Euler steps, CFG 1, overlapped decode;
- 48 kHz stereo PCM-16 output.

Six initial 30-second original songs spanning minimal techno, synthpop,
psychedelic downtempo, ambient, hard techno, and psychedelic vocals completed
and persisted.

Additional qualification:

- Euler, Heun, and DPM++ scheduler passes;
- 2B SFT and Turbo Continuous cores;
- ScragVAE;
- 60- and 120-second songs;
- one direct 240-second DPM++ instrumental;
- ACE base-model completion to 45 seconds;
- cover, repaint, audio-to-audio, lyric edit, extend, and vocal-to-BGM;
- ACE XL Turbo plus 1.7B LM at roughly 19 GB VRAM, with faster warm generation
  but weaker tempo adherence in the tested seed.

The 240-second instrumental completed in 113.984 seconds but measured roughly
104 BPM against requested 126 BPM. Long direct generation is therefore not
automatically coherent or tempo-faithful; composition is the dependable path
for longer arrangements.

#### ACE LoRAs

Verified attach/generate/detach paths include:

- raspy vocal/instrumental pack and its selectable internal files;
- acoustic fingerstyle;
- Chinese New Year;
- lofi;
- raga.

The API now supports an exact `adapter_file` for multi-file packs and reports
the selected file. Final-adapter detach recovery was fixed after upstream PEFT
left a broken empty wrapper and stale active name. The approximately 3 GB CPU
decoder backup is now released after detach.

Known adapter blockers:

- official Lyric2Vocal and Text2Samples weights are unreleased and now fail
  fast rather than starting false installs;
- one community synthpop package lacked required PEFT configuration;
- the legacy Chinese Rap package is not PEFT-compatible;
- the 4B LM with the 2B core exceeded practical 24 GB residency.

ACE completion and extension are generative. The nominally retained source
changed materially; use composition when preservation matters.

### 8.2 Long-form composition

A fundamental native route, `POST /api/outputs/audio/compose`, was added. It
works without ComfyUI and supports:

- 2-64 exact media-library audio segments;
- trim, gain, labels;
- common resampling and stereo conversion;
- triangular crossfades;
- optional one-pass EBU loudness normalization;
- target LUFS and true peak;
- 16/24-bit PCM WAV;
- 1-4 bounded CPU threads;
- cancellable job execution;
- persisted manifest and media-library visibility.

Verified outputs include:

- 60 s + 120 s with an 8 s crossfade = exactly 172.0 seconds, 48 kHz PCM-24;
- four 60-second compatible A/B/A/B sections with three 8-second crossfades =
  exactly 216.0 seconds, 48 kHz PCM-24 with loudness/true-peak normalization;
- a 60-second TTS-slice composition gate;
- a 3.0-second InfiniteTalk fixture through a persistence symlink.

### 8.3 Stable Audio Lab

Stable Audio Open Small was fixed to accept the official eight-step recipe.
Open Small is qualified for short effects/music up to 11 seconds.

Stable Audio Open 1.0 is qualified up to 47 seconds for:

- text generation;
- sequential ranked candidate generation;
- CLAP scoring;
- audio-to-audio;
- regenerate-plus-splice inpainting;
- unconditional generation;
- VAE encode/decode/reconstruct.

The following Omni defects were fixed:

- official step-count validation;
- float32 input versus fp16 VAE mismatches for A2A/inpaint;
- the same dtype mismatch in VAE-only operations;
- ranked fanout incorrectly decoding a four-item batch on a 12 GB GPU instead
  of generating candidates sequentially.

Community variants that cold-loaded, generated persisted WAVs, and cleaned up:

- SAO Instrumental;
- Audialab EDM;
- Nekochu Music;
- Infinite Pianos;
- Vocal Textures.

Tuned-100k VAE support was repaired by applying its declared stock Stable Audio
2.0 autoencoder profile and native wrapper contracts. Generation,
reconstruction, unconditional generation, and music-CLAP scoring passed.

### 8.4 Sound effects

MOSS-SoundEffect v2.0 and Stable Audio were tested as independent engines for:

- a dry wooden door slam;
- night-forest ambience;
- one pneumatic-press/mechanical cycle.

MOSS produced exact-duration 48 kHz mono PCM-16 files; Stable Audio produced
44.1 kHz stereo outputs. Both exposed persisted paths and metadata through the
general Media API. MOSS workers were explicitly deleted after each family.

### 8.5 MOSS-TTS

Historically verified MOSS outputs include 48 kHz stereo speech and a
12.945-second file successfully consumed by ACE Vocal-to-BGM. The loader was
fixed so the Mistral regex repair reaches only the text tokenizer and does not
leak into the audio codec. Non-default `speed` now uses a bounded,
pitch-preserving FFmpeg `atempo` chain.

The TTS response gained deterministic persisted-output headers, and ACE source
URLs were extended to accept safe general output-library audio paths without a
large base64 handoff.

However, a later fresh run failed in the upstream decoder with
`CUDA driver error: unknown error` and exhausted 1,024 WSL `dxgresource` file
descriptors. Omni now retires a TTS worker after any worker-side 5xx so that a
poisoned process is never returned to the ready pool. Until a new bounded fresh
pass succeeds, MOSS-TTS should be described as historically verified but not
currently dependable for repeated cold inference on this runtime.

### 8.6 MiniMax Music 3

MiniMax Music 3 was integrated as its own standalone audio worker, not as a
Comfy or ACE checkpoint.

Implementation characteristics:

- official Diffusers modular pipeline;
- pinned model/runtime revisions;
- selective roughly 28 GiB Diffusers component download instead of the full
  roughly 53.4 GiB dual-runtime repository;
- BF16 plus bounded dynamic CPU offload on the 3090;
- direct-to-storage 44.1 kHz stereo PCM-24 WAV;
- no base64 gateway hop for large multi-minute audio;
- status, state, load, generate, cancel, unload, jobs, outputs, and existing
  setup/search integration;
- conservative compatibility labels for community variants and LoRAs.

The first live generation exposed that the official model index declares slow
`Qwen2Tokenizer` while the snapshot ships only fast tokenizer files. Diffusers
left the tokenizer `None`. Omni now registers local `Qwen2TokenizerFast` before
component loading; focused tests cover this compatibility requirement.

Qualified technical run:

- physical RTX 3090 / worker-local `cuda:0`;
- BF16 with a 16 GB bounded CPU-offload plan;
- corrected warm-cache load: 473 seconds;
- 60-second psychedelic minimal-techno vocal request, seed `130826`;
- job `e848e4d257194d75ab4a81732b7e018c`;
- 718.273 seconds generation;
- 60.0700 seconds, stereo, 44.1 kHz, PCM-24, 15,894,572 bytes;
- manifest, integrity, media listing, and byte-range playback verified;
- unload and exact worker deletion returned the 3090 to 24,326 MiB free.

Subjective listening found that this song approached a promising sound but
lost musical quality/coherence. That does not invalidate the API or media
pipeline, but model-parameter/prompt/lyrics tuning is still needed before
claiming satisfying music quality.

Community scan found W4A8 as the most promising NVIDIA optimization lead.
MLX is Apple-oriented; GGUF would require component-aware loading; AOTI was for
different hardware; mirrors add no capability; no usable Music 3 LoRA was
found. Only the official pinned layout is currently loadable.

## 9. Standalone multimodal worker results

### 9.1 Qwen2.5-Omni 3B

- Text on RTX 3060: exact deterministic response passed, but the worker used
  essentially all 12 GB VRAM.
- Image on RTX 3060: failed from capacity/device readiness.
- Image on RTX 3090: accurately described the rights-safe portrait in 18.1
  seconds and cleaned up.
- Two-GPU base load/inference: exact `OK.` response passed under bounded auto
  sharding and cleaned both GPUs.
- Audio/video input: explicitly 501; current worker does not wire them.
- Native speech/Talker: explicitly unavailable through current TTS routing.

### 9.2 Qwen2.5-Omni 7B GPTQ-Int4

The atomic override installer was repaired to build a compatible GPTQModel
4.2.5 environment without replacing shared Torch/Transformers. Missing runtime
imports and the nested Qwen block path were handled in Omni-owned code.

Single-3090 live results:

- all four shards loaded with GPTQModel Triton v2;
- about 12,701 MiB VRAM;
- exact deterministic text passed;
- image description passed;
- exact worker cleanup passed.

Auto multi-GPU remains intentionally blocked after the meta-device dispatch
failure.

### 9.3 MiniCPM-o 2.6

Installer/runtime repairs removed shadow CUDA packages, staged trusted local
Python files correctly, and restored the removed Whisper attention registry
compatibly.

BF16 on the RTX 3090 used about 17,889 MiB and passed:

- deterministic text;
- accurate image understanding;
- accurate transcription/understanding of local 16 kHz speech.

Video is explicitly rejected. Combined image+audio is rejected rather than
silently mishandled. Native TTS reaches the decoder but remains blocked because
the current upstream compatibility chain passes a Python list where the audio
decoder expects an object with `get_mask_sizes()`.

### 9.4 Moshi

Moshiko weights and loader exist, but the batch handler is a hard 501 and the
stream/session architecture has no full-duplex audio transport. Loading a 16 GB
model would not make the API usable, so a pointless cold load was deliberately
avoided.

### 9.5 AnyGPT

The fast-tokenizer protobuf failure was avoided with a scoped slow tokenizer.
The corrected worker then remained CPU-side for more than 13 minutes, grew to
roughly 16 GB RAM, outlived bridge reachability, and never became ready. The
exact worker was killed and resources recovered.

Current worker source is text-only and ignores image/music/audio inputs.
AnyGPT remains blocked pending a bounded/asynchronous loader and explicit
modality handlers.

### 9.6 Qwen3-Omni and Nemotron

Variant-specific capacity checks now run before heavy import or deserialization.

- installed Qwen3 Instruct: about 61.4 GB requirement versus roughly 32.3 GB
  combined safe GPU budget; hard stop;
- installed Nemotron BF16: about 63.5 GB; hard stop;
- Nemotron FP8: borderline and may need a small explicit CPU spill;
- Nemotron NVFP4: analysis fits the 3090 alone but remains uninstalled or
  live-unqualified in the recorded campaign.

Audio/video fields on the shared Qwen/Nemotron path return explicit 501 rather
than disappearing silently.

## 10. Cross-tool workflow results

Verified chains:

- MOSS-TTS output -> general Media API -> ACE Vocal-to-BGM;
- ACE-generated singing -> HuMo audio-driven portrait video;
- generated/uploaded rights-safe images -> H3 first/last-frame video;
- generated/uploaded images -> LTX 2.5 I2V and first/last-frame guidance;
- ACE sections -> native long-form audio composition;
- Comfy output -> media metadata/provenance/range playback.

Blocked chain:

- MOSS-TTS -> InfiniteTalk reached a corrected one-pass API graph, but the full
  stack exceeded host mapping capacity and produced no output.

These chains demonstrate why successful source generation does not imply a
successful downstream workflow. Each downstream graph requires its own
dependency and placement analysis.

## 11. Media Library and output-system improvements

The original symptom was that generated MP4 files appeared as static media and
would not play reliably in the app.

The output and bridge paths now preserve HTTP byte ranges and expose
`Accept-Ranges`, `Content-Range`, correct MIME types, and bounded streaming.
Video playback was verified in the Media Library.

The Media Library now provides:

- saved autoplay-while-browsing preference for audio/video;
- video transport controls, speed, loop, picture-in-picture, fullscreen, and
  frame stepping;
- playable media on the left and readable information beside it on the right;
- bounded readable previews for JSON, text, Markdown, logs, CSV, YAML, and
  TOML;
- parsed manifest display;
- media facts and integrity status;
- sibling model/parameter provenance;
- related playable siblings;
- tags, pinning, notes, collections, search, and filters.

Artifact probes now check PNG completion, WAV structure, ffprobe errors, and
expected audio/video streams. Five recent Comfy PNGs passed a live integrity
pass. Structural integrity does not replace visual or listening review.

## 12. Reliability and resource defects fixed

The most important Omni-owned fixes across the campaign were:

1. Workflow-search indexing moved off the event loop with caching.
2. Output metadata gained bounded dimensions, alpha/channels, duration, codec,
   stream facts, readable documents, related media, and provenance.
3. Media streaming and bridge forwarding gained proper Range handling.
4. Gateway restart can adopt validated durable Comfy/worker PID records.
5. Core update success now requires checkout/template/live qualification.
6. Extension verification and snapshot recovery became evidence-based.
7. Comfy CLIP routing now aligns the true encoding model and avoids dead
   temporary patchers.
8. Same-device model routing became a pass-through instead of a redundant
   clone.
9. LoRA/model-patch transform nodes are no longer mistaken for independent
   diffusion loaders.
10. Audio encoders are discovered, categorized, and routed separately.
11. Warm placement no longer blindly double-counts or blindly reuses VRAM;
    current ownership and current free memory govern analysis.
12. GPU telemetry cache staleness was reduced.
13. Krea Raw invalid conditioning/schedule semantics gained analyzer warnings.
14. Staged VAE decode, H3, and LTX paths gained correct stage accounting.
15. Workflow readiness now becomes false whenever placement is invalid.
16. Host mapped-weight footprint became a hard admission gate.
17. Heavyweight workers were isolated in a child cgroup with gateway reserve.
18. Scoped workload-cache release and process-placement operations became
    race-safe.
19. ACE/Audio Lab busy state and exact timeout retirement were implemented.
20. TTS worker 5xx now retires the poisoned exact worker.
21. Stable Audio dtype, ranking, VAE, and community-loader defects were fixed.
22. ACE staging, LoRA detach, multi-file adapters, source conditioning, and
    cross-output URL handoff were fixed.
23. MOSS tokenizer/speed/output discovery behavior was fixed.
24. Standalone installers/loaders gained scoped compatibility repairs and
    early capacity checks.
25. Snapshot installs became atomic and corruption-aware.
26. Full shutdown became idempotent; frontend recovery became bounded and
    credential-aware.
27. Persistent `/var/lib/omni_studio` state became fast-start safe and
    symlink-safe for outputs/composition.

No Comfy core or third-party extension source was patched to hide upstream
behavior. Omni-owned bridge nodes and gateway compatibility layers were used.

## 13. Tests and verification record

The repository currently contains 51 test modules. Campaign test counts below
are historical suite results and overlap; they must not be summed as unique
tests.

Recorded focused passes include:

- 111 focused tests plus three environment-dependent skips during tree
  hygiene/startup verification;
- Comfy placement 18, multi-GPU 13, workflow requirements 11;
- Comfy extensions 16, discovery/search 12, startup options 8;
- MOSS-SFX output contract 4 and GPU/runtime policy 3;
- standalone Omni placement/router/worker integration 27;
- workload cgroup isolation/cache cleanup 5;
- 49 combined Audio Lab/Comfy placement/workflow tests after library expansion;
- 63 H3/placement/workflow tests plus 8 CLI tests during H3 Turbo work;
- 56 then 58 focused LTX staged-guide/sampler/placement tests as guide and
  crop behavior were corrected;
- 59 combined Comfy/audio contract tests during frontier continuation;
- 63 placement/workflow/output/composition/TTS lifecycle tests plus five
  startup-layout tests during host-capacity/persistence verification;
- focused MiniMax Music 3 contract tests: 6 passed after the tokenizer fix.

Verification methods included:

- Python compilation for modified modules;
- `node --check` for changed JavaScript;
- typed request-model tests;
- synthetic analyze-only graphs with empty-queue confirmation;
- live output retrieval and metadata probing;
- visual contact sheets and sampled video frames;
- audio format/duration/loudness analysis;
- exact worker/instance cleanup and device-memory rechecks;
- skill validation with `quick_validate.py`.

Important warning: do not run `test_comfy_recovery.py` inside the interactive
agent command host. Its process-isolation behavior closed the host stdout
stream and repeatedly interrupted the chat. Run it only in isolated CI or a
disposable test process.

## 14. Skills, documentation, clients, workflows, and fixtures

Project-local skills now include:

- `omni-capability-audit`
- `omni-gpu-orchestration`
- `omni-comfy-api`
- `omni-comfy-testing`
- `omni-audio-api`
- `omni-model-api`

Their purpose is to ensure future agents read the correct domain contract and
do not confuse Comfy routing, standalone sharding, audio worker placement, or
cleanup rules.

Canonical guides:

- `docs/api-capability-inventory.md`
- `docs/capability-confidence.md`
- `docs/comfy-api.md`
- `docs/audio-api.md`
- `docs/omni-model-api.md`
- `docs/repository-layout.md`

Token-safe clients:

- `skills/omni-comfy-api/scripts/omni_comfy_api.py`
- `skills/omni-audio-api/scripts/omni_audio_api.py`

Reusable assets beneath `test_assets/comfy-capability/` include neutral,
profile, full-body, style, first/last-frame, speech, singing, driving-video,
contact-sheet, API graph, placement-policy, install/uninstall, and audio
composition fixtures.

Saved, qualified, or diagnostic workflows include BiRefNet, Krea Raw/Turbo and
style reference, HiDream T2I/edit/multi-reference, staged Ideogram, Wan
Animate, HuMo, InfiniteTalk preflight, staged H3 strength/Spectrum/Turbo, and
multiple staged LTX 2.5 T2V, multishot, two-stage, fast-decode, I2V, and
first/last-frame graphs.

## 15. Repository and runtime hygiene

The tree audit removed obsolete or distracting material while preserving
models, outputs, workflows, credentials, active runtime records, fixtures, and
durable reports.

Recoverable Windows Recycle Bin removals included:

- a generated browser/profile/cache tree including a roughly 4.27 GB Chrome
  on-device model cache;
- obsolete Windows-side `storage/`, including a roughly 12.1 GiB duplicate
  Stable Audio snapshot;
- Playwright snapshots, superseded browser harnesses, one-off probes, terminal
  captures, old audit bundles, and typo artifacts.

Immediate distro cleanup included:

- 444 abandoned immediate temp children;
- obsolete runtime/repos trees;
- a verified stale MiniCPM quarantine;
- one-off H3 probe images;
- a stale disabled Omni bridge duplicate.

Source cleanup removed an unreferenced legacy audio-score implementation,
35 unused imports, two abandoned locals, and a stale annotation. Daily
`prune-tmp` maintenance was added with a two-day threshold and safe symlink
behavior. Setup now synchronizes `AGENTS.md`, `docs/`, and `skills/` into the
distro.

## 16. Known limitations and future enhancement work

### 16.1 Current hard or practical blockers

- H3 Turbo LoRA inference on the current Torch/CUDA stack.
- H3 768x448 at 124 frames on 24 GB.
- H3 pinned async offload with 4 GiB reserve on this WSL environment.
- Wan 480x832 at 49 frames.
- InfiniteTalk's ordinary full stack on the 32 GB guest.
- official LTX 2.3 full stack.
- LTX 2.5 diffusion VAE temporal 2048 on the 12 GB GPU.
- Moshi full-duplex transport.
- AnyGPT's unbounded cold load and absent multimodal handlers.
- Qwen audio/video/Talker wiring.
- MiniCPM native TTS decoder compatibility.
- installed Qwen3/Nemotron BF16 capacity.
- MOSS-TTS fresh-inference stability after the descriptor/CUDA failure.

### 16.2 Native enhancement gap

Omni has a native audio composer with trim, gain, resampling, crossfade,
loudness normalization, and true-peak control, but no simple single-clip
mastering/restoration route.

Missing native operations include:

- single-file normalize/boost;
- compressor, limiter, EQ, high/low-pass;
- denoise, dereverb, declip, de-ess;
- stem separation, pitch correction, time stretch;
- extract/replace/remux video audio;
- conventional video scale, sharpen, denoise, stabilize, and color finishing.

Comfy Manager discovery found promising AI candidates such as Egregora Audio
Super-Resolution, ClearVoice/Resemble Enhance, SeedVR2, Real-ESRGAN,
RIFE/FILM, and Wavelet Color Fix. They were not installed or qualified and
must not be advertised as present capabilities. The recommended future design
is native Omni/FFmpeg for dependable everyday processing and tested API-format
Comfy workflows for learned GPU enhancement. Detailed recommendations are in
`reports/omni_future_enhacers.md`.

## 17. Safe operating rules for the next agent

1. Start with the relevant project skill and typed route models.
2. Use the normal loopback bridge and never print the token.
3. Read `/api/devices`, `/api/workers`, and `/api/comfy/instances` separately.
4. Resolve GPU UUIDs after every boot; never assume `cuda:N` identity.
5. Distinguish asset-installed, process-ready, model-loaded, request-accepted,
   and output-verified states.
6. Analyze without weights before every large worker spawn or Comfy run.
7. Stop immediately on `valid: false`, `ready_to_spawn: false`,
   `ready_to_run: false`, host-mapping blocker, license blocker, or pressure
   blocker.
8. Prefer one GPU when the workload fits. `require_all=true` is a constraint,
   not an optimization.
9. Never describe ordinary Comfy component routing as single-model sharding.
10. Use staged nodes when unload boundaries are required.
11. Keep ACE, Audio Lab, MOSS, Music 3, standalone workers, and Comfy as
    separate resource families.
12. Run one cold load/download/inference at a time. Avoid broad process or
    filesystem scans while a model is active.
13. After any timeout, inspect exact state before retrying.
14. Treat Comfy `/free` as best effort; stop the exact instance for a hard
    release when material residency remains.
15. Delete the exact worker after audio/standalone testing when residency is
    unwanted.
16. Do not globally drop caches. Use scoped workload cleanup only after the
    final workload exits.
17. Do not touch another distro. Final cache release targets only
    `linbox-Omni_Studio` after state is synced and empty.
18. Verify every media result through history, file retrieval, metadata,
    integrity, and actual visual/listening inspection.
19. Keep model deletion exact; it is immediate and unrecoverable.
20. Do not rerun known unsafe H3 offload or interactive recovery-test cases.

## 18. Current state at report time

Historical campaign teardowns repeatedly ended with:

- zero Omni model/audio workers;
- zero Comfy instances;
- empty queues;
- no Omni compute PIDs;
- RTX 3090 near 24,326 MiB free;
- RTX 3060 near its normal display-adjusted baseline;
- persisted outputs/workflows/models retained;
- no global cache drop;
- only the Omni distro terminated when final page-cache release was required.

On 2026-08-14, while preparing this report, a lightweight authenticated
`GET /api/devices` check could not connect because the Windows bridge/app was
not running. No process was started merely to populate this document. The
historical cleanup state is therefore the last verified runtime state; the
next agent must start the app and perform fresh cheap state reads before any
operation.

## 19. Detailed source reports

This consolidated report summarizes rather than replaces the detailed ledgers:

- `reports/minimax-h3-capability-probe-2026-08-09.md`
- `reports/comfy-full-capability-findings-2026-08-09.md`
- `reports/audio-music-capability-findings-2026-08-10.md`
- `reports/omni-app-followup-audit-2026-08-10.md`
- `reports/tree-hygiene-audit-2026-08-10.md`
- `reports/api-review-2026-08-11.md`
- `reports/library-expansion-findings-2026-08-11.md`
- `reports/comfy-frontier-capability-findings-2026-08-11.md`
- `reports/minimax-h3-turbo-qualification-2026-08-12.md`
- `reports/2026-08-13-minimax-music3-integration.md`
- `reports/omni_future_enhacers.md`

Those files retain exact prompt IDs, output paths, model revisions, durations,
memory readings, source diagnoses, and negative controls that would make this
single handoff unnecessarily unwieldy.

## 20. Final assessment

The work achieved the user's central goals:

- Omni is broadly operable by API rather than requiring manual UI interaction.
- Comfy can use a dynamic detected GPU pool through safe component routing and,
  where implemented, real unload-separated staged execution.
- Compatible standalone base models can use bounded multi-GPU device maps.
- Audio generation, sound effects, TTS handoff, long-form composition, and
  MiniMax Music 3 have explicit model lifecycle and persisted-media APIs.
- Media playback and adjacent provenance are usable in the application UI.
- Future agents have skills, guides, clients, fixtures, workflows, confidence
  grades, detailed findings, and safety rules.
- Known failures are preserved as blockers rather than hidden or repeatedly
  retried.

The remaining work is primarily quality expansion and selective integration,
not reconstruction of the control plane. Future development should preserve
the existing separation between engine types, build on the analyze-first
contracts, and add new capability only after a real output and cleanup pass.
