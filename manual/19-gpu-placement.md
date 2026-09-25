# GPU placement

Omni Studio can see more than one NVIDIA GPU. Placement is how you tell it
which cards may run which **components**. There are three user-facing modes:
**`single`**, **`auto`**, and **`manual`**. Saved policies store **stable GPU
UUIDs**, not `cuda:0`, because indices change between boots.

This page is the special-usage counterpart to the GPU placement panel on
[Workflows](08-workflows.md) and the worker analyze/spawn path on
[Runtime](07-runtime.md).

## Three different placement systems

Keep them distinct or you will "split a model" in a way the engine cannot
do:

1. **Comfy component placement** — diffusion MODEL, CLIP, VAE, audio
   encoders as whole objects on selected GPUs. Ordinary graphs. Components
   may overlap in residency.
2. **Comfy staged execution** — Omni staged nodes load, run, move results
   to CPU, unload, then the next stage. This is the only Comfy path with a
   guaranteed boundary for the weights owned by those stages. Generic
   `OmniStageVAEDecode` releases its VAE only; upstream ordinary loaders stay
   resident and count against capacity.
3. **Standalone layer/expert sharding** — a bounded Hugging Face device map
   for **declared compatible** Omni chat families. Not Comfy. Not ACE-Step.

Audio engines are mostly **one explicit `cuda:N`**. ACE-Step may place DiT
on the primary and the 5 Hz LM on an auxiliary GPU (component split). Music
3 does not pool two GPUs as one device-map.

## Always start from live devices

**Do this:**

1. Open Runtime or run `omni-cli.bat devices list`.
2. Note each row's current `cuda:N`, marketing name, **UUID / stable_id**,
   free VRAM, and compute PIDs.
3. Save UUIDs in workflow metadata and in any JSON you keep. Resolve UUID →
   `cuda:N` again at spawn/run time.

If a foreign compute PID is on the card, the planner warns and stays
conservative. Close the other app or pick another GPU.

## Modes

### `single`

Put the whole compatible workload on **primary_device**. Prefer this whenever
the model or graph fits with reserve. It avoids inter-GPU copies.

Workflows: **Single GPU** in the placement panel. Runtime spawn with one
Device dropdown is this mode.

### `auto`

Choose a viable subset of **eligible** UUIDs, primary first.

- Comfy: largest diffusion model favors the largest fitting GPU; smaller
  components use the smallest fitting GPU to protect the big card.
- Standalone: exposes the pool via `CUDA_VISIBLE_DEVICES` and a Hugging Face
  device map with memory ceilings. Enabled for declared Qwen / Nemotron /
  AnyGPT loaders. **Qwen GPTQ auto is blocked** (Accelerate meta-device
  failure). Use `single` for GPTQ.

`require_all: true` forces every selected GPU to receive a component. That
can be slower than one GPU. Leave it off unless you asked to use every card
in a meaningful way.

### `manual`

Lock named components (Comfy) or module names (standalone) onto UUIDs, then
auto-plan the rest. Do not guess module names from another architecture.

CPU targets need `allow_cpu` and a positive `cpu_memory_mb` inside the
workload cgroup budget. Automatic mixed-device placement stays GPU-only;
CPU is never a silent spill target.

## Analyze-before-run

**Analyze-before-run** is mandatory for Comfy graphs and for large standalone
spawns. Analysis **does not load model weights**.

### Comfy

On Workflows: **Plan GPUs** or **Check again** / **Check draft**. The API is
`POST /api/workflows/analyze`.

Read:

- workflow `readiness` / `ready_to_run`
- `placement_plan.valid`
- `status` (`ready`, `blocked`, `restart-required`)
- `blockers` and `warnings`
- component assignments and `summary.staged`

Then queue **only** if the plan is valid. Run/queue recalculates from
current devices; an old screenshot of a plan is not capacity.

### Standalone workers

`POST /api/workers/analyze` with the same body you would spawn (model,
variant, precision, placement object). Read `ready_to_spawn` and
`placement_plan.valid`. Spawn recalculates; it will not trust a stale
client plan.

CLI spawn does not currently expose the full placement object; use Runtime
for simple single-GPU, or a JSON client for UUID pools.

### Audio / Music 3

Load routes analyze GPU + cgroup budgets. Music 3 load refuses an unsafe
CPU offload budget. Use `GET /state?autospawn=false` as a cheap probe.

## `valid: false` is a hard stop

If the plan says **`valid: false`** (or `ready_to_spawn: false`, or
`ready_to_run: false` because of placement blockers):

1. **Stop.**
2. Read every blocker (VRAM, missing gpu_pool GPU, host RAM, cgroup,
   incompatible GPTQ auto, 30B too large).
3. Change the graph, variant, pool, reserve, or unload other residents.
4. Analyze again.
5. Do not queue, spawn, or "try anyway."

The UI disables Run when it can. CLI `workflows run` will still hit the
server, which should refuse an invalid plan — treat a refusal as success of
the safety system, not as a CLI bug to work around.

`restart-required` means the running Comfy instance's GPU pool is too
small. Stop it, start with the extra GPU checked, analyze again.

Warnings (foreign PIDs, ordinary-loader overlap) are not a license to
ignore blockers.

## Reserves and host RAM

- Per-device **reserve_mb** (Workflows default 1024) is headroom on each
  eligible GPU.
- Comfy **startup** `reserve_vram` is process-wide and shrinks auxiliaries
  too.
- A plan can fit both GPUs and still fail because mapping both checkpoints
  exhausts WSL host RAM. That blocker is real.
- Workers run in a workload cgroup smaller than the app parent so the
  gateway keeps reserved RAM. Explicit CPU offload must fit the child
  budget (Music 3's 16000 MB field is this class of check).

## Operator recipes

### One 24 GB GPU

Mode `single`, that UUID as primary, reserve 1024. Unload Chat before
Comfy video. Unload Comfy before Music 3.

### 24 GB + 12 GB

Start Comfy with both cards in the **gpu_pool**, primary = 24 GB. Mode
`auto`. Analyze. If the 12 GB card is assigned a decode that OOMs, switch
to `manual` and override that component onto the 24 GB UUID.

Standalone 7B GPTQ: `single` on the 24 GB UUID, not auto.

ACE XL SFT + 4B LM: DiT on 24 GB, LM on 12 GB, 12 GB cleared of Audio Lab
CLAP first.

### "Use all GPUs"

Eligible = all UUIDs, `require_all` still false, mode `auto`. Only set
`require_all` after the engine actually has two placeable components.

## Related pages

- [Workflows](08-workflows.md)
- [Engine & queue](09-engine-and-queue.md)
- [Runtime](07-runtime.md)
- [Feature compatibility](21-feature-compatibility.md) — 30B hard stops, GPTQ auto block
