---
name: omni-comfy-api
description: Operate, inspect, document, test, or modify Omni Studio's ComfyUI subsystem through its stable API. Use for Comfy lifecycle and startup flags, GPU pools and component placement, workflow/template analysis and execution, models and Xet downloads/deletion, custom-node discovery/install/update, Comfy core or Manager updates, queue verification, and troubleshooting inside the Omni_Studio app.
---

# Omni Comfy API

Use Omni's guarded API and stable nodes. Do not patch ComfyUI core to implement
app behavior.

## Establish context

1. Treat the directory two levels above this skill as the Omni Studio app root.
2. Read the app-root `AGENTS.md`.
3. Use `docs/comfy-api.md` as the canonical human-facing specification.
4. Inspect current typed router models before composing an unfamiliar request.
   If the local gateway was deliberately started with `OMNI_ENABLE_DOCS=1`,
   `GET /openapi.json` is also authoritative. Docs are disabled by default.
5. Never print or persist `X-Omni-Token`.

## Route the task

- For starting, stopping, startup options, core updates, Manager updates, or
  recovery, read [references/lifecycle-and-updates.md](references/lifecycle-and-updates.md).
- For workflow requirements, templates, GPU policies, plan preview, manual
  overrides, running, and output verification, read
  [references/workflows-and-gpus.md](references/workflows-and-gpus.md).
- For model search, Xet downloads, exact folders, deletion, custom-node search,
  installation, and verification, read
  [references/models-and-nodes.md](references/models-and-nodes.md).
- For cross-engine scheduling, layer sharding, "use all GPUs", or host/GPU
  admission decisions, also use `../omni-gpu-orchestration/SKILL.md`.

Read every selected reference completely before acting. Read multiple
references when the request crosses those boundaries.

## Operating rules

- Use existing endpoints. Do not add an endpoint without explicit user
  approval.
- Prefer read-only discovery before mutation.
- A user-authorized mutation does not require a dry run unless the user asks
  for one or the target is ambiguous.
- Resolve devices from `GET /api/devices` immediately before analysis and
  after cleanup; save UUIDs, not only `cuda:N`. Admission uses current-free
  VRAM for every process, including the selected instance. Unload resident
  models explicitly and reanalyze when that headroom is insufficient.
- Resolve the running instance and its visible `gpu_pool` before placement.
- Analyze API-format workflow JSON before running. Never bypass readiness or
  placement blockers.
- For all graphs, including staged graphs, require
  `summary.host_weight_footprint_mb <= summary.host_model_budget_mb` as well as
  valid GPU assignments. LoRAs and model patches still consume host mapping
  capacity even when they are not independent routing components.
- Treat `prompt_id` as queue acceptance, not successful completion.
- Poll returned job-status paths and verify filesystem/node results after
  downloads, installs, or updates.
- Keep live checks small and sequential. Avoid broad scans and never load a
  heavyweight model merely to validate API wiring.
- Ask before destructive deletion when the exact file was not already named by
  the user. Model deletion reports `recoverable: false`.
- For LTX-2.5, use the official `Lightricks/ComfyUI-LTXVideo` 2.5 examples and
  verify its low-VRAM loader classes live. The official weights are gated:
  never substitute an ungated mirror to evade license acceptance. On Ampere,
  prefer official INT8 ConvRot over NVFP4 unless the runtime actually exposes
  the required fused kernels. Import UI examples for inspection, but export or
  construct API-format graphs before analyze/run; nested UI subgraphs are not
  directly queueable through the API.

## Use the bundled client

Use `scripts/omni_comfy_api.py` for repeatable discovery, analyze, lifecycle,
and run requests. It reads the token from `OMNI_API_TOKEN` or `--token-file`
and never prints it.

Mutation subcommands require `--execute`. This is an intentional guard, not a
request to perform an unnecessary dry run.

Examples:

```text
python scripts/omni_comfy_api.py devices
python scripts/omni_comfy_api.py instances
python scripts/omni_comfy_api.py analyze workflow.json --instance comfy-cuda1-8188 --policy policy.json
python scripts/omni_comfy_api.py run workflow.json --instance comfy-cuda1-8188 --policy policy.json --execute
```

Use the app's normal base URL `http://127.0.0.1:9200` unless current runtime
configuration says otherwise.

## Verify proportionally

- Schema/source: compile affected Python and generate/check the FastAPI schema
  locally; check `/openapi.json` only when docs were deliberately enabled.
- Planner: run `tests.test_comfy_placement`.
- Routing/startup: run `tests.test_comfy_multigpu`.
- Workflow analysis: run `tests.test_workflow_requirements`.
- UI: run `node --check` on changed JavaScript and visually inspect when a
  browser connection exists.
- Live plan: analyze a synthetic graph using installed filenames; do not queue
  it. Confirm the queue is empty afterward.

Report the exact instance, physical/UUID-to-logical mapping, plan blockers or
warnings, job/prompt identifier, and verification result. State plainly when
visual or completion verification was unavailable.
