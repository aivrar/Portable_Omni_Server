# Omni Studio repository layout

This directory is the canonical Windows-side source for the Omni Studio child
app. Keep the tree small enough that a new agent can distinguish product code,
durable evidence, and generated runtime state at a glance.

## Canonical source

- `server/`: gateway, workers, route modules, setup, and static UI.
- `cli/`: supported command-line clients for the typed API.
- `comfy_nodes/`: app-owned ComfyUI custom nodes copied into the distro.
- `skills/`: task-specific agent workflows; `AGENTS.md` routes agents to them.
- `docs/`: canonical human/API guides and this layout map.
- `manual/`: operator guide for the shipped UI and CLI.
- `manual/images/`: selected, reviewed screenshots used by the manual and
  README, with dimensions and hashes in `manifest.json`. These are deliberate
  documentation assets; raw captures and browser profiles stay outside the repo.
- `tests/`: regression and contract tests, including dated security regressions.
- `packaging/`: release preparation helper and maintainer build instructions.
- `tools/`: maintained repository tools, including the manual-to-wiki exporter.
- `test_assets/`: deliberately retained, reusable capability-test inputs.
- `assets/branding/`: editable app-icon artwork and its PNG export.
- `app.ico`, `app-icon.rc`: packaged Windows icon and executable resource source;
  see [Windows icon packaging](windows-icon.md).
- `reports/`: current investigation plans and findings that remain useful.
- `bridge.py`, `bridge_watchdog.py`, `windows_loopback_relay.py`, `app.json`,
  `omni-cli`, `omni-cli.bat`, and `omni-cli.ps1`: launcher, relay, and CLI entry points.

## Local packaged runtime artifacts

- `ext4.vhdx` (preinstalled-image import) or `wsl/ext4.vhdx` (older bootstrap
  and explicit imports): the live WSL distro filesystem. Its apparent size can be
  large and sparse; never treat it as a disposable cache or commit it to Git.
- `linux/ubuntu-base.tar.gz` and `linux/rootfs.setup_hash`: local distro
  bootstrap inputs used by the packaged launcher.
- `Omni_Studio.exe` and `webview.dll`: local Windows application runtime.
- Release packages also include `runtime/python/` and app-local Microsoft C++
  runtime DLLs; these generated binary components stay outside Git.
- `Omni_Studio.exe.pre-icon-backup`: original launcher retained during the icon
  update; local backup, excluded from Git.

The GitHub-ready tree is a **source repository**, not a zipped installed app.
The local packaged artifacts above are ignored by Git. The Windows launcher
and WebView binaries are built outside this child app tree, so a clone alone
cannot recreate them. See the root `README.md` for the source/package boundary.

## Canonical distro locations

The built distro exposes stable public paths beneath `/opt/omni_studio`:

- `/opt/omni_studio/server`: running gateway source.
- `/opt/omni_studio/comfyui/models`: ComfyUI-only model store.
- `/opt/omni_studio/models`: standalone and audio model stores.
- `/opt/omni_studio/output`: persisted media and logs.
- `/opt/omni_studio/workflows`: persisted workflows and metadata.
- `/opt/omni_studio/cache`: rebuildable caches, locks, temp data, and runtime
  records managed by the app.

Mutable model, output, workflow, Comfy input, and Comfy user-data paths may be
guarded symlinks into `/var/lib/omni_studio` so they survive runtime source
refreshes and app restarts. Treat the `/opt` paths and typed APIs as the public
contract. Inspect resolved storage from inside the distro; Windows UNC does not
reliably follow absolute Linux symlinks.

There is no supported Windows-side `storage/` model tree. Do not recreate one
or route workers to it.

## Generated material

Do not place browser profiles, Playwright snapshots, terminal captures,
dependency checkouts, model downloads, or one-off probe output in the source
tree. Use an OS temporary directory for short-lived host work, the app's
managed distro cache for runtime work, `test_assets/` for intentional reusable
inputs, and `reports/` for durable findings.
