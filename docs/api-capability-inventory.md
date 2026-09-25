# Omni Studio API capability inventory

This is the complete source-level map of the Omni Studio gateway API. It is
intended for users and future agents. The typed request
models in `server/routers/` remain the exact field contract; when the gateway
is deliberately started with `OMNI_ENABLE_DOCS=1`, its `/openapi.json` is the
machine-readable schema.

The multi-method Comfy proxy retains its existing GET, POST, PUT, DELETE and
PATCH operations, each with a distinct OpenAPI operation ID. Use the typed
routers or generated schema for the exact current operation inventory.

## Connection and authentication

Use the Windows bridge/API base (`http://127.0.0.1:9200` in the normal app)
for clients. The gateway's internal default is port 8200 and is not the normal
public client surface.

- `GET /api/session` is the loopback-only bootstrap. It returns the current
  per-instance token, the `X-Omni-Token` header name, auth mode, API base URL,
  and configured OpenAI aliases. Never print or persist the token in reports.
- Send `X-Omni-Token: <token>` for `/api/*`. The session cookie
  `omni_session` is also accepted.
- `/v1/*` accepts the same loopback token or `Authorization: Bearer ...`.
  In bearer mode, registered scoped API keys are accepted. Scopes are enforced
  centrally: read for discovery and no-weight planning, generate for inference
  and workflow runs, manage for worker/assets changes, and admin for credentials,
  shutdown, restart and maintenance. Autospawning state reads require manage.
- `/metrics` is token-gated. `/api/session` is the only gateway API bootstrap
  exemption.
- The bridge answers `GET /api/live` and `GET /api/status` without reaching the
  gateway: they report bridge/API connectivity. The gateway's own `/api/live`
  is authenticated.
- Query-string `token=` is deliberately accepted only for browser media/SSE
  cases that cannot set a custom header: output/media GETs, audio/ACE output
  files and zips, and `/api/logs/stream`.
- `GET /`, `GET /health`, and static UI files are public. Interactive docs,
  ReDoc, and `/openapi.json` are disabled unless `OMNI_ENABLE_DOCS=1`.
- Origins are checked against the request host; CORS does not grant remote
  access. Remote binding requires explicit operator configuration.

## Root, discovery, configuration, and browser hand-off

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/` | Serve the local UI. |
| GET | `/health` | Lightweight gateway health. |
| GET | `/api/live` | Gateway live response when called directly. |
| GET | `/api/session` | Bootstrap token and API metadata. |
| GET | `/api/config` | Registered models, variants, LoRA compatibility, Comfy install state, download policy. |
| GET | `/api/capabilities` | Machine-readable capability groups, readiness blockers, loaded workers/Comfy instances, artifact roots, model aliases, and policy. |
| POST | `/api/open-url` | Open a validated `http`/`https` URL in the Windows/system browser. |

`/api/capabilities` is the best first machine-readable summary, but its
capability names are deliberately broad. A capability is not production-ready
merely because it appears there; consult
[`capability-confidence.md`](capability-confidence.md) and the typed status
routes.

## API keys

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/keys` | List registered key metadata, never secret material. |
| POST | `/api/keys` | Create a scoped API key. |
| GET | `/api/keys/whoami` | Inspect the authenticated key/principal. |
| DELETE | `/api/keys/{key_id}` | Revoke one exact key. |

Keys matter when bearer auth is enabled. The per-instance loopback token still
supports the local UI.

## Devices, jobs, system state, and maintenance

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/devices` | Enumerate GPUs, stable UUIDs, logical CUDA IDs, free/total VRAM, compute PIDs, and placement warnings. |
| GET | `/api/jobs` | List background jobs. |
| GET | `/api/jobs/{job_id}` | Read one job and result/error. |
| POST | `/api/jobs/{job_id}/cancel` | Cancel one background job. |
| GET | `/api/jobs/{job_id}/stream` | Stream job progress. |
| GET | `/api/system/info` | Host, distro, gateway, process, and runtime information. |
| GET | `/api/system/disk` | Disk/storage information. |
| GET | `/api/system/gpu` | GPU/process snapshot. |
| GET | `/api/system/processes` | App-relevant process snapshot. |
| POST | `/api/system/refresh` | Refresh/reconcile runtime state. |
| POST | `/api/system/shutdown` | Graceful gateway shutdown; requires `{"confirm":true}`. Teardown stops workers and Comfy; the bridge may relaunch the gateway while the app remains open. |
| POST | `/api/system/restart` | Graceful gateway restart; requires confirmation. Teardown stops workers and Comfy. |
| POST | `/api/shutdown` | Alias of system shutdown. |
| POST | `/api/restart` | Alias of system restart. |
| POST | `/api/app/shutdown` | Bridge-level full shutdown of workers, Comfy, jobs, gateway, bridge, and watchdog; requires token and confirmation. |
| GET | `/api/maintenance/policy` | Read scheduled cleanup policy. |
| PUT | `/api/maintenance/policy` | Update known maintenance task settings only. |
| GET | `/api/maintenance/status` | Read cadence, last-run, next-run, and due state. |
| POST | `/api/maintenance/run/{task}` | Run one existing maintenance task as a background job. |
| GET | `/api/logs/stream` | Authenticated server log SSE. |
| GET | `/metrics` | Token-gated Prometheus metrics for jobs, workers, Comfy, and request timing. |

The normal job rule is: install/update/zip/compose/maintenance operations that
return `status: running` provide a `job_id`; poll `/api/jobs/{job_id}` and stop
at `done`, `error`, or `cancelled`. ACE-Step and Audio Lab inference calls are
synchronous despite persisting their media under a job-shaped output ID; do not
blindly poll the generic job table for those calls.

## Standalone Omni model workers and chat

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/workers` | List exact worker IDs, model/variant, device, state, and memory. |
| POST | `/api/workers/analyze` | Weight-free placement and capacity analysis. This is the required preview. |
| POST | `/api/workers/spawn` | Spawn a model worker after rechecking current devices. |
| DELETE | `/api/workers/{worker_id}` | Stop/delete one exact worker and release its model. |
| POST | `/api/workers/kill-all` | Stop all managed workers; use only when every worker is in scope. |
| GET | `/api/workers/{worker_id}/logs` | Read one worker's logs. |
| POST | `/api/chat/{model}` | Non-streaming model chat with optional image/audio/video fields according to the selected worker's contract. |
| POST | `/api/chat/{model}/stream` | Streaming model chat. |
| POST | `/api/chat/{model}/cancel/{job_id}` | Cancel a streaming/background chat job. |
| POST | `/v1/chat/completions` | OpenAI-compatible chat. |
| POST | `/v1/completions` | OpenAI-compatible text completion. |
| GET | `/v1/models` | OpenAI-compatible model listing. |

Placement modes are `single`, `auto`, and expert `manual`. Save stable GPU
UUIDs, not remembered `cuda:N` indices. `auto` uses a bounded Hugging Face
device map for supported standalone model families; Comfy component placement
is a separate system. `valid: false` or `ready_to_spawn: false` is a hard stop.

Known source-level capability boundaries:

- Qwen 2.5 Omni text is verified; audio/video input and native speech output
  are explicitly unwired/501.
- MiniCPM-o text, image, and audio understanding are verified; video is
  explicitly rejected and native TTS is blocked by its decoder compatibility.
- Moshi has no usable batch or full-duplex stream transport in this API.
- AnyGPT is text-only in the worker source and its cold-load behavior is not
  dependable through the bridge window.
- Qwen3-Omni/Nemotron large variants hard-stop before spawn when capacity is
  insufficient. Quantized candidates still require live qualification.

## In-memory chat sessions

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/chat/sessions` | List current in-memory conversation sessions. |
| POST | `/api/chat/sessions` | Create a session. |
| GET | `/api/chat/sessions/{session_id}` | Read one session and history. |
| POST | `/api/chat/sessions/{session_id}/messages` | Send a session message and receive a completed response. |
| POST | `/api/chat/sessions/{session_id}/messages/stream` | Stream a session response. |
| DELETE | `/api/chat/sessions/{session_id}` | Delete one exact session. |

Sessions expire after 24 hours of inactivity by default and are lost when the
gateway restarts. They are not persisted to the media or workflow stores.

## ComfyUI lifecycle and instance control

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/comfy/instances` | List running/known Comfy instances, ports, devices, pools, readiness, and state. |
| GET | `/api/comfy/{instance_id}/multigpu` | Read that instance's visible GPU pool and placement capability. |
| GET | `/api/comfy/start-options` | Read supported startup flags and defaults. |
| GET | `/api/comfy/installation/status` | Inspect Comfy core/Manager installation and update state. |
| POST | `/api/comfy/installation/update` | Separately update Comfy core and/or Manager; verify each operation. |
| POST | `/api/comfy/start` | Start one instance with explicit device/pool/start options. |
| POST | `/api/comfy/{instance_id}/stop` | Stop one exact instance. |
| POST | `/api/comfy/stop-all` | Stop every managed Comfy instance. |
| GET | `/api/comfy/models` | List installed Comfy model files by category. |
| GET | `/api/comfy/nodes` | List installed/disabled custom nodes. |
| GET | `/api/comfy/{instance_id}/nodes/search` | Search nodes visible to one live instance. |
| GET | `/api/comfy/{instance_id}/logs` | Read instance logs. |
| POST | `/api/comfy/{instance_id}/upload/image` | Upload an input image to the exact instance. |
| POST | `/api/comfy/{instance_id}/upload/mask` | Upload a mask to the exact instance. |
| GET/POST/PUT/DELETE/PATCH | `/api/comfy/{instance_id}/proxy/{subpath:path}` | Guarded Comfy HTTP passthrough for allowlisted paths. |
| WS | `/api/comfy/{instance_id}/ws` | Authenticated Comfy WebSocket passthrough; token is supplied in the handshake query or token subprotocol. |
| GET | `/api/comfy/{instance_id}/previews/current` | Current preview frame. |
| GET | `/api/comfy/{instance_id}/previews/stream` | Preview SSE stream. |

Comfy models stay under `/opt/omni_studio/comfyui/models/<category>`. Start
the instance only after checking its `gpu_pool`; an auxiliary GPU is never
assumed visible. Ordinary workflow placement moves components between visible
devices; it is not general tensor/layer sharding. Staged nodes provide the
strongest unload-between-stage behavior.

## Comfy workflows and templates

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/workflows` | List saved user workflows. |
| GET | `/api/workflows/search` | Search saved workflows. |
| GET | `/api/workflow-templates/{template_id}` | Retrieve a built-in/default workflow template. |
| GET | `/api/workflows/requirements` | Inspect requirements for an inline graph/query. |
| GET | `/api/workflows/{filename}/requirements` | Inspect requirements for a saved graph. |
| POST | `/api/workflows/analyze` | Resolve API-format validity, models, nodes, placement, and blockers without loading weights. |
| POST | `/api/workflows/probe` | Probe workflow structure/dependencies. |
| POST | `/api/workflows/run` | Run an inline API-format graph. |
| POST | `/api/workflows/import` | Import a workflow into the saved workflow store. |
| GET | `/api/workflows/{filename}` | Read one saved graph. |
| PUT | `/api/workflows/{filename}` | Save/replace one graph. |
| PUT | `/api/workflows/{filename}/metadata` | Persist tags, description, and separate GPU placement policy. |
| DELETE | `/api/workflows/{filename}` | Delete one saved workflow. |
| POST | `/api/workflows/{filename}/run` | Run a saved graph with flat parameter overrides. |
| POST | `/api/workflows/{filename}/queue` | Queue a saved graph. |

The safe sequence is `GET /api/devices` → `GET /api/comfy/instances` →
`POST /api/workflows/analyze` → run only when `ready_to_run` and the placement
plan are valid → verify the resulting output/media. UI-format graphs must be
exported to API format first. Saved GPU policy is stored as metadata, not
embedded into portable workflow JSON. Run responses can contain a Comfy
`prompt_id`; acceptance is not proof of completed output, so verify history and
files.

## Comfy asset storage, model search, and custom nodes

### Local asset routes

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/assets/comfy` | List all managed Comfy assets. |
| GET | `/api/assets/comfy/storage` | Report canonical storage roots. |
| GET | `/api/assets/comfy/scan` | Scan installed assets. |
| GET | `/api/assets/comfy/{category}` | List one exact model category. |
| POST | `/api/assets/comfy/{category}/install` | Install a model from a typed source/registry request. |
| POST | `/api/assets/comfy/{category}/install-url` | Install from an explicitly allowed URL. |
| POST | `/api/assets/comfy/upload/{category}` | Upload a local model asset. |
| DELETE | `/api/assets/comfy/{category}/{filename:path}` | Delete one exact model file; immediate and not recoverable. |
| GET | `/api/assets/comfy/nodes` | List asset-managed custom nodes. |
| POST | `/api/assets/comfy/nodes/install` | Install a node from an asset request. |
| DELETE | `/api/assets/comfy/nodes/{name}` | Delete one exact asset-managed node. |
| GET | `/api/assets/comfy/blueprints/requirements` | Resolve blueprint dependencies. |
| POST | `/api/assets/comfy/blueprints/install` | Install blueprint dependencies. |
| GET | `/api/assets/comfy/templates/requirements` | Resolve template dependencies. |
| POST | `/api/assets/comfy/templates/install` | Install template dependencies. |

### Registry/catalog routes

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/registry/checkpoints` | Read configured checkpoint catalog. |
| GET | `/api/registry/comfy/models/search` | Search the configured model registry. |
| GET | `/api/registry/comfy/models/repository` | Inspect one model repository/source. |
| GET | `/api/registry/comfy/manager-models/search` | Search Comfy Manager-installable model entries. |
| GET | `/api/registry/comfy/installed-models/search` | Search locally installed model files. |
| GET | `/api/registry/comfy/nodes/search` | Search Manager/catalog custom nodes. |
| GET | `/api/registry/comfy/nodes/{node_id}` | Inspect one catalog node and install metadata. |
| POST | `/api/registry/comfy/nodes/{node_id}/install` | Install one catalog node. |
| GET | `/api/comfy/extensions/status` | Read active/disabled extension state. |
| POST | `/api/comfy/extensions/manage` | Install/update/remove/enable/disable or bulk-update extensions. |
| POST | `/api/comfy/models/manager/install` | Install a Manager model entry. |

Core Comfy update and Manager `update_all` are distinct operations. A model
dependency should be resolved to an exact category and relative filename before
download or deletion; never infer a folder from a display name.

## Generic local speech and sound effects

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/stt/{model}` | Native local speech-to-text for a registered model. |
| POST | `/v1/audio/transcriptions` | OpenAI-compatible local transcription. |
| POST | `/api/tts/{model}` | Native local text-to-speech for a registered model. |
| POST | `/v1/audio/speech` | OpenAI-compatible local speech output. |
| POST | `/api/moss/sfx` | MOSS-SoundEffect text-to-SFX generation. |

Use explicit device/model state for audio. Generic modality fields do not imply
that every model implements a modality; consult the capability matrix.

## ACE-Step music API

### State, loading, LoRA, and lifecycle

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/ace_step/status` | Installed ACE model/LM/VAE/LoRA registry and task limits. |
| GET | `/api/ace_step/state` | Runtime-loaded ACE components; supports cheap `autospawn=false`. |
| POST | `/api/ace_step/load` | Load/switch model, LM, VAE, precision, offload, and device. |
| POST | `/api/ace_step/unload` | Unload model, LM, VAE, or all components. |
| POST | `/api/ace_step/cancel` | Cancel active ACE inference. |
| POST | `/api/ace_step/install-model` | Install an ACE model variant. |
| DELETE | `/api/ace_step/install-model/{variant_id}` | Delete one exact ACE model variant. |
| POST | `/api/ace_step/install-lm` | Install an ACE language-model variant. |
| DELETE | `/api/ace_step/install-lm/{variant_id}` | Delete one exact ACE LM variant. |
| POST | `/api/ace_step/install-vae` | Install an ACE VAE variant. |
| DELETE | `/api/ace_step/install-vae/{variant_id}` | Delete one exact ACE VAE variant. |
| POST | `/api/ace_step/install-lora` | Install an ACE LoRA pack/adapter. |
| DELETE | `/api/ace_step/install-lora/{name}` | Delete one exact ACE LoRA. |
| POST | `/api/ace_step/install-custom` | Install a declared ACE custom asset. |
| DELETE | `/api/ace_step/install-custom/{kind}/{name}` | Delete one exact ACE custom asset. |
| GET | `/api/ace_step/lora/list` | Runtime-attached LoRAs. |
| POST | `/api/ace_step/lora/attach` | Attach one installed adapter/pack file. |
| POST | `/api/ace_step/lora/detach` | Detach one adapter. |
| GET | `/api/ace_step/enums` | Valid enum values and task settings. |
| GET | `/api/ace_step/progress` | Current generation progress. |

### Generation and transformation

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/ace_step/generate` | Vocal or instrumental text-to-music. |
| POST | `/api/ace_step/generate-ranked` | Generate 1–16 candidates and optionally rank with CLAP. |
| POST | `/api/ace_step/a2a` | Audio-to-audio transformation. |
| POST | `/api/ace_step/repaint` | Masked temporal repaint. |
| POST | `/api/ace_step/edit` | Lyrics-only or remix edit. |
| POST | `/api/ace_step/extend` | Prepend/append generated continuation. |
| POST | `/api/ace_step/cover` | Re-style an input song. |
| POST | `/api/ace_step/vocal2bgm` | Convert vocals/voice into accompaniment. |
| POST | `/api/ace_step/extract` | Extract a named stem with a base model. |
| POST | `/api/ace_step/lego` | Rebuild audio from a named track with a base model. |
| POST | `/api/ace_step/complete` | Complete named tracks with a base model. |
| POST | `/api/ace_step/create-sample` | Plan a caption, lyrics, and metadata from a style query using the LM. |
| POST | `/api/ace_step/format-sample` | Expand a caption and lyrics into structured metadata using the LM. |
| POST | `/api/ace_step/understand` | Derive metadata from 5 Hz audio codes using the LM. |
| POST | `/api/ace_step/simple` | Plan from a style query, then generate in one call. |
| POST | `/api/ace_step/lyric2vocal` | Specialized lyric-to-vocal path; depends on released assets. |
| POST | `/api/ace_step/text2samples` | Specialized text-to-samples path; depends on released assets. |
| POST | `/api/ace_step/analyze` | Analyze BPM, key, loudness, and objective audio metadata. |

### ACE outputs/jobs

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/ace_step/jobs` | List ACE persisted output jobs. |
| DELETE | `/api/ace_step/jobs/{job_id}` | Delete one ACE output job. |
| GET | `/api/ace_step/outputs/{job_id}` | List files for one output. |
| GET | `/api/ace_step/outputs/{job_id}/{filename}` | Retrieve one generated file. |
| GET | `/api/ace_step/zip/{job_id}` | Download an output bundle. |

ACE supports prompts, lyrics, negative prompts, duration, steps, CFG,
scheduler, seed, guidance interval, timestep `shift`, typed BPM/key/time
signature, and bounded audio initialization as defined
by `_GenRequest` and the operation-specific typed models. The documented local
duration ceiling is up to 600 seconds, but long tracks should be assembled with
the composition API and listened to for continuity.

## MiniMax Music 3

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/minimax_music3/status` | Official variant, installation, limits, license, and LoRA-runtime status. |
| GET | `/api/minimax_music3/state` | Cheap runtime state with `autospawn=false`. |
| POST | `/api/minimax_music3/load` | Load the official Diffusers pipeline on an explicit GPU with optional CPU offload. |
| POST | `/api/minimax_music3/generate` | Synchronous caption + lyrics song generation, 1–360 seconds. |
| POST | `/api/minimax_music3/unload` | Release loaded model components. |
| POST | `/api/minimax_music3/cancel` | Retire the exact active worker and unload its model. |
| GET | `/api/minimax_music3/jobs` | List persisted Music 3 manifests. |
| GET | `/api/minimax_music3/outputs/{job_id}/{filename}` | Retrieve a managed WAV or manifest. |

Install `official-diffusers` through `/api/setup/install-variant`. Search Hub
models and future LoRAs through `/api/search/hf` with
`family=minimax_music3`; compatibility labels are conservative and only the
official pinned layout is currently loadable. Output is direct-to-storage
44.1 kHz stereo PCM-24 rather than gateway base64.

## Stable Audio Lab

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/audio_lab/status` | Installed Stable Audio model/VAE/CLAP/custom assets. |
| GET | `/api/audio_lab/state` | Runtime components; use `autospawn=false` for a cheap read. |
| GET | `/api/audio_lab/workers` | Audio Lab worker records. |
| POST | `/api/audio_lab/install-model` | Install a Stable Audio model variant. |
| DELETE | `/api/audio_lab/install-model/{variant_id}` | Delete one exact Stable Audio model variant. |
| POST | `/api/audio_lab/install-vae` | Install a Stable Audio VAE variant. |
| DELETE | `/api/audio_lab/install-vae/{variant_id}` | Delete one exact Stable Audio VAE variant. |
| POST | `/api/audio_lab/install-clap` | Install a CLAP ranking variant. |
| DELETE | `/api/audio_lab/install-clap/{variant_id}` | Delete one exact CLAP variant. |
| POST | `/api/audio_lab/install-custom` | Install a declared Audio Lab custom asset. |
| DELETE | `/api/audio_lab/install-custom/{kind}/{name}` | Delete one exact Audio Lab custom asset. |
| POST | `/api/audio_lab/load` | Load Stable Audio/CLAP components on an explicit device. |
| POST | `/api/audio_lab/unload` | Unload one/all audio components. |
| POST | `/api/audio_lab/cancel` | Cancel active Audio Lab inference. |
| GET | `/api/audio_lab/samplers` | Valid sampler/objective settings. |
| POST | `/api/audio_lab/generate` | Text-to-audio/music/effects generation. |
| POST | `/api/audio_lab/generate-ranked` | Candidate fanout and optional CLAP ranking. |
| POST | `/api/audio_lab/a2a` | Audio-to-audio transformation. |
| POST | `/api/audio_lab/inpaint` | Regenerate/inpaint an audio interval. |
| POST | `/api/audio_lab/uncond` | Unconditional/latent-oriented generation path. |
| POST | `/api/audio_lab/score` | CLAP text/audio similarity score. |
| POST | `/api/audio_lab/vae/encode` | Encode audio to VAE representation. |
| POST | `/api/audio_lab/vae/decode` | Decode VAE representation. |
| POST | `/api/audio_lab/vae/reconstruct` | Encode/decode round trip with RMS diagnostic. |
| GET | `/api/audio_lab/progress` | Current progress. |
| GET | `/api/audio_lab/jobs` | List persisted Audio Lab output jobs. |
| DELETE | `/api/audio_lab/jobs/{job_id}` | Delete one Audio Lab output job. |
| GET | `/api/audio_lab/outputs/{job_id}` | List output files. |
| GET | `/api/audio_lab/outputs/{job_id}/{filename}` | Retrieve one output file. |
| GET | `/api/audio_lab/zip/{job_id}` | Download an output bundle. |

## Model installation and asset setup

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/setup/status` | Overall installed/default model state. |
| POST | `/api/setup/install/{model}` | Install one registered standalone model. |
| POST | `/api/setup/install-variant` | Install one named model variant. |
| POST | `/api/setup/install-missing-defaults` | Queue missing configured defaults. |
| GET | `/api/setup/jobs` | List setup/install jobs. |
| GET | `/api/setup/jobs/{job_id}` | Read one setup job. |
| GET | `/api/setup/jobs/{job_id}/log` | Read setup job log. |
| POST | `/api/setup/jobs/{job_id}/cancel` | Cancel one setup job. |
| GET | `/api/variants/{model}` | List variants for a registered model. |
| GET | `/api/loras` | List installed/registered generic LoRAs. |
| POST | `/api/loras/install` | Install one generic LoRA. |
| DELETE | `/api/loras/{name}` | Delete one exact generic LoRA. |
| GET | `/api/search/hf` | Search Hugging Face sources; optional `family=minimax_music3` adds compatibility classification. |
| POST | `/api/setup/hf-token` | Store/update the local Hugging Face token. |

Dedicated ACE and Audio Lab asset routes are listed in their sections because
they validate engine-specific model/LM/VAE/CLAP/LoRA/custom kinds.

## Outputs, media library, metadata, and long-form composition

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/outputs` | List media with kind/media-kind filters, pagination, and lightweight facts. |
| GET | `/api/outputs/{relpath:path}` | Stream/download one exact output. |
| HEAD | `/api/outputs/{relpath:path}` | Probe media availability/range metadata. |
| DELETE | `/api/outputs/{relpath:path}` | Delete one exact output immediately. |
| GET | `/api/outputs/metadata/{relpath:path}` | Probe duration, sample rate, channels, dimensions, and format. |
| POST | `/api/outputs/zip` | Stream a synchronous output ZIP archive; no job ID. |
| POST | `/api/outputs/prune` | Prune outputs subject to keep/pin policy. |
| GET | `/api/outputs/collections` | List media collections. |
| GET | `/api/outputs/meta/{relpath:path}` | Read tags/pin/collection/note metadata. |
| PUT | `/api/outputs/meta/tags/{relpath:path}` | Set tags. |
| PUT | `/api/outputs/meta/pinned/{relpath:path}` | Pin/unpin. |
| PUT | `/api/outputs/meta/collections/{relpath:path}` | Set collection membership. |
| PUT | `/api/outputs/meta/notes/{relpath:path}` | Set notes. |
| DELETE | `/api/outputs/meta/{relpath:path}` | Clear metadata. |
| POST | `/api/outputs/audio/compose` | Compose 2–64 exact library audio segments with trim, gain, crossfade, optional normalization, and bounded CPU threads. |

The media routes use exact `kind` and relative paths returned by listing. The
audio composer returns a background job; poll its generic job ID, then verify
the final WAV and manifest. Output deletion and model deletion are immediate
and not recoverable.

## API usage rules and known gaps

1. Read devices and state before loading. Pass `autospawn=false` when the goal
   is discovery rather than implicit model loading.
2. Use explicit `cuda:N` only after resolving the current device response;
   persist UUIDs in policy. Re-analyze immediately before spawn/run.
3. Analyze Comfy workflows before queueing. A false placement plan or missing
   dependency is a hard stop.
4. Keep Comfy, standalone Omni workers, ACE-Step, Audio Lab, MOSS-TTS, and
   MOSS-SFX as separate worker families. Unload components, then delete the
   exact worker ID when residency is no longer wanted.
5. Do not infer support from a typed field. Current Qwen audio/video and
   native Qwen speech, MiniCPM video/TTS, Moshi, and several large Omni
   variants are explicitly blocked or unqualified; see the confidence matrix.
6. A Comfy `prompt_id` means queue acceptance, not completed media. Verify
   Comfy history, output file metadata, and the `/api/outputs` entry.
7. A background job ID and a persisted inference output ID are not equivalent.
   Poll only when the response says the operation is running.
8. Do not use `kill-all`, `stop-all`, or full app shutdown for a single-model
   cleanup.

## Internal worker API (not the stable gateway contract)

Each spawned standalone/audio worker has a loopback HTTP service behind the
gateway. These routes explain the implementation boundary but should not be
called directly by users or agents; use the corresponding authenticated
`/api/*` gateway route so worker IDs, placement, timeouts, and cleanup stay
consistent:

- Core worker: `GET /health`, `POST /load`, `POST /unload`, `POST /infer`,
  `POST /infer/stream`, `POST /abort`, and `POST /infer/tts`.
- Audio Lab worker: `POST /audio_lab/load_sa`, `POST /audio_lab/load_clap`,
  `POST /audio_lab/unload`, `GET /audio_lab/state`,
  `POST /audio_lab/cancel`, `POST /audio_lab/cancel/clear`, plus
  `/infer/audio_gen`, `/infer/audio_score`, `/infer/audio_a2a`,
  `/infer/audio_inpaint`, `/infer/audio_uncond`, `/infer/vae_encode`,
  `/infer/vae_decode`, and `/infer/vae_reconstruct`.
- ACE worker: `POST /ace_step/load_model`, `POST /ace_step/load_lm`,
  `POST /ace_step/unload`, `GET /ace_step/state`,
  `POST /ace_step/cancel`, `POST /ace_step/cancel/clear`,
  `POST /ace_step/lora/attach`, `POST /ace_step/lora/detach`,
  `GET /ace_step/lora/list`, and the `/infer/ace_*` generation and analysis
  handlers.
- MiniMax Music 3 worker: `POST /minimax_music3/load_model`,
  `GET /minimax_music3/state`, `POST /minimax_music3/unload`, and
  `POST /infer/minimax_music3_generate`.

The gateway is the compatibility layer. Direct worker calls can bypass the
placement recheck, worker retirement after timeout, token boundary, and exact
cleanup rules.

## Source and live verification status

The route table and counts above were reconciled with the current included
typed router declarations. This source review did not check a live
authenticated `/api/capabilities` response or regenerate the in-process
FastAPI schema. Start the normal app/bridge before relying on live readiness,
installed assets, or output state. Source inspection does not load models or
start Comfy.

Canonical detailed guides:

- [`comfy-api.md`](comfy-api.md)
- [`audio-api.md`](audio-api.md)
- [`omni-model-api.md`](omni-model-api.md)
- [`capability-confidence.md`](capability-confidence.md)
