# Preinstalled distro release qualification

The maintainer selected the larger preinstalled distro with model weights
downloaded separately. The build uses a new Ubuntu base and reviewed source;
the personal studio's VHDX, credentials, models and generated media are not
release inputs.

## Identity and provenance

- Repository: [aivrar/Portable_Omni_Server](https://github.com/aivrar/Portable_Omni_Server).
- Runtime source: `38c267e4f7166b530e3d955ba566b92d908ee695`.
- Foundation: [aivrar/portable-linux-in-a-box](https://github.com/aivrar/portable-linux-in-a-box),
  source revision `0d03820f443e74b34fae5b604a1924157ba80505`.
- Existing Windows launcher with Omni icon resources; this work does not claim
  a fresh reproducible compilation of that upstream executable.
- Linux: Ubuntu 24.04.4 LTS amd64, with current package upgrades at build time.
- ComfyUI: `1ac60da2c9c8f83654204b2a1db13908cf7614f7`.
- ComfyUI-Manager: `7955e638db7d4a4b8bf7a614e724a2013b83dfd7`.
- ACE-Step source: `6d467e4b5081ccb0abf1ec1bf4fdf9051a2d34b0`.
- Music 3 Diffusers source: `dafe3733fcfdbf3c48915fe77be3aef65b5d6a2d`.
- MOSS source: `934d6826b084c46a0d033402174d5f8ac4ed2519`.
- Windows Python: official embeddable 3.13.15 amd64, upstream SHA-256 checked
  and valid executable signature checked before extraction.
- Microsoft C++ runtime: 14.51.36247.0 x64, extracted from the official signed
  redistributable; all 12 app-local DLL signatures checked.

`release-info/` in the Windows package records complete Windows file hashes,
upstream links, resolved Python package inventories and Linux provenance.
Original project code is MIT. Component licenses remain with their components;
Python, WebView, the distro foundation and Microsoft notices accompany the
Windows package. An optional source asset contains all 308 exact Ubuntu source
packages corresponding to the installed binary package inventory.

## Build and cleanup

The shared environment and all nine dependency-only runtime families were
installed sequentially with bounded CPU/download concurrency. Real setup
success stamps were produced and retained. No model weights were downloaded.
The shared PyTorch version is 2.7.1+cu128; the two MOSS environments retain
their distinct Torch/Transformers dependencies inside the distro.

Before export, build-owned services were gracefully stopped. Transient API
tokens, discovery/worker records, logs, pip/Hugging Face caches, shell history,
test outputs and machine identity were removed. Model stores and persisted
output stores were checked empty. Runtime source and editable MOSS paths point
inside the Linux filesystem. The host app symlink from the test launch was
removed. The source tree, runtime dependencies, package license files and real
setup completion stamp were retained.

## Build qualification results

| Check | Result |
| --- | --- |
| Full clean shared setup | Passed, including ACE-Step and Stable Audio import checks |
| Nine dependency-only runtime installers | Passed |
| Gateway, Qwen/GPTQ, MiniCPM, Qwen3, Moshi, AnyGPT, Music 3 imports | Passed with Hugging Face network access disabled |
| Separate MOSS-TTS and MOSS-SoundEffect environment imports | Passed |
| Native launch from a separate folder containing spaces | Passed; gateway ready in about 6 seconds with setup fast path |
| Gateway session, health, devices and cheap audio state reads | HTTP 200; audio reads used `autospawn=false` |
| Comfy CPU startup | Passed with an explicitly selected CPU device |
| Comfy node discovery | 714 nodes; standard load/sample/decode/save nodes and OmniRouteModel/CLIP/VAE present |
| Comfy queues | Empty before stop; no generation submitted |
| Browser navigation | Home, Runtime, Model library and Comfy engine passed; no page errors |
| Native window icons | Both small and large icon handles present |
| Graceful app shutdown | HTTP 202; test gateway/bridge and launcher stopped |
| Personal studio after test shutdown | Existing port 9200 still returned HTTP 200 |

Focused source checks passed: three relay tests using the actual embedded
Windows Python; seven model-install-script tests; three release-preparation
tests; five manual-coverage tests; and the isolated shutdown-port precedence
test. The preparation tests cover bit-exact offline assembly, repeat use,
corrupt-part rejection and preservation of an existing different image.

The clean build found and repaired missing AnyGPT tokenizer import dependencies
and shutdown cleanup that assumed the default gateway port. The
[main audit](2026-09-25-full-read-only-code-audit.md) records these and the other
packaging repairs as P-01 and P-04 through P-08.

## Wiki publication

The [wiki](https://github.com/aivrar/Portable_Omni_Server/wiki) contains 23 manual
pages, navigation and 18 reviewed screenshots. Home, Installation and ACE-Step
pages returned HTTP 200 after publication; the served hero image's SHA-256
matched the reviewed source. The manual exporter validates links and image
references before updating the separate wiki checkout.

## Image and delivery

- Compressed Linux image: **15,194,484,171 bytes** (about 14.2 GiB).
- Uncompressed exported tar stream: **27,931,811,840 bytes** (about 26.0 GiB).
- Complete image SHA-256:
  `3c64e0d45c831ae568836ca709bd53371b19b197cca13375198e1d14ecc559a1`.
- Eight numbered image parts; each is at most 1,900 MiB and below GitHub's
  per-asset limit. The Windows ZIP carries the launcher, source, Windows
  runtimes, preparation helper, manifest and license/provenance files.
- `Prepare-Omni.cmd` transfers and joins this preinstalled image. It does not
  build the AI dependencies at first launch. Allow at least 80 GiB free for
  preparation/import, plus room for selected weights and generated media.
- Optional Ubuntu corresponding-source archive: **1,638,120,190 bytes**,
  SHA-256 `4b7c555c9830ca9e49e73188327a81508ffb3c8941aa1f5115d95007712e5db3`.

`release-manifest.json` records the ordered image parts, sizes and SHA-256
hashes. `SHA256SUMS.txt` additionally covers the Windows ZIP, manifest and
Ubuntu source asset.

## Fresh-registration qualification

The Windows ZIP was extracted into a different folder containing a space.
The unmodified preparation helper verified all eight actual release parts,
assembled the full image and verified its complete SHA-256. Only the test
copy's app name/title and ports were changed to isolate it from the working
studio. The launcher then imported the image itself with no prior registration
of the test name; the test did not pre-register the disk through a separate
import command.

The resulting registration was **WSL2**. Its BasePath was the new application
folder, with `ext4.vhdx` beside the executable. This differs from the old
minimal-base bootstrap's `wsl/` subfolder; the manual and wiki were corrected.
First import, startup and browser checks took about **10.2 minutes** on this
host. Subsequent launches reuse the registered disk.

Checks after import passed:

- Native launcher, bundled Windows relay and gateway connected successfully.
- Setup used the retained real completion stamp and reported its fast path.
  The launch environment set `PIP_NO_INDEX=1` and `HF_HUB_OFFLINE=1`.
- All nine runtime directories and the self-contained Linux source were present.
- Session authentication worked; the fresh and personal studios had distinct
  tokens. Token values were not printed or written to the test report.
- Worker and Comfy registries were empty; Comfy was installed.
- Home, Model library and Runtime browser navigation passed without page errors.
  Visual review showed an idle studio with zero ready model families.
- Small and large native window icons were present.
- The Windows CLI passed `--help` and an authenticated `workers list` call after
  repairing its quoted-distro transport through a PowerShell entry point.
- Graceful shutdown returned HTTP 202. Test ports 9520/8520 closed and the
  native launcher exited. The personal studio still returned HTTP 200 on 9200.

The Windows CLI repair affects only the host entry points. The Linux image
remains built from the runtime revision recorded above; the Windows package
records its later source revision separately. P-09 and P-10 in the main audit
describe the two findings from this final restore pass.

## Published release and cleanup

[Version 1.0.0](https://github.com/aivrar/Portable_Omni_Server/releases/tag/v1.0.0)
was published as the normal latest release at **2026-09-26 01:36:44 UTC**
(September 25 in the maintainer's timezone), with 12 assets. It is neither a
draft nor a prerelease. The tag points to Windows/source revision
`4b9ef4540e09c840fe1e32b2277fdcc7d773d3bd`; the manifest separately identifies
the Linux image's runtime revision.

All 12 GitHub asset sizes and server-reported SHA-256 digests matched the local
release inventory. Anonymous HTTPS downloads through the preparation helper's
Windows .NET HTTP stack additionally verified the complete Windows ZIP,
manifest, checksum file and eighth image part, including their full hashes.

- Windows ZIP: **15,973,318 bytes**; SHA-256
  `37b0b14f137bea314ba5e41fc3a85762c8c76a54f56f5bd45c57f1602b959f9e`.
- The wiki's corrected disk-layout pages were pushed in revision `2d93501`.
- Both temporary build/restore registrations were unregistered only after
  checking their exact names and BasePaths against the owned temporary folders.
  Large temporary image copies were removed after public download verification.
  The verified Windows ZIP, small manifests and test evidence were retained.

## Qualification scope

Dependency installation and import success are distinct from model inference.
This packaging campaign loads no model weights and submits no generation jobs.
Existing engine-specific results and supported modalities remain documented in
[feature compatibility](../manual/21-feature-compatibility.md).

Some intentionally isolated upstream packages declare older or conflicting
dependency pins; runtime import checks passed, but this is not a claim that
every environment passes an unconstrained `pip check`. WSL2, WebView2,
hardware virtualization and the Windows NVIDIA driver remain host prerequisites.
Testing on this Windows host does not establish behavior on every account,
physical PC, GPU or driver combination.
