# Omni Studio ComfyUI guide

This is the canonical guide for people and agents operating Omni Studio's
ComfyUI subsystem. It describes the existing API; it does not require direct
changes to ComfyUI core.

For the current verification grade and app-wide rollout order, see
[`capability-confidence.md`](capability-confidence.md).

## Contents

1. [Core ideas](#core-ideas)
2. [Authentication and discovery](#authentication-and-discovery)
3. [Safe operating sequence](#safe-operating-sequence)
4. [GPU placement policy](#gpu-placement-policy)
5. [Workflow preparation and execution](#workflow-preparation-and-execution)
6. [Models and Xet downloads](#models-and-xet-downloads)
7. [Custom nodes and updates](#custom-nodes-and-updates)
8. [Templates](#templates)
9. [User interface](#user-interface)
10. [Troubleshooting](#troubleshooting)

## Core ideas

- Omni owns the stable API, GPU planner, routing nodes, and staged H3 nodes.
  ComfyUI and custom-node updates do not replace those contracts.
- A Comfy instance has one primary physical GPU and an optional primary-first
  GPU pool. Inside that process, the primary is logical `cuda:0`; auxiliaries
  are logical `cuda:1`, `cuda:2`, and so on.
- Saved policies use NVIDIA UUIDs because `cuda:N` indices can change between
  boots.
- Automatic placement assigns complete components—diffusion model, text
  encoder, or VAE—to GPUs. It is not tensor/layer sharding of one model.
- Omni staged H3 nodes load, execute, move their result to CPU, unload, and
  release cache before the next heavy stage. Ordinary loader routing does not
  guarantee that memory boundary.
- Workflow analysis, dependency probing, and placement previews do not load
  model weights. Running a workflow does.

## Authentication and discovery

The normal Windows-facing base URL is `http://127.0.0.1:9200`. Protected API
requests use the `X-Omni-Token` header. Keep the token in an environment
variable or secret store; never paste it into source or logs.

The API contract is discoverable from:

- Typed Pydantic request models in the current router source
- `GET /openapi.json` for agents/generated clients only when the gateway was
  deliberately started with `OMNI_ENABLE_DOCS=1`
- `/docs` for interactive Swagger documentation under that same development
  flag
- `GET /api/devices` for current UUIDs, VRAM, and physical IDs
- `GET /api/comfy/instances` for running Comfy instances and GPU pools

Schema/docs routes are disabled by default so the local appliance does not
advertise its administration surface. Do not enable them on a non-loopback or
otherwise exposed gateway merely for convenience.

PowerShell setup used by the examples:

```powershell
$base = "http://127.0.0.1:9200"
$headers = @{ "X-Omni-Token" = $env:OMNI_API_TOKEN }
```

Do not assume identifiers shown in this document exist on another system.
Always query devices and instances first.

## Safe operating sequence

### 1. Detect devices

```http
GET /api/devices
```

Each CUDA row includes:

- `id`: current physical index, such as `cuda:1`
- `uuid` and `stable_id`: durable NVIDIA identifier
- `vram_total_mb` and `vram_free_mb`
- `compute_pids`: current NVIDIA compute process IDs (deduplicated)
- active workers and primary Comfy instances

Placement uses current free VRAM. Even when every compute PID on a device
belongs to the selected Comfy instance, the planner does not assume its
resident allocation is reusable: it may contain context overhead, weights
from another graph, or routed copies. If a warm rerun is blocked, explicitly
unload resident Comfy models and analyze again. Device telemetry is
short-lived, so query it immediately before analysis and again after cleanup.

### 2. Start Comfy with the required visible pool

Starting Comfy does not load a model.

```http
POST /api/comfy/start
Content-Type: application/json

{
  "device": "cuda:1",
  "gpu_pool": ["cuda:1", "cuda:0"],
  "vram_mode": "normal",
  "preview_method": "auto",
  "disable_pinned_memory": false,
  "startup_options": {}
}
```

The selected `device` becomes `primary`. Pool ordering is normalized so the
primary is first. Include every GPU a workflow may target. Placement cannot
make a GPU visible to an already-running instance; a plan will return
`restart-required` when the pool is insufficient.

Use `GET /api/comfy/start-options` before setting advanced startup options.
Do not invent raw flags when an advertised structured option exists.

HuMo 17B plus the distilled LightX2V LoRA has a verified full-load OOM on a
24 GiB primary even with `vram_mode: "low"` by itself. On that class of card,
analysis blocks the graph unless the selected live instance uses either
`vram_mode: "none"`, or `vram_mode: "low"` with
`startup_options.reserve_vram >= 9`. The qualified 3090+3060 profile used 10
GB, disabled pinned memory, RAM-pressure caching, aggressive offload, and
disabled async offload. `reserve_vram` is a Comfy-wide flag and reduces usable
memory on auxiliary GPUs too; Omni's per-device placement reserves do not
change that upstream runtime behavior.

When the selected instance explicitly uses `vram_mode: "low"` or `"none"`,
placement may accept a component that is slightly larger than current GPU
headroom by reporting `estimated_cpu_offload_mb`. The aggregate offload must fit
the live reclaim-aware `/omni_studio/workloads` cgroup budget or the plan is
blocked. Normal-VRAM mode never assumes CPU offload will rescue an oversized
assignment.

Comfy and standalone model workers run inside that workload cgroup, while the
gateway remains outside it under the app-wide parent limit. When the final
workload process exits, Omni reclaims page cache through that empty cgroup's
`memory.force_empty` control. It never performs a host-wide `drop_caches`.

### 3. Analyze before running

Call `POST /api/workflows/analyze` with the API-format graph, target instance,
and optional placement policy. Check both workflow `readiness` and
`placement_plan.valid`.

### 4. Run through the existing workflow endpoint

- Inline graph: `POST /api/workflows/run`
- Saved graph with parameter overrides: `POST /api/workflows/{filename}/run`
- Saved graph with its stored policy: `POST /api/workflows/{filename}/queue`

The run path recalculates and reapplies placement. Do not transform a graph on
the client and then assume it remains valid.

For LTX 2.5 on bounded host RAM, use the Omni staged classes
`OmniLTXStageConditioning`, `OmniLTXStageEmptyAVLatent`,
`OmniLTXStageSampler`, and `OmniLTXStageAVDecode`. For native two-stage spatial
x2, place `OmniLTXStageSpatialRefine` between the sampler and final decoder;
it stages guide decode/upscale on its auxiliary device and dev-model refinement
on its primary device. These nodes retain complete Gemma conditioning while
unloading heavyweight components between phases. The
upstream conditioning saver is not lossless: it persists the conditioning
tensor and attention mask but omits other LTX 2.5 conditioning options.
Analyze first and confirm every heavyweight component reports `staged: true`.

For image-to-video, first/last-frame, or multi-keyframe guidance, chain one
`OmniLTXStageGuide` per image/video reference between the empty AV latent and
the sampler. Each guide accepts `frame_idx`, `strength`, an optional attention
mask, and optional native IC-LoRA parameters. Route its video VAE explicitly;
the node encodes the guide and unloads that VAE before the 22B transformer is
loaded. The staged sampler removes native appended guide tokens from the video
stream after sampling and preserves the audio stream unchanged. This is the
staged equivalent of native `LTXVAddGuide` plus `LTXVCropGuides` and preserves
the requested A/V duration.

For final LTX 2.5 decode, choose the official diffusion VAE for maximum fine
texture or the official conv VAE for speed. The qualified 1024x640x121 fast
profile uses conv VAE, `tile_size=512`, `temporal_size=64`, and routes final
video decode to the primary 24 GB GPU after the transformer unloads; audio
decode remains on the auxiliary GPU. Do not assume `temporal_size=2048` is a
safe general optimization: it OOMed the tested 12 GB GPU, and conv/t64 was
slightly faster than conv/t2048 on the same 24 GB GPU.

For prompt-based shot length, use `OmniLTXStageDurationPredictor` after
`OmniLTXStageConditioning`. It loads the selected LTX transformer and duration
head together on one selected device, returns the snapped `8k+1` frame count
and raw seconds, then unloads both. The resulting frame count can drive the
subsequent staged latent node. Analyze it like any other heavyweight stage;
the duration head is tracked in the `model_patches` category.

### 5. Verify

Use the returned `prompt_id`, instance proxy/queue, workflow history, or the UI
execution panel. A successful HTTP queue response means Comfy accepted the
prompt; it does not by itself prove output generation succeeded.

### 6. Stop when wanted

```http
POST /api/comfy/{instance_id}/stop
```

or:

```http
POST /api/comfy/stop-all
```

Downloads, model deletion, workflow editing, and core file maintenance do not
require a running Comfy instance unless their endpoint explicitly uses the
ComfyUI-Manager API.

## GPU placement policy

The same typed `placement` object is accepted by analyze and run requests.
Saved workflow metadata calls it `placement_policy`.

| Field | Meaning |
|---|---|
| `mode` | `single`, `auto`, or `manual` |
| `eligible_devices` | Selected UUIDs or current device aliases |
| `primary_device` | The single-mode target or preferred identifier |
| `reserve_mb` | Default headroom retained on every selected GPU |
| `device_reserve_mb` | Per-device reserve overrides |
| `overrides` | Component/node/model keys mapped to device identifiers |
| `require_all` | Block unless every selected GPU receives a component |
| `allow_cpu` | Permit explicit overrides to target `cpu`; automatic mixed-device placement remains GPU-only |

Example:

```json
{
  "mode": "manual",
  "eligible_devices": [
    "GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a",
    "GPU-80c24f57-c604-b55c-5433-5d248304eec9"
  ],
  "primary_device": "GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a",
  "reserve_mb": 1024,
  "device_reserve_mb": {
    "GPU-80c24f57-c604-b55c-5433-5d248304eec9": 1536
  },
  "overrides": {
    "12:model": "GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a",
    "19:video_vae": "GPU-80c24f57-c604-b55c-5433-5d248304eec9"
  },
  "require_all": false,
  "allow_cpu": false
}
```

### Modes

- `single`: place every discovered component on `primary_device`. The plan
  blocks when estimated peak memory exceeds usable VRAM.
- `auto`: use all eligible GPUs only when useful. The largest diffusion model
  favors the largest fitting GPU; smaller components use the smallest fitting
  GPU to preserve large-card headroom.
- `manual`: apply overrides first and plan every unlocked component
  automatically. Overrides are also honored in `auto`, enabling lightweight
  hybrid use.

When `allow_cpu` is true on a mixed GPU instance, CPU becomes available only
to a component explicitly named in `overrides`. It is never selected as an
automatic spill target. CPU placement can make an otherwise blocked graph fit,
but decode and inference kernels may be dramatically slower and some Comfy
operations do not poll the interrupt flag while running. Analyze first and use
an exact instance stop if a CPU operation will not exit cooperatively.

`require_all` is deliberately advanced. “Use all GPUs” normally means all are
eligible, not that every card must be forced into a workflow.

### Plan result

Treat these fields as authoritative:

- `valid`: whether the graph may be run with this policy
- `status`: `ready`, `blocked`, or `restart-required`
- `blockers`: hard failures that must not be bypassed
- `warnings`: limitations or ordinary-loader overlap notices
- `components`: resolved component IDs, estimates, targets, and reasons
- `devices`: current usable memory and estimated peak per selected GPU
- `summary.staged`: whether every discovered heavy component has staged
  unload semantics

The planner estimates from installed file sizes plus role-specific overhead.
Free VRAM is current, not guaranteed future capacity. Leave an appropriate
reserve for the desktop, Comfy runtime, and concurrent work.

Placement uses current free VRAM even when the only compute PID is the selected
Comfy instance. The API cannot prove that a resident CUDA allocation belongs to
the same graph or is unloadable, so it does not count that allocation as reusable
capacity. If a warm rerun is blocked, explicitly unload Comfy's resident models,
then analyze again before queueing.

## Workflow preparation and execution

Comfy's UI-format workflow is not directly queueable. Export API format or use
an API-format template. Analysis reports `needs-api-export` when conversion is
required.

Upload reference images through the authenticated instance proxy instead of
copying into a guessed Comfy directory:

```http
POST /api/comfy/{instance_id}/proxy/upload/image
Content-Type: multipart/form-data

image=<file>
subfolder=omni-capability-tests
type=input
overwrite=true
```

Use the returned name in `LoadImage`. The runtime input directory is
configuration-dependent and need not be `<comfy-root>/input`.

### Analyze an inline workflow

```http
POST /api/workflows/analyze
Content-Type: application/json

{
  "workflow": { "...": "Comfy API graph" },
  "filename": "example.json",
  "instance_id": "comfy-cuda1-8188",
  "placement": {
    "mode": "auto",
    "eligible_devices": ["GPU-...", "GPU-..."],
    "reserve_mb": 1024
  }
}
```

Before running, require:

- API-format execution mode
- `ready_to_run: true`
- no missing models or node classes
- `placement_plan.valid: true` when placement was requested

`needs-review` can also identify a graph that is syntactically runnable but
uses known-incompatible model semantics. For example, Krea 2 Raw requires its
resolution-derived flow shift and a separately encoded empty unconditional
branch; a Turbo-style Krea graph is blocked before it spends a full run on
noise.

### Run inline

```http
POST /api/workflows/run
Content-Type: application/json

{
  "workflow": { "...": "the same API graph" },
  "instance_id": "comfy-cuda1-8188",
  "client_id": "my-client",
  "placement": { "mode": "auto", "reserve_mb": 1024 }
}
```

### Run a saved graph with parameter overrides

```http
POST /api/workflows/example.json/run
Content-Type: application/json

{
  "instance_id": "comfy-cuda1-8188",
  "params": {
    "seed": 20260810,
    "5.width": 1280,
    "5.height": 1280
  }
}
```

The flat `params` keys may use a node ID plus input field, a node title, or a
supported convenience alias such as `seed`, `steps`, `prompt`, and
`negative_prompt`. Inspect `patch_report.applied` and `patch_report.unmatched`
in the response. The server recalculates placement; never bypass a returned
invalid plan merely because the previous run was valid.

### Persist policy separately

```http
PUT /api/workflows/example.json/metadata
Content-Type: application/json

{
  "tags": ["video", "h3"],
  "description": "Staged H3 workflow",
  "placement_policy": {
    "mode": "auto",
    "eligible_devices": ["GPU-...", "GPU-..."],
    "reserve_mb": 1024
  }
}
```

The graph remains portable Comfy JSON. Omni-specific policy remains editable
without rewriting it.

### Placement mechanisms

- `OmniH3StageConditioning`, `OmniH3StageFL2VConditioning`,
  `OmniH3StageSampler`, and `OmniH3StageDecode`: direct, memory-bounded H3
  stage placement. The FL2V node can run ClipProj and first/last-frame VAE
  encoding on separate selected devices, then unload both before sampling.
- `OmniH3ReferenceStrength`: sets native H3 visual/audio conditioning keys.
  Defaults are visual `0.999` and audio `1.0`; lowering visual strength can
  change pose, expression, and composition without changing the seed, but can
  also change generated audio.
- `OmniH3StageSampler` optionally applies installed Spectrum H3 acceleration.
  Its safe starting point is video blend `0.5`, audio blend `0.0`, offline
  smoothing replay enabled, with history and offline archive retained in
  system RAM. Spectrum remains default-off and is approximate.
- `OmniStageVAEDecode`: loads and releases its own named VAE on the selected
  GPU. It does not unload upstream ordinary models or encoders; those remain
  resident in admission accounting. Use the explicitly staged H3/LTX nodes
  when the graph requires model release between inference phases.
- `OmniRouteModel`, `OmniRouteCLIP`, and `OmniRouteVAE`: stable routing for
  ordinary loader outputs.
- `OmniRouteAudioEncoder`: routes Whisper/Wav2Vec conditioning encoders without
  requiring the diffusion-only deep-clone capability.
- Existing route nodes are updated instead of duplicated.
- Unsupported or unsafe multi-GPU reload behavior fails visibly; it is never
  silently ignored.

Staged peak accounting adds components that coexist within one node stage,
then takes the maximum across stages that release their models. Separate
sequential guide nodes have separate residency intervals. Ordinary loaders
remain resident across those intervals. All graphs also have a host-memory
check, including staged graphs and explicit CPU assignments.

## Models and Xet downloads

Comfy models are independent of other Omni model stores and live beneath:

```text
/opt/omni_studio/comfyui/models/<category>/
```

Setup may back that canonical path with an ext4 symlink to
`/var/lib/omni_studio/comfyui/models` so downloaded weights survive a
replaceable `/opt/omni_studio` runtime-tree rebuild. This is an implementation
detail, not a second model root: APIs, workflows, deletion checks, and agents
must continue to use `/opt/omni_studio/comfyui/models/<category>/`. The same
persistent-state layer protects Omni outputs, saved workflows, Comfy inputs,
and Comfy user data while keeping their public paths unchanged.

Common categories include `checkpoints`, `diffusion_models`, `text_encoders`,
`clip`, `clip_projections`, `vae`, `loras`, `controlnet`, and
`upscale_models`. Query
`GET /api/assets/comfy/storage` or `GET /api/assets/comfy/scan` rather than
guessing a folder.

Workflow analysis returns exact installed status and, where available,
verified install actions. Prefer those returned action bodies.

Search sources:

- Installed: `GET /api/registry/comfy/installed-models/search?q=...`
- Manager: `GET /api/registry/comfy/manager-models/search?q=...`
- Hugging Face: `GET /api/registry/comfy/models/search?q=...&category=...`

Install an exact Hugging Face file with Xet-backed download:

```http
POST /api/assets/comfy/diffusion_models/install
Content-Type: application/json

{
  "repo": "owner/repository",
  "file": "path/model.safetensors",
  "name": "model.safetensors"
}
```

An exact Hugging Face `/resolve/` or `/blob/` URL can be sent to:

```http
POST /api/assets/comfy/{category}/install-url

{
  "url": "https://huggingface.co/owner/repo/resolve/main/model.safetensors",
  "name": "model.safetensors"
}
```

Both paths use Hugging Face Hub with `hf_xet` and return a job plus its
`job_status_path`. Poll that path and verify the final target file before
declaring success.

Delete only after resolving the exact category and relative filename:

```http
DELETE /api/assets/comfy/{category}/{filename}
```

Stop all managed Comfy instances before asset deletion; an unloaded but live
instance still holds references that cannot be proven safe. Conflicting
installs also block deletion. Deletion is immediate and reports
`recoverable: false`. Whole-repository installs use an isolated subdirectory
inside the category instead of writing into the category root.

## Custom nodes and updates

### Search and install

Search Manager's available catalog:

```http
GET /api/registry/comfy/nodes/search?q=minimax&limit=50
```

Inspect an exact record with `GET /api/registry/comfy/nodes/{node_id}`. Prefer
the returned install action, catalog key, repository URL, and expected node
classes. After installation/restart, verify expected classes through live
Comfy object information or `GET /api/comfy/{instance_id}/nodes/search`.

Manager deliberately refuses some repositories that are absent from its
catalog. For an exact user-approved repository, the fallback installer is:

```http
POST /api/assets/comfy/nodes/install

{
  "repo_url": "https://github.com/owner/repository",
  "ref": "verified-tag-or-commit"
}
```

Poll the returned job, restart the selected Comfy instance once, and verify
the expected live node classes. This fallback installs; it does not weaken
Manager's security policy or make the repository eligible for Manager
`update_all`.

### Core update is separate from update-all

Core Comfy update:

```http
POST /api/comfy/installation/update

{
  "ref": "latest",
  "dry_run": false,
  "restart_instances": true
}
```

Manager/custom-node update-all:

```http
POST /api/comfy/extensions/manage

{
  "action": "update_all",
  "instance_id": "comfy-cuda1-8188",
  "dry_run": false,
  "auto_restart": true,
  "timeout_s": 1800
}
```

`update_all` does not update Comfy core. A complete maintenance operation must
perform and verify both operations. Do not run them concurrently. Preserve the
instance's device, GPU pool, VRAM mode, precision, and startup options across
restart.

A successful core-update job includes `qualification`. With no restarted
instance, `checkout-and-templates` verifies the exact target commit, a clean
checkout, and workflow-template discovery. With restarted instances,
`live-runtime` additionally verifies readiness, `/system_stats`,
`/object_info`, core node classes, and OmniBridge routing/staged VAE classes. Qualification blockers fail the update
job; a successful Git command alone is not considered a completed update.

Manager jobs share one mutation queue. Real `update_all` requires
`auto_restart: true` and verifies that previously working node classes remain
available after restart. Cancellation, timeout, or a transport failure during
mutation stops the owning Comfy instance before the job finishes; a queued
mutation is not safely cancelled by marking its gateway job alone. Recovery
uses only a newly created, identifiable snapshot.

Other guarded extension actions are `install`, `update`, `enable`, `disable`,
`reinstall`, `fix`, and `uninstall`. Use `dry_run: true` to inspect mutations
when the user asks for a preview; it is not a prerequisite for an explicitly
authorized update.

## Templates

- Search installed templates:
  `GET /api/workflows/search?q=...&scope=templates&limit=30`
- Fetch the exact live template:
  `GET /api/workflow-templates/{template_id}?source=...&package=...`
- Analyze the returned `workflow` before saving or running.

Pass the exact `source` and `package` values from the chosen search result.
Template IDs can exist in more than one provider, and an unqualified fetch may
resolve a different item or return 404 even though search found a candidate.

Templates may be UI-format, API-format, local-inference, or API-key workflows.
Do not infer required models or credentials from the template title alone.

## User interface

### ComfyUI tab

1. Select the primary GPU.
2. Enable component placement when more than one GPU should be visible.
3. Check all GPUs or only the desired auxiliary cards.
4. Choose memory, precision, and advertised advanced startup options.
5. Start ComfyUI. This does not load weights.

### Workflows tab

1. Select the running Comfy instance.
2. Choose Single GPU, Automatic pool, or Manual/hybrid.
3. Select eligible GPUs and a VRAM reserve.
4. Click **Plan GPUs**.
5. Review every component, estimate, warning, and blocker.
6. Optionally choose per-component overrides and re-plan.
7. Save the policy as the workflow default if desired.
8. Click **Run** and confirm the final plan.

Automatic mode is the recommended default. Manual overrides are useful when a
specific model is known to fit or behave better on a particular card.

## Troubleshooting

### Plan says restart-required

The requested UUID is not visible inside the current Comfy process. Stop and
restart the instance with that GPU included in `gpu_pool`, then analyze again.

### Plan says insufficient headroom

- Confirm no other model or application is consuming the GPU.
- Increase available VRAM or reduce the reserve only when safe.
- Use staged nodes when the workflow supports them.
- Reduce resolution, frames, batch size, or model precision.
- Use manual overrides only after checking the reported estimate.

### Ordinary loader warning

Routing can place components on separate GPUs, but the original loaders may
remain resident together. This is expected and is not a memory leak. Use a
staged workflow when strict unload boundaries are required.

Comfy's proxied `POST /free` request with `unload_models=true` and
`free_memory=true` is a best-effort cache/model release. Verify the result with
fresh `GET /api/devices` telemetry. If the selected Comfy PID still owns
material VRAM, stopping that exact instance is the dependable hard-release
fallback before changing model families.

### Update succeeded but nodes are missing

Check `GET /api/comfy/extensions/status`, restart the instance, then verify the
expected class names. A downloaded repository is not considered operational
until its classes load successfully.

### Download job finished but model is absent

Read the returned job status and log, verify the exact target path beneath the
Comfy model root, and re-run workflow analysis. Never report success based only
on a queued download response.
