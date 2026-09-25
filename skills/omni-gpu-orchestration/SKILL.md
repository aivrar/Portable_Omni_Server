---
name: omni-gpu-orchestration
description: Detect GPUs and host-memory capacity, choose devices, plan maximum safe resource use, and verify placement across Omni Studio. Use when users ask to use all or selected GPUs, split or shard models, route Comfy components, stage model loading, run parallel workers, apply bounded CPU offload, schedule mixed Comfy/audio/standalone workloads, or diagnose why a nominal multi-GPU plan does not fit.
---

# Omni GPU Orchestration

Translate user intent such as "use all GPUs" into the placement mechanism the
selected engine actually supports. Never treat component routing, layer
sharding, staged execution, and parallel workers as interchangeable.

## Establish context

1. Treat the directory two levels above this skill as the app root.
2. Read the app-root `AGENTS.md` and
   [references/placement-strategies.md](references/placement-strategies.md).
3. Read the domain skill for every engine in the workload:
   - Comfy: `../omni-comfy-api/SKILL.md`
   - standalone models: `../omni-model-api/SKILL.md`
   - audio engines: `../omni-audio-api/SKILL.md`
4. Inspect current typed analyze/placement request models before composing an
   unfamiliar policy. Never invent a sharding field.
5. Never print or persist the Omni API token.

## Use the resource sequence

1. Read `GET /api/devices`, `GET /api/workers`, and
   `GET /api/comfy/instances` separately.
2. Resolve stable GPU UUIDs to current physical `cuda:N` indices. Record total
   and free VRAM, foreign compute PIDs, and every running engine.
3. Classify the workload and select only a supported strategy.
4. Analyze without loading weights. Require the engine's plan to be valid and
   check both GPU and host-memory budgets.
5. Re-read devices immediately before spawning or queueing; plans are not
   reservations.
6. Run one bounded smoke and verify actual per-device residency.
7. Unload or stop the exact process before changing strategy or engine family.

## Non-negotiable rules

- Prefer a single GPU when the complete workload fits with reserve; unused GPUs
  are not a failure.
- `require_all=true` is a user constraint, not a performance optimization.
- Comfy ordinary routing places independent components and can keep all mapped
  weights resident. It does not shard one diffusion model.
- Only staged nodes guarantee unload-between-stage behavior.
- Standalone layer/expert sharding is allowed only for model families declared
  compatible by `omni-model-api`; checkpoint support must be proven live.
- Dedicated ACE-Step, Audio Lab, MOSS-TTS, and MOSS-SFX workers are pinned to
  one selected device unless their typed contract explicitly says otherwise.
- CPU offload must have an explicit, current cgroup budget. Never treat Linux
  swap as usable model capacity.
- A valid plan is an estimate. Compare actual residency and stop on OOM,
  descriptor exhaustion, socket-buffer exhaustion, or host pressure.
- Never bypass host mapping blockers to force a queue.

## Report the decision

State the chosen strategy, stable UUID-to-physical/logical mapping, per-device
reserve and assignment, host-weight/offload budgets, foreign-process warnings,
whether every requested GPU was actually used, live verification result, and
exact cleanup. Explain when a smaller GPU set is safer or faster than using all
detected devices.
