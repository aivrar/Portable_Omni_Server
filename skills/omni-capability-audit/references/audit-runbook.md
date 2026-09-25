# Comprehensive capability audit runbook

## 1. Define the ledger

Create or update one dated findings file. For each scoped family record:

- control API and domain skill;
- installation state and exact revision;
- baseline fixture, seed, settings, and expected output;
- analysis/admission result;
- smoke, balanced, and frontier cases;
- output identifiers and objective media facts;
- visual/listening observations where applicable;
- failure owner: `omni`, `comfy-core`, `custom-node`, `model/upstream`, or
  `environment`;
- cleanup result and remaining risk.

Use `verified`, `blocked`, `failed`, or `deferred`. Do not call a dependency
download or queue acceptance a successful capability.

## 2. Establish a cheap baseline

Read gateway health, devices, workers, Comfy instances, queue state, and host
memory in separate requests. Record stable GPU UUIDs, current `cuda:N`
indices, free VRAM, foreign compute PIDs, workload-cgroup headroom, and
reclaimable cache. Do not start a model merely to populate the ledger.

If state from a prior family is resident, unload/delete the exact worker or
stop the exact Comfy instance before proceeding.

## 3. Reconcile the API inventory

Compare typed routers with `docs/api-capability-inventory.md`. Exercise cheap
read routes first. For mutation or inference routes, use one representative
request only when the domain skill's admission checks pass.

Treat these as separate states:

1. asset installed;
2. worker/Comfy process ready;
3. model component loaded;
4. request queued or accepted;
5. persisted output verified.

## 4. Test one family at a time

For each family:

1. discover current assets and compatible community alternatives;
2. materialize an API-format request or workflow;
3. run analyze/preflight without weights;
4. stop on any placement, modality, license, or host-memory blocker;
5. execute the smallest useful smoke;
6. verify output metadata and inspect the real media;
7. run a balanced quality case;
8. run one frontier dimension only after cleanup is proven;
9. unload and compare resource state with baseline.

Frontier dimensions include resolution, duration, steps, precision, reference
strength, LoRA, sampler, quantization, or GPU strategy. Change only one at a
time when drawing a comparison.

## 5. Test cross-tool chains explicitly

Cross-tool tests must preserve the source output URL/ref and verify the target
tool consumes that persisted artifact. Examples include TTS to lip-sync,
ACE-Step vocals to audio-driven video, generated images to first/last-frame
video, and short music segments to long-form composition.

A source generator passing does not prove the downstream chain. Analyze the
downstream graph independently and calculate duration expansion before
queueing. An admission blocker is the correct result when the combined stack
cannot fit.

## 6. Handle defects without losing the campaign

For an Omni defect:

1. capture the smallest reproducible request and exact error;
2. inspect only the relevant source path;
3. implement the narrow fix when authorized;
4. add a focused regression test;
5. deploy only changed runtime files;
6. restart only the lightweight gateway unless the fix requires more;
7. re-run the failed request and update the ledger.

For upstream or environment defects, preserve exact revisions and stack traces,
clean up the affected worker, and continue to the next independent family.
Do not retry a poisoned or unknown-state worker blindly.

## 7. Maintain agent guidance

Update an existing domain skill when a finding changes endpoint spelling,
admission checks, cleanup behavior, or a qualified baseline. Create a new skill
only for a reusable workflow that crosses existing domains; do not create one
skill per model family.

Validate every changed skill and regenerate its `agents/openai.yaml` when the
trigger description or intended prompt changes.

## 8. Final cleanup

1. Detach active LoRAs.
2. Unload engine components.
3. Delete exact standalone/audio workers.
4. Send Comfy `/free`, then stop the exact instance if material residency
   remains or the audit is ending.
5. Verify `/api/workers` and `/api/comfy/instances` are empty.
6. Record final device and host-memory state.
7. Sync persisted distro state.
8. Close the desktop app normally. If its distro remains running after all
   state is synced and empty, terminate only `linbox-Omni_Studio` to release
   its page cache. Never touch unrelated distros.
