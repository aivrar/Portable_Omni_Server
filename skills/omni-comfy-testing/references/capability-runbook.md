# Capability test runbook

## 1. Baseline

- Confirm gateway health, Comfy instance state, queue state, workers, and
  `/api/devices` one request at a time.
- Record physical IDs, stable GPU UUIDs, current logical IDs, VRAM totals/free,
  WSL host memory, Comfy commit, startup options, and installed custom nodes.
- If a previous worker/model is resident, unload it before starting the next
  family. Do not infer a leak until cleanup was requested and given time.

## 2. Fixtures

- Keep generated inputs under `test_assets/comfy-capability/`.
- Upload Comfy inputs through
  `/api/comfy/{instance_id}/proxy/upload/image` into a dedicated subfolder
  such as `omni-capability-tests`; use the returned name in `LoadImage`.
  Never assume the runtime input directory is `<comfy-root>/input`.
- Retain matched first/last frames, a neutral portrait, profile/full-body
  references, style art, speech, singing, and a short driving clip as needed.
- Use the same seed/prompt/input across settings intended for comparison.

## 3. Graph and dependency discovery

- Search saved and package templates through `/api/workflows/search`.
- Fetch official templates through `/api/workflow-templates/{template_id}` and
  pass the exact `source` and `package` returned by search; IDs are not globally
  unique across providers.
- Official UI graphs are inspectable but not queueable until exported or
  materialized as API format. Never send UI JSON to `/prompt`.
- Prefer declared model URLs embedded in template node properties. Pin the
  concrete Hugging Face revision rather than relying on moving `main`.
- Use workflow analysis/probe to identify missing node classes and model files.
  Do not guess categories from the filename when analysis supplies one.

## 4. Installation

- Submit one Xet-backed model job at a time with the exact category, URL, and
  destination filename.
- Poll `job_status_path`. A client timeout does not mean failure.
- Verify `status=done` and query installed models for the final category/name.
- Learned encoder projections belong in `clip_projections`; keep them separate
  from `clip` and `text_encoders` so analysis can resolve exact dependencies.
- Prefer Manager's catalog-backed extension action. If Manager rejects a
  user-approved uncatalogued repository, use
  `/api/assets/comfy/nodes/install` with an exact tag or commit, poll its job,
  restart once, and verify the expected live classes. This does not enroll the
  extension in Manager `update_all`.
- Defer optional LoRAs/upscalers until the base smoke succeeds.

## 5. Placement analysis

- Fetch `/api/devices` immediately before analysis so UUID-to-`cuda:N` mapping
  reflects the current boot.
- Use workflow metadata for persistent policies. `auto` should include only the
  user-selected UUIDs; `manual` should lock explicit components and let the
  planner place the remainder.
- `allow_cpu` exposes CPU only to an explicit component override in a mixed
  GPU plan; it does not make CPU an automatic overflow device. Treat CPU model
  or VAE placement as a compatibility probe until wall time is measured.
- Confirm `valid`, blockers, component sizes, device assignments, used GPU
  count, and warnings. Ordinary `OmniRoute*` nodes place components but may keep
  them resident together. Staged nodes are required for unload boundaries.
- For an ordinary graph, record `host_weight_footprint_mb` and
  `host_model_budget_mb`; the former must not exceed the latter. This gate
  includes LoRAs and model patches that are not independently routable. Do not
  bypass it merely because the per-GPU assignments fit.
- For routed CLIP/text encoders, verify both active GPU use and cleanup. Comfy
  executes encoding through `CLIP.cond_stage_model`; that reference must match
  the routed `patcher.model`, or the original encoder can stay resident and run
  on the wrong device even though the placement plan is valid.
- If correct CLIP placement still produces a dead-model warning, check whether
  its cached reload factory eagerly registered a temporary patcher. A clean
  routed reload initializes on the offload device and performs the intentional
  load only through the final routed patcher.
- Confirm the Comfy queue is empty after analyze; analysis must not load weights.
- For `OmniStageVAEDecode`, confirm every loader feeding its `samples` ancestry
  is reported as stage 1 and the VAE as stage 2. Same-stage estimates must add;
  a plan that uses only the largest same-stage component is unsafe.

## 6. Execution ladder

Run variants in increasing cost:

1. smallest supported smoke at the documented default steps;
2. canonical quality resolution;
3. one controlled seed/settings comparison;
4. higher resolution or duration only after cleanup is proven;

For a saved graph, prefer its existing parameterized `/run` route for seed,
steps, prompt, or explicit `<node_id>.<field>` changes. Require every intended
change in `patch_report.applied` and an empty `patch_report.unmatched`; placement
is recalculated for the patched graph.
5. optional style/reference/LoRA path;
6. multi-GPU placement and single-GPU control where meaningful.

Do not queue a second heavy graph while one is pending or in progress. Record
the prompt/job ID before polling. If a timeout occurs, inspect queue/history
instead of resubmitting.

For MiniMax H3, treat reference strength and Spectrum as separate comparisons.
First hold seed/input constant and compare native visual strengths. Then return
to the native default strength before enabling Spectrum, so approximation and
reference-noise effects are not conflated. Spectrum's video and audio blend
weights are also separate; keep audio at zero for the first comparison.

For H3 Turbo qualification, pin the LoRA repository revision and exact file,
apply it through `OmniH3StageSampler`, and retain native `12/3` as the no-LoRA
control. Change to the Turbo recipe's step count and sigma shifts only in the
named Turbo case. Test SageAttention with the identical graph after verifying
that the selected Comfy environment imports a compatible binary wheel; do not
allow pip to fall back to a source build during an interactive campaign.
Query live object-info and use one of the exact advertised Sage choices; a
friendly label such as `sageattn` is not necessarily a valid node input.

Treat a Turbo LoRA OOM during comfy-kitchen INT8 linear execution as a
weight-patching/inference-headroom failure until proven otherwise. Lowering
resolution or frame count is not the first retry because it does not reduce
the checkpoint patch materialization peak. A per-model Sage retry isolates
attention cost; if both variants reach device capacity, stop that cohort.
`reserve_vram` can force partial loading but is global across the visible GPU
pool and may turn the run into heavy CPU/PCIe offload. If gateway and Comfy
HTTP both become unreachable or WSL reports socket-buffer exhaustion, do not
start more probes or retry the workflow. Close/restart only Omni Studio, verify
zero instances/residency, record the environment as unqualified, and leave
unrelated distros untouched.

If Comfy core or a custom node changes H3 sampling, sigma scheduling,
conditioning, attention, or audio-stream transport, start a new comparison
cohort. Record the exact core commit and node revisions, rerun the fixed-seed
native control, and never combine pre-change and post-change measurements in
one aggregate. A queueable old graph is not evidence that its output remains
numerically or perceptually comparable.

For `InfiniteTalkAutoSampler`, do not treat `length` as an output-frame cap.
The node consumes the complete audio and uses `length` as its per-pass chunk.
Before queueing, compute:

```text
total_frames = ceil(audio_duration_s * framerate)
extend_frames = length - motion_frame_count
passes = 1 + max(0, ceil((total_frames - length) / extend_frames))
```

Reject `extend_frames <= 0`. For a one-pass smoke, trim the audio so
`total_frames <= length`; at the common 81 frames/25 fps, use no more than
3.24 seconds (prefer 3.0 seconds). Record the computed pass count in the test
ledger. A short `length` with long audio can silently become several expensive
diffusion passes and make normal interrupt/status requests slow or unavailable.

## 7. Output verification

- List outputs through `/api/outputs`, filtered by kind/subdir/prefix or prompt
  ID when possible.
- Fetch `/api/outputs/metadata/{relpath}` for dimensions, alpha, duration,
  codec, and embedded Comfy prompt/workflow metadata.
- Treat API paths as authoritative when canonical `/opt/omni_studio` output
  roots resolve into persistent `/var/lib/omni_studio` state. Windows UNC does
  not reliably traverse those absolute Linux symlinks.
- Inspect the actual image, audio, or video. A successful queue status alone is
  insufficient.
- For video playback, verify HTTP Range behavior in the Media library rather
  than assuming a static thumbnail indicates a bad render.

## 8. Cleanup and leak check

- Use the selected Comfy instance's proxied `/free` request with
  `unload_models=true` and `free_memory=true`.
- Delete or unload standalone workers through their lifecycle APIs.
- Re-query `/api/devices`; compare free VRAM with baseline.
- Treat `/free` as best-effort model/cache release, not a guaranteed hard
  release. If the selected Comfy PID still owns material VRAM after a short
  cleanup window, record the graph and amounts, then stop that exact instance
  through its lifecycle API before changing model families. A successful stop
  is the dependable hard-release boundary.
- Distinguish Linux file cache from live Comfy graph cache. `free`/`meminfo`
  `buff/cache` pages are reclaimable and should not be dropped during a model
  mmap. Process RSS that remains after `/free` is not released by dropping
  kernel caches. Use `cache_policy=none` at start when post-prompt RAM release
  is required, or restart the exact Comfy instance after the batch.
- Comfy's `startup_options.reserve_vram` is global across every visible GPU.
  A reserve large enough to force partial loading on a 24 GB primary may also
  push text encoders/VAEs off a 12 GB auxiliary; record measured residency,
  not just the placement target.
- Use a small targeted process check only if memory does not recover. Avoid
  recursive process/filesystem scans.

## 9. Defect handling

Classify each issue:

- `omni`: route contract, validation, lifecycle, metadata, UI, placement,
  persistence, or performance owned by this app;
- `comfy-core`: behavior in installed upstream Comfy;
- `custom-node`: extension behavior;
- `model/upstream`: checkpoint, tokenizer, architecture, or reference code;
- `environment`: driver, CUDA/PyTorch build, storage, or WSL limits.

For Omni fixes, add focused regression coverage and run the AGENTS-required
placement/workflow tests when related. Deploy only changed runtime files and
restart the lightweight gateway; do not restart Comfy unless required. A
development-only gateway restart that must preserve Comfy should be abrupt so
the next gateway can re-adopt the validated PID record; a graceful gateway
shutdown intentionally invokes stop-all as part of full application cleanup.
