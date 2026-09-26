# Portability and package contents

Portable Omni Server is a **Linux distro with the Omni Studio Windows desktop
launcher**. The release includes its Linux dependencies and Windows helper
runtimes. Model weights are downloaded separately inside the app.

## What is included

| Component | Location |
| --- | --- |
| Gateway, API, CLI and browser UI | `/opt/omni_studio/server` and the distro's app directories |
| Shared Python, PyTorch and CUDA userspace libraries | `/opt/omni_studio/venv` |
| ACE-Step and Stable Audio dependencies | Shared Python environment |
| ComfyUI, Manager and OmniBridge nodes | `/opt/omni_studio/comfyui` |
| Qwen, MiniCPM, Qwen3, Nemotron, Moshi, AnyGPT and Music 3 runtime slots | `/opt/omni_studio/overrides` |
| Isolated MOSS-TTS and MOSS-SoundEffect environments and source | `/opt/omni_studio/overrides` |
| Standalone and audio model storage | `/opt/omni_studio/models`, backed by `/var/lib/omni_studio/models` |
| ComfyUI model storage | `/opt/omni_studio/comfyui/models/<category>` |
| Workflows and generated output | Public `/opt/omni_studio` paths backed by `/var/lib/omni_studio` |
| Windows launcher and icons | `Omni_Studio.exe`, `webview.dll`, `app.ico` |
| Windows networking helper interpreter | `runtime/python/`; separate Windows Python installation unnecessary |
| Microsoft C++ runtime | Signed runtime DLLs beside the Windows executable |

Installed runtime code does not mean model weights are installed or a worker
is loaded. The initial studio has empty model and output stores. Use Model
library to choose weights that fit your hardware and accept any upstream model
terms. Community Comfy nodes and later updates may require additional packages.
See [feature compatibility](../manual/21-feature-compatibility.md) for each
engine's supported inputs and behavior.

## Windows host requirements

- 64-bit Windows 11, or Windows 10 21H2 or later for WSL GPU workloads.
- WSL2 with hardware virtualization enabled.
- Microsoft WebView2 Runtime for the desktop window.
- A compatible NVIDIA GPU and Windows driver for CUDA workloads.
- At least 80 GiB free on the Windows drive containing the app for image
  preparation/import, plus room for selected model weights and generated media.

WSL2, Windows GPU drivers and WebView2 are host components. They are not stored
in the Linux disk. Microsoft provides the [WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install),
[GPU prerequisites](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gpu-compute),
and [WebView2 Runtime guidance](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).

## Download and first launch

1. Download the Windows ZIP from [Releases](https://github.com/aivrar/Portable_Omni_Server/releases/latest)
   and extract it to the drive where the studio will live.
2. Run `Prepare-Omni.cmd`. It downloads the numbered preinstalled-image parts,
   verifies SHA-256 checksums, and assembles `linux/rootfs.tar.gz`.
3. Launch `Omni_Studio.exe`. The launcher imports the image, creating
   `ext4.vhdx` beside the executable, and registers `linbox-Omni_Studio`.
   The Linux AI dependencies are already installed.
4. Download the model weights you want inside the app.

For an offline transfer, download every numbered image part into `linux/parts/`
and run `Prepare-Omni.cmd -Offline`. The preparation helper does no dependency
installation. Missing weights, community nodes, gated access and updates can
still require internet access.

Keep the entire app folder together, including `runtime/python/` and the
runtime DLLs. Older bootstrap installations and explicit `wsl --import`
installations may instead keep their disk under `wsl/`. The registered disk
grows as models and media are added. The app's
inside-distro free-space display is distinct from the Windows drive's physical
free space; check both before a large download.

## Moving an existing studio

Windows records the path of each registered WSL distro. A second extracted
folder with the same app identity reuses `linbox-Omni_Studio`; it does not
create an independent studio. Do not drag a registered live VHDX to a new
location or overwrite it with the clean release image.

Use the app's Shutdown button first, then export your studio to a backup:

```powershell
wsl --export linbox-Omni_Studio "D:/Backups/OmniStudio.tar"
```

On a destination PC with WSL2 installed and **no existing distro of that name**,
extract the Windows package and import the backup into its `wsl/` directory:

```powershell
wsl --import linbox-Omni_Studio "D:/Apps/Portable_Omni_Server/wsl" "D:/Backups/OmniStudio.tar" --version 2
```

Launch the executable from that destination app folder. Preserve the original
studio and backup until the restored models and outputs have been checked.
The clean public image contains no personal models, tokens, histories or output.

## Source, provenance and qualification

GitHub's automatic source ZIP contains source code and documentation. Use the
named Windows release asset for the packaged application. The distro and
launcher derive from [aivrar/portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box).

Release assets include checksums, dependency inventories and component
provenance. Upstream license notices remain with their components; original
Omni code is MIT. See [third-party terms](../THIRD_PARTY_NOTICES.md) and the
[maintainer packaging guide](../packaging/README.md).

The release checks distinguish import/startup tests, empty-queue Comfy node
discovery and browser navigation from actual model inference. A fresh WSL
registration on the maintainer's Windows host exercises image transfer without
claiming a test on every Windows account, PC or GPU. Model-specific evidence
and limits remain in [feature compatibility](../manual/21-feature-compatibility.md).
