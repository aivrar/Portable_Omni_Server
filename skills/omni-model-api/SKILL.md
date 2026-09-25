---
name: omni-model-api
description: Analyze, load, run, inspect, and unload Omni Studio's standalone multimodal model workers, including Qwen2.5-Omni, Qwen3-Omni, MiniCPM-o, Nemotron Nano Omni, AnyGPT, and Moshi. Use for safe single- or multi-GPU placement, Hugging Face device maps, GPU pools, CPU offload budgets, model chat/TTS capabilities, worker lifecycle, resource cleanup, and API troubleshooting outside ComfyUI and the dedicated ACE-Step/Stable-Audio workers.
---

# Omni Model API

Operate standalone model workers through Omni's authenticated API. Keep this
placement system separate from Comfy component routing and from dedicated
audio-engine state.

## Establish context

1. Treat the directory two levels above this skill as the Omni Studio app root.
2. Read the app-root `AGENTS.md`.
3. Read `docs/omni-model-api.md` completely before acting.
4. Check `docs/capability-confidence.md` before trusting a typed modality.
5. Inspect current typed route models before using an unfamiliar field. When
   deliberately enabled, `GET /openapi.json` is the machine contract.
6. Never print or persist the Omni API token.
7. For cross-engine scheduling, "use all GPUs", or comparison with Comfy/audio
   placement, also read `../omni-gpu-orchestration/SKILL.md`.

## Use the safe sequence

1. Read `GET /api/workers` and `GET /api/devices`.
2. Call `POST /api/workers/analyze` with the exact model, variant, precision,
   and placement request. Analysis must load no weights.
3. Treat `ready_to_spawn: false` or `placement_plan.valid: false` as a hard
   stop. Do not bypass memory or modality blockers.
4. Reuse the same body with `POST /api/workers/spawn`; the server recalculates
   the plan from current free VRAM and compute PIDs immediately before launch.
5. Wait for the typed spawn response. After any timeout, query workers before
   retrying so a slow loader is never duplicated.
6. Run one small inference with `autospawn=false`, verify the result, then
   delete the exact worker unless residency was requested.
7. Recheck workers, devices, and the analysis `memory_state` after cleanup.
   Inspect `pressure_status` and every `pressure_alert`; a pressure blocker is
   a hard stop even when nominal GPU memory fits.

## Choose placement deliberately

- `single`: one primary GPU. Prefer when the model fits; it avoids PCIe costs.
- `auto`: select the smallest viable subset from eligible GPUs and pass a
  bounded Hugging Face `device_map="auto"`. Set `require_all=true` only when
  the user explicitly wants every eligible GPU used.
- `manual`: expert mapping by exact Hugging Face module name. Use stable GPU
  UUID targets, not remembered `cuda:N` indices.
- CPU offload requires `allow_cpu=true` and an explicit `cpu_memory_mb`. The
  app-cgroup budget is authoritative; never exceed its blocker.

Automatic layer/expert sharding is supported only for the models named by the
guide and remains checkpoint-dependent until a bounded live pass succeeds.
Other model families may still run as separate workers on different GPUs.
Qwen GPTQ checkpoints currently require `single` placement: live Accelerate
dispatch failed while moving meta-device GPTQ weights, so `auto` multi-GPU is
blocked before spawn. Qwen base checkpoints remain eligible for bounded
multi-GPU placement.

Current machine-qualified standalone paths are Qwen2.5-Omni 3B base image
understanding on the 3090, Qwen2.5-Omni 7B GPTQ-Int4 text generation on the
3090, and MiniCPM-o 2.6 base image understanding on the 3090. Their cold starts
can take roughly 4-8 minutes under host load, so keep one spawn request alive
and query workers before retrying. Installed Qwen3-Omni Instruct and Nemotron
BF16 are hard capacity stops: current two-GPU plans provide about 32.3 GB
against 61.4/63.5 GB estimates. Analyze the smaller Nemotron NVFP4 separately;
a valid capacity plan is not a verified model transport.

## Guard shared resources

- Current free VRAM already reflects unrelated processes. Preserve warnings
  for non-Omni compute PIDs and re-analyze when they appear or disappear.
- Do not run broad process/model scans or synchronous cold loads merely to test
  wiring. Use analysis and focused unit tests.
- Reclaim model-owned RAM by unloading/deleting the exact worker. Reclaimable
  Linux file cache is not a leak and must not be globally dropped. After the
  final model/Comfy workload exits, Omni releases cache charged to its empty
  workload cgroup through scoped `memory.force_empty`.
- A bridge timeout does not prove a worker stopped. Query state, then delete the
  exact worker if it is still starting/loading.
- Never use kill-all when only one model is in scope.

## Verify proportionally

- Compile modified Python.
- Run `tests/test_omni_multigpu.py`,
  `tests/test_workers_multigpu_routes.py`, and
  `tests/test_omni_worker_local_code.py`.
- For live API wiring, analyze only and confirm the registry stays empty.
- Load weights only for an actual capability test whose plan is valid.

Report physical UUID-to-logical mapping, per-device budgets, foreign-process
warnings, model/variant, capability result, exact cleanup, GPU state, and the
app memory state's effective usage versus reclaimable cache.
