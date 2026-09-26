# Omni Studio usage manual

This folder is the operator usage manual for **Omni Studio**. Read it when you
want to run the app as a person sitting at a Windows PC: launch it, install a
model, generate media, inspect results, and shut it down cleanly.

Omni Studio is a **Linux distro packaged as a Windows application for Windows
users**. You double-click `Omni_Studio.exe` like any other Windows program. Under
the hood the launcher starts a dedicated WSL2 distro named `linbox-Omni_Studio`
and shows the Linux workspace in a WebView2 window. You do not install Ubuntu by
hand, you do not manage Docker, and you do not need to learn Linux first. You
do need the Windows-side requirements in
[What Omni Studio is](01-what-omni-studio-is.md) and the launch steps in
[Launch and first run](02-launch-and-first-run.md).

> [!IMPORTANT]
> **Install WSL2 on Windows before launching the distro.** Enable hardware
> virtualization and install the WebView2 Runtime. CPU-capable workloads can
> run without an NVIDIA GPU; CUDA workloads need a compatible NVIDIA GPU and
> Windows driver. Python is included. The host requirements page also covers
> CPU/GPU selection and disk space.

This manual is written as how-to prose. Each page walks through the actual
buttons, fields, and consequences of using that part of the app. It is not a
route catalog. The in-repo `docs/` folder remains the API contract for agents
and programmers.

## How to use this manual

The [screenshot gallery](screenshots.md) provides real captures of every main
destination, a repository hero, and narrow-window navigation. Screenshots
are also embedded in the matching pages below.

1. Start with identity and launch if you have never opened the app.
2. Use Home to decide the next destination, then follow that destination's
   page.
3. Use [Feature compatibility](21-feature-compatibility.md) to select model
   inputs, engine dependencies and GPU capacity for your task.
4. Use the CLI and GPU-placement pages when you leave the graphical workspace
   or when a workflow asks you to choose GPUs.

## Pages

### Windows host, Linux distro, and first launch

| Page | What it teaches |
|---|---|
| [What Omni Studio is](01-what-omni-studio-is.md) | Linux-distro-for-Windows identity, Windows 10/11, WSL2, WebView2, NVIDIA GPU, disk headroom, and what stays inside the distro |
| [Launch and first run](02-launch-and-first-run.md) | Keep the app folder together, double-click `Omni_Studio.exe`, first-boot setup, connecting badge, navigation, and the Shutdown button |

### Workspace

| Page | What it teaches |
|---|---|
| [Home](03-home.md) | Recommended next step, HuggingFace token, default downloads, LoRA search, and install jobs |
| [Chat](04-chat.md) | Sessions, spawn-a-worker, attachments, sampling knobs, abort, and which models actually consume image/audio/video |
| [Media library](05-media-library.md) | Browse, filter, pin, tag, collections, lightbox, ZIP export, delete, and media retrieval |

### Models and runtime

| Page | What it teaches |
|---|---|
| [Model library](06-model-library.md) | Install, variants, ComfyUI template catalog, Audio Lab / ACE assets, and deletion |
| [Runtime](07-runtime.md) | Devices, spawn/unload workers, Comfy from Runtime, API keys, and OpenAI-compatible `/v1` |

### ComfyUI

| Page | What it teaches |
|---|---|
| [Workflows](08-workflows.md) | Import, verify, GPU placement (`single` / `auto` / `manual`), analyze-before-run, queue, and `valid: false` as a hard stop |
| [Engine & queue](09-engine-and-queue.md) | Start options, GPU pool, queue, interrupt, unload models, custom nodes, and assets |

### Audio

| Page | What it teaches |
|---|---|
| [Voice & TTS](10-voice-and-tts.md) | The **Soon** surface and the current speech-generation limitation |
| [Audio Lab](11-audio-lab.md) | Stable Audio load, generate, ranked candidates, A2A, inpaint, VAE lab, CLAP score |
| [Music](12-music.md) | ACE-Step install, load, generate, transform, LoRAs, and specialized-adapter requirements |
| [Music 3](13-music-3.md) | MiniMax Music 3 official Diffusers install, CPU offload, lyrics + caption generation |
| [MOSS](14-moss.md) | MOSS-SoundEffect, the MOSS-TTS controls, and the current TTS decoder failure |

### Developer tools and shutdown

| Page | What it teaches |
|---|---|
| [Testing](15-testing.md) | One-shot Omni inference with optional media; Audio Lab and ACE-Step load controls |
| [Logs](16-logs.md) | Live log stream, auto-scroll, clear, and what the colors mean |
| [Shutdown](17-shutdown.md) | The top-bar Shutdown button versus gateway restart and exact-worker cleanup |

### Special usages

| Page | What it teaches |
|---|---|
| [Command-line interface](18-cli.md) | `omni-cli.bat` on Windows and `omni-cli` inside the distro |
| [GPU placement](19-gpu-placement.md) | `single` / `auto` / `manual`, stable GPU UUIDs, analyze-before-run, `valid: false` |
| [Jobs, tokens, composition, and media retrieval](20-jobs-huggingface-and-special-usages.md) | HuggingFace token, install jobs, polling jobs, compose long audio, fetch files |
| [Feature compatibility](21-feature-compatibility.md) | Model input types, speech-runtime compatibility, specialized adapters and GPU capacity |

## A one-screen mental model

Omni Studio has three kinds of heavy runtime, and they do not share a single
"Load model" button:

1. **Standalone Omni workers** — Chat models such as Qwen and MiniCPM. Spawn
   them from Runtime, Chat, or Testing. Unload by killing the exact worker.
2. **ComfyUI instances** — Image and video graphs. Start the engine from
   Engine & queue, then run a graph from Workflows. Starting Comfy does not
   load a checkpoint.
3. **Audio engines** — Audio Lab, Music (ACE-Step), Music 3, and MOSS each
   have their own load/unload controls. Do not assume a Chat worker can sing,
   and do not assume ACE-Step can chat.

Nothing heavy starts until you ask. Home can look "ready" while every GPU is
idle. That is intentional.

Models, workflows, and generated files live **inside the distro**, not next to
`Omni_Studio.exe` on NTFS. Treat the Windows folder as the launcher. Treat
`linbox-Omni_Studio` as the studio.

## Conventions used on every page

- **Do this** means an operator step in the shipped UI or CLI.
- **Then** is the next observable result you should see.
- **Do not** is a real failure mode in this build, not a style preference.
- **Unavailable** means a task requires an implementation or compatible
  dependency beyond the installed configuration. Reserved destinations carry
  the **Soon** badge.

If a page and a live dialog disagree, trust the live dialog: device lists,
installed variants, and free VRAM change between boots. Re-read devices before
you spawn or queue.
