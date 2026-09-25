# Portability and package contents

Portable Omni Server is the repository name for the **Omni Studio** app.
The app's Linux runtime is designed to travel as one WSL2 distribution. That
does not put Windows, WSL itself, Windows GPU drivers, or the desktop browser
runtime inside the Linux disk.

## What is contained where

| Component | Location | Current qualification |
| --- | --- | --- |
| Gateway and browser UI | `/opt/omni_studio/server` in the distro | Live browser connection verified September 25, 2026. |
| Main Python environment | `/opt/omni_studio/venv` in the distro | Eleven named core packages resolve inside this environment; this is a path check, not full inference qualification. |
| ComfyUI and custom nodes | `/opt/omni_studio/comfyui` in the distro | Checkout exists. Dependencies and models vary by installed workflow. |
| Standalone/audio models | `/opt/omni_studio/models`, normally linked into `/var/lib/omni_studio/models` | Persisted inside the distro. The screenshot session reported zero installed standalone/audio model families. |
| Comfy weights | `/opt/omni_studio/comfyui/models/<category>` | Managed inside the distro; installed files are workflow-specific. |
| Workflows and output | Public `/opt/omni_studio` paths, with persistent targets under `/var/lib/omni_studio` | Inside the distro; retained user content is not a clean public release image. |
| Windows launcher | `Omni_Studio.exe`, `webview.dll`, `app.json` beside the distro files | Local binaries exist; their build source/toolchain is outside this child repository. |
| WSL2 | Windows host | Required, with virtualization enabled. |
| GPU hardware and Windows driver | Windows host | Required for the GPU workloads being used. Linux CUDA userspace libraries do not replace the Windows driver. |
| WebView2 Runtime | Windows host, or a separately packaged Windows runtime | Required for the desktop window. `webview.dll` alone is not the WebView2 Runtime. |
| Windows loopback relay interpreter | Currently found on the host as `pythonw.exe` | A real external dependency of the relay used during browser capture. Not bundled in the current package. |
| Model downloads, updates and gated access | Network when requested | Missing packages/weights need installation. A license or login requirement is not satisfied by carrying the app folder. |

Microsoft documents the [WSL GPU host prerequisites](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gpu-compute)
and the [WebView2 Runtime distribution requirement](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).
Its Fixed Version option can be packaged beside a Windows application; it is
still a Windows component, not a dependency installed in the Linux distro.

## What was checked

On September 25, 2026, a bounded read-only check inspected the seven canonical
runtime/storage paths, the main venv's import paths, `.pth`/`.egg-link` path
entries, and immediate custom-node symlinks. No inspected runtime path resolved
to a Windows mount or another mounted filesystem. There were no external
import paths, direct editable-path entries, or immediate custom-node symlinks.

`fastapi`, `uvicorn`, `websockets`, `torch`, `transformers`, `diffusers`,
`acestep`, `PIL`, `httpx`, `huggingface_hub` and `peft` all resolved under
`/opt/omni_studio/venv/lib/python3.12/site-packages`. The probe located modules
without importing model frameworks or loading weights. It did not inventory
every shared library, isolated engine environment, plugin, or cached model.

The main environment is therefore locally contained for the inspected scope.
This evidence does not establish that every optional capability is installed,
compatible, or usable offline. See [capability confidence](capability-confidence.md).

## Three different deliverables

### Source repository

`aivrar/Portable_Omni_Server` contains code, setup instructions, the manual,
screenshots and tests. Git ignores the distro disk, packaged Windows binaries,
bootstrap archives, model weights, credentials and generated media. GitHub's
source ZIP will not be a runnable, fully populated studio.

The repository name does not rename the executable or registered distro.
Existing installations still use `Omni_Studio.exe`, `linbox-Omni_Studio`, and
the `/opt/omni_studio` paths. Changing those identifiers requires a separate
migration and launcher qualification.

### Portable application release

A release package must include the required Windows launcher files and a
prepared, transferable Linux environment, plus any chosen Windows helper
runtimes. The reviewed launcher source supports a prepared
`linux/rootfs.tar.gz`; when it is absent and no matching distro is registered,
it bootstraps from the minimal `linux/ubuntu-base.tar.gz`.

The current app folder has a roughly 30 MB bootstrap archive and its live
`wsl/ext4.vhdx`, but **no prepared `linux/rootfs.tar.gz`**. `app.json` also has
automatic snapshot export disabled. A live VHDX being present is not proof
that the launcher will adopt a copied disk automatically on a fresh PC.
The exact distributed launcher and archive must be tested together.

The setup completion stamp is currently absent after the source-only refresh.
A complete setup/repair run and a clean release export are still needed before
claiming a ready-to-run offline package. First install, repair, new models,
and updates can require network access.

### Private studio backup

A backup may include the user's entire distro, models and output. Back up
through a clean shutdown/export procedure; do not copy a live, mounted VHDX
as if it were an ordinary inactive file. Windows maintains a distro
registration as well as the disk. Preserve the original until the copied
environment has been restored and verified.

Never treat the user's working VHDX as the public release image. Prepare a
separate clean image so access tokens, API keys, histories and private output
do not ship with the public app. The source repository's ignore rules do not
remove secrets from a Linux disk image.

## Before claiming complete portability

1. Bundle or replace the Windows relay's Python dependency, and document the
   WebView2 strategy alongside the WSL2/GPU host requirements.
2. Build the launcher from a reproducible source revision or provide the
   separate launcher build instructions and release artifact provenance.
3. Finish setup in a clean release distro; define which optional engines are
   included. Download model weights separately unless redistribution is intended
   and permitted by their terms.
4. Export a clean runtime image with matching setup metadata. Verify that the
   archive, rather than the maintainer's existing WSL registration, is used.
5. Test from a different directory and Windows account on a fresh host, without
   developer Python, the original source directory, or an existing Omni distro.
6. For an offline claim, repeat the supported installed-model smoke test with
   networking disabled after host prerequisites are satisfied.

None of these steps requires altering or deleting the current working studio.
The [release plan](github-release-plan.md) tracks the preparation boundary.
