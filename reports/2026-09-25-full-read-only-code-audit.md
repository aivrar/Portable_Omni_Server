# Omni Studio full read-only code audit — 2026-09-25

## Scope and evidence standard

This is a static audit of the `Omni_Studio` child app only: bridge and gateway,
typed routers, jobs and maintenance, ComfyUI orchestration and custom nodes,
standalone and audio workers, CLI, setup scripts, browser UI, tests, manuals,
skills, and source packaging. The parent template and installed WSL runtime
were not modified or exercised. The only file created for this request is this
report. No API calls, model loads, workflow queues, executable tests, process
signals, downloads, or source edits were made.

At the start of this pass, Git listed 390 eligible source/fixture files. A
whole-tree syntax pass parsed 133 Python files and checked 15 browser JavaScript
files with no syntax failures. A local-link check covered 69 Markdown files
and found no missing target files. Ruff `F,B` ran with `--no-cache`; its 160
findings are mostly exception chaining, redundant f-strings, and test/style
rules, so they are not presented as 160 functional defects. Static checks do
not establish runtime correctness. The broad interactive test suite was not
run: the prior repository audit documented its process-lifecycle hazard.

**Severity:** High means an authorization, process, data, or resource boundary
can fail, or a core operation is blocked. Medium means a real contract or
state bug with narrower triggers. Low means a cleanup or qualification gap.
“Confirmed” below means the cited control flow or contract mismatch is visible
in source. The stated runtime consequences remain conditional unless a past
report already contains live evidence; this pass did not reproduce them live.

## High-priority findings

### H1. The Windows ingress relay can bypass loopback authentication

[bridge.py](../bridge.py#L248) forwards raw connections from a server bound to
the WSL non-loopback address ([binding](../bridge.py#L334)) to the loopback
bridge. The gateway permits loopback clients to bootstrap the API token from
`/api/session` ([middleware](../server/omni_comfy_server.py#L551),
[route](../server/routers/sessions.py#L194)). A client able to reach the WSL
ingress address can therefore obtain the token and use authenticated routes.
Network reachability beyond the Windows host depends on the WSL network mode
and was not probed. **Direction:** authenticate the ingress before forwarding
or constrain it to an operating-system boundary that cannot be reached by
untrusted peers; test the effective peer address seen by `/api/session`.

### H2. Shutdown and orphan cleanup trust stale PID records

[omni_shutdown.py](../server/omni_shutdown.py#L165) and its gateway-pidfile
branch ([line 196](../server/omni_shutdown.py#L196)) use recorded PIDs/PGIDs
after liveness checks, then `_kill_pid_tree` signals the group
([line 103](../server/omni_shutdown.py#L103)). The bridge fallback also trusts
`gateway.pid` ([bridge.py](../bridge.py#L1314)). Separately,
[comfy_manager.py](../server/comfy_manager.py#L622) checks only that a recorded
PID exists before killing its group; it does not use its available Comfy
command-identity matcher. Recovery can leave a nonmatching live-PID record in
place ([line 230](../server/comfy_manager.py#L230)). After PID reuse or a
corrupt/stale record, normal shutdown or scheduled PID cleanup can signal an
unrelated process group. **Direction:** verify command line, app instance,
process start time, and group identity immediately before any signal; discard
records that fail ownership checks. No process was signalled during this audit.

### H3. Busy audio workers cannot be cancelled and may be duplicated

ACE-Step and Audio Lab mark an inference worker `busy`
([ACE](../server/routers/ace_step.py#L200),
[Audio Lab](../server/routers/audio_lab.py#L198)), while their worker resolvers
select `ready` or `starting/loading`, never `busy`
([ACE](../server/routers/ace_step.py#L144),
[Audio Lab](../server/routers/audio_lab.py#L154)). Their Cancel routes call
those resolvers with autospawn disabled
([ACE](../server/routers/ace_step.py#L625),
[Audio Lab](../server/routers/audio_lab.py#L462)), so a cancel request during
active generation reports no worker instead of reaching its cancel endpoint.
The cheap state routes also report `running: false` while inference is busy
([ACE](../server/routers/ace_step.py#L489),
[Audio Lab](../server/routers/audio_lab.py#L356)). Conversely, generation
resolves the worker before taking the inference lock, with autospawn enabled
by default ([ACE](../server/routers/ace_step.py#L882),
[Audio Lab](../server/routers/audio_lab.py#L542)); overlapping requests can
spawn duplicate heavyweight workers and exhaust VRAM. **Direction:** identify
the exact live worker across `ready` and `busy`, serialize admission before
autospawn, and keep cancel/state paths independent of ready-only selection.

### H4. Comfy placement can approve a plan that execution cannot follow

The planner represents the LTX transformer and duration head as separately
placeable components bound to one node `device` input, and the spatial
upscaler and VAE as separate components bound to one `upscale_device` input
([comfy_placement.py](../server/comfy_placement.py#L528)). It can assign the
pair to different GPUs, but `apply_placement_plan` writes the shared input
once for each assignment; the last value wins
([line 1131](../server/comfy_placement.py#L1131)). The node uses one device
for each pair ([omni_staged_ltx.py](../comfy_nodes/omni_bridge/nodes/omni_staged_ltx.py#L98)). A `valid: true` plan can therefore undercount one GPU's actual peak and misstate `require_all`. **Direction:** treat each shared input as one
indivisible placement unit in analysis and in the returned plan.

### H5. Explicit staged CPU placement bypasses the host-memory gate

The Comfy planner gives an explicit CPU target effectively unlimited capacity
([comfy_placement.py](../server/comfy_placement.py#L875)). Its bounded CPU
offload calculation counts GPU overflow, not a component directly assigned to
CPU ([line 1000](../server/comfy_placement.py#L1000)); an all-staged graph
also skips the aggregate host-weight check
([line 1012](../server/comfy_placement.py#L1012)). A large staged component
overridden to CPU can return `valid: true` without fitting the workload cgroup
or host RAM. **Direction:** count direct CPU assignments in a current-memory
budget and keep `valid: false` as a hard stop when they exceed it.

### H6. Chat session switching can lose the explicit worker cancel

[tab-chat.js](../server/static/tab-chat.js#L591) aborts the stream and clears
`currentSession` immediately. The asynchronous abort callback only sends the
cancel request if `currentSession` still exists and derives the model from that
mutable session ([line 747](../server/static/tab-chat.js#L747)). Worker stream
generation runs in a producer thread that stops on an abort flag; generator
close only joins briefly and does not set that flag
([omni_worker.py](../server/omni_worker.py#L1020),
[line 1329](../server/omni_worker.py#L1329)). Switching sessions can therefore
skip or misdirect explicit cancellation. Continued GPU work after disconnect
is a source-supported risk, not live verified here. **Direction:** retain the
original model/job ID in the stream closure and send cancellation before
clearing or replacing session state.

### H7. Scheduled Hub cleanup ignores its claimed installed-reference guard

`run_prune_hub` says it removes revisions “not referenced by installed
models,” but selects **every** cache revision older than `stale_days` without
checking installed references
([maintenance.py](../server/maintenance.py#L282)). The default is seven days,
and the scheduler enqueues due tasks, including this one, from policy
([maintenance.py](../server/maintenance.py#L55),
[scheduler.py](../server/scheduler.py#L62)). Current installers often
materialize separate model copies, so this does not prove that installed
weights will be deleted. It can still erase cache revisions needed for
offline/repeated use and violates the stated protection. There is no focused
prune-hub test in `tests/test_maintenance.py`. **Direction:** establish and
test the reference rule, or describe the operation honestly as age-only cache
eviction; make destructive policy explicit.

## Medium-priority findings

### M1. Comfy recovery can leave a healthy instance stuck in `starting`

On gateway recovery, one failed three-second readiness probe registers a
matching live Comfy process as `starting`
([comfy_manager.py](../server/comfy_manager.py#L245)). The health loop skips
every `starting` instance ([line 719](../server/comfy_manager.py#L719)), so
there is no later promotion if Comfy becomes healthy. **Direction:** health
check adopted starting processes until ready or a bounded failure timeout.

### M2. Workflow preflight can queue a graph after node verification fails

Failure to fetch Comfy `/object_info` yields no node catalog
([workflows.py](../server/routers/workflows.py#L955)) and an analysis result
of `readiness="check-nodes"`, `ready_to_run=False`
([line 826](../server/routers/workflows.py#L826)). The run preflight blocks
other nonready states but omits `check-nodes`
([line 1114](../server/routers/workflows.py#L1114)), so it still submits the
graph. The catalog cache is keyed only by instance ID/port for 30 seconds,
which can also give stale results after a quick same-port restart. **Direction:**
gate on `ready_to_run` and bind cached catalogs to the process/instance
generation.

### M3. Staged VAE decode is not the guaranteed unload boundary claimed

[omni_staged_vae.py](../comfy_nodes/omni_bridge/nodes/omni_staged_vae.py#L24)
calls `_release_memory(())`, which runs collection and CUDA cache release but
does not unload upstream patchers or Comfy models
([omni_staged_h3.py](../comfy_nodes/omni_bridge/nodes/omni_staged_h3.py#L91)).
The planner nevertheless treats upstream loaders as a separate stage and
waives the aggregate host-weight gate for all-staged graphs
([comfy_placement.py](../server/comfy_placement.py#L466),
[line 1012](../server/comfy_placement.py#L1012)). The Comfy guide also
describes an unload boundary. **Direction:** qualify and implement the
specific unload guarantee or weaken admission/documentation to the behavior
the node actually provides.

### M4. Comfy asset inventory can report missing weights as installed

The inventory includes zero-byte weight files and recursive basenames
([assets.py](../server/routers/assets.py#L443)). A graph naming bare
`foo.safetensors` can pass analysis when only
`subfolder/foo.safetensors` exists, although Comfy expects the relative
subfolder path; an interrupted empty file can also look present
([line 632](../server/routers/assets.py#L632)). Separately, Manager catalog
matching accepts any same-basename file in a category as installed
([registry.py](../server/routers/registry.py#L410)). **Direction:** require
positive-size exact category-relative names and distinguish catalog identity
from filename coincidence.

### M5. Whole-repository Comfy install writes into a shared model category

The asset route permits `file=None`
([assets.py](../server/routers/assets.py#L1402)); the installer then downloads
the entire Hugging Face snapshot directly into
`comfyui/models/<category>` without a repository subfolder or staging
([install_model.sh](../server/install_model.sh#L823)). Its free-space check is
a fixed 5 GB, unrelated to repository size
([line 779](../server/install_model.sh#L779)). This can collide with existing
filenames or fill the disk. **Direction:** require an exact file or a bounded
manifest/size plan and stage a repository-scoped directory before commit.

### M6. Custom-node repository identity is reduced to its basename

[install_model.sh](../server/install_model.sh#L1295) derives the destination
from the repository basename. If that directory already has `.git`, it fetches
its existing origin without comparing it with the newly requested URL
([line 1300](../server/install_model.sh#L1300)), then reports success. Two
distinct repositories sharing a basename can install/update the wrong node.
**Direction:** verify canonical origin identity or reject the collision.

### M7. Comfy may start outside the workload cgroup while placement assumes it

Comfy startup logs a warning but proceeds when workload-cgroup placement
returns `applied:false` ([comfy_manager.py](../server/comfy_manager.py#L422)).
Documented host-memory admission assumes the workload child and its gateway
reserve. **Direction:** make degraded isolation an explicit blocker or
recalculate admission using actual unisolated limits. The consequences depend
on the host's cgroup configuration.

### M8. Audio/model compatibility routes have mismatched contracts

The speech-to-text gateway accepts up to 50 MiB of raw audio
([audio.py](../server/routers/audio.py#L59)), while the worker's base64 audio
field is capped at 20 MiB ([omni_worker.py](../server/omni_worker.py#L1221));
roughly 15–50 MiB uploads can be accepted and archived before 422 rejection.
`/v1/audio/transcriptions` defaults to `gpt-3.5-turbo`
([audio.py](../server/routers/audio.py#L203)), aliased to Qwen 3B
([config.py](../server/config.py#L688)), whose audio handler returns 501
([omni_worker.py](../server/omni_worker.py#L821)). Finally, OpenAI speech
defaults to MP3 ([audio.py](../server/routers/audio.py#L237)), while the MOSS
implementation produces WAV ([moss_tts_loaders.py](../server/moss_tts_loaders.py#L263));
that mismatch becomes observable if fresh MOSS-TTS inference works again.
**Direction:** align accepted sizes, choose a working default or require an
explicit model, and reject or transcode unsupported output formats.

### M9. Audio asset deletion and loading lack active-operation coordination

Audio Lab and ACE model/LM/VAE/LoRA deletion routes remove directories without
checking a live worker or install job
([setup.py](../server/routers/setup.py#L1408),
[line 1631](../server/routers/setup.py#L1631)). Deletion during use or install
can leave files/state inconsistent. Audio Lab's multi-component load path has
no transaction lock ([audio_lab.py](../server/routers/audio_lab.py#L409));
ACE-Step has one ([ace_step.py](../server/routers/ace_step.py#L560)). Overlapping
SA/CLAP loads can interleave. **Direction:** refuse deletion of an active
asset/install target and serialize component load transactions.

### M10. Audio Lab persists source base64 in output manifests

A2A, inpaint, and VAE output manifests serialize the full request, including
large inline audio/latent fields
([audio_lab.py](../server/routers/audio_lab.py#L959)). ACE-Step excludes its
equivalent source payloads ([ace_step.py](../server/routers/ace_step.py#L800)).
This duplicates media on disk, makes manifests unexpectedly large, and can
retain source content beyond the output's intended lifetime. **Direction:**
persist bounded settings and source references, excluding inline media.

### M11. Media metadata keys omit the output kind

The metadata API accepts `kind` but stores and looks up records by only the
relative path ([outputs.py](../server/routers/outputs.py#L688),
[line 710](../server/routers/outputs.py#L710),
[output_meta.py](../server/output_meta.py#L57)). `output`, `input`, `temp`, and
`omni` roots can contain the same relative filename. Tags, notes, collections,
and pins then bleed between different files; unpinning one can remove another
file's prune protection ([outputs.py](../server/routers/outputs.py#L1304)).
**Direction:** include `kind` in persisted identity and migrate existing
records carefully.

### M12. Direct output pruning reports unsuccessful deletions as successful

`POST /api/outputs/prune` appends a deletion and subtracts its size before
calling `unlink`; it swallows an `OSError`
([outputs.py](../server/routers/outputs.py#L1337)). Its returned `deleted`
and `freed_bytes` can therefore claim success and an achieved cap when files
remain. The scheduled prune implementation updates counts only after a
successful unlink ([maintenance.py](../server/maintenance.py#L164)). **Direction:**
record each deletion only after success and surface failures separately.

### M13. Job cancellation can leave a child process group running

Subprocess jobs start a new session and read inherited stdout
([jobs.py](../server/jobs.py#L340)). If the parent exits while a child keeps
the pipe open, the read loop can time out, but both the exception path and
`_kill_proc_tree` skip group termination once the parent `returncode` is set
([line 350](../server/jobs.py#L350),
[line 399](../server/jobs.py#L399),
[line 572](../server/jobs.py#L572)). The job can end in error/cancelled while
a child keeps running. Current tests cover live-parent cancellation, not this
exit-with-descendants case. **Direction:** track the owned PGID independently
and verify/terminate remaining group members without trusting only parent
returncode. This is a conditional source-supported risk, not a live finding.

### M14. Token rotation breaks browser and CLI recovery

Gateway auth returns 403 for an invalid token
([omni_comfy_server.py](../server/omni_comfy_server.py#L571)); the browser
clears its cached token only on 401
([app.js](../server/static/app.js#L49)). After a real token reset/rotation,
polling reuses the stale header instead of re-bootstrapping. The CLI resolves
the user token file before the runtime token or session endpoint
([client.py](../cli/client.py#L57)), and `session refresh` resaves that same
resolved value ([omni.py](../cli/omni.py#L81)). Ordinary gateway restarts
normally preserve the token; reset/rotation is the trigger. **Direction:**
handle the gateway's 403 recovery case and make refresh bypass cached tokens.

### M15. Periodic Setup rendering discards partly entered form values

The app rerenders active Setup every eight seconds
([app.js](../server/static/app.js#L418)). `TabSetup.render()` replaces its
whole DOM, including token and LoRA fields
([tab-setup.js](../server/static/tab-setup.js#L13)); `preserveFocus` restores
only the focused control ([app.js](../server/static/app.js#L303)). Other
entered values disappear during normal polling. **Direction:** update status
regions without rebuilding forms or retain all draft form state.

### M16. Workflow UI can label the wrong GPU as Comfy primary

[tab-workflows.js](../server/static/tab-workflows.js#L67) filters the global
device list by pool membership without preserving `instance.gpu_pool` order.
It labels the first result `primary` and defaults to that device
([line 236](../server/static/tab-workflows.js#L236)). For a pool ordered
`[cuda:1,cuda:0]` and a global list ordered `[cuda:0,cuda:1]`, it labels and
saves the wrong default primary. **Direction:** order from the instance pool
and map to current stable UUIDs.

### M17. Setup can silently substitute an unintended Comfy/Manager ref

`checkout_git_ref` falls back from a failed requested fetch to `origin`, then
from a failed requested checkout to `FETCH_HEAD`
([setup.sh](../server/setup.sh#L315)). Only 40-character SHA overrides are
checked against final `HEAD` ([line 344](../server/setup.sh#L344)). A typo or
unavailable branch/tag override can install the default branch while setup
reports success. **Direction:** require the requested named ref to resolve
and verify the final commit/ref for both default and override paths.

### M18. Venv repair chooses a CUDA wheel without detecting the host

Repair mode skips system/GPU detection
([setup.sh](../server/setup.sh#L358)), then defaults `CUDA_WHEEL` to `cu126`
([line 449](../server/setup.sh#L449)) if PyTorch needs reinstall. A CPU-only
or older CUDA host may receive an inappropriate wheel. **Direction:** retain
the installed wheel index or run the bounded CUDA probe during repair before
installing Torch. This affects repairs that actually reinstall Torch.

### M19. Documented Comfy core-update payloads fail validation

The typed core-update route requires a nonempty `ref`
([comfy.py](../server/routers/comfy.py#L517));
[docs/comfy-api.md](../docs/comfy-api.md) omits it in its example and the
[lifecycle skill](../skills/omni-comfy-api/references/lifecycle-and-updates.md)
shows `"ref": null`. Following either guide returns HTTP 400. **Direction:**
show an explicit exact ref or the route's supported `latest` value.

## Lower-priority and release-qualification gaps

- [tab-setup.js](../server/static/tab-setup.js#L114) retains an unreferenced
  `_renderLegacy` function. It appears safe to remove in a separate cleanup
  change after confirming no dynamic caller.
- [setup.sh](../server/setup.sh#L302) takes its fast-start exit before the
  OmniBridge copy later in that script. A new Comfy start refreshes the bridge
  through [comfy_manager.py](../server/comfy_manager.py#L192), so this is
  chiefly a misleading “every setup run” comment and a reminder that an
  already-running Comfy process needs a deliberate restart for node-code
  changes.
- [tab-workflows.js](../server/static/tab-workflows.js#L603) starts all
  verified model downloads together, while the capability runbook asks for
  bounded sequential heavy downloads. Concurrent transfers can raise disk
  and memory pressure; no current live saturation was measured.
- Generic variant/status checks can call a model installed when any weight
  file exists, without the install-complete/snapshot-integrity gate
  ([setup.py](../server/routers/setup.py#L496),
  [line 1182](../server/routers/setup.py#L1182)). This mainly affects partial
  legacy or externally copied trees. Add a partial-install regression.
- The [app manifest](../app.json#L5) searches only the original
  `/mnt/*/linux/template/app/apps/Omni_Studio/server/setup.sh` location. A
  standalone GitHub clone in another directory cannot bootstrap through this
  manifest. The root README already describes the source/package boundary;
  this remains a release packaging gap, not a failure of the current template.
- The [prior repository audit](2026-09-25-repository-audit.md) found the
  installed distro missing CLI and several canonical runtime paths. Source
  fixes were not deployed then or during this read-only pass. Historical A/B
  grades in [capability-confidence.md](../docs/capability-confidence.md) do
  not qualify that current VHDX as a runnable release. A public source release
  still needs a license and retained fixture/report media rights review.

## Documented blockers, not newly discovered defects

The confidence matrix already marks fresh MOSS-TTS inference, MiniCPM speech
output, Qwen audio/video and speech, Moshi duplex/batch, AnyGPT multimodal
operation, and oversized 30B variants as blocked or unverified. This audit did
not load those models or change their grades. API presence, an installer, or
an old generated file is not evidence that a current path works.

## Focused regression gaps

The current test tree is broad, but it does not directly exercise the failure
transitions behind several findings above: busy audio-worker Cancel and a
second concurrent autospawn, same-input/different-GPU Comfy assignments,
explicit staged CPU weights against a host budget, stale PID reuse, an
untrusted relay peer, metadata collisions across output kinds, an unlink
failure in direct output pruning, or a subprocess parent that exits while a
descendant retains stdout. [test_maintenance.py](../tests/test_maintenance.py)
does not cover the scheduled Hub-prune reference claim. These are specific
missing cases, not a claim that the whole test suite fails. Several tests also
accept any `Exception` rather than the intended failure class
([ACE source test](../tests/test_ace_step_audio_sources.py#L34),
[blueprint tests](../tests/test_blueprint_assets.py#L107)), which can conceal
the wrong failure path.

## Suggested repair and verification order

1. Close the relay trust boundary and require process identity before every
   PID/PGID kill. Test both with isolated fake peers/process records.
2. Fix audio busy-worker selection, cancel/state behavior, and admission
   serialization; use model-free mocks before any bounded live inference.
3. Make Comfy placement respect shared node inputs and bounded host memory;
   block `check-nodes` runs, then validate with analyze-only synthetic graphs.
4. Guard destructive asset/cache operations and repair output metadata/prune
   accounting. Test failure paths in temporary directories.
5. Correct browser/CLI token recovery, chat cancellation, form persistence,
   and the documented core-update payload. Run focused JS and route tests in
   an isolated test host, then perform deliberate live qualification.

This report records source findings, not applied fixes. No source or runtime
file was changed in this audit.

## Second-pass addendum — 2026-09-25

This follow-up reviewed the same child app again, concentrating on auth,
placement, cancellation, concurrent writes, output handling, and update
qualification. The findings below were absent from the first pass. They are
source-confirmed control-flow or contract gaps; the stated failure conditions
were not reproduced live. No API requests, model loads, workflow runs, process
signals, or executable tests were made. Only this report was updated.

### Additional high-priority findings

#### S-H1. Bearer-key scopes are not enforced outside key management

In `OMNI_AUTH_MODE=bearer`, the gateway accepts any registered key and stores
its scopes on the request ([middleware](../server/omni_comfy_server.py#L575)).
Only the [key-management router](../server/routers/keys.py#L34) reads those
scopes; the intended `read`, `generate`, and `manage` boundaries are stated in
[key_store.py](../server/key_store.py#L7). A key issued with `read` can therefore
call authenticated management and destructive routes, including
[Comfy asset deletion](../server/routers/assets.py#L1459). Key revocation still
requires admin, but that does not protect the other routes. **Direction:**
enforce scopes centrally for each existing route family and test a read key
against write, delete, and shutdown operations. This requires bearer mode.

#### S-H2. An explicitly remote-bound bridge exposes the session bootstrap

The [bridge bind settings](../bridge.py#L46) permit a non-loopback listener via
`OMNI_API_ALLOW_REMOTE`, `OMNI_BRIDGE_HOST`, or `BRIDGE_HOST`. The bridge
[proxies `/api/session`](../bridge.py#L738) over its loopback gateway connection,
and the [session route](../server/routers/sessions.py#L194) returns the
per-instance token. Even when the gateway's own remote flag is off, it sees the
bridge as a loopback peer. A client that can reach this bridge listener can
bootstrap an admin token. This extends H1 with a separate, explicit remote-bind
trigger; actual network reachability was not probed. **Direction:** require an
authenticated remote bootstrap or keep `/api/session` behind a trusted local
boundary independent of the proxy's upstream peer address.

#### S-H3. Worker placement preflight ignores requested precision

[Spawn requests](../server/routers/workers.py#L71) accept `precision`, but both
[analyze and spawn preflight](../server/routers/workers.py#L238) omit it from
placement analysis. The [planner](../server/omni_placement.py#L27) uses a fixed
registry VRAM estimate, while the [worker loader](../server/omni_worker.py#L142)
can select FP32. MiniMax Music 3 likewise analyzes a fixed variant estimate
without `bf16` ([route](../server/routers/minimax_music3.py#L219)), although
`bf16: false` selects FP32 in its [loader](../server/minimax_music3_loaders.py#L110).
A plan can be marked valid with too little memory for its actual dtype; an OOM
depends on available headroom and offload. **Direction:** include dtype and
offload policy in the no-weight estimate and reject unsupported precision
combinations before spawn/load.

#### S-H4. Linked LTX duration can erase decode activation headroom

The [LTX decode profile](../server/comfy_placement.py#L398) converts every
latent dimension with `int()` in one `try` block. If `length` is a Comfy link to
a duration predictor, the conversion error clears width, height, length, and
batch; the function then [returns zero overhead](../server/comfy_placement.py#L419).
That zero is assigned to the [video decoder component](../server/comfy_placement.py#L582).
A dynamic-duration graph can therefore pass a GPU budget without decode
activation headroom. The existing [admission test](../tests/test_comfy_placement.py#L110)
uses a literal length. **Direction:** resolve a safe bound for linked duration,
or block the plan when the bound is unknown; test a linked-duration graph.

#### S-H5. Concurrent Comfy asset installs can write the same destination

The URL installer resolves a destination from category and local name but keys
its active job by [URL plus name](../server/routers/assets.py#L1383); the
repository installer keys by [repo and source file](../server/routers/assets.py#L1438).
The shell installer also locks the [full argument vector](../server/install_model.sh#L78),
not the destination. Different sources aimed at one filename can therefore run
together. [Materialization](../server/file_materialize.py#L96) uses a shared
`<destination>.part` name and removes an existing part before copying/linking,
so the jobs can interfere, fail, or publish bytes from the other source while
reporting their own source. **Direction:** reserve the canonical destination
across every install path and give each staged file a unique name before an
atomic commit. The outcome depends on timing; no installs were started here.

#### S-H6. Cancelling a Comfy Manager job does not cancel its mutation

The [Manager queue helper](../server/routers/extensions.py#L467) resets,
enqueues, and starts the upstream queue before checking `cancel_event`
([check](../server/routers/extensions.py#L500)). On cancellation,
[extension management](../server/routers/extensions.py#L891) re-raises
`JobCancelled` without its snapshot restore; [Manager model installs](../server/routers/extensions.py#L715)
use the same queue helper. A prompt Cancel can still begin an install/update,
and a later Cancel can mark the gateway job cancelled while Manager keeps
mutating. **Direction:** check cancellation before queueing, send Manager's
supported interruption/reset command, wait for a settled upstream state, and
report any completed mutation accurately.

### Additional medium-priority findings

#### S-M1. Streaming chat drops tool and JSON-format behavior

[ChatRequest](../server/routers/workers.py#L80) accepts `tools`, `tool_choice`,
and `response_format`. The nonstream route applies preambles and parses/coerces
the result ([route](../server/routers/workers.py#L331)); the native
[stream route](../server/routers/workers.py#L386) and OpenAI-compatible
[stream helper](../server/routers/workers.py#L600) send the raw request instead.
The worker's [infer request](../server/omni_worker.py#L1221) has no such fields,
so streaming silently yields ordinary text tokens with neither tool calls nor
JSON-mode enforcement. **Direction:** implement streaming equivalents of the
preamble and result contract, or reject those options explicitly on streams.

#### S-M2. Job log streams stop emitting lines after the tail reaches 200

Each job stores log lines in a [200-entry deque](../server/jobs.py#L100).
The [SSE route](../server/routers/jobs_routes.py#L58) tracks only the prior
deque length and emits when `len(tail) > seen`. Once full, length remains 200
as new lines displace old ones, so an already connected client misses every
later line. Status and final events still arrive. **Direction:** track a
monotonic line sequence or queue cursor, including a gap indicator after
truncation, and test a stream with more than 200 lines.

#### S-M3. Proxy failures can write a query-string API token to logs

The gateway accepts `?token=` on selected output and SSE paths
([credential extraction](../server/omni_comfy_server.py#L513)). A bridge proxy
exception [logs `self.path`](../bridge.py#L847), which includes the query string;
the [logger](../bridge.py#L115) writes that line to stdout and the bridge log.
A timeout or other upstream error on a tokenized URL can persist the API token.
No token was read or printed in this audit. **Direction:** log a path with
credential query parameters removed, and redact them in all proxy errors.

#### S-M4. Core updates can stop a Manager model download

The [core-update gate](../server/routers/comfy.py#L558) checks active jobs only
for `comfy_extension` before [stopping instances](../server/routers/comfy.py#L591).
Manager model downloads run as
[`comfy_model_install`](../server/routers/extensions.py#L741), so an overlapping
core update can stop the Comfy instance that hosts the Manager queue. Exact
partial-file behavior depends on Manager and timing. **Direction:** coordinate
all Comfy-owned queue operations and model installs with the maintenance
reservation, then test update admission while a model download is active.

#### S-M5. Ranked audio generation retains all candidate media in gateway RAM

ACE-Step [accumulates full base64 responses](../server/routers/ace_step.py#L968)
through generation and scoring before [persisting](../server/routers/ace_step.py#L1021);
Audio Lab does the same ([generate](../server/routers/audio_lab.py#L639),
[persist](../server/routers/audio_lab.py#L698)). ACE accepts up to
[16 candidates of 600 seconds](../server/routers/ace_step.py#L321). A large
ranked request can retain gigabytes of encoded audio in the lightweight
gateway, and a gateway crash before persistence loses completed candidates.
**Direction:** persist each candidate as it completes, score from bounded
files/streams, and keep a partial manifest throughout the job.

#### S-M6. Audio ZIP downloads assemble the whole archive in memory

The [ACE-Step ZIP route](../server/routers/ace_step.py#L1470) and
[Audio Lab ZIP route](../server/routers/audio_lab.py#L1209) each build a ZIP in
`BytesIO`, then yield one `buf.read()` copy. Their `StreamingResponse` labels
do not bound archive memory. Large ranked jobs can exceed the gateway's memory
reserve during download. **Direction:** write a bounded temporary archive or
stream ZIP chunks with cleanup on disconnect. Memory exhaustion depends on
archive size; no download was attempted.

#### S-M7. Output ZIP creation blocks the gateway event loop

[`zip_outputs`](../server/routers/outputs.py#L1198) is asynchronous but writes
and verifies the complete archive synchronously, before returning its stream.
The request permits [up to 10 GiB](../server/routers/outputs.py#L1194) of
inputs despite a nearby 500 MB comment. A large request can delay unrelated
gateway requests, including status and cancellation. **Direction:** move ZIP
construction and integrity checking to a bounded worker/job, with admission
limits for disk space and concurrency.

#### S-M8. Output listing scans the entire tree on the event loop

[`list_outputs`](../server/routers/outputs.py#L553) synchronously traverses
every file with `rglob`, resolves paths, builds metadata, and sorts all matches
before applying the response `limit` ([scan](../server/routers/outputs.py#L582),
[sort](../server/routers/outputs.py#L617)). The limit caps returned rows, not
filesystem work. A large output library can stall other requests. **Direction:**
offload scanning, then use an index or bounded paging strategy and document
the cost of an exact `total`.

#### S-M9. Manager `update_all` verifies inventory, not working nodes

After `update_all`, [verification](../server/routers/extensions.py#L562) checks
that each extension still appears in Manager's installed list and reports
`node_classes_checked: 0`; it does not compare revisions or inspect live
`/object_info`. A package that updated but failed to load its node classes can
still yield a verified job if the Manager queue logs complete. **Direction:**
verify expected revisions and required node classes after a restart, or label
the result as inventory-only until live qualification succeeds.

#### S-M10. CLI Hugging Face token input exposes the secret in process arguments

The [CLI command](../cli/omni.py#L751) requires the token as a positional
argument. Normal invocation puts it into shell history and the process
command line. The audit did not run the command or handle a token.
**Direction:** accept an interactive hidden prompt, protected stdin, or a
restricted token file; keep command arguments free of secrets.

### Additional lower-priority gaps

- [Core-update qualification](../server/routers/comfy.py#L371) requires only
  `CheckpointLoaderSimple` and `KSampler` from `/object_info`. It can declare a
  restarted runtime valid without checking OmniBridge routing or staged node
  classes. Add those classes to capability qualification after a core update.
- [Key creation](../server/routers/keys.py#L28) permits an explicit empty
  `scopes` list, but [storage](../server/key_store.py#L150) replaces it with
  `admin` (and [load](../server/key_store.py#L82) does likewise). Only an admin
  can create keys, so this is an unsafe issuance surprise rather than a
  separate untrusted privilege escalation. Reject empty scopes explicitly.
- Scheduled [`verify-models`](../server/maintenance.py#L374) inspects only a
  top-level `model.safetensors.index.json` under each `models/omni` directory
  and checks shard existence, not nonzero size or integrity. It can return
  `checked: 0` while unindexed or nested weights are present. Label this as a
  shard-index subset check or extend it to each installed variant's declared
  files and install-complete record. The shell
  [verify command](../server/install_model.sh#L1445) has the same narrow scope.
- The [Comfy API skill](../skills/omni-comfy-api/SKILL.md) says same-instance
  VRAM may be reusable, while [current placement](../server/comfy_placement.py#L284)
  sets `reusable_instance_vram: false` and the
  [canonical guide](../docs/comfy-api.md) explains current-free budgeting.
  Align the skill with the planner so an operator does not expect warm resident
  memory to satisfy a blocked plan.
- Both ranked-audio routes create an output directory before confirming the
  worker model is loaded ([ACE](../server/routers/ace_step.py#L932),
  [Audio Lab](../server/routers/audio_lab.py#L594)). Failed prechecks leave an
  empty directory; later failures can leave files without a manifest, and
  [job listings](../server/routers/audio_lab.py#L1161) skip such directories.
  Clean failed directories or record a partial/error manifest.

The addendum identifies further source risks, not applied fixes. Runtime
consequences remain conditional on the stated triggers and should be checked
with isolated, focused tests before a public release.

## Further read-only pass — 2026-09-25

This pass searched for gaps not covered by either section above. It remained
static: no API request, model load, browser execution, workflow, test suite,
process signal, or runtime action was performed. The findings below describe
source behavior; timing-dependent or model-dependent effects are identified as
such. Only this report was updated.

### Further high-priority findings

#### F-H1. Chat media fields can inject HTML into the session view

The [session message model](../server/routers/chat_sessions_routes.py#L55)
length-bounds `image`, `audio`, and `video` but does not validate their base64
alphabet or escape attribute characters. The streaming route
[appends the user message before inference](../server/routers/chat_sessions_routes.py#L190),
and a [session snapshot](../server/chat_sessions.py#L49) returns those strings.
The [chat renderer](../server/static/tab-chat.js#L347) concatenates them into
quoted media `src` attributes, then assigns the result to
[`innerHTML`](../server/static/tab-chat.js#L328). An authenticated client with
a ready chat worker can plant quote-containing media text; opening that
session can execute injected HTML. The UI holds its API token in
[JavaScript state](../server/static/app.js#L20), so this can cross into the
gateway's admin session. **Direction:** validate media as bounded base64 and
build DOM attributes with element properties/text nodes; retain a regression
case for a quote-containing attachment. No payload was executed in this audit.

#### F-H2. Exact-file deletion follows an in-tree symlink to another file

[`safe_child_path`](../server/helpers.py#L43) and
[`safe_subtree_path`](../server/helpers.py#L60) resolve the final symlink before
returning a path. Consequently, the later symlink guard in
[workflow deletion](../server/routers/workflows.py#L1462) sees the resolved
target as a regular file; [Comfy asset deletion](../server/routers/assets.py#L1459)
and [output deletion](../server/routers/outputs.py#L963) also unlink resolved
targets. Deleting an alias to another file *inside* the allowed tree can
therefore remove the target and leave a broken alias. Outside-tree symlinks are
rejected; the trigger requires an in-tree symlink created outside these API
routes. **Direction:** retain the lexical final path for exact-file operations,
reject final symlinks where appropriate, and check containment separately.

#### F-H3. The Comfy proxy forwards gateway bearer credentials upstream

The proxy's [internal-header list](../server/proxy.py#L33) removes
`X-Omni-Token` and cookies but omits `Authorization`.
[`_filter_request_headers`](../server/proxy.py#L80) therefore passes a bearer
key into a [Comfy request](../server/proxy.py#L121). Under bearer auth, a
client calling a proxied Comfy route sends its gateway credential to ComfyUI
and installed custom-node code. Whether that upstream code logs or uses the
secret is conditional; the boundary crossing is source-confirmed.
**Direction:** strip every gateway credential header before upstream forwarding
and test with both `X-Omni-Token` and `Authorization`.

### Further medium-priority findings

#### F-M1. Unknown manual placement overrides are silently ignored

The [override matcher](../server/comfy_placement.py#L795) looks up keys only
while iterating discovered components. The [placement loop](../server/comfy_placement.py#L916)
never checks for supplied keys that matched nothing. A typo or stale component
ID in a `mode: manual` policy can receive an automatic assignment while the
plan remains `valid: true`. **Direction:** report every unused override and
block manual plans with unmatched keys, including saved policies after a graph
edit.

#### F-M2. A disconnect before first stream iteration can strand a worker

Native chat [marks a worker busy](../server/routers/workers.py#L393) before
constructing the response generator; session chat
([route](../server/routers/chat_sessions_routes.py#L177)) and the
[OpenAI-compatible stream](../server/routers/workers.py#L600) follow the same
pattern. Their release lives inside each generator's `finally`, which does not
run if the response is cancelled before the generator first starts.
[Health checks](../server/worker_manager.py#L759) skip a live `busy` worker,
so it can remain unavailable until explicit recovery. **Direction:** bind
reservation release to the response lifecycle even when iteration never
begins. The trigger is a narrow disconnect timing window; it was not
reproduced live.

#### F-M3. A custom CLI URL can receive the local admin token

[`resolve_token`](../cli/client.py#L57) reads the environment and local token
files before considering `base_url`; [Client](../cli/client.py#L99) sends the
resolved `X-Omni-Token` to its chosen server. Thus `--base-url` pointing to
another host without an explicit `--token` can send the local Omni admin token
to that host ([CLI option](../cli/omni.py#L25)). **Direction:** bind discovered
tokens to a trusted local origin; require an explicit credential for any
nonlocal URL. No network request was made here.

#### F-M4. Concurrent workflow imports can violate `overwrite=false`

[Import](../server/routers/workflows.py#L1488) checks existence before an
`await file.read(...)`, then uses an [unconditional `os.replace`](../server/routers/workflows.py#L161).
Two imports of the same new filename can both pass the check while their reads
overlap; the later write replaces the first despite `overwrite=false`.
**Direction:** reserve the destination atomically or recheck under a
per-filename lock immediately before commit. This finding is specific to the
import path's await window; a concurrent request was not run.

#### F-M5. A saved workflow placement policy cannot be cleared with `null`

Both [workflow save](../server/routers/workflows.py#L1429) and
[metadata update](../server/routers/workflows.py#L1451) interpret
`placement_policy: null` as “retain the current policy.” The typed request
allows null, but there is no API operation here to restore no-policy behavior
once a policy has been saved. A stale GPU UUID policy continues to affect
[queueing](../server/routers/workflows.py#L1555) until replaced or the workflow
is recreated. **Direction:** distinguish an omitted field from explicit null
and let explicit null clear the persisted policy.

#### F-M6. Comfy asset deletion is not coordinated with active use or install

The [delete route](../server/routers/assets.py#L1459) unlinks the selected
model immediately after path validation. It does not check active Comfy queues,
loaded models, or install jobs targeting the same file. Deletion during a
download can race its publish; deletion during use can make a later reload
fail. This extends the active-asset coordination issue in M9 to the Comfy
tree. **Direction:** resolve the exact target, reject active consumers and
writers, and serialize deletion against installation.

#### F-M7. Deleted output files leave metadata that can attach to later files

[Output deletion](../server/routers/outputs.py#L963) removes the file without
calling [metadata deletion](../server/output_meta.py#L130); scheduled pruning
does likewise ([maintenance.py](../server/maintenance.py#L164)). The metadata
clear route [requires the file to exist](../server/routers/outputs.py#L689),
so stale pins, tags, notes, and collection counts cannot be cleared through
that API after deletion. If a new file uses the same relative path, it inherits
the stale record. This is independent of M11's cross-kind key collision.
**Direction:** delete the metadata record after a successful file unlink and
reconcile records whose files no longer exist.

#### F-M8. ACE batch generation returns only its first result

The request accepts [`batch_size` up to eight](../server/routers/ace_step.py#L360)
and the loader forwards that value into `GenerationConfig`
([ace_step_loaders.py](../server/ace_step_loaders.py#L1833)). After generation,
it reads only [`result.audios[0]`](../server/ace_step_loaders.py#L1857) and
returns one `audio_base64`. If the upstream generator returns multiple audios,
all later results consume time and memory but are neither returned nor saved.
**Direction:** persist and return the full batch or restrict `batch_size` to 1.

#### F-M9. ACE ranked seeds can overflow the worker's accepted range

The gateway permits seeds through `2^31-1` and adds the candidate index
without wrapping ([ranked loop](../server/routers/ace_step.py#L978)); the
[worker schema](../server/omni_worker.py#L1859) rejects a seed above that
maximum. For example, the maximum seed with two candidates makes the second
request fail validation, yielding a partial job. **Direction:** wrap or reject
seed-plus-count combinations before generation.

#### F-M10. Temporary-file creation bypasses best-effort persistence handling

The Omni output helper promises `None` on any write failure
([contract](../server/omni_outputs.py#L40)), but
[`tempfile.mkstemp`](../server/omni_outputs.py#L79) is outside its `try` block.
A disk-full or permission error there escapes; TTS/SFX can turn successful
inference into HTTP 500, while STT can fail before transcription
([caller](../server/routers/audio.py#L183)). **Direction:** include temp-file
creation in the best-effort error path and keep output delivery independent of
archival failure.

#### F-M11. MiniMax load can race generation on a busy worker

The gateway load and generation routes use separate locks
([load](../server/routers/minimax_music3.py#L240),
[generation](../server/routers/minimax_music3.py#L269)); worker selection also
permits a [`busy` worker](../server/routers/minimax_music3.py#L78). The worker's
generation path [captures `pipeline`](../server/minimax_music3_loaders.py#L160)
before taking the state lock, while load can replace that pipeline under the
same lock ([load](../server/minimax_music3_loaders.py#L90)). Overlapping calls
can run a stale pipeline after a new model load and temporarily retain both
large pipelines. **Direction:** use one load/infer admission lock and fetch the
pipeline only after acquiring the worker state lock.

#### F-M12. The bridge timeout is shorter than valid long audio requests

The Windows bridge defaults to a [1,860-second proxy timeout](../bridge.py#L71)
for [upstream response reads](../bridge.py#L795). MiniMax synchronous
generation permits a [7,200-second worker timeout](../server/config.py#L1284);
ACE ranked generation can run [16 serial candidates](../server/routers/ace_step.py#L967).
A valid request lasting more than 31 minutes can time out at the bridge while
gateway or worker work continues. **Direction:** align proxy and operation
budgets or expose long generation through a cancellable job response. The
failure depends on actual duration and whether the bridge is in the path.

#### F-M13. Chat sessions have no aggregate media-memory budget

Each session message permits three large inline media fields
([request](../server/routers/chat_sessions_routes.py#L55)); the in-memory store
retains up to [100 messages per session](../server/chat_sessions.py#L32) and
[1,000 sessions by default](../server/chat_sessions.py#L28).
The streaming route saves the user message before inference finishes
([route](../server/routers/chat_sessions_routes.py#L190)). Repeated large
authenticated submissions can therefore consume gigabytes of gateway memory
despite per-field limits. **Direction:** bound aggregate session bytes, store
media outside the session object, and evict by memory as well as count/age.

### Further lower-priority gaps

- MiniMax's [`cpu_memory_mb` request](../server/routers/minimax_music3.py#L45)
  affects placement admission but is not sent to the worker
  ([route](../server/routers/minimax_music3.py#L240)); automatic
  [CPU offload](../server/minimax_music3_loaders.py#L110) receives no such
  ceiling. Treat the value as an estimate in the API or enforce the planned
  budget at runtime. Actual overrun depends on the loaded model and cgroup.
- MiniCPM audio input [fully decodes and resamples](../server/omni_worker.py#L766)
  before checking its 60-second decoded limit. A long, highly compressed
  input can use substantial CPU and memory before rejection. Probe duration
  or bound decoding work ahead of full allocation.
- Worker `precision` is an [unrestricted string](../server/routers/workers.py#L71),
  while the [loader](../server/omni_worker.py#L142) recognizes only `fp32`,
  `fp16`, and `bf16`; any other value silently falls back to BF16 on CUDA.
  Reject unsupported values so the requested and loaded precision cannot
  disagree. This is separate from S-H3's VRAM-estimate mismatch.

The further pass records source findings only. No application or runtime file
was changed.

## Focused follow-up pass — 2026-09-25

The preceding passes still uncovered consequential bugs, so this read-only
follow-up checked media parsing, credential durability, subprocess cancellation,
and the CLI/manual contract. These are additional findings only. The source
tree and runtime were not changed or exercised; only this report was updated.

### Additional high-priority findings

#### G-H1. Compressed PNG metadata has no decompressed-size limit

The PNG reader caps file bytes examined at 4 MiB
([reader](../server/routers/outputs.py#L171)) but calls unbounded
`zlib.decompress` for [`zTXt`](../server/routers/outputs.py#L212) and compressed
[`iTXt`](../server/routers/outputs.py#L234). A small compressed text chunk in a
managed PNG can inflate into a very large allocation when the
[metadata route](../server/routers/outputs.py#L823) or a probed listing reads
it. The parser also runs directly in the metadata route's event loop. A
crafted file could exhaust gateway memory or stall other requests; no file was
created and no request was made during this audit. **Direction:** cap each
decompressed chunk and total metadata bytes, and offload bounded parsing.

#### G-H2. A reported API-key revocation can be undone by restart

[`ApiKeyStore._save`](../server/key_store.py#L125) logs and swallows disk write
or replace failures. [`revoke`](../server/key_store.py#L157) still removes the
key in memory and returns success. If the on-disk file remains unchanged,
[`_load`](../server/key_store.py#L74) restores that revoked bearer key after a
gateway restart. Key creation has the reverse problem: a reported new key can
disappear after restart. This requires a persistence failure; none was induced
here. **Direction:** make the save outcome part of the transaction, roll back
on failure, and return an error unless the key change is durable.

### Additional medium-priority findings

#### G-M1. Cancel can miss a subprocess during the spawn window

A subprocess job becomes `running` before
[`create_subprocess_exec`](../server/jobs.py#L328) finishes. [`cancel`](../server/jobs.py#L482)
sets `cancelling` but signals only an already assigned `job.process`. If Cancel
arrives while spawn is awaited, the new process is not signalled and the runner
does not check the cancellation flag after assignment; it can finish a model
install or other mutation, then report the job `cancelled`. **Direction:**
recheck cancellation immediately after spawn and terminate that exact process
group before reading its output. This is a narrow timing window and was not
reproduced live.

#### G-M2. Output-pin changes can appear saved but disappear after restart

[Output metadata saving](../server/output_meta.py#L80) swallows temp write and
replace errors. [`upsert`](../server/output_meta.py#L104) then returns the new
in-memory pin as if it were durable. After restart, the old metadata file can
restore an unpinned state, allowing [scheduled pruning](../server/maintenance.py#L137)
to delete an output the operator believed protected. This is separate from the
cross-kind and stale-record bugs in M11 and F-M7. **Direction:** surface save
failure to callers or roll back the in-memory mutation; test pin persistence
under a simulated write failure.

#### G-M3. CLI media commands cannot select nondefault output roots

`outputs list` accepts `--kind omni`, `input`, or `temp`
([CLI](../cli/omni.py#L624)), but [`get`](../cli/omni.py#L659),
[`delete`](../cli/omni.py#L676), and [`zip`](../cli/omni.py#L686) send no `kind`.
The corresponding API routes default to `output`
([GET](../server/routers/outputs.py#L877),
[ZIP](../server/routers/outputs.py#L1190)). A path copied from an Omni listing
normally returns 404 in `get`, while a duplicate relative name in `output`
could select the wrong file. The [media manual](../manual/05-media-library.md#L113)
demonstrates this broken list-to-get flow. **Direction:** add `--kind` to each
CLI command and pass it to the API, then update the examples.

#### G-M4. CLI `--older-than` never reaches the prune contract

The [CLI](../cli/omni.py#L701) advertises values such as `30d` but sends
`{"older_than": "30d"}` ([body](../cli/omni.py#L707)). The API accepts a
numeric [`older_than_days`](../server/routers/outputs.py#L1285) field instead.
With only `--older-than`, the route returns 400 because it sees neither prune
criterion; with `--max-gb` too, it silently ignores age and prunes solely by
size. The [manual](../manual/05-media-library.md#L146) recommends the broken
option. **Direction:** parse the CLI duration into days, send the exact typed
field, and display the returned background job ID/status.

#### G-M5. An overlong but satisfiable media byte range receives 416

[`_parse_range`](../server/routers/outputs.py#L637) rejects an end offset at
or beyond the file size, even when the start is within the file; the
[GET route](../server/routers/outputs.py#L915) uses that parser. For a
100-byte file, `Range: bytes=0-1023` returns 416 rather than the available
bytes 0-99. Media/download clients that request broad blocks can fail to
seek or resume. **Direction:** clamp a requested end to the last available
byte when the start is valid; retain 416 for an unsatisfiable start.

#### G-M6. Windows CLI export examples write to a Linux path

[`omni-cli.bat`](../omni-cli.bat#L5) runs the CLI inside WSL, while
[`outputs get --out`](../cli/omni.py#L653) passes its argument straight to
Linux `Path` and `open` without converting a Windows drive path. The
[media](../manual/05-media-library.md#L113),
[CLI](../manual/18-cli.md#L165), and
[jobs](../manual/20-jobs-huggingface-and-special-usages.md#L121) manuals show
`--out D:\...` as an NTFS export. That spelling is a Linux filename, not the
Windows D: drive, when passed to the WSL Python process. **Direction:** use
`/mnt/d/...` in examples or explicitly convert Windows paths in the shim/CLI.

#### G-M7. Audio composition can wait forever on a full stderr pipe

The [ffmpeg runner](../server/routers/outputs.py#L1035) creates a stderr pipe,
then repeatedly awaits [`proc.wait()`](../server/routers/outputs.py#L1051)
before reading [stderr](../server/routers/outputs.py#L1054). If ffmpeg emits
more error output than the finite pipe buffer, it blocks writing and cannot
exit, while the gateway waits for exit. This requires a sufficiently noisy
error path and was not reproduced here. **Direction:** drain stderr
concurrently with process execution, with a bounded retained tail and the
existing cancellation path.

### Additional documentation corrections

- [The API inventory](../docs/api-capability-inventory.md#L416) and
  [media manual](../manual/05-media-library.md#L67) call output ZIP a
  background job, but [`POST /api/outputs/zip`](../server/routers/outputs.py#L1198)
  builds and streams the archive in the request and returns no job ID. The
  [jobs manual](../manual/20-jobs-huggingface-and-special-usages.md#L73)
  should remove ZIP from its pollable-job list. This also matters because
  S-M7 documents the route's event-loop blocking work.
- [The media manual](../manual/05-media-library.md#L74) says output metadata
  lives beside each file. Tags, pins, collections, and notes actually live in
  [`RUNTIME_DIR/output_metadata.json`](../server/output_meta.py#L1); the
  distinction matters for backup and recovery, especially after a failed
  save as described in G-M2.

This follow-up records source findings, not fixes. Only the audit report was
updated.

## Additional read-only contract pass — 2026-09-25

This pass traced CLI requests into typed routes, saved workflow state into
queueing, browser selection into request and response handling, and audio
worker failure paths. Findings below are additional to the earlier sections.
They are source-confirmed control-flow or contract gaps; no live requests,
browser runs, model loads, or tests were used to reproduce their consequences.

### Medium-priority findings

#### A-M1. CLI maintenance policy changes always fail request validation

[`maintenance policy set`](../cli/omni.py#L921) sends a JSON body with an
`updates` key. The typed [`PolicyUpdate`](../server/routers/maintenance_routes.py#L260)
requires `policy`, which [`put_policy`](../server/routers/maintenance_routes.py#L299)
reads. Thus every use of this documented CLI setter receives a 422 response
before a policy is changed. **Direction:** send the task/key/value mapping
under `policy` and add a CLI-to-route contract check.

#### A-M2. Selecting CPU for ComfyUI can start it on a GPU

The browser offers Device and Memory mode independently, with `normal` as the
default ([controls](../server/static/tab-comfy.js#L384),
[request](../server/static/tab-comfy.js#L670)). For `device="cpu"`,
[`normalize_gpu_pool`](../server/comfy_manager.py#L97) returns an empty pool.
[`start_instance`](../server/comfy_manager.py#L358) then sets no GPU visibility
constraint, while the `--cpu` flag is added only for
[`vram_mode="cpu"`](../server/config.py#L182). On a GPU host, selecting only
Device = CPU can therefore let Comfy select a GPU while the instance record
claims CPU. The inverse combination, CUDA device plus CPU memory mode, also
records misleading placement. **Direction:** validate or couple the two
settings, pass an explicit CPU flag for a CPU device, and reconcile the
reported device with Comfy's actual runtime device.

#### A-M3. Corrupt workflow metadata silently removes saved GPU placement

[`_load_metadata`](../server/routers/workflows.py#L190) returns
`placement_policy=None` when a metadata file is unreadable or invalid JSON,
and also drops a policy rejected by normalization. Saved
[`queue_workflow`](../server/routers/workflows.py#L1547) passes that result to
[`_queue_on_instance`](../server/routers/workflows.py#L1081), which builds and
enforces a placement plan only when a policy is present. A workflow whose
saved policy becomes corrupt can therefore queue with ordinary Comfy device
selection instead of honoring its prior GPU constraints. This requires a
damaged or manually edited metadata file; it was not reproduced at runtime.
**Direction:** distinguish missing metadata from invalid metadata and block
queueing with a repairable error when a stored policy cannot be read.

#### A-M4. Workflow analysis can call an incomplete graph ready to run

[`_workflow_document_kind`](../server/routers/workflows.py#L465) classifies a
dictionary containing any `class_type` node as API format. The analysis
checks class availability and recognized model references, then sets
[`ready_to_run`](../server/routers/workflows.py#L879) from those checks; the
payload validator only serializes and size-checks the graph
([`_validate_workflow_payload`](../server/routers/workflows.py#L919)). It does
not check required node inputs or links against live `/object_info`. For
example, a graph with an installed `KSampler` class but missing required
inputs can be marked ready and pass gateway preflight, then be rejected by
[Comfy's `/prompt` request](../server/routers/workflows.py#L1114). **Direction:**
validate node inputs and links against the live schema before asserting
`ready_to_run`, or label the result as dependency readiness only.

#### A-M5. Importing over a workflow can retain an unrelated placement policy

[`import_workflow(overwrite=true)`](../server/routers/workflows.py#L1477)
replaces the graph JSON but does not update or remove its sibling metadata.
The saved [queue route](../server/routers/workflows.py#L1547) still loads that
metadata's placement policy for the new graph. An imported graph can inherit
old GPU UUIDs, node overrides, or constraints and either be blocked or routed
unexpectedly. Metadata preservation may be intentional for tags, but it is
not reconciled with a replacement graph. **Direction:** define explicit
metadata preservation on overwrite and clear or revalidate placement against
the imported graph.

#### A-M6. Audio Lab's encoded-size limit does not bound decoded audio work

Audio Lab accepts up to 64 MiB of base64 characters for source audio
([request models](../server/routers/audio_lab.py#L900)). The shared
[`_decode_audio_base64`](../server/audio_lab_loaders.py#L1091) fully decodes
the file through `soundfile.read` without first limiting frames or duration.
VAE encode and reconstruct then resample and copy that waveform to the
selected device ([encode](../server/audio_lab_loaders.py#L1534),
[reconstruct](../server/audio_lab_loaders.py#L1592)); scoring also fully
decodes its input ([score](../server/audio_lab_loaders.py#L1630)). A compact,
long audio file can consume much more RAM or VRAM than its request size
suggests. Actual exhaustion depends on codec and duration. **Direction:**
inspect audio metadata and enforce a decoded frame/duration budget before
allocating the complete waveform, then bound resampling and GPU transfer.

#### A-M7. OpenAI streaming can return an unreachable worker to the ready pool

The OpenAI-compatible stream catches any [`httpx.HTTPError`](../server/routers/workers.py#L673)
and emits a transport-error SSE event without marking the worker dead. Its
`finally` calls [`_release_worker`](../server/routers/workers.py#L194), which
marks a busy worker ready while its process still exists. The native stream
does mark connection and protocol failures dead
([native handler](../server/routers/workers.py#L422)). If a loopback worker
becomes unreachable before its process exits, the next request can select it
again until later health recovery. **Direction:** retire the exact worker for
connection/protocol failures in the OpenAI stream path, while preserving
separate handling for recoverable HTTP errors.

#### A-M8. MOSS sound-effect failures can return a bad worker to service

The [MOSS-SFX route](../server/routers/moss.py#L66) raises on any worker
non-200 response but always calls [`_release_worker`](../server/routers/moss.py#L94).
A still-running worker is returned to ready even after a worker-side 5xx.
The MOSS-TTS path explicitly retires such a worker because a driver failure
can poison CUDA or device descriptors ([TTS handling](../server/routers/audio.py#L275)).
Reuse is harmful only for failure types that leave the SFX worker unhealthy;
no such failure was induced in this pass. **Direction:** classify fatal SFX
5xx failures and retire or health-check the worker before reuse.

#### A-M9. Rapid chat switching can show one session while sending to another

Each [`selectSession`](../server/static/tab-chat.js#L591) starts a session GET
and assigns its response to `currentSession` without checking whether the
user has since selected another session ([assignment](../server/static/tab-chat.js#L608)).
If session A responds after B was selected, A's history and model appear
under B. [`sendCurrent`](../server/static/tab-chat.js#L664) appends optimistically
to that stale snapshot but posts the message using the newer `currentId`
([stream URL](../server/static/tab-chat.js#L721)). This is a timing-dependent
display and edit mismatch, distinct from the stream-cancel issue in H6.
**Direction:** accept a session response only while its requested ID matches
the selected ID, or use a request generation counter.

#### A-M10. Comfy execution controls can act on a different instance than shown

Changing the selected instance clears the panel and requests a refresh
([`selectExecutionInstance`](../server/static/tab-comfy.js#L524)). An earlier
[`refreshExecution`](../server/static/tab-comfy.js#L546) blocks that new fetch
through `executionLoading`, then unconditionally assigns its old jobs and
queue to the panel ([assignment](../server/static/tab-comfy.js#L555)). The
display can show instance A's queue while [`clearPending`](../server/static/tab-comfy.js#L624)
or Cancel All uses the currently selected instance B. **Direction:** capture
the instance ID with each refresh and discard stale responses; ensure a new
fetch follows a selection change.

### Lower-priority contract and state gaps

#### A-L1. The Audio Lab VAE decode `shape` field is ignored

[VAE encode](../server/audio_lab_loaders.py#L1534) returns a latent `shape`,
and the [decode request](../server/routers/audio_lab.py#L934) accepts and
forwards it. [`vae_decode`](../server/audio_lab_loaders.py#L1559) never reads
the parameter or compares it with the deserialized tensor before moving it
through the VAE. A stale or mismatched shape is silently accepted until a
later tensor/model failure, if any. **Direction:** validate the supplied shape
against the tensor, or remove the unused request field and document shape as
caller-only metadata.

#### A-L2. A failed MiniMax hot swap retains the previous variant metadata

[`load_model`](../server/minimax_music3_loaders.py#L76) clears its pipeline and
component manager before loading a replacement, but updates `model_variant`
and `load_kwargs` only on success. After a load error, the worker remains
available through the [gateway load route](../server/routers/minimax_music3.py#L124),
and [`current_state`](../server/minimax_music3_loaders.py#L55) reports
`model_loaded=false` alongside the previous variant and settings. The stale
metadata is source-confirmed; retained partial model allocations are
possible but unverified. **Direction:** clear variant/settings on swap start
or failure, and distinguish a failed load from an intentionally unloaded
worker in state reporting.

No application files or runtime state were changed in this pass. This report
is the only file updated.

## Another static audit pass — 2026-09-25

This pass revisited persistence and deletion boundaries, GPU admission, audio
ranking, the Windows bridge, and server-side chat sessions. The findings below
were checked against the preceding sections. They are based on source control
flow; no live request, model load, browser run, or executable test was made.

### High-priority findings

#### B-H1. Deleting a custom-node symlink can delete its real target directory

The custom-node [delete route](../server/routers/assets.py#L1230) passes the
requested name to [`safe_child_path`](../server/helpers.py#L43), which resolves
symlinks before returning the path. If an alias symlink points to another
directory inside `custom_nodes`, the returned path is the real directory.
The later [`is_symlink` guard](../server/routers/assets.py#L1239) is then false,
and [`shutil.rmtree`](../server/routers/assets.py#L1242) deletes the real node
instead of the named alias. This extends the exact-file symlink issue in F-H2
to a separate recursive directory deletion route. **Direction:** inspect the
unresolved named entry for symlink status before canonical containment checks,
and delete only the exact entry requested.

#### B-H2. A full GPU can appear to have all its VRAM free in Comfy placement

The device inventory can report [`vram_free_mb: 0`](../server/worker_manager.py#L850).
When building a Comfy placement device, [`instance_devices`](../server/comfy_placement.py#L249)
uses `source.get("vram_free_mb") or total`, so explicit zero is replaced with
the GPU's total VRAM. The resulting [`usable_mb`](../server/comfy_placement.py#L271)
can support a `valid` plan even when the card has no current headroom. A queue
attempt can then fail from memory pressure despite a supposedly safe plan.
This requires a device reading of zero free MiB; it was not exercised live.
**Direction:** distinguish a missing free-memory field from a reported zero,
and retain zero as a hard capacity value.

### Medium-priority findings

#### B-M1. Concurrent Comfy model uploads can overwrite with `overwrite=false`

The upload route checks whether the destination exists
([`upload_asset`](../server/routers/assets.py#L1269)) before awaiting file
chunks ([read loop](../server/routers/assets.py#L1311)). Two requests for the
same filename can both pass that check, then each unconditionally
[`os.replace`](../server/routers/assets.py#L1334) its temporary file onto the
destination. The later upload silently replaces the earlier one even though
both specified `overwrite=false`. **Direction:** serialize by exact target or
use an atomic create-only commit for the no-overwrite case.

#### B-M2. Wrong-shaped output metadata can prevent gateway startup

[`OutputMetaStore._load`](../server/output_meta.py#L58) catches unreadable and
invalid JSON, but assumes any successfully parsed value is an object with an
iterable `records` field ([iteration](../server/output_meta.py#L66)). A valid
JSON value such as `[]` raises `AttributeError`; `{"records": 1}` raises
`TypeError`. The store is constructed at [module import](../server/output_meta.py#L146)
and imported by the outputs router, so a malformed sidecar can prevent the
gateway from starting instead of being reported as a recoverable metadata
problem. **Direction:** validate the top-level object and `records` list
before iteration, then quarantine or skip invalid records.

#### B-M3. Cancelling pip-cache maintenance does not stop its purge

[`run_prune_pip`](../server/maintenance.py#L258) checks cancellation before
spawning `pip cache purge`, then awaits the child with
[`proc.communicate()`](../server/maintenance.py#L275) without checking the
event or registering that process with the job. The generic
[`cancel`](../server/jobs.py#L458) path can only set the event for this callable,
so a job marked cancelling can keep deleting cache and ultimately be marked
cancelled after the purge finishes. The function also returns
[`purged: True`](../server/maintenance.py#L278) without checking the child's
exit code. **Direction:** own and terminate the exact child on cancellation,
and treat a nonzero exit as an error rather than a successful purge.

#### B-M4. ACE ranked output can label an unscored candidate as best

ACE sorts ranked candidates with a `None` score key of `-1.0`
([sort](../server/routers/ace_step.py#L1046)). The CLAP scorer returns cosine
similarity, whose ordinary scores are below 1.0
([scorer](../server/audio_lab_loaders.py#L1701)); their descending-sort keys
are therefore greater than `-1.0`. If one score call fails while another
succeeds, the unscored candidate moves ahead of the scored one and receives
the [`best_` copy](../server/routers/ace_step.py#L1066). **Direction:** sort
missing scores after all numeric scores and reserve `best_` for a candidate
with a successful score.

#### B-M5. ACE can miss CLAP loaded in a second Audio Lab worker

[`_try_get_audio_lab_clap_worker`](../server/routers/ace_step.py#L262) inspects
only [`ready[0]`](../server/routers/ace_step.py#L268). If multiple Audio Lab
workers are ready and the first lacks CLAP while a later one has it, ACE
reports no scorer and [generates unranked output](../server/routers/ace_step.py#L927).
**Direction:** inspect ready workers until a loaded CLAP component is found,
or select an exact scorer worker explicitly.

#### B-M6. The Windows bridge drops chunked request bodies

The bridge's [HTTP proxy](../bridge.py#L763) derives request-body size only
from `Content-Length`, defaulting to zero when that header is absent. It then
forwards [`data=None`](../bridge.py#L780) for such requests; it does not decode
`Transfer-Encoding: chunked`. A chunked JSON POST/PUT or streamed multipart
upload therefore reaches the gateway with an empty body and fails its normal
validation, although the bridge accepts those verbs. **Direction:** decode
chunked framing with a size limit, or explicitly reject unsupported framing
instead of silently dropping the body.

#### B-M7. A timed-out session chat can return its worker to service

The session [synchronous message route](../server/routers/chat_sessions_routes.py#L103)
catches an `httpx.TimeoutException` and returns 504
([handler](../server/routers/chat_sessions_routes.py#L137)), but its `finally`
calls [`_release_worker`](../server/routers/chat_sessions_routes.py#L145).
That helper marks a still-running process ready
([implementation](../server/routers/workers.py#L194)). A read timeout can
leave inference active in the worker while another request is admitted to it.
The stateless chat route retires its worker on the same timeout
([comparison](../server/routers/workers.py#L351)). **Direction:** retire the
exact timed-out session worker, or positively confirm it stopped before
returning it to the ready pool.

#### B-M8. Direct chat-session access does not enforce the advertised TTL

The store's [`_evict`](../server/chat_sessions.py#L110) runs on create and
listing, but [`get`](../server/chat_sessions.py#L136) and
[`snapshot`](../server/chat_sessions.py#L140) return cached sessions without
checking expiry. A client holding a session ID can keep reading or sending to
it after the configured idle TTL until another client creates or lists
sessions. This also retains sensitive history longer than the stated
expiration. **Direction:** enforce expiry on direct reads and message append,
with the same lock used by eviction.

#### B-M9. Multi-turn chat drops prior image, audio, and video context

The session store retains each message's `image`, `audio`, and `video` fields
([message model](../server/chat_sessions.py#L43)). But
[`render_history_for_worker`](../server/chat_sessions.py#L184) flattens only
roles and text. The message route sends only the current request's media
([payload](../server/routers/chat_sessions_routes.py#L116)), so a follow-up
turn without reattaching media cannot have the worker inspect earlier media
again. **Direction:** state this limit in the session contract or add a
bounded, model-aware way to carry referenced prior media into subsequent
turns.

#### B-M10. Concurrent turns in one chat session can use stale history

Both session send paths flatten history before awaiting worker inference
([synchronous](../server/routers/chat_sessions_routes.py#L112),
[streaming](../server/routers/chat_sessions_routes.py#L174)). A completed
synchronous turn is appended only after the worker responds
([append](../server/routers/chat_sessions_routes.py#L149)); there is no
per-session turn lock across that interval. With multiple ready workers, two
overlapping synchronous requests for one session can both run against the
same previous history, then append in response-completion order rather than
request order.
**Direction:** serialize turns per session or reject a second in-flight turn
with an explicit busy response.

### Lower-priority behavior gaps

#### B-L1. Concurrent audio jobs can overwrite and clear each other's progress

ACE and Audio Lab set a single shared progress slot before acquiring their
inference locks ([ACE](../server/routers/ace_step.py#L886),
[Audio Lab](../server/routers/audio_lab.py#L546)). They clear it in `finally`
after leaving the lock ([ACE](../server/routers/ace_step.py#L914),
[Audio Lab](../server/routers/audio_lab.py#L574)). A waiting request can
display its progress while an earlier job runs, and an earlier job can clear
progress after a later one starts. **Direction:** tie progress to the active
locked operation or identify each job separately in the progress route.

#### B-L2. CLI Comfy preview frames can overwrite earlier frames

[`comfy watch`](../cli/omni.py#L344) writes each preview to a temp filename
based on whole-second time and format ([filename](../cli/omni.py#L400)). Two
same-format frames in one second use the same path, so the second overwrites
the first although both are announced as saved. **Direction:** add a frame
counter or unique identifier to each preview filename.

Only this report was edited. The findings are static and their runtime impact
was not reproduced in this pass.

## Further route and lifecycle audit — 2026-09-25

This pass traced additional request, placement, persistence, and UI transitions.
These findings are distinct from the earlier entries. They are source-confirmed;
the conditional runtime outcomes were not reproduced with live services.

### High-priority findings

#### C-H1. The documented gateway-only refresh kills resident model workers

The [repository procedure](../AGENTS.md#L116) says a gateway-only `SIGKILL`
refresh preserves workers for adoption by the restarted gateway. Each worker
records the old gateway as its [`owner_pid`](../server/worker_manager.py#L366).
Startup creates an [empty registry](../server/state.py#L30), then calls
[`kill_orphan_workers`](../server/omni_comfy_server.py#L427) before recovering
only Comfy instances. The orphan scan treats a dead owner and an unregistered
worker as orphaned and [kills its process group](../server/worker_manager.py#L647)
([signal](../server/worker_manager.py#L688)). Consequently the documented
refresh interrupts resident standalone and audio workers even if no job was
running. **Direction:** recover verified worker records before orphan cleanup,
or change the refresh contract and procedure to reflect actual worker teardown.

#### C-H2. A media source change during refresh can delete a file in the wrong root

The Media tab [drops refresh calls while loading](../server/static/tab-media.js#L254),
but a source change [updates `state.source` and calls refresh](../server/static/tab-media.js#L404).
If an old-root list request is pending, that new refresh is skipped and the old
response later [populates the displayed files](../server/static/tab-media.js#L259)
under the new source. Bulk deletion then sends those paths with the current
[`state.source`](../server/static/tab-media.js#L817). The server resolves that
source root and [unlinks the matching path](../server/routers/outputs.py#L962).
If both roots contain the same relative filename, the UI can permanently delete
a different file than the one shown. **Direction:** bind each list, selection,
and delete operation to one captured source; discard stale list responses.

### Medium-priority findings

#### C-M1. Snapshot validation can accept shards outside the snapshot

[`inspect_snapshot`](../server/snapshot_install.py#L21) joins each index
`weight_map` value to the index parent and accepts any existing nonempty file
([loop](../server/snapshot_install.py#L65)). It does not reject absolute paths,
`..` escapes, or symlinks outside the snapshot. With one in-tree weight file,
an index referring to an existing external shard can return `valid: true`; the
installer then writes [`.install_complete`](../server/install_model.sh#L439)
([commit](../server/install_model.sh#L445)). **Direction:** resolve every
declared shard and require containment within the staged snapshot before
marking installation complete.

#### C-M2. Manual model placement counts GPU memory that its map never uses

Manual placement selects [every eligible GPU](../server/omni_placement.py#L203)
and checks the model estimate against their [summed budgets](../server/omni_placement.py#L251).
The manual module map is resolved afterward
([mapping](../server/omni_placement.py#L280)); no check ties the budget to the
actual target devices or verifies that `require_all` assigned any module to
each GPU. A map targeting only a small GPU can pass because an unused second
GPU contributes capacity, then fail at load. **Direction:** validate the
resolved module map against the selected devices and calculate applicable
capacity from actual targets, with loader-level coverage checks where needed.

#### C-M3. ACE dual-GPU analysis has no per-component capacity check

ACE analyzes split placement with [one family-level model estimate](../server/routers/ace_step.py#L125)
and `require_all: true`, then uses `valid` to admit the spawn
([route](../server/routers/ace_step.py#L159)). The placement analyzer compares
that estimate with the [sum of both GPU budgets](../server/omni_placement.py#L251),
without checking whether the primary can hold the chosen DiT and the auxiliary
can hold the chosen LM. A large auxiliary can make a too-small primary appear
valid. **Direction:** analyze the selected ACE variant and its DiT/LM component
requirements against their respective device budgets.

#### C-M4. The diffusers ACE LM loads on the primary but reports an auxiliary

The registered [diffusers ACE variant](../server/config.py#L952) can take the
nonnative LM branch. That branch loads the LM with
[`to(state.device)`](../server/ace_step_loaders.py#L796) but records
[`state.lm_device` from the requested device](../server/ace_step_loaders.py#L807).
An auxiliary LM request therefore reports a split placement while loading the
LM on the primary, potentially exhausting the primary GPU. This variant is
marked untested; the mismatch itself is visible in source. **Direction:**
resolve the LM device before loading and pass that same device to `to()`.

#### C-M5. Audio Lab accepts `clap_variant` without applying it to scoring

The ranked and score requests both expose
[`clap_variant`](../server/routers/audio_lab.py#L311). Ranked generation only
checks that some CLAP is loaded and [drops the requested variant](../server/routers/audio_lab.py#L614)
([payload](../server/routers/audio_lab.py#L628)); the direct
[`/score` route](../server/routers/audio_lab.py#L800) never reads the field.
Requests can silently score with a different loaded CLAP model. **Direction:**
select or verify the exact loaded variant, or reject mismatched requests.

#### C-M6. A valid chat session can exceed the worker's text limit

Session creation allows a system prompt up to the worker's text cap, and each
message can independently reach that cap
([schemas](../server/routers/chat_sessions_routes.py#L49)). The history renderer
adds the system prompt, all earlier turns, the current message, and role labels
without an aggregate bound ([renderer](../server/chat_sessions.py#L184)). The
route forwards that string ([payload](../server/routers/chat_sessions_routes.py#L112))
to a worker whose [`text` field has the same single cap](../server/omni_worker.py#L1221).
Even a near-limit system prompt plus a short user turn can yield a worker 422;
long sessions become unusable. **Direction:** budget and trim rendered history
before dispatch, while preserving the current turn and explicit truncation
semantics.

#### C-M7. Chat-session `response_format` is silently ignored

The session message schema accepts [`response_format`](../server/routers/chat_sessions_routes.py#L63)
and both routes forward it (for example, [synchronous](../server/routers/chat_sessions_routes.py#L125)).
The worker's [`InferRequest` schema](../server/omni_worker.py#L1221) has no such
field, while the stateless route handles JSON-mode preambles and coercion
itself ([implementation](../server/routers/workers.py#L106)). A session caller
can receive unconstrained text despite requesting structured output.
**Direction:** apply the same format handling in session routes or reject the
unsupported field explicitly.

#### C-M8. OpenAI-compatible chat can send an earlier image instead of the current one

[`_first_oai_image_b64`](../server/routers/workers.py#L555) scans messages from
oldest to newest and returns the first image it finds. The chat payload carries
only that one image ([builder](../server/routers/workers.py#L570)). When an
earlier turn contains image A and the current user turn contains image B, the
flattened current text is paired with A. **Direction:** choose the current
user turn's image, and define or reject unsupported multi-image history.

#### C-M9. Explicit remote binding rejects same-origin browser requests

The bridge supports [opt-in remote binding](../bridge.py#L43), but its Origin
check accepts only a loopback origin even when the Origin matches the Host
([check](../bridge.py#L627)); the proxy then returns 403
([gate](../bridge.py#L738)). The gateway's corresponding matcher has the same
loopback requirement ([check](../server/security.py#L26)). A browser opened at
an explicitly configured non-loopback app address sends same-origin `Origin`
headers on POSTs and is rejected; clients omitting `Origin` are unaffected.
**Direction:** allow exact same-origin Host matches for explicitly configured
remote hosts, retaining the rest of the authentication policy.

#### C-M10. A wrong-shaped API-key JSON file can stop gateway import

The key store catches invalid JSON but assumes the decoded value has `.get`
([loader](../server/key_store.py#L74)). A syntactically valid JSON array such as
`[]` raises `AttributeError` before the module-level
[`ApiKeyStore` singleton](../server/key_store.py#L208) finishes initializing.
This is another persistence-shape failure, separate from the output metadata
case in B-M2. **Direction:** validate the top-level object and `keys` list,
then quarantine or report malformed data without preventing startup.

#### C-M11. The Media ZIP path buffers a multi-gigabyte download in the browser

The UI requests a [5 GiB ZIP limit](../server/static/tab-media.js#L792), then
awaits [`resp.blob()`](../server/static/tab-media.js#L806) before offering the
file for download. The server supports that size and
[streams the response](../server/routers/outputs.py#L1258). Large exports can
exhaust browser/WebView resources or delay saving until the complete archive
has been materialized client-side. **Direction:** use a browser download path
that streams to the destination, or impose a realistic UI size limit.

### Lower-priority behavior gaps

#### C-L1. Setup install responses can say running while the job is queued

The job manager creates download jobs as
[`status="queued"`](../server/jobs.py#L281) until capacity is available, but a
setup route returns hard-coded [`"status": "running"`](../server/routers/setup.py#L1050)
immediately after creation. Other setup install responses follow the same
pattern. **Direction:** return the created job's actual status.

#### C-L2. Lightbox deletion can remove the wrong item from the visible list

The lightbox captures a file, awaits its delete request, then splices
`state.files` at the [current mutable lightbox index](../server/static/tab-media.js#L1379).
Navigation can change that index while the request is pending
([navigation](../server/static/tab-media.js#L879)). The server deletes the
captured file, but the UI removes a different row or, after closing, the last
row. **Direction:** capture the index or remove the completed file by its
stable path, then reconcile the current lightbox position.

#### C-L3. A failed cached-model copy can leave a large `.part` file

When hardlinking is unavailable, materialization copies into a deterministic
`.part` path ([fallback](../server/file_materialize.py#L100)). The copy occurs
before the cleanup [`try` block](../server/file_materialize.py#L109). A read,
write, or metadata-copy failure midway bypasses the unlink handler and leaves
the partial model in the target directory. **Direction:** put fallback copy
inside the cleanup guard and remove the partial file on every failure.

Only this report was edited during this pass. No live behavior was exercised.

## Additional independent audit pass — 2026-09-25

This pass checked install result reporting, saved workflow behavior, browser
transport, and ACE loading after the preceding findings were written. Each
entry below has a distinct trigger from the earlier report. No runtime action
or executable test was used to establish the outcome.

### High-priority finding

#### D-H1. Workflows renders accumulate Run handlers and can queue duplicate jobs

The Workflows tab [renders twice on an ordinary load](../server/static/tab-workflows.js#L30).
Each render replaces its child HTML and calls
[`bindEvents`](../server/static/tab-workflows.js#L224), which attaches a fresh
delegated click listener to the *persistent* `#tab-workflows` element
([listener](../server/static/tab-workflows.js#L425)). No guard or removal
prevents accumulation. Each listener handles one Run click by calling
[`queue`](../server/static/tab-workflows.js#L430), which can submit
[`POST /api/workflows/{filename}/run`](../server/static/tab-workflows.js#L796)
after separate plan/confirm steps. A normal first load already has multiple
handlers; later renders increase the count. If the user accepts their prompts,
one click can queue multiple expensive GPU workflows. Other delegated actions
also repeat. **Direction:** attach the delegated listener once, or remove the
previous listener before rebinding; keep transient child listeners separate.

### Medium-priority findings

#### D-M1. The Workflows UI overrides a saved GPU policy after reload

The tab initializes placement controls to [`auto` defaults](../server/static/tab-workflows.js#L5).
It can [save a policy in workflow metadata](../server/static/tab-workflows.js#L576),
but its [load path](../server/static/tab-workflows.js#L30) never restores that
policy into the controls. Analyze and Run always send a freshly constructed UI
placement ([analyze](../server/static/tab-workflows.js#L520),
[run](../server/static/tab-workflows.js#L796)). The run route uses saved
metadata only when request placement is absent
([selection](../server/routers/workflows.py#L1536)). After reload, a saved
manual/UUID policy can silently become the UI's auto policy. **Direction:**
hydrate controls from saved policy or omit placement when the user has not
changed it; show the effective policy before execution.

#### D-M2. Browser Comfy WebSockets through the bridge fail the Origin check

For a WebSocket upgrade, the bridge forwards the browser's `Origin` but drops
its `Host` and adds [`Host: 127.0.0.1:<gateway-port>`](../bridge.py#L892).
The gateway requires `Origin` to match the forwarded Host
([WebSocket guard](../server/routers/comfy.py#L891),
[matcher](../server/security.py#L26)). A browser opened at the normal bridge
port sends that port in its Origin, so the gateway rejects the upgrade before
token validation. Clients without an Origin header are unaffected. This is
separate from C-M9's non-loopback browser restriction. **Direction:** preserve
the public Host for Origin validation through a trusted proxy header, or make
the bridge and gateway agree on the original public origin.

#### D-M3. A failed reinstall can be reported as completed because old weights exist

The setup job adapter changes an `error` job to `completed` when the error text
does not match a limited rule set and the target passes a
[presence check](../server/routers/setup.py#L744)
([grouped status](../server/routers/setup.py#L800),
[individual status](../server/routers/setup.py#L865)). That check looks for
weights in the target and, where applicable, an override marker; it does not
prove the *attempted* install produced a complete new snapshot or revision.
An existing older installation can therefore mask a failed update/reinstall,
even while the underlying job has an error and nonzero return code.
**Direction:** retain the process failure as job status and report existing
weights separately; verify the attempted revision before claiming success.

#### D-M4. Blueprint asset planning includes saved workflows the installer never scans

The API's template roots include the saved [`WORKFLOWS_DIR`](../server/routers/assets.py#L236),
and blueprint asset [planning](../server/routers/assets.py#L765) and install
counts use that scan ([route](../server/routers/assets.py#L1081)). The install
job invokes `install_model.sh comfy-blueprints`
([argv](../server/routers/assets.py#L1118)), whose `blueprint_files()` scans
Comfy blueprints, Comfy user workflows, and packages, but
[omits `WORKFLOWS_DIR`](../server/install_model.sh#L1041). A saved workflow with
a declared downloadable model can appear in dry-run or the install count but
be skipped by an all-template install; selecting only that filename instead
fails as unknown to the script. **Direction:** give planning and execution one
shared template inventory or pass exact planned assets to the installer.

#### D-M5. ACE Simple Mode rejects a query-only request

The Simple route says it plans from a query and generates in one call
([route](../server/routers/ace_step.py#L1270)), but `_SimpleRequest` inherits a
required [`prompt`](../server/routers/ace_step.py#L321) while also requiring
[`query`](../server/routers/ace_step.py#L449). A request with the planner query
alone receives a Pydantic 422 before the route can replace the prompt with the
planned caption. The UI [duplicates query into prompt](../server/static/tab-ace-step.js#L1666)
to work around this. **Direction:** make prompt optional for Simple Mode and
derive it from the plan, or document and validate its actual required role.

#### D-M6. ACE LM device and backend changes can report `unchanged`

The ACE load request accepts `lm_device` and `lm_backend`
([schema](../server/routers/ace_step.py#L310)), but its LM reuse check compares
only loaded state and variant ID ([check](../server/routers/ace_step.py#L594)).
If the same variant is requested on another available device or with another
backend, the route returns [`unchanged: true`](../server/routers/ace_step.py#L610)
without calling the loader that applies those choices
([loader](../server/ace_step_loaders.py#L753)). **Direction:** include effective
device and backend in the reuse check, or explicitly unload/reload on a changed
request.

#### D-M7. A diffusers ACE pipeline rebuild can leave its LM detached

[`_free_model`](../server/ace_step_loaders.py#L460) discards the current pipe
but retains LM state, while the new diffusers pipe
([builder](../server/ace_step_loaders.py#L625)) does not attach that retained
LM. The gateway skips `load_lm` for the same retained variant
([reuse check](../server/routers/ace_step.py#L594)), so switching diffusers
DiT variants can report an LM as loaded even though it was not attached to the
new pipe. Rebuilding the bundled VAE follows the same pattern
([rebuild](../server/ace_step_loaders.py#L1369)). This is specific to the
diffusers path; native ACE passes the LM separately. **Direction:** reattach the
retained LM whenever a diffusers pipe is rebuilt, or clear LM loaded state and
require an explicit reload.

#### D-M8. Hugging Face token checks block the gateway event loop

The async setup status and token-save routes call
[`_check_hf_token`](../server/routers/setup.py#L235) inline
([status](../server/routers/setup.py#L703),
[save](../server/routers/setup.py#L1282)). On a cache miss, that helper makes a
synchronous [`urllib.request.urlopen` call with an 8-second timeout](../server/routers/setup.py#L261).
A slow or unreachable remote check can stall unrelated gateway requests until
it finishes. **Direction:** run validation in an async HTTP client or bounded
thread, coordinating cache refreshes so simultaneous status reads do not all
block on the same remote service.

#### D-M9. Override replacement can destroy the previous working install

Override installers build a temporary tree, then run `rm -rf "$target"`
before `mv "$target_tmp" "$target"`
([generic path](../server/install_model.sh#L228),
[Qwen path](../server/install_model.sh#L292),
[MiniCPM path](../server/install_model.sh#L333)). A cancellation, crash, or
move failure between those commands removes the previous working override
while the replacement is incomplete. Weight snapshot commits already have a
backup/rollback pattern ([comparison](../server/snapshot_install.py#L94)).
**Direction:** rename the previous override to a backup, publish the staged
tree, and restore the backup on every failed publish.

#### D-M10. Normal Workflows navigation discards an unsaved editor draft

Returning to the tab calls [`load()`](../server/static/tab-workflows.js#L20),
which renders twice ([path](../server/static/tab-workflows.js#L30)). Render
creates a blank filename field and JSON textarea, then replaces the tab's
[`innerHTML`](../server/static/tab-workflows.js#L209). It does not capture or
restore an unsaved draft. A user who opens a template or edits a graph, visits
Models/Comfy to inspect prerequisites, and returns loses both the JSON and
filename without warning. **Direction:** retain draft state across renders
and tab navigation, or warn before discarding it.

### Lower-priority behavior gaps

#### D-L1. CLI `comfy free` never asks Comfy to free cached memory

The CLI sends `{}` by default and only `{"unload_models": true}` with
`--unload-models` ([command](../cli/omni.py#L327)). The app UI sends both
`unload_models` and `free_memory`
([UI](../server/static/tab-comfy.js#L657)), matching the documented
[best-effort release request](../docs/comfy-api.md#L640). Thus the CLI command
never requests the cached-memory part, and its default requests neither
action. **Direction:** expose `free_memory` explicitly and make the command's
default behavior match its name and documented release semantics.

#### D-L2. Transient Hugging Face HTTP errors are treated as invalid tokens

The token checker sets `token_valid=False` for
[every HTTP error](../server/routers/setup.py#L270), including non-authentication
responses such as 429 or 503. The save route then
[rejects the token](../server/routers/setup.py#L1287). A service or rate-limit
failure can prevent saving an otherwise valid token. **Direction:** classify
401/403 as rejected credentials and report other HTTP failures as temporarily
unverifiable, consistent with the existing network-exception path.

Only the audit report was edited in this pass. Live services and source files
were not changed.

## New cross-library static audit pass — 2026-09-25

This pass independently traced deletion paths, chat admission, Comfy Manager
mutation, placement, audio generation options, and browser media listing.
Findings below were checked against the earlier sections. The code paths are
confirmed in source; no request, model run, or destructive operation was made.

### High-priority findings

#### E-H1. Deleting a LoRA named `.` deletes the whole LoRA store

The [LoRA deletion route](../server/routers/setup.py#L1221) passes its decoded
`name` directly to [`safe_child_path`](../server/helpers.py#L43). That helper
rejects empty names, separators, and `..`, but accepts `.`; resolving
`LORA_DIR / "."` returns `LORA_DIR` itself
([resolution](../server/helpers.py#L53)). The route then checks that it is a
directory and calls [`shutil.rmtree`](../server/routers/setup.py#L1224).
An authenticated request with an encoded dot path segment can therefore remove
the entire LoRA library instead of one adapter. This is distinct from the
earlier final-symlink deletion findings. **Direction:** require a true child
path, reject `.` explicitly, and apply the same strict name format used by
other recursive deletion routes.

#### E-H2. An invalid chat tool can strand a worker in `busy` state

The nonstream chat route [marks a worker busy](../server/routers/workers.py#L331)
before calling `_apply_request_preambles`, but enters its cleanup `try/finally`
only afterward ([sequence](../server/routers/workers.py#L333)). `tools` is
typed only as `list[dict]` ([schema](../server/routers/workers.py#L90)); a
schema-valid entry such as `{"function":"bad"}` makes
[`render_tool_preamble` call `.get` on a string](../server/tools_compat.py#L81).
The request fails before [`_release_worker`](../server/routers/workers.py#L359),
and health checks [skip a living busy worker](../server/worker_manager.py#L759).
That worker remains unavailable for subsequent chats until manual cleanup or
restart. **Direction:** validate tool shapes before claiming a worker and put
all post-claim processing inside the worker-release guard.

### Medium-priority findings

#### E-M1. Independent Manager jobs can reset each other's shared queue

Manager model installs use a per-file
[`active_key`](../server/routers/extensions.py#L741), while extension mutations
use a different per-instance key
([enqueue](../server/routers/extensions.py#L906)). Both enter
[`_run_queue`](../server/routers/extensions.py#L467), which checks
`is_processing`, then awaits queue reset, one or more enqueue calls, and start
without a per-instance lock. Two jobs can both observe idle, interleave those
calls, and clear or mix each other's pending mutations. This also affects two
different model files on one instance. **Direction:** serialize the entire
Manager queue transaction per Comfy instance and reserve that instance before
enqueuing a Manager job.

#### E-M2. A failed workflow save can still replace its graph

`PUT /api/workflows/{filename}` validates the graph and
[writes it atomically](../server/routers/workflows.py#L1426) before validating
and writing metadata ([order](../server/routers/workflows.py#L1428)). Invalid
tags can then raise HTTP 400 in
[`_normalise_tags`](../server/routers/workflows.py#L170). The caller sees a
failed save, but the new graph is already on disk with its previous metadata
and placement policy. **Direction:** validate all metadata before publishing
either file, then commit graph and metadata with a recoverable transaction.

#### E-M3. Comfy host-memory analysis can count the wrong same-name weight

The ordinary-graph footprint scan looks for each referenced filename across
all model categories and [stops at the first match](../server/comfy_placement.py#L720).
If distinct category files share a relative name, multiple graph references
can resolve to the same first file; their actual combined weight footprint is
undercounted. The [host budget gate](../server/comfy_placement.py#L1012) can
then approve a graph that maps more model bytes than budgeted. **Direction:**
resolve each weight using its node's expected category, or conservatively count
all plausible distinct matches instead of the first arbitrary category.

#### E-M4. Snapshot recovery can restore an older extension state

After requesting a new Manager snapshot,
[`_save_snapshot`](../server/routers/extensions.py#L414) falls back to the
first item from the entire post-save list when no new ID appears
([selection](../server/routers/extensions.py#L419)). On a later mutation or
verification failure, the recovery path restores that returned ID
([restore](../server/routers/extensions.py#L652),
[caller](../server/routers/extensions.py#L893)). If snapshot creation produced
no new listed ID, this can roll the instance back to an unrelated older
snapshot. **Direction:** require a positively identified new snapshot before
starting the mutation or offering rollback; otherwise fail safely.

#### E-M5. Manual placement can silently change the requested primary GPU

The analyzer resolves and validates explicit
[`primary_device`](../server/omni_placement.py#L177), but manual mode keeps
eligible devices in caller order
([selection](../server/omni_placement.py#L203)) and returns their first entry
as [`primary_device`](../server/omni_placement.py#L320). With primary `cuda:1`
and eligible `[cuda:0,cuda:1]`, it reports `cuda:0` as primary. The worker
launcher uses that reported primary
([launch](../server/worker_manager.py#L244)). **Direction:** put the explicit
primary first in the selected pool and preserve the intended logical mapping,
or reject inconsistent ordering.

#### E-M6. ACE can mistake its auxiliary GPU for the requested DiT GPU

[`_ace_worker_has_device`](../server/routers/ace_step.py#L117) accepts a
requested device if it occurs anywhere in the worker's `gpu_pool`. The load
route uses this helper to select the worker for the requested DiT device
([selection](../server/routers/ace_step.py#L143),
[call](../server/routers/ace_step.py#L557)). A worker with DiT primary
`cuda:0` and LM auxiliary `cuda:1` can be reused for a later request asking
for DiT on `cuda:1`; its DiT remains on the original primary. **Direction:**
match DiT device to the worker's primary, and LM device separately to its
auxiliary placement.

#### E-M7. MiniMax Music 3 has no way to select a loaded second worker

The load route can [spawn or reuse by explicit device](../server/routers/minimax_music3.py#L207),
so two Music 3 workers can be resident. State and generation resolve the
[first matching worker without a selector](../server/routers/minimax_music3.py#L185)
([generation](../server/routers/minimax_music3.py#L254)); unload likewise
touches only [`workers[0]`](../server/routers/minimax_music3.py#L305).
The second loaded model cannot be deliberately used or unloaded through these
routes. **Direction:** accept an exact worker/device selector on these existing
routes and make an unqualified unload's scope explicit.

#### E-M8. Diffusers Audio Lab ignores accepted sampler, sigma, and A2A noise controls

The Audio Lab schema accepts `sampler`, `sigma_min`, and `sigma_max`
([fields](../server/routers/audio_lab.py#L297)). Native generation forwards
them, but the [diffusers call](../server/audio_lab_loaders.py#L1016) omits all
three. The same omission occurs in the diffusers A2A and unconditional calls
([A2A](../server/audio_lab_loaders.py#L1219),
[unconditional](../server/audio_lab_loaders.py#L1484)). A2A also accepts
[`init_noise_level`](../server/routers/audio_lab.py#L900), passes it to native
generation, but omits it from the diffusers call and still
[echoes it in the result](../server/audio_lab_loaders.py#L1230). These valid
controls silently have no effect for diffusers variants. **Direction:** map
them to supported pipeline arguments or reject them by format with an explicit
capability response.

#### E-M9. Audio Lab replaces valid zero guidance and noise values with defaults

`cfg_scale=0.0` passes the route schema
([field](../server/routers/audio_lab.py#L302)), but generation converts
`params.get("cfg_scale") or 7.0` to float
([loader](../server/audio_lab_loaders.py#L1004)); A2A and inpaint repeat this
pattern. A2A likewise accepts `init_noise_level=0.0`
([schema](../server/routers/audio_lab.py#L900)) but replaces it with `0.7`
([loader](../server/audio_lab_loaders.py#L1193)). Native calls then use the
wrong values. **Direction:** default only when a field is absent or `None`,
preserving explicit zero.

#### E-M10. Nonstream Qwen and AnyGPT ignore `top_p`

The worker accepts [`InferRequest.top_p`](../server/omni_worker.py#L1221), and
streaming Qwen/AnyGPT generation passes it
([Qwen](../server/omni_worker.py#L1083),
[AnyGPT](../server/omni_worker.py#L1125)). Their nonstream paths read other
sampling options but omit `top_p` from
[`gen_kwargs`](../server/omni_worker.py#L839) or the direct
[`generate` call](../server/omni_worker.py#L919). Changing `top_p` on a valid
nonstream chat request therefore has no effect for these models. **Direction:**
use the same sampling arguments in both execution paths.

#### E-M11. CLI token cache is briefly created with broad file permissions

`session refresh` calls [`cache_user_token`](../cli/omni.py#L81), which writes
the admin token with `Path.write_text()` and only then applies POSIX mode
`0600` ([order](../cli/client.py#L84)). On a multi-user POSIX host with an
ordinary `022` umask, the new file is initially readable by other local users;
if interrupted before `chmod`, that state persists. **Direction:** create the
file with restrictive mode at open time and atomically replace the cache file.

#### E-M12. Media's sort options cannot reach files beyond the newest 500

The Media tab always requests [`limit=500`](../server/static/tab-media.js#L227)
and sorts only the returned array for “Oldest first,” “Largest first,” and name
([client sort](../server/static/tab-media.js#L243)). The API sorts all matches
newest first and truncates to that limit
([selection](../server/routers/outputs.py#L617)); it exposes no offset or
cursor in this route ([schema](../server/routers/outputs.py#L552)). Once a
source has over 500 matches, the UI's alternative sorts are only sorts of the
newest subset, and older files cannot be browsed without narrowing filters.
The [manual lists these sort controls](../manual/05-media-library.md#L26), and
the API inventory incorrectly calls this route paginated
([entry](../docs/api-capability-inventory.md#L411)). **Direction:** add a
consistent server-side sort and browse mechanism on the existing route, then
correct the API inventory to match its actual contract.

#### E-M13. Tool and JSON preambles can push accepted chat past the worker limit

`ChatRequest.text` is capped at
[`INFER_MAX_TEXT_CHARS`](../server/routers/workers.py#L80), but
[`_apply_request_preambles`](../server/routers/workers.py#L95) prepends tool
definitions and JSON-mode instructions after request validation and returns a
`model_copy(update=...)` without checking the combined text
([copy](../server/routers/workers.py#L112)). The worker applies the same cap
to the forwarded [`InferRequest.text`](../server/omni_worker.py#L1221). A
near-limit prompt with valid tools or response format can thus pass the public
schema and fail at the worker with 422. **Direction:** reserve a preamble budget
before accepting the request and validate the final text before dispatch.

#### E-M14. Chained LTX guides can be rejected by a false staged-memory sum

Every `OmniLTXStageGuide` is assigned the same
[`stage=3`](../server/comfy_placement.py#L549), and the planner sums all
same-stage estimates on a device
([projection](../server/comfy_placement.py#L908),
[accumulation](../server/comfy_placement.py#L979)). The documented graph can
chain one guide per reference ([guide](../docs/comfy-api.md#L167)), while each
guide unloads its VAE before the next executes
([finally](../comfy_nodes/omni_bridge/nodes/omni_staged_ltx.py#L279)). Two
sequential guides can therefore be blocked for the sum of their VAE memory
even when the true peak fits. **Direction:** model their sequential release
boundaries or report a conservative estimate without presenting it as a hard
capacity blocker.

### Lower-priority resource gap

#### E-L1. Comfy log tail reads the whole file on the gateway event loop

[`GET /api/comfy/{instance_id}/logs`](../server/routers/comfy.py#L815) reads
the entire log file and splits every line before taking only the requested
tail ([read](../server/routers/comfy.py#L823)). A large long-lived Comfy log
can consume substantial memory and stall unrelated async requests even when
the caller asks for a few lines. **Direction:** seek backward or use a bounded
tail reader in a thread, with a byte cap as well as a line cap.

Only this report was edited during the pass. These findings have not been
reproduced against a running distro.


## Repair campaign - authorized 2026-09-25

The user authorized source fixes after the read-only passes above. Historical
findings remain intact; this ledger records their disposition and verification.
No model weights have been loaded and no running gateway, worker, Comfy process,
or distro has been stopped or refreshed during this repair campaign.

### Batch 1 - paths, credentials, metadata, and bounded output operations

| Findings | Source changes | Verification / remaining work |
| --- | --- | --- |
| E-H1, F-H2, B-H1 | Child paths reject `.` and root aliases; containment checks retain the requested final directory entry, allowing symlink deletion checks to work. | Regression covers root rejection and deleting an in-tree link without deleting its target. |
| H1, S-H2 | Windows-to-WSL ingress now authenticates a per-launch capability stored in the private relay lease; remote session bootstrap is rejected at bridge and gateway. | Relay transport test passes; bridge ingress and gateway authorization integration checks remain pending. |
| S-H1 | Gateway applies read/generate/manage/admin scopes to existing route families. | Scope classification regression passes; complete route-map review remains pending. |
| G-H2, C-M10; empty-scope issuance note | Key stores reject malformed shapes, reject empty issued scopes, write atomically, propagate persistence errors, and roll back failed in-memory mutations. | Failed revocation/reload and malformed-store regressions pass. |
| F-H3, S-M3 | Comfy HTTP proxy strips Authorization; bridge error logs omit query strings and exception URLs. | Credential-filter regression passes. |
| M14, E-M11, F-M3 | Invalid gateway credentials return 401; CLI refresh bypasses its cache, runtime token precedes cache, remote bases require an explicit credential, and cache creation is private and atomic. | CLI privacy and remote-disclosure regressions pass; browser rotation integration pending. |
| C-M9, D-M2 | Remote-origin opt-in accepts only matching HTTP(S) hosts; WebSocket bridge retains original Host for origin validation. | Origin regression passes; browser WebSocket integration pending. |
| B-M6 | Bridge rejects unsupported Transfer-Encoding with 411 and closes the connection instead of silently discarding the request body. | Explicit Content-Length is required; integration regression pending. |
| M11, G-M2, B-M2, F-M7 | Metadata uses `(kind, path)`, migrates legacy records to output, rejects malformed shapes, reports failed saves, rolls back memory, and clears metadata during deletion/pruning. | Root collision, legacy migration, and disk-failure regressions pass. |
| M12 | Failed prune deletions are reported separately and excluded from deleted/freed totals. | Failure-path regression pending. |
| G-H1, G-M5 | PNG metadata has aggregate decompression bounds; valid range ends beyond EOF are clamped. | Compression-bomb and range regressions pass. |
| S-M7, S-M8, E-M12 | ZIP route executes in FastAPI's worker thread; list scans use a thread; existing listing supports offset and server-side sort. | Pagination regression passes; Media UI pagination still pending. |
| E-L1 | Comfy logs use a bounded 256 KiB tail read off the event loop. | Source change; focused regression pending. |
| C-M1, C-L3 | Snapshot indexes reject external shard paths; partial-file cleanup encloses fallback copying. | Focused regressions pending. |
| F-M12 | Current baseline already uses a 1,860-second bridge timeout for the 1,800-second audio contract. | Historical finding does not reproduce in the starting source. |

Verification so far: isolated `python -B -m unittest` execution of
`tests.test_audit_repairs`, `tests.test_outputs_path_safety`,
`tests.test_output_artifact_metadata`, `tests.test_security_and_registry`, and
`tests.test_windows_loopback_relay`: **38 tests ran successfully: 37 passed and one existing POSIX-only
test skipped on Windows**. The new in-tree symlink regression passed on this
host. Child stdout/stderr was redirected to a temporary file, separate from the
interactive chat. No full-suite or live-model claim is made.

All findings not marked above remain under review; this campaign is not complete.


### Batch 2 - chat, UI request ownership, and CLI contract fixes

| Findings | Source changes | Verification / remaining work |
| --- | --- | --- |
| E-H2, E-M13; unsupported precision note | Tool/format shapes and combined preamble length are checked before reserving a worker; precision is limited to fp16/bf16/fp32. | Malformed-input and preamble-limit regressions pass. |
| S-M1, A-M7, F-M2, H6 (server) | Native/OpenAI streaming share body-owned worker reservations and explicit nested-generator closure; structured output is buffered and parsed; disconnects request abort and retire a stuck producer; transport failures do not advertise it as ready. Worker producer cleanup sets abort and joins before releasing the inference lock. | Reservation/disconnect and structured-parser tests pass. Real model streaming remains unqualified. |
| B-M7, B-M8, B-M9, B-M10, C-M6, C-M7, F-M13 | Sessions reuse stateless timeout/format handling, reject concurrent turns, enforce TTL on access, retain latest media context, trim old text to worker limits, and bound stored media memory. | TTL, concurrent turn, prompt trim, media context, JSON preamble and memory-budget regressions pass. |
| C-M8, E-M10 | OpenAI image selection uses the latest attachment; Qwen/AnyGPT nonstream generation forwards top_p. | Latest-image regression passes; generation kwargs checked in source without weights. |
| F-H1, H6 (UI), A-M9 | Chat escapes media attributes, tracks session-fetch generations, and captures original model/job/session for cancellation. SSE accepts job ID from response headers. | UI media escaping regression and node syntax checks pass; interactive streaming not run. |
| C-H2, C-L2, E-M12 | Media discards superseded listings, captures storage roots before asynchronous mutations, removes the originally deleted row, pages through listings and requests server-side sorting. | Stale-listing and multi-file delete-root tests pass. |
| C-M11 | ZIP exports stream to a chosen file where the browser supports it; fallback blob exports are capped at 128 MiB. | JavaScript syntax verified; actual file-picker behavior awaits browser verification. |
| D-H1, D-M1, D-M10, A-M10, M16 | Workflow delegated handler is replaced on render; a run is single-flight; saved GPU policies hydrate controls; draft text/name survive renders; analysis/run retain the same instance, policy and draft; GPU labels follow instance pool order. | UI tests cover handler replacement, saved policy, GPU order, frozen run context and double click. |
| M15; unreachable legacy renderer note; concurrent install submission note | Setup preserves all entered fields and open panels across polls; unused `_renderLegacy` removed; workflow missing-model requests are submitted sequentially. | Node syntax checks pass. |
| G-M3, G-M4, A-M1, D-L1, B-L2, S-M10 | CLI get/delete/zip/prune select a root; durations map to older_than_days; maintenance uses policy envelope; free sets free_memory; previews use unique temporary files; HF credentials come from a hidden prompt or stdin. | Contract regressions pending. |

Additional defects discovered and fixed during repairs:

- **R-1:** Stateless chat autospawn created another model worker when existing
  matching workers were busy. It now returns busy before spawning. Regression
  confirms no spawn call occurs.
- **R-2:** Native chat media markup also interpolated role/id attributes without
  escaping. These attributes now use the same escaping helper as media URLs.
- **R-3:** Plain-text response_format could incorrectly activate JSON coercion.
  Text mode stays plain text; unknown format types now return a validation error.
- **R-4:** AnyGPT accepted temperature zero but still requested sampling with a
  zero temperature. It now switches sampling off for zero.

Verification: both audit repair unittest modules ran **27 tests, all passed**.
`node tests/test_ui_audit_repairs.js` passed its fake-DOM regressions for media
escaping, stale listings, delete root ownership, event binding, GPU pool order,
saved policies, frozen run context and double clicks. Changed chat, app, Media,
Workflows and Setup JavaScript passed `node --check`. No browser, model, live
Comfy queue or process signal was used for these checks.


### Batch 3 - workflow persistence and process/job lifecycle

| Findings | Source changes | Verification / remaining work |
| --- | --- | --- |
| E-M2, F-M4, F-M5, A-M3, A-M5 | Validate metadata before graph writes; restore graph on metadata-write failure; publish no-overwrite files atomically; imports clear unrelated metadata; explicit null clears placement; corrupt metadata blocks execution instead of silently removing policy. | Invalid metadata, rollback, concurrent import, null clear, corrupt store and replacement-policy tests pass. |
| M2, A-M4 | Run preflight requires ready_to_run; object-info cache includes process generation; partial nodes, missing required inputs and dangling links are blockers. | Structural regressions and existing workflow requirement tests pass. |
| H2 | Durable process records gain start time; orphan/shutdown paths verify exact script and port; recorded groups cannot redirect signals to an unrelated/shared group; legacy fallbacks no longer accept a matching basename alone. Bridge fallback verifies its gateway command. | Synthetic identity/group regressions and mocked shutdown/GC tests pass; no real process signals issued by these tests. Bridge fallback integration remains pending. |
| M1, A-M2, M7 | Recovered starting Comfy instances are re-probed; CPU device selection supplies --cpu; failed workload-cgroup admission fails launch. | Syntax checked; mocked manager regression work remains pending. |
| M13, G-M1, S-M2 | Subprocess creation is shielded until its handle can be cleaned; cancellation is checked immediately after spawn; owned groups can be cleaned after parent exit; timers are cancelled at completion; wait is bounded; log tails have monotonic cursors. | Spawn-window cancellation, dead-parent group and tail-rollover regressions pass; existing job lifecycle tests pass. |
| C-H1 | AGENTS and lifecycle guidance no longer promise worker adoption after SIGKILL. Source refresh guidance now requires a graceful restart when resident workloads may be stopped. | Checked against startup: worker orphan cleanup precedes Comfy recovery. This is a corrected operational contract, not new standalone worker adoption. |
| S-H3, C-M2, E-M5 | Standalone estimates account for FP32; manual primary is first; unused eligible GPUs do not count toward manual capacity; require_all checks actual map targets. | Placement regressions pending. |

New **R-5**: standalone workers also continued after cgroup admission failure,
like M7's Comfy instances. Both launch paths now fail admission and use their
existing process cleanup handlers.

Verification: isolated pytest across audit lifecycle repairs, workflow
requirements/search cache, jobs, CLI, snapshot installation, file materialization,
shutdown and PID GC: **63 passed**. Process identity and signalling tests use
synthetic records and mocked signals; subprocess lifecycle tests start only tiny
Python test children. This is not runtime/model qualification.


### Batch 4 - placement admission and faithful Comfy routing

- Fixed S-H3, C-M2, and E-M5: worker estimates include requested precision;
  manual capacity counts targets actually used by the map and preserves the
  selected primary. CPU plans also report their effective precision.
- Fixed H4, B-H2, F-M1, S-H4, and E-M14: shared Comfy device fields are allocated
  together; conflicting or unknown overrides block; zero free VRAM remains
  zero; linked duration uses the predictor's upper bound; separately unloaded
  guide nodes have separate residency intervals.
- Fixed H5 and E-M3: explicit CPU components and GPU offload share a finite RAM
  budget, staged graphs receive a host mapping check, and same-name files in
  different categories cannot disappear from host accounting. Unknown filename
  category mappings conservatively count all installed matches.
- Fixed M3 by correcting admission and the node's stated contract: generic VAE
  decode releases its own VAE but does not guarantee unloading upstream models.
  Upstream ordinary loaders remain resident in the plan. No global unload was
  added to a node where prior qualification observed a hang.
- Verification: **81 tests passed**, including the required Comfy placement,
  multigpu and workflow requirement suites, standalone placement routes, and
  eight new admission regressions. No model weights were loaded.

### Batch 5 - audio operations, persistence, and compatibility

- Fixed H3, M9, F-M11, B-L1, and E-M6: engine mutations have an admission gate
  before worker selection/progress; busy workers remain inspectable/cancellable;
  exact worker claims cannot overwrite another job; ACE primary selection no
  longer treats auxiliary membership as a primary match. Audio asset deletion
  refuses active installs or existing engine workers.
- Fixed D-M5/D-M6/D-M7/C-M4: Simple Mode accepts query-only input; LM reuse checks
  backend/device; diffusers LM loading uses the selected device and reattaches
  retained LM state after rebuilding the pipeline.
- Fixed F-M8/F-M9/B-M4/B-M5: native ACE batch outputs are all persisted; unsupported
  legacy batches fail explicitly; ranked seeds wrap safely; unscored results
  sort last; scorer discovery checks every ready Audio Lab worker.
- Fixed S-M5/S-M6/M10/F-M10: ranked candidates spool to disk, archives stream from
  temporary files, manifests exclude source base64, and temporary-file failures
  respect best-effort Omni output persistence. Partial ranked runs retain an
  incomplete manifest rather than an unexplained directory.
- Fixed A-M6/A-L1/C-M5/E-M8/E-M9: decoded audio has duration/channel/sample bounds;
  VAE decode checks declared shape and tensor size on CPU first; requested CLAP
  variant must match; diffusers rejects unsupported native sampler/noise knobs;
  valid zero guidance/noise is preserved. UI omits native-only controls from
  diffusers requests. MiniCPM audio decode is bounded before its 60-second check.
- Fixed E-M7/A-L2: existing MiniMax routes accept exact worker selection and reject
  ambiguous selection; failed hot swaps clear stale loaded-variant metadata.
- Fixed M8/A-M8/G-M7: STT upload size fits the worker's encoded limit and requires
  an explicit compatibility-route model; MOSS speech can be transcoded to the
  requested format; failed MOSS SFX workers are retired; composition drains a
  bounded stderr tail while the child runs.
- New R-6: second-resolution Omni filenames could collide across concurrent
  persistence calls. Added a random filename suffix, removing the check/replace
  collision window.
- New R-7: cancelled STT/TTS/SFX requests could release workers still computing.
  Cancellation now retires the exact worker under shielded cleanup; specialized
  audio requests use the same lifecycle rule.
- Verification: **48 tests passed** across audio lifecycle, ACE generation and
  staging, source handling, MiniMax contracts, composition, and 16 new audio
  regression tests. Includes end-to-end fake-worker ranked persistence, a real
  small subprocess emitting beyond stderr pipe capacity, and ZIP inspection.
  `node --check server/static/tab-audio-lab.js` passed. No model/service changes.
- Remaining verification: real backend inference and capacity qualification,
  ACE per-component admission, and MiniMax offload-budget enforcement remain
  separate from these synthetic checks; the campaign is not complete.

### Batch 6 - installers, maintenance, and Comfy mutations

- HF token validation moved off the event loop; transient HTTP failures leave
  validity unknown. Failed reinstall jobs retain failed status even if older
  weights exist; queued install responses now report their actual status.
- Hub cache pruning now inspects installed symlink references and shared blob
  hardlinks, skips referenced repositories and active installs, and fails closed
  when reference inspection fails. Pip purge supervises its child on cancellation
  and checks its exit status.

- Fixed H7/B-M3/D-M8/D-L2: installed Hub references and hardlinks prevent prune;
  maintenance and new gateway downloads exclude one another; pip cancellation
  terminates its own child; transient HF failures remain unknown.
- Fixed M4/M5/M6/S-H5/B-M1/F-M6: inventory requires exact nonempty filenames;
  whole-repository installs use isolated category subdirectories and remote-size
  admission; uploads publish without overwrite races; conflicting writes and
  deletion while Comfy instances exist are blocked. Custom-node updates verify
  repository identity and use the fetched ref.
- Fixed M17/M18/D-M9/D-M4: exact setup refs fail visibly; GPU detection also runs
  for venv repair; override publication preserves/rolls back the previous tree;
  blueprint installers scan saved workflows as well as shipped blueprints.
- Fixed S-H6/S-M4/S-M9/E-M1/E-M4: one Manager mutation queue, fresh identifiable
  recovery snapshots, cancellation/timeout cleanup, core-update coordination,
  required restart and prior-node-class checks for update_all. Core qualification
  checks OmniBridge classes and invalidates the template cache. Transport
  failure after Manager start also stops the owning instance before recovery.
  Recovery can relaunch a stopped owner before requesting snapshot restore.
- Fixed D-M3/C-L1: failed reinstall status remains failed despite older weights;
  queued install responses expose the actual job state.
- Initial installer/maintenance regression batch: **78 passed**. Later focused
  audio/installer/placement/setup/snapshot/maintenance/bridge/ACE checks:
  **80 passed**. These runs overlap.

### Batch 7 - completeness, source release, final regressions

- Fixed C-M3: ACE plans check primary DiT and auxiliary LM capacity separately.
  Exact load-time variants are checked against their actual visible destinations.
  Unknown custom capacity estimates fail admission. Catalog checks do not claim
  measured peaks or automatic reclamation of already resident weights.
- Shared status/verify-models validation requires completion markers, nonempty
  weights, no partial files, and all declared shard maps, including nested and
  unindexed models. The level is `file-presence-and-shard-map`, not tensor/hash
  integrity verification.
- Fixed M19/G-M6 and related docs: core update examples include a ref; the Windows
  CLI shim runs inside WSL, so Windows-drive examples use `/mnt/d/`. ZIP responses
  are synchronous archives. Metadata lives in the runtime metadata store.
  Concurrency, cancellation, worker selection and native-audio limits are explicit.
  The Comfy skill now uses current-free VRAM and host admission for staged graphs.
- MiniMax `cpu_memory_mb` is explicitly an admission estimate with a shared
  workload cgroup at runtime. Its UI selects exact workers and preserves inputs.
- Added the user-approved MIT license and `THIRD_PARTY_NOTICES.md`. Unreviewed
  binary fixture/report media remains local and is excluded from the default Git
  release pending provenance approval. The setup manifest accepts an explicit
  source root or clone CWD and preserves setup failure status. No package was
  built or published.

#### Additional defects found while repairing

| ID | Finding and resolution |
| --- | --- |
| R-8 | Manager model installs used an unregistered job kind. Registered `comfy_model_install`, including download guards. |
| R-9 | Lost Manager start/status responses could leave mutation active during recovery. Failure exits after start retire the owner; invalid queue state blocks mutation. |
| R-10 | A failed unlink could remove metadata from a surviving file. Shared deletion restores metadata on failure; prune counts only successful deletion. |
| R-11 | Read-only scope could admit ACE inference and autospawning state reads. Exact scope rules now protect these operations, composition and shutdown aliases. |
| R-12 | ACE VAE-only requests could do nothing. They now rebuild the current model with retained settings, or reject when no model is loaded. |
| R-13 | Missing/nonfinite Audio Lab scores could look like successful zero scores. They are now unscored and cannot outrank valid negative scores. |
| R-14 | ACE ranking could use a hot-swapped CLAP variant. Scoring takes the Audio Lab operation gate and rechecks the expected variant. |
| R-15 | Liveness checks used `os.kill(pid, 0)` on Windows. A shared query-only Windows helper now uses OpenProcess/GetExitCodeProcess and closes its handle; all liveness callers migrated. |
| R-16 | Final static checks found two repair implementation errors: an out-of-scope `job_dir` in ACE cleanup and a missing `shutil` import in Audio Lab ranking. Both are fixed with result-path regressions. |
| R-17 | Existing Comfy proxy HTTP methods shared an OpenAPI operation ID. Each existing operation now has a distinct ID; endpoint paths/methods are unchanged. |

For R-15, see [Python os.kill](https://docs.python.org/3/library/os.html#os.kill)
and the [Windows process query API](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getexitcodeprocess).
The discovery regression used its own test-process PID during registry
publication. This establishes an unsafe Windows path, but does not prove which
process/event disrupted the chat.

#### Verification incident and remaining boundaries

A directory-wide pytest invocation was attempted in a child interpreter with
output redirected. The user reported another chat disruption. Its result was
not captured and **is not counted as a successful run**. A narrowly filtered
check found no matching audit pytest process afterward. No broad run was
repeated. `tests/conftest.py` now blocks directory-wide pytest on Windows
unless a disposable host sets `OMNI_DISPOSABLE_TEST_HOST=1`. Redirection and
a child interpreter alone are not claimed to provide sufficient isolation.

Subsequent checks named reviewed files explicitly and retained temporary logs:

- Completion/storage/scope, docs and startup checks: **20 passed** at that revision.
- Final audio and completion regressions: **28 passed**.
- Windows process-query, synthetic recovery and temporary discovery: **14 passed**.
  The Windows liveness regression forbids signal calls.
- Final Manager/install/completion/manual checks: **46 passed**.
- Node VM UI regressions passed, including captured Music 3 worker cancellation.
  All seven changed browser modules passed `node --check`.
- Explicit Git Bash `-n` checks passed for setup and installer scripts.
  Python AST and Ruff F401/F811/F821/F841 checks passed after cleanup.

Counts overlap and must not be summed as unique tests. Required Comfy
placement/multigpu/workflow tests passed in batch 4. No weights were loaded
and no live model/service update, restart, install or deletion was intentionally
issued. The broad test incident means its effect on the interactive host
cannot be excluded merely from those source-only intentions.

**Source disposition:** all 139 numbered historical findings have a source
or contract correction, except F-M12, which was already nonreproducible in
the starting source. This does not certify the VHDX, external model backends,
browser/WebSocket behavior or CUDA memory peaks. Historical blocked models
remain unqualified. Runtime deployment, live-backend qualification and
media-rights approval remain release work.

### Finding-by-finding disposition

This table supersedes intermediate pending notes in batches 1-5. Fixed in
source means the audited defect/contract was corrected; batch evidence and
the live-qualification boundaries above still apply.

| Finding | Disposition | Repair batch |
| --- | --- | --- |
| H1 | Fixed in source | 1 |
| S-H2 | Fixed in source | 1 |
| E-H1 | Fixed in source | 1 |
| F-H2 | Fixed in source | 1 |
| B-H1 | Fixed in source | 1 |
| S-H1 | Fixed in source | 1; final follow-up in 7 |
| G-H2 | Fixed in source | 1 |
| C-M10 | Fixed in source | 1 |
| F-H3 | Fixed in source | 1 |
| S-M3 | Fixed in source | 1 |
| M14 | Fixed in source | 1 |
| E-M11 | Fixed in source | 1 |
| F-M3 | Fixed in source | 1 |
| C-M9 | Fixed in source | 1 |
| D-M2 | Fixed in source | 1 |
| B-M6 | Fixed in source | 1 |
| M11 | Fixed in source | 1 |
| G-M2 | Fixed in source | 1 |
| B-M2 | Fixed in source | 1 |
| F-M7 | Fixed in source | 1 |
| M12 | Fixed in source | 1; final follow-up in 7 |
| G-H1 | Fixed in source | 1 |
| G-M5 | Fixed in source | 1 |
| S-M7 | Fixed in source | 1 |
| S-M8 | Fixed in source | 1 |
| E-M12 | Fixed in source | 1 |
| E-L1 | Fixed in source | 1 |
| C-M1 | Fixed in source | 1 |
| C-L3 | Fixed in source | 1 |
| F-M12 | Already corrected in starting source; timeout regression retained | 1 |
| E-H2 | Fixed in source | 2 |
| E-M13 | Fixed in source | 2 |
| S-M1 | Fixed in source | 2 |
| A-M7 | Fixed in source | 2 |
| F-M2 | Fixed in source | 2 |
| H6 | Fixed in source | 2 |
| B-M7 | Fixed in source | 2 |
| B-M8 | Fixed in source | 2 |
| B-M9 | Fixed in source | 2 |
| B-M10 | Fixed in source | 2 |
| C-M6 | Fixed in source | 2 |
| C-M7 | Fixed in source | 2 |
| F-M13 | Fixed in source | 2 |
| C-M8 | Fixed in source | 2 |
| E-M10 | Fixed in source | 2 |
| F-H1 | Fixed in source | 2 |
| A-M9 | Fixed in source | 2 |
| C-H2 | Fixed in source | 2 |
| C-L2 | Fixed in source | 2 |
| C-M11 | Fixed in source | 2 |
| D-H1 | Fixed in source | 2 |
| D-M1 | Fixed in source | 2 |
| D-M10 | Fixed in source | 2 |
| A-M10 | Fixed in source | 2 |
| M16 | Fixed in source | 2 |
| M15 | Fixed in source | 2 |
| G-M3 | Fixed in source | 2 |
| G-M4 | Fixed in source | 2 |
| A-M1 | Fixed in source | 2 |
| D-L1 | Fixed in source | 2 |
| B-L2 | Fixed in source | 2 |
| S-M10 | Fixed in source | 2 |
| E-M2 | Fixed in source | 3 |
| F-M4 | Fixed in source | 3 |
| F-M5 | Fixed in source | 3 |
| A-M3 | Fixed in source | 3 |
| A-M5 | Fixed in source | 3 |
| M2 | Fixed in source | 3 |
| A-M4 | Fixed in source | 3 |
| H2 | Fixed in source | 3; final follow-up in 7 |
| M1 | Fixed in source | 3 |
| A-M2 | Fixed in source | 3 |
| M7 | Fixed in source | 3 |
| M13 | Fixed in source | 3 |
| G-M1 | Fixed in source | 3 |
| S-M2 | Fixed in source | 3 |
| C-H1 | Refresh instructions corrected; worker adoption not promised | 3 |
| S-H3 | Fixed in source | 4 |
| C-M2 | Fixed in source | 4 |
| E-M5 | Fixed in source | 4 |
| H4 | Fixed in source | 4 |
| B-H2 | Fixed in source | 4 |
| F-M1 | Fixed in source | 4 |
| S-H4 | Fixed in source | 4 |
| E-M14 | Fixed in source | 4 |
| H5 | Fixed in source | 4 |
| E-M3 | Fixed in source | 4 |
| M3 | Admission/docs corrected; upstream unload not promised | 4 |
| H3 | Fixed in source | 5 |
| M9 | Fixed in source | 5 |
| F-M11 | Fixed in source | 5 |
| B-L1 | Fixed in source | 5 |
| E-M6 | Fixed in source | 5 |
| D-M5 | Fixed in source | 5 |
| D-M6 | Fixed in source | 5 |
| D-M7 | Fixed in source | 5 |
| C-M4 | Fixed in source | 5 |
| F-M8 | Fixed in source | 5 |
| F-M9 | Fixed in source | 5 |
| B-M4 | Fixed in source | 5 |
| B-M5 | Fixed in source | 5 |
| S-M5 | Fixed in source | 5 |
| S-M6 | Fixed in source | 5 |
| M10 | Fixed in source | 5 |
| F-M10 | Fixed in source | 5 |
| A-M6 | Fixed in source | 5 |
| A-L1 | Fixed in source | 5 |
| C-M5 | Fixed in source | 5 |
| E-M8 | Fixed in source | 5 |
| E-M9 | Fixed in source | 5 |
| E-M7 | Fixed in source | 5; final follow-up in 7 |
| A-L2 | Fixed in source | 5 |
| M8 | Fixed in source | 5 |
| A-M8 | Fixed in source | 5 |
| G-M7 | Fixed in source | 5 |
| H7 | Fixed in source | 6 |
| M4 | Fixed in source | 6 |
| M5 | Fixed in source | 6 |
| M6 | Fixed in source | 6 |
| M17 | Fixed in source | 6 |
| M18 | Fixed in source | 6 |
| S-H5 | Fixed in source | 6 |
| S-H6 | Fixed in source | 6 |
| S-M4 | Fixed in source | 6 |
| S-M9 | Fixed in source | 6 |
| F-M6 | Fixed in source | 6 |
| B-M1 | Fixed in source | 6 |
| B-M3 | Fixed in source | 6 |
| C-L1 | Fixed in source | 6 |
| D-M3 | Fixed in source | 6 |
| D-M4 | Fixed in source | 6 |
| D-M8 | Fixed in source | 6 |
| D-M9 | Fixed in source | 6 |
| D-L2 | Fixed in source | 6 |
| E-M1 | Fixed in source | 6 |
| E-M4 | Fixed in source | 6 |
| M19 | Documentation contract corrected | 7 |
| G-M6 | Documentation contract corrected | 7 |
| C-M3 | Fixed in source | 7 |

### Unnumbered audit items

| Item | Disposition |
| --- | --- |
| Unreferenced Setup renderer | Removed; JavaScript checked. |
| Fast setup skipped OmniBridge copy | Copy moved ahead of fast exit; resident Comfy still needs deliberate later restart to load node changes. |
| Concurrent workflow downloads | UI submits verified installs sequentially. |
| Generic status and narrow verify-models | Shared completion/file/shard checks; partial/nested regressions passed. |
| Hardcoded source path | Manifest accepts source root/CWD; synthetic setup exit-code test passed. |
| Source license | MIT selected by user; third-party terms retained. |
| Unreviewed media rights | Binary fixtures/reports excluded from default Git release, retained locally; provenance approval remains external. |
| Installed distro missing paths/CLI | Setup source contains synchronization fixes; not deployed or qualified here. |
| Broad exception assertions in named tests | ACE source and blueprint tests assert intended HTTP errors. |
| Missing OmniBridge core qualification | Routing/staged VAE classes added. |
| Empty scopes elevated to admin | Empty issuance rejected; malformed stores handled. |
| Comfy skill claimed reusable VRAM | Current-free VRAM and all-graph host admission documented. |
| Partial ranked directories lacked manifests | Running/incomplete manifests and disk-spooled candidates retained. |
| MiniMax CPU estimate was described as a cap | API, plan warning, UI and docs now describe admission estimate/shared cgroup accurately. |
| MiniCPM decode before duration check | Decode bounded before allocation/resampling. |
| Unrestricted precision | Typed requests accept only fp16/bf16/fp32. |
| Previously blocked external models | Remain explicitly blocked/unverified; no live inference claim added. |

## Screenshot task and bounded browser qualification — 2026-09-25

The user authorized screenshots for the future GitHub repository/manual and
additional testing. The [capture report](2026-09-25-screenshots-and-browser-checks.md)
records 18 reviewed images, all 14 navigation destinations, and 16 distinct
browser checks. The [gallery](../manual/screenshots.md) and README now use the
actual app captures. No generation or GPU workload was started.

### Newly discovered and repaired during live visual review

| ID | Finding | Fix and verification |
| --- | --- | --- |
| UI-01 | Packaged startup failed on CRLF in `server/setup.sh`; text-mode tests had hidden the bytes. | Normalize LF. Explicit Git Bash syntax check and all 7 startup-layout tests pass, including a new raw-byte entrypoint check. Full dependency setup was not rerun. |
| UI-02 | Music 3's undefined CSS classes left controls ungrouped and textareas narrow. | Scoped responsive card/form CSS; real Chromium confirms usable control widths and retained inputs after Refresh. |
| UI-03 | Chat's capability map offered unwired Qwen audio/video and AnyGPT image/audio. | Use implemented modalities and a conservative unknown-model fallback; actual attachment gates and focused Node regressions pass. |
| UI-04 | Runtime copied an internal `8200/v1` URL into Windows client instructions. | Display the current browser origin in both API address locations; live `9200/v1` assertion passes. This does not rewrite session API metadata. |
| UI-05 | Filtering Media to zero matches left Loading displayed after request completion. | Render the result after clearing the loading flag in the current request's completion block; focused empty-result regression and live filter pass. |
| UI-06 | A routed Comfy skill retained obsolete staged-memory and VAE-unload claims. | Remove the staged host-admission exception and explain that generic VAE decode releases only its own VAE. |

### Runtime evidence and limits

The initial normal launch reproduced UI-01 before the bridge could start.
After fixing the source, a backed-up source-only refresh synchronized 72
changed gateway/bridge/CLI/guidance files into the inactive distro runtime and
restored missing model/output links. The existing Python environment was used;
the gateway became ready in approximately 45 seconds and its Windows relay
served the browser. Later UI changes were copied without a restart. Comfy's
separate custom-node tree was not deployed or executed by this refresh.

Live checks now cover 401/session recovery, transport failure recovery,
draft retention, empty-session lifecycle, empty Media filtering, workflow JSON
validation, three autospawn-false audio state routes, local Comfy installation
status, Shutdown confirmation cancellation, and narrow-window navigation.
Both initial and final worker/Comfy registries were empty. Published screenshots
show idle/uninstalled states with truthful captions. The Chat example session
was removed; drafts were never saved or submitted. Browser contexts were closed.

This evidence supplements the earlier source-only qualification. It does not
establish a complete installer run, actual gateway restart recovery, Comfy
execution, model loading, inference quality, multi-GPU behavior or external
media redistribution rights. See the capture report for exact evidence and
remaining scope. The bridge/gateway remains available and idle for review.

## Repository preparation and bounded portability check - 2026-09-25

The requested repository identity is `aivrar/Portable_Omni_Server`.
README/manual identity and the local `origin` remote were prepared.
No repository was created remotely and
no commit or push was performed. See the [release plan](../docs/github-release-plan.md).

The [portability guide](../docs/portability.md) records a bounded read-only
inspection of seven canonical runtime/storage paths, the main environment's
Python import paths and direct editable-path entries, and immediate Comfy
custom-node symlinks. Eleven named core packages resolved beneath the distro's
main venv. No inspected path depended on a Windows mount. This does not qualify
every engine environment, shared library, model, or offline inference path.

### Additional release gaps

| ID | Finding | Disposition and remaining verification |
| --- | --- | --- |
| P-01 | The Windows loopback relay locates a host-installed `pythonw.exe`; that interpreter is not bundled. | Dependency documented. Bundle or replace it before claiming the captured relay path needs no developer Python. Native WSL localhost forwarding may work without this relay; that does not qualify the relay itself. |
| P-02 | The app folder has the live VHDX and minimal Ubuntu bootstrap archive, but no prepared `linux/rootfs.tar.gz`; automatic snapshot export is disabled and the setup completion stamp is absent. | Packaging gap documented. Prepare a separate clean runtime export after complete setup, then verify the actual distributed launcher/archive together on a fresh host. Copying the current live disk is not a qualified transfer procedure. |
| P-03 | Copying the app folder has not been qualified under another directory/account or with no existing WSL registration. The reviewed launcher source does not establish automatic adoption of the copied VHDX. | Earlier portability wording corrected. Relocation, fresh registration and supported offline workloads remain unverified. Preserve the current studio while testing a separate clean package. |

Windows WSL2, virtualization, the Windows GPU driver and WebView2 remain host
prerequisites. They are not dependencies contained inside the Linux distro.
The existing Windows launcher binaries are present, but their build source is
outside this child repository; reproducible build instructions or artifact
provenance remain part of the release plan.

Validation after these documentation changes: all five
`tests.test_manual_coverage` tests passed. No runtime restart, setup run,
dependency installation, model load, inference, export or publication was
performed during this preparation.

## Preinstalled release preparation follow-up — 2026-09-25

The maintainer authorized the larger preinstalled distro, with model weights
downloaded separately, after the source repository was published. The new
work is in a separately registered clean build distro; the personal studio
disk is not a release input.

- **P-01 source repair:** `bridge.py` now prefers the embedded Windows Python
  beneath the launcher's current `TQ_APP_DIR`. Relay transport, lease handling,
  and relocation-path tests pass using the actual embedded interpreter.
- **P-04 — Native launcher C++ runtime dependency:** inspecting PE imports
  found `MSVCP140`, `VCRUNTIME140`, and `VCRUNTIME140_1` requirements. The Windows
  package now stages the signed Microsoft x64 runtime alongside the executable,
  with its own license and provenance. WSL2, WebView2 and Windows GPU drivers
  remain explicit host requirements.
- **P-05 — MOSS source pin could silently fall back:** a failed requested ref
  could fall back to default-branch code and even ignore a failed checkout.
  `ensure_moss_source` now fetches the requested ref explicitly, supports commit
  SHAs and fails without changing an existing checkout when the ref is absent.
  A local Git regression tests missing refs on fresh and existing checkouts,
  successful exact-commit checkout, and preservation after failure.
- **P-06 — Dependencies could not be prepared separately from weights:** added
  `install_model.sh runtimes FAMILY`, a separate dispatch for the nine isolated
  runtime families. Its test exercises every family with only dependency
  helpers available, and rejects unknown families. No API route was added.
- **P-07 — AnyGPT codec runtime omitted import dependencies:** the clean build
  exposed `ModuleNotFoundError: beartype` when SpeechTokenizer imported its
  trainer. The trainer also needs `lion-pytorch`, omitted by the deliberate
  `--no-deps` install. Both are now installed by one shared AnyGPT override
  helper used by full and dependency-only installs. The repaired codec, trainer
  and optimizer import together successfully in the clean Linux environment.

The clean main setup completed with real success stamps and import checks for
ACE-Step and Stable Audio, plus installed pinned ComfyUI and Manager. The
release preparation helper passed offline assembly, repeat-run, corrupt-part,
and existing-image preservation checks. Image export, restoration and final
publication evidence belongs in the separate preinstalled-release report.
