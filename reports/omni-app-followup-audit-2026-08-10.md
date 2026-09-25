# Omni Studio follow-up audit — 2026-08-10

## Scope and safety

This pass inspected the ten remaining reliability areas identified after the
Comfy, audio, and standalone-model capability work. Checks were small and
sequential. No heavyweight Comfy or audio generation was queued. Live model
loading was limited to two bounded standalone placement checks, followed by
exact worker deletion and scoped workload-cache release. No global cache drop
was used.

## Results

| Area | Result | Evidence and action |
|---|---|---|
| 1. Gateway shutdown/restart and WebView retry | Improved and verified | Made the broad shutdown sweep idempotent, made bridge process-group killing exact, added explicit restart-deadline supervision, and retained the bridge startup/reload page. Four gateway-only restarts recovered in roughly five seconds. API-only full shutdown now performs one cleanup pass and closes only the matching Windows `Omni_Studio.exe`; the live host exited without manual intervention and relaunched connected. |
| 2. Long-running timeout/state semantics | Improved and verified | Bridge proxy timeout now exceeds ACE-Step's 1800-second synchronous inference timeout. OS browser opening moved off the event loop. ACE-Step and Audio Lab workers report busy during inference and are retired on server timeout. |
| 3. Lightweight memory/I/O pressure preflight | Implemented and live-checked | No-weight analysis now reports cgroup effective usage, reclaimable cache, RSS, swap, peak/fail counts, active PIDs, host available memory, and PSI when available. Live state was `pressure_status: ok`; PSI is unavailable under this WSL host and is treated as supported degraded mode. |
| 4. Interrupted downloads, integrity, disk headroom, atomic placement | Improved and unit-verified | Snapshot installs now use a stable sibling staging directory, detect partial/empty files and missing indexed shards, write an atomic completion marker, and transactionally publish with rollback. Existing single-file Comfy downloads retain HF/Xet cache and atomic materialization. |
| 5. Standalone multi-GPU readiness and cleanup | Partly qualified | Qwen 3B base loaded across the RTX 3090 and RTX 3060 and returned exact `OK.` output. Exact deletion released both GPUs and all workload cgroup usage/cache. Qwen 7B GPTQ multi-GPU failed in Accelerate meta-device dispatch; the planner now blocks GPTQ `auto` placement and directs it to the verified single-GPU path. |
| 6. Post-update Comfy regression qualification | Implemented and unit-verified | A core update is no longer declared successful from Git alone. It must verify exact commit, clean checkout, and template discovery; restarted instances also verify readiness, system stats, object info, and core nodes. Core and Manager update-all remain separate operations. No upstream update was run during this audit. |
| 7. Media provenance, indexing, and damaged output handling | Improved and live-checked | Artifact probes now expose explicit integrity status. PNG completion, WAV headers, ffprobe errors, and expected media streams are checked. Bounded sidecar/embedded provenance is attached where available. Five recent Comfy PNG outputs passed live integrity and dimension checks. |
| 8. Remaining audio-family contracts and lifecycle | Improved and verified without model load | ACE-Step, Audio Lab, MOSS-TTS, MOSS-SFX, and generic worker timeout paths now retire the exact timed-out worker. Windows adapter staging falls back from symlink to hardlink/copy. Autospawn-false TTS/SFX probes returned expected unavailable responses and left zero workers. |
| 9. Frontend backend-recovery UX | Improved; visual pass blocked | The UI now clears stale credentials on 401, reacquires `/api/session`, retries backend recovery at 1/2/4/8 seconds, and reloads config/jobs/LoRAs/devices after reconnect. Source checks and tests pass. A live browser fault-injection pass was not possible because the browser controller reported no available session. |
| 10. Concurrent cleanup/cache-release races | Fixed and verified | Workload placement and scoped `memory.force_empty` now share one lock. Reclaim checks both `tasks` and `cgroup.procs`, preventing a worker from entering between the idle check and cache release. Worker/Comfy duplicate teardown paths are otherwise idempotent through cleared handles, tolerant PID removal, and set-based port release. |

## Live placement evidence

- Physical pool: RTX 3090 plus RTX 3060, selected by stable UUID policy and
  resolved to current `cuda:N` identifiers.
- Qwen 3B base budget: 4608 MiB per GPU, `require_all: true`, no CPU offload.
- Observed model memory: approximately 4963 MiB on the 3090 and 5606 MiB on
  the 3060.
- Bounded inference: prompt `Reply with exactly OK.`, result `OK.`.
- Cleanup: zero Omni workers, zero Comfy instances, no workload cgroup PIDs,
  and zero workload usage/cache/RSS/swap after scoped release.

The GPTQ failure occurred after checkpoint shard loading while Accelerate moved
quantized weights from a meta device. It did not leave a managed worker. A
separate failure-cleanup fix now reclaims empty workload cache after a failed
cold load.

## Verification summary

Focused suites cover bridge supervision, shutdown, session responsiveness,
frontend recovery, snapshot installs, multi-GPU planning/routes, workload
cgroup behavior, Comfy dynamic update qualification, extensions, outputs,
audio lifecycle, ACE staging/sources, composition, and MOSS contracts. The
project-mandated Comfy placement/multi-GPU/requirements tests and JavaScript
syntax check are included in the final regression pass.

Final idle state after relaunch: API connected, frontend cache-bust `v18`, zero
workers, zero Comfy instances, zero workload tasks, and only 20 KiB charged to
the empty workload cgroup. A live output probe returned `integrity: ok` and
`source: comfy-png` provenance for the newest media item.

## Remaining limitations

1. Repeat the frontend restart/fault-injection scenario when a controllable
   browser session is available.
2. Treat standalone layer sharding as checkpoint-specific. Only Qwen 3B base
   has a bounded cross-device inference pass here; GPTQ auto-placement is
   intentionally blocked.
3. A future real Comfy core update should be allowed to exercise the new
   checkout/template/live-runtime qualification path end to end.
4. Media structural integrity does not replace subjective playback or visual
   quality review.
5. PSI telemetry remains unavailable on this WSL kernel; the rest of the
   pressure preflight remains active.
