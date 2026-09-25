---
name: omni-capability-audit
description: Inventory, test, qualify, optimize, and document Omni Studio capabilities across ComfyUI, local audio, standalone multimodal workers, media outputs, lifecycle, and resource cleanup. Use for full-application reviews, comprehensive API test plans, maximum-quality capability scans, post-update regression audits, cross-library workflows, confidence scoring, or requests to exercise everything the app can do without leaving models resident.
---

# Omni Capability Audit

Run broad Omni evaluations as bounded, resumable API campaigns. Delegate
engine-specific decisions to the existing domain skills and retain one durable
evidence ledger instead of reconstructing state from chat history.

## Establish context

1. Treat the directory two levels above this skill as the app root.
2. Read the app-root `AGENTS.md`, `docs/api-capability-inventory.md`, and
   `docs/capability-confidence.md`.
3. Read [references/audit-runbook.md](references/audit-runbook.md) completely.
4. Inspect current typed route models before using unfamiliar fields. Treat
   `/openapi.json` as authoritative only when docs were deliberately enabled.
5. Never print or persist the Omni API token.

## Route each capability

- Comfy lifecycle, workflows, assets, or extensions: use
  `../omni-comfy-api/SKILL.md`.
- Comfy quality/frontier testing: also use
  `../omni-comfy-testing/SKILL.md`.
- ACE-Step, Stable Audio, MOSS, TTS, SFX, or composition: use
  `../omni-audio-api/SKILL.md`.
- Standalone multimodal workers: use `../omni-model-api/SKILL.md`.
- GPU selection, sharding, staged placement, or cross-engine scheduling: use
  `../omni-gpu-orchestration/SKILL.md`.

Read each selected skill and only the references it routes to. Do not duplicate
their endpoint contracts inside the audit.

## Operating rules

- Keep checks small and sequential. Run only one cold load, download, or heavy
  inference at a time.
- Inventory and analyze before loading weights. A typed blocker is a hard stop
  and counts as a qualified capacity result.
- Distinguish installed assets, runtime-loaded state, queued work, and persisted
  output state.
- Change one quality variable at a time and preserve fixed seeds/fixtures for
  comparisons.
- Fix reproducible Omni-owned defects when authorized and add focused tests.
  Record upstream defects without editing upstream source during a probe.
- Keep dated findings under `reports/` and reusable fixtures under
  `test_assets/`. Update skills when a finding changes future operating safety.
- End with exact unload/stop verification. Never use a global cache drop or a
  broad kill operation to clean up one capability family.

## Completion bar

An audit is complete only when every scoped family is classified as verified,
blocked, failed, or deferred with evidence; outputs are inspected where a run
succeeded; Omni fixes have focused regression coverage; skills/docs reflect
new reusable rules; workers and Comfy instances are empty unless residency was
requested; and persisted state is synced before any final distro shutdown.

Report what worked, what failed, the best qualified settings, hard capacity
boundaries, fixes made, test counts, retained artifacts, and final cleanup.
