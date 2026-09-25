# Placement strategies and decision process

## Strategy meanings

| Strategy | What it does | Supported use |
|---|---|---|
| Single GPU | Places the whole compatible workload on one card. | Preferred when it fits with reserve. |
| Comfy component placement | Routes independent MODEL, CLIP, VAE, or audio-encoder objects to selected GPUs. | Ordinary Comfy API graphs. Components may overlap in residency. |
| Comfy staged execution | Loads named stages sequentially and returns CPU intermediates between stages. | Omni staged H3 and staged VAE nodes; only method with guaranteed stage boundaries. |
| Standalone layer/expert sharding | Uses a bounded Hugging Face device map to divide one compatible checkpoint. | Only model/variant pairs declared compatible by `omni-model-api`. |
| Parallel workers | Pins independent model servers to different GPUs. | Cross-engine concurrency when host RAM and both APIs permit it. |
| CPU offload | Moves declared overflow or modules into bounded system RAM. | Explicit low-VRAM/standalone policies only; never implicit. |

## Interpret user intent

- "Use the best GPU": choose the smallest single device that fits the full
  workload with reserve, unless latency evidence favors the largest.
- "Use these GPUs": restrict eligibility to the supplied stable UUIDs and let
  analysis choose a valid subset.
- "Use all GPUs": set `require_all` only after confirming the engine exposes
  multiple independently placeable components or a compatible sharding plan.
  Block rather than manufacture meaningless assignments.
- "Split the model": determine whether the user means component placement or
  true layer sharding. Comfy diffusion checkpoints are not automatically
  layer-sharded by a multi-GPU instance.
- "Run several models": plan independent workers, then sum their host-memory
  mappings and keep room for the gateway and outputs.

## Admission checks

### Device state

Use the current `/api/devices` response. Save policies by GPU UUID, then resolve
UUIDs to physical `cuda:N` on every boot. Comfy additionally maps physical
devices into instance-local logical IDs; report both mappings.

Current free VRAM includes unrelated processes. Preserve warnings and analyze
again when foreign compute PIDs appear or disappear.

### GPU capacity

Keep at least the requested per-device reserve. For Comfy, require every
component assignment to fit `usable_mb`. In low/none VRAM mode also require
`summary.estimated_cpu_offload_mb <= summary.cpu_offload_budget_mb`.

### Host capacity

For ordinary Comfy graphs, require
`summary.host_weight_footprint_mb <= summary.host_model_budget_mb`. The
footprint includes referenced LoRAs and model patches even though they are not
independent routing components. A plan can fit both GPUs yet exhaust WSL while
mapping the combined files; this is a hard blocker.

All-staged graphs report zero aggregate host mapping gate because their
contract unloads between stages. Verify that every heavy loader is actually a
declared stage before trusting this exception.

For standalone workers, require the model analyzer's cgroup, CPU-offload, and
pressure checks to pass. For parallel workers, include already-running workers
in the host-memory decision; free VRAM alone is insufficient.

## Engine decisions

### Comfy

Start the instance with a primary-first physical `gpu_pool`. Use UUIDs in saved
workflow policies. `auto` best-fits components, `manual` locks overrides and
plans the rest, and `single` keeps all components on the primary. Analyze the
exact patched graph; never reuse an older plan after changing dimensions,
frames, model files, or steps that affect activation estimates.

Ordinary `OmniRoute*` placement can deep-clone components and retain weights.
Use staged nodes when the combined graph cannot coexist in host/GPU memory.
If no staged implementation exists, use a smaller compatible weight stack or
classify the graph as blocked.

### Standalone models

Use `/api/workers/analyze` with the exact model, variant, precision, and
placement body. Prefer single placement when it fits. Use auto sharding only
for declared compatible base checkpoints; GPTQ/meta-device and other known
incompatible variants remain single-GPU blockers.

### Audio engines

Select one current physical `cuda:N` explicitly. Load and verify state before
inference. Schedule a large MOSS worker and an ACE/Audio Lab worker
sequentially unless current host and VRAM admission demonstrates that both can
coexist. Delete the exact worker when handing the GPU to Comfy.

## Verification

After load or queue start, use only lightweight targeted checks:

- engine state and exact worker/instance ID;
- per-device memory from the API or narrow `nvidia-smi` query;
- expected component assignments or worker device map;
- host `MemAvailable` and cgroup pressure when the model stack is large.

Do not run recursive process/filesystem probes during inference. Treat a client
timeout as unknown state and inspect the exact queue/worker before retrying.

Activation-heavy decode can require a different GPU than its model file size
suggests. For staged video graphs, reassign the final decoder to the largest
GPU after the transformer unloads when resolution, frame count, or temporal
tile size makes the auxiliary GPU unsafe. Model-weight-only analysis does not
prove that an aggressive full-temporal decode will fit. The qualified LTX 2.5
1024x640x121 case OOMed diffusion-VAE `temporal_size=2048` on 12 GB but
completed on 24 GB; conv `temporal_size=64` on the freed 24 GB GPU was faster
than either large-temporal profile.

## Cleanup

Detach adapters, unload components, delete exact workers, and send Comfy
`/free`. Stop the exact Comfy instance when `/free` leaves material residency
or when crossing model families. Recheck workers, instances, devices, and host
availability. Release only the Omni distro cache after persisted state is
synced; never globally drop caches or stop unrelated distros.
