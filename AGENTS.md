# Omni Studio agent instructions

These instructions apply to the entire `Omni_Studio` app tree.

## Comprehensive capability audits

Before running a full-application review, maximum-quality capability scan,
post-update regression campaign, or cross-library test plan, read
[`skills/omni-capability-audit/SKILL.md`](skills/omni-capability-audit/SKILL.md)
completely and follow the domain skills it routes to.

## GPU orchestration

Before deciding how to use all or selected GPUs, split or shard a model, route
Comfy components, stage model loading, schedule parallel workers, or apply CPU
offload, read
[`skills/omni-gpu-orchestration/SKILL.md`](skills/omni-gpu-orchestration/SKILL.md)
completely. Keep Comfy component placement, standalone layer sharding, staged
execution, and independent worker placement distinct.

## ComfyUI work

Before inspecting, changing, or operating ComfyUI through Omni Studio, read
[`skills/omni-comfy-api/SKILL.md`](skills/omni-comfy-api/SKILL.md) completely.
Read only the references it routes to for the current task. The canonical
human/API guide is [`docs/comfy-api.md`](docs/comfy-api.md).

Treat the current typed route models as the machine-readable API contract.
When the gateway was deliberately started with `OMNI_ENABLE_DOCS=1`, its
`GET /openapi.json` response is also authoritative. Do not invent request
fields or endpoint names.

## Local audio work

Before inspecting, changing, or operating ACE-Step, Stable Audio Lab,
MOSS-TTS, MOSS-SoundEffect, audio composition, or their model workers through
Omni Studio, read
[`skills/omni-audio-api/SKILL.md`](skills/omni-audio-api/SKILL.md) completely.
Read the sections it routes to in the canonical human/API guide,
[`docs/audio-api.md`](docs/audio-api.md).

Keep installation state, runtime-loaded state, and persisted output state
distinct. Use cheap `/state?autospawn=false` reads before loading anything,
select a current device explicitly when capacity matters, and unload then
delete the exact audio worker after testing when residency is not wanted.
ACE-Step and Audio Lab inference calls are synchronous even though their
persisted output has a `job_id`; only poll `/api/jobs/{job_id}` for operations
that explicitly return `status: "running"`.

## Standalone Omni model work

Before inspecting, changing, or operating Qwen Omni, MiniCPM-o, Nemotron,
AnyGPT, Moshi, or standalone multi-GPU workers, read
[`skills/omni-model-api/SKILL.md`](skills/omni-model-api/SKILL.md) completely
and follow [`docs/omni-model-api.md`](docs/omni-model-api.md).

Analyze placement without weights before spawning. Recalculate from current
devices at spawn time, preserve warnings for foreign compute PIDs, and never
bypass `valid: false`. Keep standalone layer/expert sharding distinct from
Comfy component placement and from separate audio-engine workers.

## Project constraints

- Do not add API endpoints without explicit user approval. Extend existing
  analyze, run, metadata, device, Comfy lifecycle, registry, and asset routes.
- Keep ComfyUI-specific models beneath the distro's Comfy tree at
  `/opt/omni_studio/comfyui/models/<category>`.
- Keep Omni GPU policies separate from Comfy workflow JSON when persistence is
  wanted; store them through workflow metadata.
- Prefer stable NVIDIA GPU UUIDs in saved policies. Resolve them to current
  `cuda:N` identifiers through `GET /api/devices` at runtime.
- Never assume an auxiliary GPU is visible. Check the selected instance's
  `gpu_pool` and placement plan first.
- Preview with `POST /api/workflows/analyze` before running. Analysis must not
  load model weights.
- A plan with `valid: false` is a hard stop. Do not bypass its blockers.
- Treat ordinary routing as component placement, not single-model layer
  sharding. Only staged nodes guarantee unload-between-stage behavior.
- Core update and Manager `update_all` are distinct operations. Verify each.
- Model deletion is immediate and reports `recoverable: false`; resolve the
  exact category and relative filename before calling it.
- Never expose the Omni API token in output, logs, examples, or source files.

## Verification discipline

- Keep live checks small and sequential. Do not combine broad process,
  filesystem, or model scans.
- Do not queue a heavyweight workflow merely to test API wiring.
- Use targeted tests:
  `python -m unittest tests.test_comfy_placement tests.test_comfy_multigpu tests.test_workflow_requirements`
- Check changed JavaScript with `node --check`.
- For live placement verification, use a synthetic API graph with installed
  filenames and call analyze only. Confirm the Comfy queue remains empty.

## Source and runtime

The Windows app source is this directory. A running distro executes gateway
files beneath `/opt/omni_studio/server`. Source changes may require copying only
the changed runtime files and restarting the lightweight gateway. Do not stop
or restart Comfy unless the change or user request requires it.

Follow [`docs/repository-layout.md`](docs/repository-layout.md) when deciding
where artifacts belong. Do not create browser profiles, Playwright snapshots,
terminal captures, dependency checkouts, model downloads, or one-off probe
output in this source tree. Use an OS temporary directory for short-lived host
work, `/opt/omni_studio/cache/tmp` for managed distro work, `test_assets/` for
intentional reusable inputs, and `reports/` for durable findings. The distro is
self-contained; do not recreate a Windows-side `storage/` model tree.

For the complete route-family and capability map, use
[`docs/api-capability-inventory.md`](docs/api-capability-inventory.md) before
inventing an API call. Treat the typed router models and deliberately enabled
OpenAPI schema as the exact field contract.

The gateway's normal `SIGTERM` lifespan deliberately stops every worker and
Comfy instance. A forced gateway kill is not a supported way to preserve model
workers: startup currently clears orphan workers and only Comfy records can be
re-adopted. For a source-only refresh, stage the changed runtime files, wait for
in-process jobs to finish, and schedule a graceful gateway restart when resident
workloads may be stopped. Do not force-kill a gateway to bypass its lifecycle.
