# GitHub publication — Portable Omni Server

## Repository identity

| Field | Value |
| --- | --- |
| Owner | `aivrar` |
| Distro and launcher foundation | [aivrar/portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box) |
| Repository name | `Portable_Omni_Server` |
| Repository URL | `https://github.com/aivrar/Portable_Omni_Server` |
| Default branch | `main` |
| App name | Omni Studio |
| License for original project work | MIT; preserve third-party terms |
| Description | AI music generation, ComfyUI image/video workflows, multimodal chat and sound effects in one portable local AI studio. ACE-Step, MiniMax Music 3, Stable Audio and Qwen Omni, with model management, multi-GPU controls, a media library, CLI and API. A Linux distro; Windows requires WSL2. |

## GitHub discovery metadata

Lead with the jobs people want to do: AI music generation, ComfyUI image/video
workflows, multimodal chat and sound effects. Name the integrated engines to
serve searches for those tools. Keep the Linux distro identity and WSL2
requirement in the description and prominent in README; the topic slots are
reserved for capabilities and relevant model/engine communities.

The published description above is 285 characters. Published topics (20):

```text
ai-music ai-music-generator music-generation text-to-music
ace-step minimax-music stable-audio audio-generation sound-effects
comfyui comfyui-workflow image-generation video-generation
text-to-image text-to-video image-to-video local-ai multimodal-ai
omni qwen-omni
```

GitHub permits up to 20 topics, using lowercase letters, numbers and hyphens;
each topic can contain at most 50 characters. See
[GitHub topic rules](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

Topic research on September 25, 2026 found existing communities for
[AI music](https://github.com/topics/ai-music),
[text-to-music](https://github.com/topics/text-to-music),
[ComfyUI](https://github.com/topics/comfyui),
[ComfyUI workflows](https://github.com/topics/comfyui-workflow) and
[MiniMax Music](https://github.com/topics/minimax-music).
These selections reflect relevant search intent and topic usage, not measured
monthly search volume or a ranking guarantee. Keep `omni` paired with the
more specific `qwen-omni` and `multimodal-ai` terms because the word alone
has multiple meanings. Do not use unrelated product names as traffic bait.

This metadata is applied to the public repository and was checked against
GitHub's API after publication.

## Product presentation

The maintainer requests a normal finished-product release under **Portable
Omni Server**, with the Linux distro identity and Windows WSL2 requirements
prominent. Do not add a development-stage badge to the product name. Use a
supported-feature list and factual compatibility instructions in the manual.
Complete the release work below before describing the packaged download as
finished; copy changes do not establish installer or model test results.

The public repository was created under `aivrar` on September 25, 2026.
Its default branch is `main`; the initial source commit is
[`bc47b8e`](https://github.com/aivrar/Portable_Omni_Server/commit/bc47b8e97d56447c86d3cf849db015558e5618d4).
The [publication record](../reports/2026-09-25-source-publication.md) describes
the exact source boundary and checks.

## Published contents

- README uses the new repository identity and credits `aivrar` and the
  `portable-linux-in-a-box` foundation. Internal app/distro identifiers retain
  their existing values.
- MIT license and third-party notices are present.
- The manual includes 18 reviewed app screenshots and a README hero.
- A multi-size `app.ico` and editable branding source are present. The local
  executable has the embedded icon; future builds use `app-icon.rc` as described
  in [Windows icon packaging](windows-icon.md).
- Runtime/private artifacts are excluded by `.gitignore`; reviewed manual
  images remain eligible for Git.
- [Portability](portability.md) documents the preinstalled release contents,
  bundled Windows helpers, host prerequisites and WSL registration/backup rules.
- `origin` points to the public repository; local `main` tracks `origin/main`.

## Source publication record

- [x] Review the exact initial file list, credential-pattern scan and retained
  report/fixture publication scope.
- [x] Exclude the live VHDX, tokens, generated media, caches and local packaged
  binaries; verify the remote Git tree contains only the intended source assets.
- [x] Run focused source/documentation checks from a clean staged checkout.
- [x] Create the public repository and push the initial commit to `main`.
- [x] Apply and verify the description, 20 topics, manual homepage and MIT license.
- [x] Enable the repository wiki.
- [x] Populate the separate wiki after the maintainer created its first page;
  publish all 23 manual pages, navigation and 18 reviewed screenshots with
  working wiki/image links.

## Packaged release work

The maintainer selected the larger preinstalled distro with model weights
downloaded separately. A clean build now contains the shared stack, ComfyUI,
Manager, OmniBridge and all nine model-specific runtime slots. The Windows
package includes embedded Python and signed Microsoft C++ runtime DLLs;
WebView2, WSL2 and the Windows GPU driver remain host prerequisites.

The clean build was tested through an isolated launcher folder on separate
ports. Gateway/audio state checks, offline runtime imports, CPU Comfy startup,
714-node discovery, empty queues, browser navigation, native window icons and
graceful shutdown passed. Exact Ubuntu source packages and dependency/license
inventories accompany the release. The personal working disk is not a build
input. Exact image export/import and publication results are recorded in the
[preinstalled release report](../reports/2026-09-25-preinstalled-release.md).

The 14.2 GiB compressed image passed eight-part assembly and a fresh WSL2
registration driven by the packaged launcher. Desktop/browser startup,
authentication, runtime presence, CLI access, icons and graceful shutdown
passed. First import and connected-browser qualification took about ten
minutes on this host. The release delivers the preinstalled image alongside
the Windows ZIP; model weights remain separate.

[Version 1.0.0 is published](https://github.com/aivrar/Portable_Omni_Server/releases/tag/v1.0.0)
as the latest normal release with 12 verified assets. The illustrated wiki is
also live. The release report records source revisions, image and ZIP hashes,
anonymous download verification and temporary-build cleanup.

Release notes must list included engines, whether weights are included or
downloaded on demand, host requirements, known capability limits, and the exact
qualification results. Publish archive checksums and launcher provenance with
the release. Distinguish a fresh WSL registration on the existing Windows host
from testing on another physical PC, account or GPU.
