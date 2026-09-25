# Omni Studio source repository audit — 2026-09-25

## Scope and method

This review covers only the `Omni_Studio` child app tree. The parent template
and its other apps are outside scope. It combines a tree inventory, Git ignore
checks, source reference and Ruff scans, targeted setup and manual contract
checks, a limited read-only inspection of the selected WSL distro, and focused
regression tests. It is a source-publication audit, not a fresh live model
qualification or a screenshot pass. No model weights were loaded, no Comfy
workflow was queued, and the installed gateway was not refreshed.

## Repository boundary

The parent Git worktree ignores `app/apps/`, so this child had no tracked
source in the parent repository. A separate local Git repository now exists
at this directory with `main` as its initial branch. No files were staged or
committed, and no remote or GitHub publication was created.

The child `.gitignore` excludes:

| Local path or class | Why excluded |
|---|---|
| `wsl/ext4.vhdx` | Live distro disk, nominally 90,910,490,624 bytes; it may be sparse and contains installed state. |
| `linux/ubuntu-base.tar.gz`, `linux/rootfs.setup_hash` | Local bootstrap input and generated setup marker. |
| `Omni_Studio.exe`, `webview.dll` | Packaged Windows binaries built outside this child source tree. |
| `cache/`, `storage/`, Python/test caches, logs and temporary files | Runtime or tool output. |
| Local token/key files and common model-weight formats | Credentials and downloaded weights must remain outside source control. |
| `reports/44-cumbia-*`, `reports/cumbia_*.py`, `reports/infinitetalk-cumbia-setup.md`, `reports/2026-08-21-vram-talking-i2v-plan.md` | Personal one-off media campaign material, retained locally. |

Git ignore checks confirmed those local artifacts are excluded. The current
eligible source list has 390 files totaling about 35.7 MB, including
intentional `test_assets/` fixtures. `test_assets/` contains JSON graphs and
requests plus 19 PNG, 7 WAV, and 3 MP4 files. These are useful for capability
tests, but their redistribution rights and any identifiable media content
must be reviewed before a public repository is staged. The remaining dated
`reports/` are retained as technical evidence; review them for publication
as well. A targeted text scan found no common live-token or private-key
patterns in eligible text files; that is not a complete secret audit.

Two zero-byte accidental root files, `=0.0.20,` and a malformed command-output
filename beginning `, new_name)`, had no references and were removed. The old
Windows-side `storage/` tree and
other obsolete scratch directories described in the August tree hygiene
report were already absent. The live VHDX, package inputs, caches, useful
reports, and reusable fixtures were not deleted.

## Source findings and changes

- `server/setup.sh` copied server, docs, and skills into the distro but did
  not copy `cli/` or the Linux `omni-cli` launcher. Both now sync during
  setup. The Linux launcher is linked into `/usr/local/bin` when that name is
  free. The Linux and Windows launchers now set the runtime Python package
  path explicitly, so invocation does not depend on the caller's directory.
- Nine unreferenced helpers/constants were removed from the server: the old
  blueprint file finder, an unused name regex, default-variant wrapper,
  obsolete Comfy custom-node list, unused Music 3 revision constant, key
  scope helper, output pin query, and two worker-registry queries. Unused test
  imports and one test local were also removed. The staged loader's explicit
  reference release remains because it participates in memory cleanup.
- The remaining empty Click callback registers live subcommands. Explicit
  501/unsupported paths and documented future stubs remain intentional.
- Importing `omni_comfy_server.py` previously installed process-wide SIGINT
  and SIGTERM handlers. Tests import this module during collection; an
  interrupted test host could therefore enter the gateway's worker/Comfy
  shutdown sweep. Handler registration now occurs only when the gateway is
  launched as its own script. A subprocess regression checks that importing
  it preserves existing host signal handlers. The interrupted broad pytest
  command prompted this fix; its exact effect on the chat host cannot be
  proven from source alone.
- The ACE-Step audio-source test now imports router modules using the actual
  server package path, fixing a collection-time import error.

No new API endpoints were added.

## Manual and API guidance

All 13 UI navigation tabs have corresponding manual pages, and local
Markdown links resolve. The operator/API guidance was corrected where it
had drifted from current source: gateway authentication modes, Comfy VRAM
accounting, MOSS-TTS's current decoder blocker, Testing tab behavior, normal
gateway restart cleanup, CLI installation, and the API route inventory.
`app_intents.md` now reflects the current bridge startup, session-cookie
transport, cache release, bearer mode, and package boundary. The Voice & TTS
and Runtime UI copy was aligned with those contracts. The precise API fields
remain the typed route models; no examples should be treated as an alternate
schema.

## Current installed distro snapshot

A small read-only check of the selected child distro found
`/opt/omni_studio/server`, `docs`, and `skills`, but no
`/opt/omni_studio/cli` or `/opt/omni_studio/omni-cli`. It also did not find
the canonical `/opt/omni_studio/models`, `/opt/omni_studio/output`,
`/opt/omni_studio/cache`, or `/opt/omni_studio/comfyui/models` paths, nor
their model-store counterparts beneath `/var/lib/omni_studio`. This is a
partial installed-runtime snapshot. The source setup fix has **not** been
applied to that distro, and the local VHDX has **not** been qualified as a
release package. Do not infer live model readiness from source routes or the
old capability reports.

## Verification and remaining publication work

- `python -m unittest tests.test_manual_coverage tests.test_cli`: 13 passed.
  Focused gateway import-safety, startup-layout, and ACE audio-source pytest
  modules: 9 passed; the startup-layout module was rerun after the final CLI
  link edit: 6 passed. Ruff's selected dead-code checks, Python compilation,
  and `node --check` on changed browser JavaScript passed.
- The broad interactive pytest run was stopped after it disrupted the chat.
  The full suite, including process-lifecycle tests, belongs in an isolated
  test host. No broad test run is claimed here.
- Before public publication, choose a repository license, review retained
  fixture media and reports for rights/private information, and decide how
  the separately built Windows launcher binaries will be distributed or
  reproduced. Re-run setup and qualify the installed distro separately
  before advertising a ready-to-launch package. Screenshots are planned for
  a later turn.
