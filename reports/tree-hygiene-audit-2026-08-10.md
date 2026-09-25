# Omni Studio tree-hygiene audit — 2026-08-10

## Scope and safety boundary

The review covered the Windows child-app tree, active source references,
packaging/setup paths, and rebuildable or abandoned data beneath
`/opt/omni_studio`. Live state was checked before runtime cleanup: zero model
workers, zero ComfyUI instances, and zero queued/running jobs. Persisted models,
outputs, workflows, credentials, current runtime records, and performance
caches were not deleted.

## Removed from the Windows source tree

The following had no active source, launcher, test, documentation, or runtime
reference and were moved to the Windows Recycle Bin:

- `.codex_tmp/`: generated browser profile and probe scratch, including a
  4,269,932,544-byte Chrome on-device model cache; 4,520,829,588 bytes total.
- `storage/`: obsolete host-side model/output tree, including an approximately
  12.1 GiB duplicate Stable Audio model snapshot. The supported model stores
  are inside the distro.
- `.playwright-mcp/`, `agent-tools/`, `cache/`, and `terminals/`: expired page
  snapshots, a superseded local browser harness/dependency tree, one-off
  source/model probes, and terminal captures.
- `claude/` and `codex/`: completed or superseded audit bundles and a throwaway
  watchdog smoke-test script. Current durable findings remain under `reports/`.
- `.claude/`, two old root accessibility snapshots, and the zero-byte
  `=0.46.2,` typo artifact.

The two large host trees account for roughly 16.3 GiB. Their Recycle Bin
copies are recoverable until the bin is emptied.

## Removed from the distro

- All abandoned immediate children of `/opt/omni_studio/cache/tmp`; this
  included 444 entries and at least one 393,138,322-byte orphaned temporary
  file. The empty managed temp directory remains.
- Obsolete `/opt/omni_studio/runtime` and empty `/opt/omni_studio/repos` trees.
  Current runtime state is `/opt/omni_studio/cache/runtime`.
- The verified obsolete MiniCPM quarantine
  `cache/quarantine/minicpm_hyphen_o_stale_20260810`.
- Seven one-off `cache/h3_probe_*` diagnostic images.
- The stale app-owned duplicate
  `comfyui/custom_nodes/.disabled/omni_bridge`; the current active bridge and
  Manager's `.disabled` container remain.

These distro deletions were immediate and are not recoverable from the Windows
Recycle Bin.

## Source cleanup and drift prevention

- Removed the unreferenced `_infer_audio_score_legacy` implementation.
- Removed 35 unused imports and two abandoned local variables; fixed one stale
  NumPy annotation reference. Intentional NumPy availability checks are marked
  explicitly.
- Replaced a stale startup regression assertion that rejected legitimate
  app-owned bridge synchronization with assertions against the actual retired
  host-model migration paths.
- Added the existing-maintenance-API task `prune-tmp`, scheduled daily with a
  two-day age threshold. It only evaluates immediate app-owned temp children,
  does not follow symlinks, and leaves a margin beyond the 12-hour install
  timeout.
- Setup now synchronizes `AGENTS.md`, `docs/`, and `skills/` into the distro so
  runtime guidance cannot silently lag the child source.
- Added `.gitignore` guards and the canonical
  `docs/repository-layout.md`; updated `AGENTS.md` and stale intent notes.

## Intentionally retained

- `wsl/ext4.vhdx`, although large, is the active distro filesystem.
- `linux/ubuntu-base.tar.gz`, `linux/rootfs.setup_hash`, `Omni_Studio.exe`, and
  `webview.dll` are packaging/runtime inputs.
- `test_assets/comfy-capability/` is referenced by the capability runbook.
- Dated regression test modules remain active regression coverage.
- Current `reports/`, typed compatibility aliases, Manager disabled-layout
  support, distro models, media, workflows, locks, and managed performance
  caches remain intentional.

## Verification

- Selected Ruff dead-code checks: zero `F401`, `F811`, `F821`, or `F841`
  findings across bridge, server, CLI, and Comfy bridge source.
- Python compilation passed for source and copied runtime modules.
- 111 focused tests passed; three environment-dependent cases were skipped.
  Coverage included maintenance, startup layout, Comfy placement/multi-GPU,
  workflow requirements, CLI, audio regressions, and security regressions.
- The lightweight gateway restarted through `POST /api/system/restart`, the
  live maintenance status exposes `prune-tmp`, and a manual API run completed
  with zero deletion failures.

