# App screenshots

These are real browser captures of Omni Studio, taken on September 25, 2026
through its Windows-facing bridge at `http://127.0.0.1:9200`. The gateway was
connected to the local `linbox-Omni_Studio` distro. The pictures show the
application's current controls, installed state and disabled actions.

![Omni Studio Home overview](images/hero.png)

## Choose a page

Click a screenshot to inspect its original PNG, or follow the manual page
for instructions. Most desktop captures are 1440 × 1000 pixels. The hero is
3200 × 1800 pixels, captured at a 1600 × 900 viewport with device scale 2.
The editor and Shutdown images are direct element captures, and the narrow
Home image is 390 × 844 pixels.

| Screen | Image | Manual | What is shown |
| --- | --- | --- | --- |
| Home overview | [Open PNG](images/hero.png) | [Manual overview](README.md) | Connected, idle workspace with no installed standalone model families. |
| Home | [Open PNG](images/home.png) | [Home](03-home.md) | Home recommends installing a model; the gateway is connected and idle. |
| Chat | [Open PNG](images/chat.png) | [Chat](04-chat.md) | A new empty Qwen session. No worker is running; unsupported audio/video attachments are disabled. |
| Media library | [Open PNG](images/media-library.png) | [Media library](05-media-library.md) | The filename prefix filter has no matches. Clear filters returns to the library; existing personal media is outside this capture. |
| Model library | [Open PNG](images/model-library.png) | [Model library](06-model-library.md) | Expand a model family to inspect variants, size and installation status before downloading. |
| Runtime | [Open PNG](images/runtime.png) | [Runtime](07-runtime.md) | Device memory, idle worker state and the Windows-facing API address. Memory use includes activity outside Omni. |
| Workflows | [Open PNG](images/workflows.png) | [Workflows](08-workflows.md) | Saved workflow requirements after checking. Missing models and stopped Comfy keep Run disabled. |
| Engine & queue | [Open PNG](images/engine-queue.png) | [Engine & queue](09-engine-and-queue.md) | Local Comfy installation is ready, but no instance is running. No remote update check was performed. |
| Voice & TTS | [Open PNG](images/voice-tts.png) | [Voice & TTS](10-voice-and-tts.md) | The speech workspace is marked Soon. Its roadmap is not evidence of working speech generation. |
| Audio Lab | [Open PNG](images/audio-lab.png) | [Audio Lab](11-audio-lab.md) | Generate controls with no Audio Lab worker. Load a compatible engine before submitting audio work. |
| Music | [Open PNG](images/music.png) | [Music](12-music.md) | ACE-Step task controls in their unloaded state. Visible transformations have separate model requirements. |
| Music 3 | [Open PNG](images/music-3.png) | [Music 3](13-music-3.md) | An unsent instrumental example with a 30-second duration and the RTX 3090 selected. The model is not installed; Generate is disabled. |
| MOSS | [Open PNG](images/moss.png) | [MOSS](14-moss.md) | Separate speech and sound-effect controls with no installed workers. The manual documents the speech decoder limitation. |
| Testing | [Open PNG](images/testing.png) | [Testing](15-testing.md) | Developer controls for selecting installed variants and workers. Loading and inference were not performed for this capture. |
| Logs | [Open PNG](images/logs.png) | [Logs](16-logs.md) | The actual log viewer with retained local log entries. Historical entries do not describe a currently running Comfy instance. |
| Workflow JSON editor | [Open PNG](images/workflow-editor.png) | [Workflow JSON editor](08-workflows.md) | An unsaved, model-free EmptyImage/PreviewImage draft. The draft was not queued or saved to the workflow library. |
| Narrow window | [Open PNG](images/home-narrow.png) | [Narrow window](02-launch-and-first-run.md) | Home at a 390-pixel viewport. The hamburger opens navigation; Escape or the scrim closes it. |
| Shutdown control | [Open PNG](images/shutdown-control.png) | [Shutdown control](17-shutdown.md) | The top-bar Shutdown button. Cancelling its confirmation was tested without stopping the gateway. |

## Read the state shown in each image

The gateway and UI are available, while model workers and Comfy instances
are idle. No weights were downloaded or loaded for this screenshot session.
The standalone/audio installation lists report no installed model families;
the retained workflow and media libraries still exist. An installed Comfy
checkout does not mean that a Comfy instance or a workflow is running.

The Chat image uses a newly created empty session, deleted after capture.
Music 3 contains an unsent example draft. The workflow editor contains an
unsaved model-free graph. None of these images claims a completed generation.
Voice & TTS remains marked Soon, and current speech/model limitations remain
documented in [Feature compatibility](21-feature-compatibility.md).

## Assets for GitHub and the wiki

Use `images/hero.png` for the repository hero. The manual pages already use
relative image links that work when browsing the repository on GitHub.
When moving pages into a separate wiki checkout, copy the `images/` directory
alongside them and preserve those relative paths. Check links to `docs/` and
`reports/` against the eventual repository URL when publishing the wiki.

These selected images are intentional documentation assets. Browser profiles,
raw captures and one-off automation stay outside the source tree. Media
artwork, private prompts, credentials and personal account panels are not
included. Model and engine names identify the controls shown and retain their
upstream terms. Original documentation screenshots follow the repository's
license scope; they do not relicense model outputs or third-party products.

The [asset manifest](images/manifest.json) records dimensions and SHA-256
hashes. The [capture and verification report](../reports/2026-09-25-screenshots-and-browser-checks.md)
records the browser version, runtime refresh, checks, fixes and remaining
qualification work. Refresh screenshots after visible UI changes, wait for
status requests to settle, and keep readiness warnings visible.
