# What Omni Studio is

Omni Studio is a **Linux distro packaged as a Windows application for Windows
users**. The product you launch is a Windows program. The product that actually
runs models is a self-contained Linux environment. That split is the whole
reason the Windows-side requirements exist, and it is the reason models and
output never appear as ordinary folders next to the `.exe`.

> [!IMPORTANT]
> **WSL2 must be installed on Windows to run this Linux distro.** Enable
> hardware virtualization and install the Windows-side requirements below
> before launching `Omni_Studio.exe`.

If you have used a website that "runs in the cloud," this is the opposite: the
compute is local, the GPU is yours, and the files stay on this machine. If you
have used Docker Desktop or a Linux VM, this is simpler: you do not provision
an OS. You keep one app folder together and double-click `Omni_Studio.exe`.

## What you are looking at

The distro and Windows launcher were built from
[portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box),
also maintained by [aivrar](https://github.com/aivrar). Omni Studio supplies the
AI workspace and gateway on top of that foundation.

On Windows you see:

- a native window titled Omni Studio
- a top bar with a connecting/connected badge and a **Shutdown** button
- a sidebar of destinations (Home, Chat, Media library, Workflows, Engine &
  queue, Voice & TTS, Audio Lab, Music, Music 3, MOSS, Model library, Runtime,
  Testing, Logs)

That window is a **WebView2** view of a Linux web app. The Linux web app talks
to a gateway process inside WSL2. The gateway talks to model workers, ComfyUI,
and audio engines that also live inside the same distro.

The WSL distro name is **`linbox-Omni_Studio`**. Do not install a second Ubuntu
and expect Omni Studio to use it. Do not rename this distro in `wsl --list`
unless you also understand that the launcher, `omni-cli.bat`, and the VHDX all
point at that name.

## Why it is a Linux distro on Windows

Local multimodal models, ComfyUI custom nodes, and CUDA toolchains are Linux
software. Omni Studio does not reimplement them as native Win32 programs. It
ships a Linux root filesystem, copies the gateway into
`/opt/omni_studio`, and keeps mutable state (models, workflows, output, Comfy
inputs) on Linux disk.

That design gives Windows users a double-click app instead of a Linux
workstation, and it keeps GPU libraries, Python environments, and multi-gigabyte
checkpoints off NTFS. Windows Explorer is not the model store. There is no
supported Windows-side `storage/` tree. If you create one, workers will not
use it.

## Windows-side requirements

Use these as a go/no-go list before the first launch.
The repository is named **Portable Omni Server** (`aivrar/Portable_Omni_Server`);
the app is still Omni Studio. Its [portability guide](../docs/portability.md)
explains the Linux runtime, Windows prerequisites, source checkout and
prepared release archive.

### Windows 10 or Windows 11

Use 64-bit Windows 11 or Windows 10 21H2 or later for WSL GPU workloads.
Home editions work. You do not need Windows Server, Hyper-V
Manager skills, or a Linux dual-boot.

Turn on virtualization in firmware if WSL2 has never been used on the PC.
Windows will usually prompt for this during `wsl --install`.

### WSL2

WSL2 is the primary Linux backend. Omni Studio's live filesystem is the VHDX
at `wsl/ext4.vhdx` inside the app folder. The distro that VHDX becomes is
`linbox-Omni_Studio`.

If WSL2 is missing, the launcher can show setup instructions. The one-time
Windows command, from an elevated PowerShell window, is:

```powershell
wsl --install
```

Restart Windows when the installer asks. After reboot, confirm:

```powershell
wsl --status
wsl --list --verbose
```

You want WSL2 as the default version. Older WSL1 distros cannot run this app.

You do not need to create an Ubuntu distro yourself. Omni Studio imports and
owns `linbox-Omni_Studio`. Other distros on the same PC are unrelated.

### WebView2

The graphical workspace is rendered with **WebView2** (the Microsoft Edge
webview runtime) plus `webview.dll` sitting next to `Omni_Studio.exe`. Current
Windows 10/11 machines that already run Edge usually already have WebView2.

If WebView2 is missing, the window will not appear as the full UI. The
underlying Linux distro can still be reached from a terminal, and
`omni-cli.bat` can still talk to a running gateway, but that is not the
intended operator path. Install the Evergreen WebView2 Runtime from Microsoft
and relaunch.

### Windows Python for the current loopback relay

The current bridge launches its Windows loopback networking helper through
`pythonw.exe` installed on the Windows host. This is separate from the Linux
Python environment in the distro. Without it, the helper is unavailable and
connectivity depends on WSL's native localhost forwarding behavior.

Install Windows Python with `pythonw.exe` available to the current user when
using this relay. The Linux venv remains inside the distro; the relay uses
the Windows interpreter only for the host networking helper.

### NVIDIA GPU

Omni Studio is a local GPU studio. A current **NVIDIA GPU** with a Windows
driver that exposes the device to WSL2 is the practical requirement.

What "practical" means:

- Chat, Music, Audio Lab, Music 3, MOSS, and ComfyUI workflows expect CUDA
  devices such as `cuda:0`.
- The Runtime tab lists those devices with free/total VRAM.
- CPU options exist on several forms. They are fallbacks for tiny tests, not
  a replacement for a 12 GB or 24 GB card.
- Two GPUs are useful. Placement can put a diffusion model on one card and a
  text encoder on another. One strong GPU is enough to start.

Install the NVIDIA Windows Game Ready or Studio driver, then confirm WSL can
see the GPU from inside the distro after first launch. If Runtime shows only
CPU, stop and fix drivers before downloading 20 GB checkpoints.

### Disk headroom

Leave **disk headroom** on the Windows drive that holds the app folder. The
Linux disk is a VHDX that grows. Models, Comfy checkpoints, and output all
live inside it.

Rough sizes operators actually hit:

- Omni chat variants: a few GB to tens of GB each
- ACE-Step DiT + LM: several GB to well over 10 GB
- MiniMax Music 3 official Diffusers subset: about 28 GB
- ComfyUI checkpoints, VAEs, and video stacks: tens to hundreds of GB
- Generated images, videos, and WAVs: unbounded unless you prune

Home reports free space **inside the distro**, not "free on `C:`" in the
Windows sense. If Windows itself is down to a few GB, WSL cannot expand the
VHDX and installs fail in confusing ways. Keep tens of GB free on the host
before a first model download, and much more if you intend to use ComfyUI
video or Music 3.

Do not store the app folder on a RAM disk, a cloud-sync folder that locks
files, or a network share that cannot host a VHDX.

## Keep the app folder together

The Windows folder is a self-contained launcher, not a single file.

Typical siblings of `Omni_Studio.exe`:

- `app.json` — names the distro `linbox-Omni_Studio` and the start command
- `webview.dll` — WebView2 interop
- `linux/ubuntu-base.tar.gz` — bootstrap rootfs used on first import
- `wsl/ext4.vhdx` — the live Linux disk (large, sparse, not a cache)
- `bridge.py`, `bridge_watchdog.py`, `omni-cli.bat`, `omni-cli`
- `server/`, `cli/`, `docs/`, `manual/`

**Keep the app folder together.** For a transfer, use a cleanly stopped/exported
package and verify its WSL registration on the destination. Windows stores
distro registration separately from the app folder. Do not
drag `Omni_Studio.exe` to the desktop by itself. Do not delete `wsl/ext4.vhdx`
to "save space" unless you intend to destroy every installed model and every
generated file. Do not separate `webview.dll` from the exe.

For a private backup, close the app cleanly and ensure its distro disk is no
longer mounted before copying or archiving it, or use a controlled distro export.
Include the distro if you want models and output; that archive can be huge.
A backup of only the Windows sources without the distro is a source backup.
Keep a verified original until a restored copy works. A public release needs
a separate clean image without personal credentials or output.

## What stays inside the distro

The sidebar reminder is literal: **models and output stay inside this distro**.

Canonical Linux locations:

| Path | Role |
|---|---|
| `/opt/omni_studio/server` | Running gateway and UI |
| `/opt/omni_studio/models` | Standalone and audio model stores |
| `/opt/omni_studio/comfyui/models/<category>` | ComfyUI-only weights |
| `/opt/omni_studio/output` | Persisted media and logs |
| `/opt/omni_studio/workflows` | Saved Comfy graphs and metadata |
| `/opt/omni_studio/cache` | Locks, temp, runtime records |

Some of those `/opt` paths are durable links into `/var/lib/omni_studio` so
source refreshes do not wipe your library. You still talk to them through the
UI, CLI, and `/opt` paths. Windows UNC paths do not reliably follow those
Linux links. Browse files in the Media library, or copy them out with
`omni-cli outputs get`, rather than hunting through `\\wsl$`.

## Network and privacy

The normal client surface is `http://127.0.0.1:9200` on the Windows host. The
WebView talks to that loopback address. Remote machines cannot use your studio
unless you deliberately widen the bind, which this manual does not recommend.

Downloads (HuggingFace, Comfy Manager catalogs) need outbound HTTPS. Generation
itself is local. A HuggingFace token is stored inside the distro for gated
repos; it is not uploaded to Omni Studio as a cloud account.

## Choose the workspace for your task

Use the app's model installers and Comfy update controls to manage engines.
Chat input types depend on the selected model. The Voice & TTS hub is
reserved and carries a **Soon** badge; audio generation and sound-effect
workflows have their own engine pages. See
[Feature compatibility](21-feature-compatibility.md) before choosing a model
for speech, video input or another specialized task.

## Related pages

- [Launch and first run](02-launch-and-first-run.md) — double-click path
- [Home](03-home.md) — first screen after connect
- [Shutdown](17-shutdown.md) — how to stop the distro's heavy processes
- [Command-line interface](18-cli.md) — `omni-cli.bat` against `linbox-Omni_Studio`
