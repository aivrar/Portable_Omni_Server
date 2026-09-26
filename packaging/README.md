# Preinstalled Windows / WSL2 release

The release combines the Windows launcher, its bundled Python relay, and a
clean Linux root filesystem with engine dependencies installed. Model weights
are downloaded separately. WSL2, virtualization and WebView2 remain host
requirements. A compatible NVIDIA GPU and Windows driver are needed for CUDA
workloads; CPU-capable workloads can use the CPU. Both Linux and Windows
Python runtimes are included in the portable release.

## User package

- The Windows ZIP contains `Omni_Studio.exe`, `webview.dll`, `app.ico`, app
  sources, `runtime/python/`, this preparation helper, and the release manifest.
- Numbered `omni-rootfs-vVERSION.tar.gz.NNN` assets carry the preinstalled Linux
  image. Each stays below GitHub's 2 GiB asset limit.
- `Prepare-Omni.cmd` downloads missing parts and checks their sizes and SHA-256
  hashes before joining them into `linux/rootfs.tar.gz`. It also checks the
  complete image hash. It does not install Linux dependencies or model weights.
- For offline preparation, place all parts in `linux/parts/` and run
  `Prepare-Omni.cmd -Offline`. Verified parts are retained for resuming and can
  be removed after successful first launch.
- The launcher's preinstalled-image import creates `ext4.vhdx` beside the exe
  on first use. Older bootstrap installations can use `wsl/ext4.vhdx` instead.
  An existing `linbox-Omni_Studio` registration is reused; the package is not an
  automatic replacement or migration tool for an existing studio.

## Maintainer build sequence

Use a separate, explicitly named build distro. Never export the personal studio
for a public release. Keep host staging and logs outside the source repository.

1. Archive the reviewed source commit and import the Ubuntu base into the build
   distro. Extract sources at `/opt/omni_studio/source`.
2. Run `server/setup.sh` with bounded CPU/download concurrency. This installs
   the shared environment, ACE-Step, Stable Audio, ComfyUI and Manager. Require
   its real success stamp; do not create a stamp to bypass a failed setup.
3. Install the model-specific code without weights, one family at a time:

   ```bash
   for family in qwen minicpm qwen3 nemotron moshi anygpt minimax_music3 moss_tts moss_sfx; do
       bash /opt/omni_studio/server/install_model.sh runtimes "$family" || exit
   done
   ```

   Set `OMNI_MOSS_TTS_REF` to the exact reviewed MOSS commit for a release.
   Preserve that checkout: the MOSS environments install it in editable mode
   at a stable path inside the distro. Record the resolved versions of every
   environment. Runtime installation is separate from model compatibility;
   retain the manual's supported-modality guidance.
4. Include system `python3-pip` for the launcher's dependency check. Include
   upstream licenses, installed package copyright/license files, dependency
   version inventories, repository revisions and launcher hashes.
5. Download the official Windows Python embeddable ZIP, verify its upstream
   checksum/signature, and extract it unchanged under `runtime/python/` with
   its license. Keep `app.ico` beside the icon-bearing launcher.
6. Check imports and lightweight gateway routes. Stop only build-owned
   processes gracefully. Remove build caches, transient tokens, registry state,
   logs and machine identity from the clean image. Keep all user-data stores
   empty and check for paths outside the distro.
7. Export and compress the image; create numbered parts and a schema-1 manifest
   containing the image size/hash and ordered part names/sizes/hashes. Place
   `Prepare-Omni.ps1` and `Prepare-Omni.cmd` at the Windows package root.
8. Test the assembled image through a fresh, uniquely named registration and a
   relocated package directory. Use separate bridge/gateway ports to protect
   the working studio. Test startup, authentication, empty storage and graceful
   shutdown without loading weights merely to test routing.
9. Record exact results in `reports/`, commit the reviewed source, attach assets
   and `SHA256SUMS.txt` to the matching release tag, then verify the published
   downloads. Record physical-PC/hardware testing separately from an isolated
   registration on the maintainer's existing Windows host.

The small Windows ZIP is the entry point for the larger preinstalled distro.
Users may download every asset in advance; the helper never substitutes a
minimal image that builds the AI stack during first launch.

## Wiki publication

Clone the initialized wiki repository outside this tree, then run:

```text
python tools/export_wiki.py PATH_TO_WIKI_CHECKOUT
```

The exporter checks manual links, copies reviewed screenshot assets and rewrites
links for GitHub wiki pages. Review, commit and push that separate checkout.
