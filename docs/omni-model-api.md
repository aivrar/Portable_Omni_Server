# Omni standalone model API

This guide covers model workers such as Qwen2.5-Omni, Qwen3-Omni,
MiniCPM-o, Nemotron Nano Omni, AnyGPT, and Moshi. It does not replace
`docs/comfy-api.md` or `docs/audio-api.md`.

## Contract and safety

- Normal gateway: `http://127.0.0.1:9200`
- Authentication: `X-Omni-Token`; never print or commit it.
- Typed router models are authoritative. OpenAPI is available only when the
  gateway was deliberately started with `OMNI_ENABLE_DOCS=1`.
- Worker analysis loads no model weights. Always analyze before a large spawn.
- A spawn recalculates placement from current device state; an older client
  plan is never trusted as current capacity.
- After a client timeout, read `GET /api/workers` before retrying.

Use the token-safe client:

```text
python skills/omni-audio-api/scripts/omni_audio_api.py request GET /api/workers
python skills/omni-audio-api/scripts/omni_audio_api.py request GET /api/devices
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/workers/analyze --body worker.json
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/workers/spawn --body worker.json --timeout 1800
```

## Analyze placement

`POST /api/workers/analyze` accepts the same body as worker spawn. Example:

```json
{
  "model": "qwen_omni_7b",
  "variant": "base",
  "precision": "bf16",
  "placement": {
    "mode": "auto",
    "eligible_devices": ["GPU-3090-UUID", "GPU-3060-UUID"],
    "primary_device": "GPU-3090-UUID",
    "reserve_mb": 1024,
    "device_reserve_mb": {},
    "max_memory_mb": {},
    "require_all": false,
    "allow_cpu": false,
    "cpu_memory_mb": 0
  }
}
```

Use UUIDs returned by `GET /api/devices`. Current `cuda:N` values are accepted
for one boot but are not stable policy identifiers.

The response contains `ready_to_spawn` and a `placement_plan` with:

- `valid`, `blockers`, and `warnings`;
- documented `estimated_vram_mb`;
- primary-first physical `gpu_pool` and `gpu_uuids`;
- `gpu_device_map` from physical IDs to worker-logical CUDA IDs;
- current per-device `budgets_mb` after reserves/caps;
- `logical_max_memory_mb` passed to Hugging Face;
- non-Omni `external_compute_pids` seen on selected GPUs;
- app-cgroup `memory_state`, separating effective usage from reclaimable file
  cache, plus `pressure_status`, bounded `pressure_alerts`, active workload
  PIDs, swap/peak/fail counters, host available memory, and PSI when the host
  exposes it.

Do not spawn when the plan is invalid.

## Placement modes

### Single

`single` keeps the model on `primary_device`. This is preferred whenever the
model fits because it avoids inter-GPU transfers. The legacy top-level
`device` field remains a single-GPU primary when no placement object is sent.

### Auto

`auto` chooses the smallest viable subset from `eligible_devices`, keeping the
primary first. It exposes that pool through `CUDA_VISIBLE_DEVICES`, converts it
to worker-logical `cuda:0`, `cuda:1`, and passes Hugging Face an automatic
device map with hard memory ceilings.

Set `require_all: true` only for an explicit "use every selected GPU" request.
It may be slower than one GPU when the model already fits.

Automatic standalone sharding is enabled for Qwen2.5-Omni, Qwen3-Omni,
Nemotron Nano Omni, and AnyGPT loaders. A valid plan proves capacity and API
wiring, not that every custom checkpoint implements cross-device execution
correctly. Run one bounded live test before describing a model as verified.

Qwen base checkpoints have a bounded live multi-GPU pass. Qwen GPTQ `auto`
placement is deliberately blocked: a live Accelerate dispatch failed while
moving meta-device GPTQ weights. Use `single` placement for GPTQ until that
backend path is replaced or independently qualified.

MiniCPM-o, Moshi, MOSS, and Stable Audio workers currently remain
single-GPU model families. They can coexist as separate workers on different
physical GPUs. ACE-Step can occupy two GPUs in one worker: the DiT on the
primary device and the 5Hz LM on the first auxiliary device. That is a
component split, not Hugging Face layer sharding.

### Manual

`manual` accepts `placement.device_map`, mapping exact Hugging Face module names
to an eligible GPU UUID/current device or `cpu`. Module coverage is finalized
by the model loader. Do not guess module names from another architecture.

CPU targets require `allow_cpu: true` and a positive `cpu_memory_mb`. The
planner rejects budgets beyond the current workload-child allowance. The
default app parent is 24 GiB, but workers run under a 21 GiB child so 3 GiB
remains available to the gateway. With the default 4 GiB worker CPU reserve,
an empty child currently exposes at most 17 GiB for explicit CPU spill.

## Spawn and state

Send the analyzed body to `POST /api/workers/spawn`. The successful response
includes `worker_id`, primary physical `device`, placement mode, GPU pool,
physical-to-logical map, and the fresh placement plan.

`GET /api/workers` reports the same placement plus per-visible-GPU health
memory. A sharded worker appears on every participating physical device in
`GET /api/devices`, while its primary remains the top-level worker `device`.

Run inference through the existing typed routes, normally with
`autospawn=false`, for example:

```text
POST /api/chat/qwen_omni_7b?autospawn=false
POST /api/chat/qwen_omni_7b/stream?autospawn=false
```

Check `docs/capability-confidence.md` before using image, audio, video, or TTS
fields. Typed fields do not prove that a particular worker consumes them.

## Cleanup and RAM

Delete one worker with `DELETE /api/workers/{worker_id}`. Unload clears every
worker-visible CUDA allocator, removes only that process's fixed offload
directory, performs Python garbage collection, and requests glibc heap trim.
Deleting the process releases all anonymous model RAM.

Then read:

1. `GET /api/workers`
2. `GET /api/devices`
3. a fresh `POST /api/workers/analyze` when the cgroup memory breakdown matters

`memory_state.reclaimable_cache_mb` is Linux file cache, not resident model
weights. Do not use a global `drop_caches`; it disrupts unrelated processes and
makes later model loads colder. Standalone workers and Comfy run inside the
bounded `/omni_studio/workloads` child while the gateway keeps reserved parent
headroom. After the final workload exits, Omni uses only that empty cgroup's
`memory.force_empty` control to release charged page cache. Effective usage is
the important leak signal after workers exit.

## Current capacity implications

On the current 24 GB plus 12 GB GPUs, reserves leave roughly 32–33 GB of safe
combined VRAM when both are idle. This can help a model in that range, but it
cannot fit a 60–62 GB BF16 checkpoint. Nemotron NVFP4 (~21 GB) should prefer the
3090 alone; live analysis selected only that card. Nemotron FP8 (~33 GB) was
about 1.08 GB above the current GPU-only budget, but a fresh plan became valid
across both GPUs with an explicit 2 GB CPU spill inside the 17 GB safe child
budget. That is analysis-only until the exact checkpoint completes a bounded
inference run. All listed Qwen3 30B variants remain too large without a smaller
quantization or much larger safe CPU spill.

## Admission and installation validation after the source audit

Worker analysis accepts only `fp16`, `bf16`, or `fp32` precision. FP32 doubles
the catalog half-precision estimate conservatively. Manual maps count only
the budgets of actual map targets; unused eligible GPUs cannot make a plan
valid. `require_all` rejects unused manual targets and the requested primary
remains logical `cuda:0`.

Setup status and `verify-models` check completion markers, nonempty weights,
partial downloads, and every declared shard index, including nested indexes
and unindexed snapshots. The result's `validation_level` is
`file-presence-and-shard-map`. It does not validate tensor contents, hashes,
backend compatibility, or inference quality. Reinstall a partial legacy tree
through the installer instead of creating a marker to bypass validation.
