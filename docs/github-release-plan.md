# GitHub preparation — Portable Omni Server

## Repository identity

| Field | Value |
| --- | --- |
| Owner | `aivrar` |
| Distro and launcher foundation | [aivrar/portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box) |
| Repository name | `Portable_Omni_Server` |
| Intended URL | `https://github.com/aivrar/Portable_Omni_Server` |
| Default branch | `main` |
| App name | Omni Studio |
| License for original project work | MIT; preserve third-party terms |
| Suggested description | AI music generation, ComfyUI image/video workflows, multimodal chat and sound effects in one portable local AI studio. ACE-Step, MiniMax Music 3, Stable Audio and Qwen Omni, with model management, multi-GPU controls, a media library, CLI and API. A Linux distro; Windows requires WSL2. |

## GitHub discovery metadata

Lead with the jobs people want to do: AI music generation, ComfyUI image/video
workflows, multimodal chat and sound effects. Name the integrated engines to
serve searches for those tools. Keep the Linux distro identity and WSL2
requirement in the description and prominent in README; the topic slots are
reserved for capabilities and relevant model/engine communities.

The description above is 285 characters. Proposed topics (20):

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

This metadata is prepared locally for publication; it has not been applied
to a remote repository.

## Product presentation

The maintainer requests a normal finished-product release under **Portable
Omni Server**, with the Linux distro identity and Windows WSL2 requirements
prominent. Do not add a development-stage badge to the product name. Use a
supported-feature list and factual compatibility instructions in the manual.
Complete the release work below before describing the packaged download as
finished; copy changes do not establish installer or model test results.

The authenticated GitHub CLI account was verified as `aivrar` on September 25,
2026. The repository lookup returned 404 for that account at preparation time.
This is a preparation record, not a claim that the remote repository exists.

## Prepared locally

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
- [Portability](portability.md) documents actual runtime containment and the
  host Python relay dependency, missing prepared export, and fresh-PC test gap.
- `origin` is configured to the intended repository URL. No remote repository
  was created, and no files were committed or pushed during this preparation.

## Source publication work

1. Review the intended initial tree, especially historical reports and retained
   fixtures, for personal content and redistribution scope.
2. Keep the live VHDX, tokens, generated media, caches and local packaged
   binaries out of the source commit. Review the exact staged file list.
3. Run focused source/documentation checks. Do not use the known-disruptive
   broad Windows test campaign on this interactive host.
4. Create the remote repository with the intended visibility, make the reviewed
   initial commit, and push `main` when publication is requested.
5. Configure the repository description/topics and link the manual. If a
   separate wiki is wanted, copy its reviewed pages and images and resolve links
   back to the source repository.

## Packaged release work

The source repo and the portable app archive have separate readiness gates.
The current Windows relay finds a host `pythonw.exe`; a bundled implementation
is still required for a package that does not need developer Python. WebView2,
WSL2 and the GPU driver must have an explicit distribution/prerequisite policy.

The folder currently has only a minimal Ubuntu bootstrap archive plus the live
VHDX. Build a clean distributable runtime export, preserve its dependency/model
license notices, and verify it on a fresh host with no original Omni registration
or developer checkout. Do not package the personal working disk directly.

Release notes must list included engines, whether weights are included or
downloaded on demand, host requirements, known capability limits, and the exact
qualification results. Publish archive checksums and launcher provenance with
the release. Complete offline portability remains unqualified until that
fresh-host test passes.
