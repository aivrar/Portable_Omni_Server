# ComfyUI frontier capability scan

## Purpose

This is the execution plan for a maximum-quality, API-driven capability scan of
the local ComfyUI installation. The first target families are:

- Krea 2 OSS Raw and Turbo
- Wan 2.x, including Wan 2.2 and Wan Animate 2
- LTX 2/2.3
- MiniMax-H3 local workflows

The scan measures the useful quality/size/duration boundary on the detected
GPUs, not a theoretical maximum that risks taking down the host. It compares
native workflows with carefully pinned optimized alternatives, including Kijai
repositories when the registry exposes a compatible, installable candidate.

No heavyweight inference or downloads are part of this document's creation.
Execution starts only after the gateway is reachable and the baseline gates
below pass. At the time this plan was written, the Windows-facing bridge on
`127.0.0.1:9200` was not listening; that is an operational prerequisite, not a
model or workflow failure.

## Rules of engagement

1. Use existing typed routes only. No new endpoint is required for this scan.
2. Keep every check small and sequential: one registry query, one install job,
   one analysis, or one heavy workflow at a time.
3. Never run a Cartesian product of models × resolutions × durations. Each
   phase changes one variable from a qualified baseline.
4. Analyze before every run. `ready_to_run` and
   `placement_plan.valid` are hard gates; blockers are never bypassed.
5. Use stable GPU UUIDs in saved policies and resolve them to current
   `cuda:N` values immediately before analysis. A GPU being physically present
   does not make it visible to an already-running Comfy instance.
6. Treat ordinary multi-GPU routing as complete-component placement. It is not
   tensor/layer sharding. Use staged Omni nodes when an unload boundary is
   required between conditioning, sampling, and decode.
7. Preserve one native/current baseline for every family before installing an
   optimized candidate. Never replace the only working graph or model.
8. Pin every third-party repository and model to an exact tag or commit. Do not
   test a moving `main` branch and call the result reproducible.
9. Do not modify Comfy core or upstream/custom-node source during the probe.
   Classify defects first; only Omni-owned defects may be fixed under the
   normal change process.
10. After each heavy case, free the selected instance and verify telemetry.
    If material VRAM remains, stop that exact instance before changing model
    families. Do not use host-wide `drop_caches`.

## API contract and execution sequence

The following is the fixed call order. Keep the token in the environment or
secret store; it must not appear in reports, logs, fixtures, or examples.

### A. Read-only baseline

Call each request separately and record the response with a timestamp:

```text
GET /api/devices
GET /api/comfy/instances
GET /api/comfy/models
GET /api/comfy/nodes
GET /api/comfy/start-options
GET /api/comfy/installation/status
GET /api/comfy/extensions/status
GET /api/comfy/{instance_id}/multigpu
GET /api/comfy/{instance_id}/nodes/search?q={class-or-title}
GET /api/comfy/{instance_id}/logs
GET /api/assets/comfy/storage
GET /api/assets/comfy/scan
GET /api/assets/comfy
GET /api/assets/comfy/nodes
```

The aggregate asset response and exact category/list routes are authoritative;
use only the routes advertised by the live OpenAPI/typed router rather than
inventing an alias. Record Comfy commit, Manager state,
extension revisions, model category and relative filename, GPU UUID/VRAM,
current compute PIDs, WSL host memory, and the selected instance's `gpu_pool`.

If the gateway is unavailable, stop here and report the prerequisite. Do not
start a large process scan or infer health from a browser error page.

### Optional maintenance gate

Do not silently update the environment as part of a capability run. If the
selected workflow requires a newer core or extension, pause the matrix, record
the current revisions, and perform the two maintenance operations separately:

```text
POST /api/comfy/installation/update
POST /api/comfy/extensions/manage
```

The Manager request uses the typed body, for example
`{"action":"update_all","instance_id":"...","dry_run":false,
"auto_restart":true,"timeout_s":1800}`; use the live schema for the exact
instance and supported options.

Verify each operation through installation/extension status and the returned
qualification before continuing. Core update and Manager `update_all` are
distinct; never run them concurrently, and preserve the instance's GPU pool
and startup options across any restart.

### B. Discover workflows and requirements

For each family, search first and fetch only the chosen records:

```text
GET /api/workflows/search?q=krea&scope=templates&limit=30
GET /api/workflows/search?q=wan&scope=templates&limit=30
GET /api/workflows/search?q=ltx&scope=templates&limit=30
GET /api/workflows/search?q=minimax&scope=templates&limit=30
GET /api/workflow-templates/{template_id}?source={source}&package={package}
GET /api/workflows/{filename}
GET /api/workflows/{filename}/requirements
GET /api/workflows/requirements
POST /api/workflows/probe
```

Use the exact `source` and `package` returned by search. UI-format templates
must be exported/materialized as API-format graphs before analysis; a UI graph
must never be sent directly to Comfy's prompt queue.

For every selected graph, save a small manifest containing its API format,
node classes, model references, expected output type, parameter fields, and
whether it supports first/last frames, audio, LoRA, control, or reference
inputs. This manifest is the source of truth for later parameter patches.

### C. Find better/faster candidates

Search one keyword at a time and retain the raw registry result:

```text
GET /api/registry/comfy/models/search?q={term}&limit=50
GET /api/registry/comfy/manager-models/search?q={term}&limit=50
GET /api/registry/comfy/installed-models/search?q={term}&limit=50
GET /api/registry/comfy/nodes/search?q={term}&limit=50
GET /api/registry/comfy/nodes/{node_id}
GET /api/registry/comfy/models/repository
```

Terms to use, one request at a time:

```text
krea, krea2, raw, turbo, wan, wan2.2, animate, s2v,
ltx, ltx2, ltx2.3, minimax, h3, spectrum, fastwan,
lightx2v, gguf, blockswap, sageattention, taehv, cache,
upscale, control, lip, audio, infinitetalk, multitalk, talking-head
```

For each candidate record:

| Field | Required evidence |
|---|---|
| Identity | Registry ID, repository, exact revision/tag, license |
| Compatibility | Required node classes, Comfy/core range, Python/CUDA assumptions |
| Weights | Exact file, category, size, precision/quantization, checksum if exposed |
| Benefit claim | Speed, VRAM, quality, or memory behavior being tested |
| Safety | Maintainer activity, catalog status, install action, rollback path |
| Result | Analyze status, wall time, peak VRAM/RAM, output quality, regressions |

Rank candidates in this order: official/native implementation, maintained
catalog entry, pinned community optimization, then unverified discovery. A
candidate is not “better” until it wins a matched A/B case and remains within
the resource budget.

Kijai candidates are high-priority search targets, not automatic installs:

- `ComfyUI-WanVideoWrapper` for Wan-family alternative nodes and optimization
  paths (cache methods, GGUF, SageAttention, Taehv, S2V, and related helpers)
- `ComfyUI-KJNodes` for LTX/Wan helpers and preview/conditioning utilities
- `ComfyUI-WanAnimatePreprocess` where the selected Animate graph declares it
- Kijai's `WanVideo_comfy` model/LoRA repository when an exact file and
  compatible revision are returned by the model registry

Install a catalog node with `POST /api/registry/comfy/nodes/{node_id}/install`.
If a user-approved repository is not in Manager's catalog, use
`POST /api/assets/comfy/nodes/install` with its exact `repo_url` and pinned
`ref`; this does not make it part of Manager `update_all`. After the returned
job completes, restart the selected Comfy instance once and verify expected
live classes with
`GET /api/comfy/{instance_id}/nodes/search?q={class-or-title}` plus
`GET /api/comfy/{instance_id}/logs`. Install one
candidate at a time and re-run the relevant analysis.

#### Substituting a model that is not in the workflow

The API can discover a faster weight, but it cannot safely infer compatibility
from a similar filename. For every proposed substitution:

1. Read the workflow node's role, expected architecture, precision, tensor
   names/format, and declared category from `/api/workflows/probe` and
   `/api/workflows/requirements`.
2. Search installed, Manager, and configured model registries by architecture
   and role, not only by a creator name (for example, `wan 2.2 int8`,
   `lightx2v`, or `krea2 turbo`).
3. Require an exact candidate record, source revision, license, file size, and
   compatible node class. Reject a candidate when the registry does not state
   the format or the graph's loader cannot consume it.
4. Install it beside the native file under the correct Comfy category, never
   overwrite the baseline, and re-run analysis with only that node/file
   changed. A valid plan is necessary but not sufficient.
5. Run the same fixture, seed, dimensions, and steps as the native control;
   compare output semantics, resource use, speed, and cleanup. Keep the
   alternative only when the measured trade-off is favorable and reversible.

This is how Kijai's optimized weights, LoRAs, wrappers, GGUF files, and helper
nodes enter the test matrix without being mistaken for a universal drop-in.

### D. Install exact model dependencies

Prefer the install action returned by workflow analysis. Otherwise use the
typed model route, one file per job:

```text
POST /api/assets/comfy/{category}/install
POST /api/assets/comfy/{category}/install-url
```

Poll the returned `job_status_path` (or `/api/jobs/{job_id}` only when the
operation explicitly reports `status: "running"`). A client timeout is an
unknown state; inspect the job before retrying. Verify the exact final file
through the installed-model registry and the canonical path beneath
`/opt/omni_studio/comfyui/models/<category>/`, then re-analyze. Optional LoRAs,
upscalers, and helper weights are installed only after the base smoke succeeds.

### E. Start and place Comfy

Start only after the baseline and dependency gates pass:

```text
POST /api/comfy/start
{
  "device": "cuda:N",
  "gpu_pool": ["cuda:N", "cuda:M"],
  "vram_mode": "normal",
  "preview_method": "auto",
  "disable_pinned_memory": false,
  "startup_options": {}
}
```

Use `GET /api/comfy/start-options` to select advertised flags such as low
VRAM, cache policy, reserve VRAM, async offload, or preview behavior. The
primary GPU must be first in the pool. Save policies with UUIDs, not indices.

For each case, run both controls where feasible:

```json
{
  "mode": "single",
  "primary_device": "GPU-UUID",
  "eligible_devices": ["GPU-UUID"]
}
```

and:

```json
{
  "mode": "auto",
  "eligible_devices": ["GPU-PRIMARY-UUID", "GPU-AUX-UUID"],
  "primary_device": "GPU-PRIMARY-UUID",
  "reserve_mb": 1024,
  "require_all": false
}
```

Use `manual` overrides only after the automatic plan is understood. For
staged graphs, explicitly compare a policy that places conditioning on the
auxiliary and diffusion/VAE on the primary. A valid plan must still be checked
against live free VRAM immediately before queueing.

### F. Analyze, run, and verify

Analyze the unchanged API graph first:

```text
POST /api/workflows/analyze
```

Require API-format execution, `ready_to_run: true`, no missing nodes/models,
and `placement_plan.valid: true`. Confirm analysis leaves the Comfy queue
empty and does not increase model residency.

Run the exact analyzed graph with one of:

```text
POST /api/workflows/run
POST /api/workflows/{filename}/run
POST /api/workflows/{filename}/queue
```

For saved graphs, use typed `params` patches (for example
`node_id.width`, `node_id.height`, `seed`, `steps`, or `prompt`) and require
`patch_report.applied` to contain every intended change with no unmatched
fields. The server recalculates placement; never reuse an old valid plan after
changing graph parameters.

Upload images through the typed route:

```text
POST /api/comfy/{instance_id}/upload/image
```

Use the returned input name. For video/audio or other Comfy-native paths, use
the guarded `POST /api/comfy/{instance_id}/proxy/{subpath}` route only when it
is allowlisted; never copy fixtures into a guessed input directory.

Verify every output through:

```text
GET /api/outputs
GET /api/outputs/metadata/{relpath}
HEAD /api/outputs/{relpath}
GET /api/outputs/{relpath}
```

Record prompt/job ID, output path, dimensions, frames, FPS, duration, codec,
audio stream, file size, seed, embedded prompt/workflow metadata, and playback
range behavior. A successful queue response is not proof of a usable media
file.

### G. Cleanup and recovery

After each case, call the selected instance's guarded/proxied Comfy `/free` with
`unload_models=true` and `free_memory=true`; then query `/api/devices` again.
If material VRAM or model RSS remains, stop that exact instance with
`POST /api/comfy/{instance_id}/stop` and verify the process is gone before the
next family. Do not use `stop-all` during a family comparison unless the user
explicitly requests a global shutdown. Keep output files and manifests, but
remove only disposable test outputs after they have been pinned or recorded.

## Test ladder and case IDs

Every family follows the same increasing-cost ladder:

1. **Smoke:** smallest supported dimensions/duration and documented default
   steps; proves nodes, models, output, and cleanup.
2. **Canonical quality:** the official/native quality settings at the normal
   resolution for the family.
3. **One-variable A/B:** same prompt, inputs, seed, device state, and graph;
   change exactly one setting or one implementation.
4. **Scale:** raise resolution, frames/duration, or steps only after cleanup
   and recovery are proven.
5. **Optimization:** install and test one pinned alternative against the native
   winner; retain the native control.
6. **Multi-GPU:** compare single-primary, automatic pool, and a justified
   manual/staged policy. The GPU count is useful only when output and resource
   measurements improve without introducing placement or cleanup defects.

### Common axis sweep

The family tables below select representative points from this shared sweep;
the full frontier is reached only when the previous point is valid and clean:

| Axis | Tier 1 smoke | Tier 2 canonical | Tier 3 frontier (gated) |
|---|---|---|---|
| Image size | 768 square or graph minimum | 1024 square/native aspect | 1536/2048 and portrait/landscape extremes |
| Video size | 256/480p or graph minimum | 480p/720p at native FPS | 1080p/2K only when the plan and host budget fit |
| Duration | 1–3 s or 21 frames | 4–8 s or 49/73 frames | 81/121+ frames or the graph's maximum |
| Sampling | documented default | official quality setting | one-variable high-step/high-CFG comparison |
| Conditioning | text only | one reference/control/audio input | first/last, LoRA, multi-reference, or audio-driven path |
| Batch/parallelism | batch 1 | repeat same seed once warm | never use parallel heavy jobs; test repeatability sequentially |

At every point, retain a fixed-seed control and change only one axis. A failed
frontier point is useful evidence: record the planner blocker or measured
limit, then return to the last clean tier rather than trying to force it.

### Krea 2 OSS

| ID | Run |
|---|---|
| KREA-R1 | Raw native 1024 square, 52 steps, CFG 3.5, resolution-derived shift, fixed seed. |
| KREA-T1 | Turbo native 1024 square, 8-step distilled setting from the selected graph; record the local graph's CFG convention. |
| KREA-A1 | Raw vs Turbo, same prompt/seed; compare detail, text, anatomy, and wall time. |
| KREA-S1 | 768 square smoke, then 1024×1536 and 1536×1024; dimensions must satisfy the model's multiple-of-16 rule. |
| KREA-S2 | Turbo 2048 boundary only after 1024 and cleanup pass; record peak VRAM/RAM and output validity. |
| KREA-L1 | One style-reference/LoRA case after the base winner; compare with and without the LoRA. |
| KREA-G1 | Single GPU vs auto two-GPU component placement; inspect whether encoder/VAE routing actually changes residency. |

Do not copy Turbo's zero-negative conditioning or fixed flow shift into Raw.
Use the official Raw semantics and separately encoded empty unconditional
branch. A “faster” Krea candidate is accepted only if it preserves prompt and
text quality at a measured speed/memory benefit.

### Wan 2.x and Wan Animate 2

| ID | Run |
|---|---|
| WAN-N1 | Native Wan 2.2 text/image-to-video smoke, shortest supported clip, matched fixture and fixed seed. |
| WAN-N2 | Native canonical 480p/24fps; then 720p/24fps if analysis remains valid. |
| WAN-N3 | Duration ladder: 21 frames, then 49/81 frames or the graph's supported increments. |
| WAN-A1 | Wan Animate 2 matched human reference + 21-frame driver; verify identity, hands, clothing, and motion. |
| WAN-A2 | Animate 2 longer/resolution case only after WAN-A1 cleanup; retain the current qualified short baseline. |
| WAN-K1 | Native vs pinned Kijai wrapper/helper path with identical input and seed. |
| WAN-K2 | One optimization at a time: INT8/GGUF, FastWan/LightX2V LoRA, cache method, block swap, SageAttention, or Taehv when the selected graph declares it. |
| WAN-G1 | Single-primary vs auto/manual two-GPU placement; measure actual peak residency, not just planner targets. |

Use subject-matched drivers as the positive control and a deliberately
mismatched driver as a negative control. Do not claim identity preservation from
a single favorable frame. Record flicker, limbs, camera motion, and temporal
consistency.

### LTX 2/2.3

| ID | Run |
|---|---|
| LTX-N1 | Native core text-to-video smoke at the shortest supported duration. |
| LTX-N2 | Native image-to-video; verify input adherence and temporal stability. |
| LTX-F1 | First/last-frame graph with matched frames; verify both endpoint constraints. |
| LTX-Q1 | One-variable step/resolution comparison after smoke. |
| LTX-A1 | Audio+video path, then audio/video metadata and synchronization check. |
| LTX-K1 | Native core vs pinned LTXVideo/KJNodes helper path, if analysis exposes compatible classes. |
| LTX-G1 | Single GPU vs staged/two-GPU plan; include host-RAM limit and exact stop fallback. |

Treat LTX2.3 as a large-host stress case: the local checkpoint plus encoder
can exceed the current 32 GB WSL budget before activation overhead. Do not
download or queue it merely to prove that first/last-frame animation exists;
require a materialized API graph, a valid plan, and the shortest supported
case first. Pin LTX helper revisions because node updates can change audio or
preview behavior.

### MiniMax-H3 local

| ID | Run |
|---|---|
| H3-N1 | Native staged FL2V/Ref2VA smoke with the current INT8 model, fixed seed 424242, and existing matched fixtures. |
| H3-N2 | Native canonical 352×608, 73 frames, 24 fps; record generation and decode separately. |
| H3-R1 | Visual reference strength 0.999, 0.9, 0.7; exploratory 0.5 only after returning to baseline and recording final-frame behavior. |
| H3-R2 | Hold seed/reference/prompt fixed and measure identity, composition, motion, final-frame adherence, and generated audio changes. |
| H3-S1 | Native vs Spectrum H3 at identical inputs; start video blend 0.5, audio blend 0.0, offline replay enabled, RAM history/archive. |
| H3-S2 | Spectrum Turbo sampler only when the installed version and node class are explicitly discovered; compare speed and quality separately. |
| H3-Q1 | Scale one axis at a time: 448×256, 608×352, then longer frame windows; stop at the first invalid plan or memory boundary. |
| H3-G1 | Single GPU vs staged ClipProj/text on auxiliary and diffusion/VAE on primary. Confirm stage unload before the next stage. |

Visual reference strength and Spectrum are separate experiments. Return to
native `0.999` before enabling Spectrum so reference-noise and approximation
effects are not conflated. Audio reference strength remains its own recorded
field; do not assume that holding a seed holds the generated soundtrack.

## Audio, long-form TTS, ACE-Step, and lip-sync extension

This is an additional cross-tool track. It uses the app's local MOSS-TTS and
ACE-Step APIs as controls, then feeds their persisted audio into ComfyUI
lip-sync/audio-driven candidates. It does not make the Comfy process perform
TTS, and it does not assume that an audio worker can be layer-sharded across
GPUs. Each worker gets an explicit current `cuda:N`; workers are run
sequentially and cleaned up before the next heavy family.

### Audio API sequence

Use these cheap reads before loading anything:

```text
GET /api/devices
GET /api/workers
GET /api/ace_step/status
GET /api/ace_step/state?autospawn=false
GET /api/setup/status
```

For MOSS-TTS, generate binary output through the existing typed route and
record the output headers/URL without printing the token:

```text
POST /api/tts/moss_tts
{
  "text": "...",
  "response_format": "wav",
  "speed": 1.0,
  "autospawn": false,
  "device": "cuda:N"
}
```

The TTS response is binary, not JSON. Save the WAV and retain its
`X-Omni-Output-URL`/output reference. There is no reason to invent a duration
field: test longer text, and if a single request becomes unreliable, split at
sentence boundaries and use the existing composition route:

```text
POST /api/outputs/audio/compose
```

For ACE-Step, explicitly load and verify state, then make one synchronous
generation at a time:

```text
POST /api/ace_step/load
GET /api/ace_step/state?autospawn=false
POST /api/ace_step/generate
GET /api/ace_step/outputs/{job_id}
```

Use `duration_s: 60` for the requested one-minute songs. The persisted ACE
`job_id` is an output identifier, not a background job to poll; only install
or composition calls with `status: "running"` use `GET /api/jobs/{job_id}`.
For an extended instrumental, make compatible sections and compose two to 64
library files with phrase-aligned trims and a moderate crossfade. Do not use a
large ranked fanout while the host is under load.

### Audio case matrix

| ID | Run |
|---|---|
| TTS-S1 | MOSS-TTS short speech, approximately 15 seconds, WAV, fixed script and explicit larger-GPU device. |
| TTS-S2 | Same script family at approximately 30 seconds; compare speed 0.8/1.0/1.2 one at a time. |
| TTS-L1 | Approximately 60-second single-request narration; record timeout, duration drift, pauses, sample rate, and cleanup. |
| TTS-L2 | If TTS-L1 is unstable, generate sentence/paragraph chunks and compose them into a 60-second master; verify boundary clicks and timing. |
| TTS-V1 | One multilingual or voice-style case only after the English control; record language and pronunciation rather than assuming it transfers. |
| ACE-I1 | 60-second minimal techno instrumental, fixed seed, no lyrics. |
| ACE-I2 | 60-second psychedelic/trippy instrumental, fixed seed, no lyrics; compare groove continuity with ACE-I1. |
| ACE-V1 | 60-second vocal song with structured lyrics, fixed seed, documented 8-step turbo settings. |
| ACE-V2 | 60-second contrasting vocal style (for example chant, synthpop, or harder electronic); inspect lyric alignment and artifacts. |
| ACE-X1 | TTS output through ACE `vocal2bgm` or `a2a`, preserving the input URL and comparing the returned soundtrack. |
| ACE-L1 | Two compatible 60-second instrumentals composed to two or more minutes; compare direct continuation/extend against exact crossfade composition. |
| LS-T1 | TTS speech drives a frontal portrait/talking-head clip; begin with 10-15 seconds and verify mouth onset/phoneme alignment. |
| LS-T2 | The same portrait and a 60-second TTS track; test whether the method loops, pads, or requires chunked video. |
| LS-A1 | ACE vocal song drives the same portrait; evaluate singing mouth motion separately from spoken phonemes. |
| LS-A2 | ACE instrumental as a negative control: confirm that the pipeline does not claim speech lip-sync without vocal audio. |
| LS-C1 | Native Wan/LTX/H3 audio-driven path, if the selected graph declares an audio encoder. |
| LS-C2 | One pinned post-process method (LatentSync, MuseTalk, FLOAT, MOVA, or NAVA) against the native control. |
| LS-I1 | InfiniteTalk image + 10-15 second TTS speech smoke; verify audio-driven face, head, and identity preservation. |
| LS-I2 | InfiniteTalk video + replacement TTS speech; compare video-to-video lip-sync against the original motion/background. |
| LS-I3 | InfiniteTalk 30-second then 60-second speech/singing case; inspect chunk boundaries, color/identity drift, and audio alignment before any longer run. |

Lip-sync verification records output duration, FPS, audio duration, initial
offset, dropped/padded frames, mouth movement onset, phoneme alignment,
expression stability, face identity, and visual artifacts. A video with an
attached audio stream is not automatically lip-synced. Use the same speech
and singing files across candidates, and retain a short negative-control clip.

### Cross-tool GPU and cleanup policy

- Load MOSS-TTS on the explicitly selected GPU with enough free VRAM; the
  smaller auxiliary card is not assumed to fit it.
- Load ACE-Step on the other selected GPU only after TTS is unloaded, or run it
  after deleting the exact TTS worker. This measures independent-worker
  handoff, not unsupported cross-worker model sharding.
- Feed the returned library URL or an API-uploaded audio name into the
  analyzed Comfy graph. Use the typed image/mask upload routes and the guarded
  Comfy proxy for video/audio only when the subpath is allowlisted.
- After every TTS/ACE case: unload ACE components with
  `POST /api/ace_step/unload` when applicable, delete the exact audio worker
  via `DELETE /api/workers/{worker_id}`, then re-read `/api/workers` and
  `/api/devices`. Do not use `kill-all` or stop Comfy for audio cleanup.
- Keep ACE generation synchronous and set a long client timeout. If it times
  out, inspect worker state and persisted outputs before considering a retry.

### Community contribution discovery beyond Kijai

These are research candidates found outside the native app path. They are not
automatic installs. At execution time, search the configured registry one term
at a time and retain the exact catalog record, revision, license, dependencies,
node classes, model locations, and rollback path:

```text
GET /api/registry/comfy/nodes/search?q=ace-step&limit=50
GET /api/registry/comfy/nodes/search?q=latentsync&limit=50
GET /api/registry/comfy/nodes/search?q=musetalk&limit=50
GET /api/registry/comfy/nodes/search?q=float&limit=50
GET /api/registry/comfy/nodes/search?q=mova&limit=50
GET /api/registry/comfy/nodes/search?q=nava&limit=50
GET /api/registry/comfy/nodes/search?q=qwen3-tts&limit=50
GET /api/registry/comfy/nodes/search?q=moss-tts&limit=50
GET /api/registry/comfy/nodes/search?q=audio-driven&limit=50
GET /api/registry/comfy/nodes/search?q=wav2lip&limit=50
GET /api/registry/comfy/nodes/search?q=heartmula&limit=50
```

| Candidate | Intended experiment | Initial treatment |
|---|---|---|
| `ace-step/ACE-Step-ComfyUI` | Official/community Comfy nodes for text-to-music, cover/remix, repaint, and sample mode; verify whether the selected mode is local rather than cloud. | Native-adjacent comparison; do not expose an API key or use cloud mode. |
| `hiroki-abe-58/ComfyUI-AceMusic` | Full ACE-Step node suite, multilingual lyrics, cover/remix, and HeartMuLa interoperability. | Pinned candidate after the app ACE control; inspect dependency conflicts. |
| `audiohacking/acestep-cpp-comfyui` | GGUF/C++ ACE-Step path on CPU/CUDA/other backends; possible low-VRAM speed/memory alternative. | Low-priority frontier candidate; measure quality loss and build cost. |
| `filliptm/ComfyUI-FL-AceStep-Training` | Dataset/LoRA training path for personal style or voice. | Separate training lane; never run during inference qualification. |
| `jiafuzeng/comfyui-LatentSync` or `iVideoGameBoss/ComfyUI-LatentSync-Node` | Audio-conditioned post-process lip-sync; the latter documents 25 FPS/frontal-face limits and about 6.5 GB VRAM. | Strong first post-process candidate; pin Python/runtime compatibility. |
| `AIFSH/ComfyUI-MuseTalk_FSH` | Audio-driven face animation through MuseTalk. | Compare on the same portrait and speech; inspect face-mask and identity artifacts. |
| `KERRY-YUAN/ComfyUI_Float_Animator` | Still portrait plus audio to talking portrait, with emotion controls. | Candidate for speech and singing; verify model/license/face-size requirements. |
| `richservo/comfyui-mova` | Synchronized video+audio generation with pre-generated audio, auto-duration, and audio strength. | Large frontier candidate; only after a short native audio-driven smoke. |
| `ernie-research/NAVA` (`comfyui_nava`) | Native audio-visual alignment/generation path. | Discovery candidate; require a valid API graph and exact weights. |
| `MeiGen-AI/InfiniteTalk` plus its ComfyUI branch/native `WanInfiniteTalkToVideo` node | Audio-driven image-to-video and video-to-video dubbing, including synchronized facial/head/body motion and long-duration chunked generation. | Highest-priority audio-driven frontier candidate; start at 10-15 seconds, then 30/60 seconds. Do not assume its upstream multi-GPU TODO is implemented. |
| `xuhongming251/ComfyUI-InfiniteTalk-MultiImage` | Community multi-image InfiniteTalk workflow for longer or multi-shot sequences. | Optional workflow candidate after the single-image native control; validate continuity and exact model requirements. |
| `Biyikgokhan/ComfyUI-InfiniTalk-AutoScale` | Community node that attempts to scale InfiniteTalk processing to arbitrary audio length. | Experimental only; compare against manual chunking and reject if it hides seams or cannot preserve motion context. |
| `DarioFT/ComfyUI-Qwen3-TTS` or `1038lab/ComfyUI-QwenTTS` | Alternative local TTS, voice design, cloning, and multilingual speech. | Compare only after MOSS baseline; pin `transformers`/attention dependencies and do not let it silently downgrade Comfy. |
| `richservo/comfyui-moss-tts` | Community MOSS-TTS, voice cloning/design, dialogue, and SFX nodes. | Optional node-level comparison; app MOSS API remains the control. |
| `diodiogod/TTS-Audio-Suite` | Multi-engine TTS/voice conversion and subtitle/timing helpers. | Discovery-only initially; broad dependencies and many engines make it a later isolated candidate. |
| ACE-Step reference-audio nodes shared by `SkriptorL` | Reference-timbre/cover experimentation. | Archive/reproduction candidate; only use if the registry supplies a maintained/pinned package. |

The acceptance rule is the same as for Kijai: an alternative must pass
analysis, produce the same requested modality, preserve or improve quality,
show a measured resource or speed benefit, and cleanly detach. A repository
with unresolved dependency issues, a moving branch, or only a cloud endpoint is
documented and rejected rather than installed into the baseline environment.

InfiniteTalk receives an additional boundary check. The upstream project calls
its duration capability unlimited, but local memory, context-window, and seam
behavior still have to be measured. The plan therefore compares direct short
generation, explicit overlapping chunks, and any auto-scale node separately.
The `WanInfiniteTalkToVideo` graph must declare its audio encoder and exact
base/InfiniteTalk weights before it can be analyzed; a talking-video workflow
with an attached audio stream but no audio conditioning is not a valid result.

### Audio-specific deliverables

Add these to the main ledger and leaderboard:

- speech and song source hashes, lyrics/script, language, voice/style, seed,
  duration request, actual duration, sample rate, channels, and output URL;
- ACE model/LM/LoRA state, scheduler, steps, CFG, prompt, lyrics, and whether
  the result was direct, extended, remixed, or composed;
- video input/output duration and FPS, audio/video offset, mouth/phoneme and
  singing alignment notes, face identity, and any padding/looping;
- TTS/ACE worker GPU, cold/warm load time, generation time, peak VRAM/RAM,
  exact worker deletion result, and post-cleanup device telemetry;
- candidate repository and commit, node classes, dependency changes, and a
  native-vs-community decision with rollback instructions.

## Quality, performance, and reliability measurements

Record one JSON row and one human-readable note per case. At minimum:

- prompt, negative prompt/structured caption, input fixture hashes, seed,
  graph/template ID, model filenames, node/extension revisions, and policy;
- width, height, frames, FPS, duration, steps, sampler/scheduler, CFG/shift,
  LoRA/reference settings, precision, cache/offload flags, and startup options;
- cold model-load time, warm inference time, decode/encode time, total wall
  time, queue latency, cancellation behavior, and retry behavior;
- per-GPU baseline/peak/free VRAM, process RSS, WSL available memory, and
  whether cleanup returned to baseline;
- output dimensions, frame count, codec, audio sample rate/channels, file
  size, metadata presence, HTTP range/playback result, and output path;
- visual scores: prompt adherence, subject identity, composition, anatomy,
  detail, typography, temporal flicker, motion quality, and first/last-frame
  adherence;
- audio scores where present: stream exists, duration alignment, speech or
  singing intelligibility, sync, artifacts, and whether audio was copied or
  generated;
- classification: `omni`, `comfy-core`, `custom-node`, `model/upstream`,
  `environment`, or expected trade-off.

The primary leaderboard is a Pareto table, not a single “winner”: quality vs
wall time, peak VRAM, host RAM, output size, and cleanup reliability. Publish
three recommendations per family when supported: **safe**, **balanced**, and
**frontier**.

## Stop and failure rules

- Stop a case immediately on an invalid analysis plan, missing dependency,
  unexpected GPU assignment, allocator/OOM warning, runaway host RAM, or a
  non-cooperative worker. Save the response and classify it.
- Never resubmit after a timeout until queue/history/job state has been read.
- Never start a second heavyweight download or inference while one is active.
- If `/free` fails to release material residency, stop the exact instance and
  mark the cleanup result; do not call it a leak until the instance boundary is
  tested.
- A candidate that changes output semantics, loses audio, breaks metadata,
  or cannot be cleanly rolled back is not an optimization win.
- Keep source-probe notes separate from runtime results. Upstream issues are
  documented with file/class/revision and reproduction; they are not silently
  patched during the scan.

## Deliverables

The completed scan will leave:

1. A dated baseline and dependency manifest.
2. Per-case API request/response ledger (with secrets redacted), output paths,
   media metadata, and GPU/RAM telemetry.
3. Native-vs-optimized A/B tables, including exact repository revisions and
   rollback instructions.
4. A model/node discovery report showing which registry searches found useful
   candidates and which were rejected, with reasons.
5. Safe/balanced/frontier presets for Krea, Wan, LTX, and H3, including GPU
   policy and cleanup procedure.
6. A defect report split between Omni-owned fixes and upstream/core/custom-node
   findings. Omni fixes receive focused regression tests; upstream code is not
   modified by this probe.
7. A final API coverage note confirming which existing routes were exercised;
   no endpoint is added unless a genuinely missing, fundamental contract is
   discovered and separately approved.

## Research anchors used for candidate ranking

These links are discovery anchors; execution still resolves the current
registry records and pins revisions at test time:

- Krea's official repository documents Raw as the base model and Turbo as the
  distilled eight-step model, with Raw quality settings and resolution-derived
  flow shift: <https://github.com/krea-ai/krea-2>
- Wan's official repository documents the Wan 2.2 family, including the 5B
  text/image-to-video model and Wan2.2-Animate-14B:
  <https://github.com/Wan-Video/Wan2.2>
- Lightricks' LTX2 repository and Comfy extension are the native references for
  LTX2/2.3 workflows: <https://github.com/Lightricks/LTX-2> and
  <https://github.com/Lightricks/ComfyUI-LTXVideo>
- Kijai's Wan wrapper is a candidate source for alternative Wan nodes and
  memory/speed paths, not an automatic upgrade:
  <https://github.com/kijai/ComfyUI-WanVideoWrapper>
- Spectrum H3 is approximate and should be compared against native H3 with its
  documented replay/blend settings and bounded-replay version:
  <https://github.com/xmarre/ComfyUI-Spectrum-MiniMax-H3>
- InfiniteTalk's upstream project describes audio-driven image/video dubbing,
  long-duration generation, and a ComfyUI branch; its own roadmap must be
  checked before assuming multi-GPU or acceleration support:
  <https://github.com/MeiGen-AI/InfiniteTalk>
- Community audio candidates include ACE-Step Comfy integrations,
  LatentSync/MuseTalk/FLOAT lip-sync nodes, MOVA/NAVA audio-visual workflows,
  and Qwen3-TTS/MOSS-TTS node suites. Their exact revisions and dependencies
  are discovered and pinned through the registry rather than copied from a
  moving README.
