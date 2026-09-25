---
name: omni-comfy-testing
description: Plan, execute, optimize, and verify local ComfyUI capability tests through Omni Studio APIs. Use for model-family qualification, workflow dependency installation, image/video/audio fixtures, output comparison, multi-GPU placement validation, memory cleanup, TTS/ACE-Step integration, or investigation of Comfy/Omni performance and resource traps.
---

# Omni Comfy Testing

Use this skill to turn a requested Comfy capability into a repeatable API-driven
test with retained fixtures, exact dependencies, measurable outputs, cleanup,
and findings that another agent can resume.

## Required routing

1. Read `../omni-comfy-api/SKILL.md` completely before inspecting or operating
   Comfy. Follow only the references it routes to for the current operation.
2. Read `references/capability-runbook.md` for every live capability test.
3. Read `references/model-matrix.md` when the request involves a listed model
   family or when choosing baseline quality/performance settings.
4. Treat `../../docs/comfy-api.md` and the current Pydantic route models as the
   API contract. Use `/openapi.json` only when the gateway was deliberately
   started with `OMNI_ENABLE_DOCS=1`.
5. For "use all GPUs", cross-engine scheduling, or layer-sharding questions,
   also read `../omni-gpu-orchestration/SKILL.md`.

## Non-negotiable rules

- Use Omni APIs for lifecycle, discovery, analysis, downloads, queueing,
  outputs, and worker cleanup whenever a route exists.
- Never print, log, embed, or commit the Omni token.
- Keep Comfy model files under `/opt/omni_studio/comfyui/models/<category>`.
- Analyze before run. A placement plan with `valid: false` is a hard stop.
- For ordinary graphs, a host weight footprint above the current host model
  budget is a hard stop even when every GPU assignment fits.
- Confirm the selected instance's `gpu_pool`; never assume an auxiliary GPU is
  visible merely because the host detects it.
- Keep persistent GPU policy in workflow metadata and prefer GPU UUIDs.
- Keep live checks small and sequential. Do not combine inference with a large
  download, broad scan, or another heavyweight worker.
- Treat a client timeout as unknown state. Poll the returned job, worker,
  instance, or queue before retrying.
- Unload the active model/worker between model families and verify memory
  recovery. Ordinary routed graphs do not guarantee staged unloading.
- Do not patch Comfy core, upstream model repositories, or custom-node source
  during a probe. Record upstream findings. Fix reproducible Omni-owned defects
  with focused tests when authorized.
- Never delete a model without resolving the exact category and relative name;
  deletion is immediate and unrecoverable.

## Test workflow

Follow the phases in `references/capability-runbook.md`:

1. establish the runtime and resource baseline;
2. materialize rights-safe, reusable fixtures;
3. discover the exact API-format graph and dependencies;
4. install one dependency job at a time and verify its final target;
5. analyze node/model readiness and GPU placement without loading weights;
6. run a low-cost smoke, then quality/resolution/duration variants;
7. verify files through output APIs and probe their media metadata;
8. unload, compare memory with baseline, and record results;
9. update durable workflow metadata, docs, and findings.

## Evidence requirements

For every executed case retain:

- workflow filename, graph format, seed, prompt/input fixture, and settings;
- exact repository revision, relative model file, category, and installed size;
- analysis readiness, missing requirements, placement validity, and component
  assignments;
- queue prompt/job ID, start/finish times, output paths, dimensions/duration;
- cold/warm timing, GPU assignments, peak or before/after memory where useful;
- visual/audio result notes, failure text, classification, and cleanup result.

Use a dated Markdown findings file under `reports/`. Distinguish observations,
inferences, confirmed Omni defects, upstream limitations, and deferred risks.

## Completion bar

A feature is not complete because a file downloaded or one prompt queued. It is
complete only when dependencies are verified, analysis is ready, at least one
real output is retrievable, the output is inspected, cleanup succeeds, failures
are classified, and the repeatable workflow/API instructions are retained.
