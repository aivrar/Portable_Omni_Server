# Omni Studio capability confidence

This matrix tracks whether an Omni capability is merely present or actually
safe for agents and users to operate. Update it when routes, model registries,
upstream packages, or live verification results change.

## Confidence gates

A capability is production-confident only when all applicable gates pass:

1. **Discoverable**: listed by setup/status or registry APIs.
2. **Contracted**: typed request/response behavior is known and documented.
3. **Regression-tested**: focused tests cover routing and failure behavior.
4. **Live-loaded**: the declared installed variant loads on a named device.
5. **Live-inferred**: at least one bounded request completes successfully.
6. **Output-verified**: format, duration/dimensions, persisted path, and media
   retrieval are verified.
7. **Cleanup-verified**: the exact worker/model unloads and device memory is
   rechecked.

Grades:

- **A**: all applicable gates pass for the named scope.
- **B**: dependable for tested paths, with an explicit untested or subjective
  gap.
- **C**: installed and contracted but not yet live-verified.
- **D**: blocked, incompatible, or unavailable; reason is explicit.

## Current matrix

| Capability | Current scope | Grade | Evidence and remaining gap |
|---|---|---:|---|
| Comfy lifecycle | start, stop, flags, core update, Manager update | B | Core update now requires exact-commit, clean-checkout, template, and optional live-runtime qualification; no upstream mutation was performed during this audit, so repeat a bounded live check after future upstream changes. |
| Comfy registry/assets | installed, Manager, HF/Xet search/install, exact deletion | B | Search and guarded-path tests pass; external catalogs and gated repositories remain time-dependent. |
| Comfy workflow analysis | saved/inline requirements and dependency resolution | A | Typed routes and focused requirement tests pass; analysis does not load weights. |
| Comfy GPU placement | single, auto, manual, UUID resolution, staged/offload-aware routing | B | Planner and multi-GPU tests pass. Real Krea 2 Turbo INT8 generation routed its 12.9 GB diffusion model to the 3090 and 5.0 GB text encoder to the 3060. Plans now use current free VRAM and permit only cgroup-bounded CPU deficits in explicit low/none VRAM modes. This remains component placement, not tensor/layer sharding. |
| Standalone Omni worker placement | single, auto, manual, UUID resolution, bounded HF device maps | B | Qwen 3B base completed a two-GPU live load and exact `OK.` inference across the 3090 and 3060, then cleaned up completely. GPTQ multi-GPU failed in Accelerate's meta-device dispatch and is now blocked before spawn; use its verified single-GPU path. The planner reports current VRAM, foreign compute PIDs, bounded CPU memory, and host/cgroup pressure. Other checkpoints remain qualification work. |
| Comfy workflow execution | queue, history, outputs, media playback | B | BiRefNet and Krea 2 Turbo INT8 completed through saved API workflows with persisted, visually checked outputs. Ideogram 4 staged loading advanced correctly but was aborted before sampling when host scheduling pressure crossed the safety boundary; that family remains unqualified. |
| Runtime memory isolation | gateway reserve, workload cap, scoped cache release | B | Kernel-level checks place Comfy/model workers in a 21 GiB child while the gateway remains under the 24 GiB parent with 3 GiB reserved. Empty-workload `memory.force_empty` reclaimed observed checkpoint cache without global cache dropping. Placement and reclaim now share a lock and check both task/process membership, closing the start-versus-release race. More worker families still need live stop/reclaim checks. |
| Gateway lifecycle and long calls | supervised restart, full app shutdown, synchronous inference proxying | A | Repeated gateway-only restarts recovered in about five seconds with an idle registry. API-only full shutdown completed one idempotent cleanup sweep and closed only the matching Windows host, then a relaunch returned connected. Restart intent has an explicit deadline, the bridge avoids killing its own process group, and its proxy window exceeds the 1800-second ACE-Step timeout. |
| Frontend backend recovery | session reacquisition and bounded retry | B | Source and focused tests cover bounded retries. On 2026-09-25, live Chromium recovered from a browser-only 401 by reacquiring the real session and recovered from a browser-only connection refusal. The disconnected/connected state changed correctly. This does not qualify an actual gateway restart or crash; see the screenshot/browser report. |
| Media artifact integrity | image/audio/video probing and provenance | B | Live bounded probing validated five recent Comfy PNGs, including dimensions and complete PNG termination. Invalid/truncated formats, missing expected media streams, bounded manifests, and Comfy embedded metadata are unit-covered. Subjective playback and broader damaged-video samples remain separate checks. |
| Audio worker lifecycle | ACE-Step, Audio Lab, MOSS and generic timeouts | B | State probes and autospawn-false checks leave no resident workers. Inference now reports busy and retires the exact worker on server timeout instead of returning it to ready; focused lifecycle contracts pass, but no heavyweight generation was queued for this audit. |
| MiniMax Music 3 | official Diffusers model with CPU offload and shared cgroup limit on RTX 3090 | B | A live 60-second vocal generation completed after the tokenizer fix. The Media API verified a 60.07-second, 44.1 kHz stereo PCM-24 WAV and manifest; unload and exact-worker deletion returned GPU memory to baseline. Instrumental, longer-duration, cancel, and listening-quality checks remain unverified. See [`reports/2026-08-13-minimax-music3-integration.md`](../reports/2026-08-13-minimax-music3-integration.md). |
| ACE-Step 2B turbo | vocal/instrumental generation through 120 s | A | Multiple genres, durations, persisted output, and cleanup verified. |
| ACE-Step 2B base | completion and advanced transformations | B | Live completion works, but nominally retained audio is regenerated. Use composition for preservation. |
| ACE-Step XL turbo | installed `ace-xl-turbo` | B | Live 15-second generation with the 1.7B LM passed at about 19 GB VRAM; cold four-shard page-in took about ten minutes and this seed's tempo adherence was weaker than 2B. |
| ACE LoRA packs | raspy, acoustic, Chinese New Year, lofi, and raga packs | A | Exact adapter selection, attach/detach recovery, output, and cleanup verified. The legacy Chinese Rap package is installed but not PEFT-compatible and is blocked. |
| ACE specialized adapters | Lyric2Vocal, Text2Samples | D | Official weights are unreleased; registry refuses false installs. |
| Stable Audio Open Small | generation and short effects up to 11 s | A | Official recipe, transient/ambience/mechanical comparisons, output metadata, media retrieval, and cleanup verified. |
| Stable Audio Open 1.0 | generation/effects up to 47 s, A2A, inpaint | A | Live generation, ranked fanout, A2A, inpaint, default/tuned VAE round-trip, unconditional generation, and music-CLAP scoring verified; inpaint is regenerate-plus-splice. |
| Stable Audio community models | SAO Instrumental, Audialab EDM, Nekochu Music, Infinite Pianos, Vocal Textures | A | Each installed, cold-loaded, generated a persisted short WAV through the native loader, and cleaned up. Their declared native ceiling is 120 seconds; subjective specialization still requires listening. |
| MOSS-SoundEffect v2.0 | text-to-SFX up to 30 s | A | Explicit 3090 load, 3 s/48 kHz WAV, discovery headers, metadata, media listing, and worker cleanup verified. Subjective event quality requires listening. |
| MOSS-TTS | local speech and ACE Vocal-to-BGM handoff | D | Earlier 48 kHz speech and handoff succeeded, but a fresh inference failed in the upstream decoder with a CUDA driver error and WSL `dxgresource` exhaustion. Omni retires the failed worker. Fresh generation is blocked until it passes again; persisted WAVs remain valid inputs. |
| Long-form audio composition | trim, gain, crossfade, optional loudness | A | API produced a verified 172 s, 48 kHz, 24-bit master and manifest. Musical transition quality remains subjective. |
| Qwen2.5-Omni 3B text | installed base on RTX 3060 | A | Explicit load, deterministic text inference (`QWEN3B_TEXT_OK`), exact worker deletion, and empty-worker cleanup verified. The worker occupied essentially all 12 GB VRAM. |
| Qwen2.5-Omni 3B image | base on RTX 3090 | A | Explicit 3090 load and real portrait input passed; the response correctly described person, pose, teal/blue jacket, and gray background in 18.1 seconds, followed by exact worker deletion. Avoid the prior 12 GB-card path. |
| Qwen2.5-Omni audio/video input | typed 3B/7B request fields | D | Current worker source does not wire `audio` or `video` and now returns an explicit 501 instead of silently ignoring them. Do not describe these modalities as supported until the official processor path is wired and tested. |
| Qwen2.5-Omni speech output | native 3B/7B Talker/TTS | D | The generic TTS router does not register Qwen2.5-Omni and currently returns 501. Official support exists upstream but is not wired through Omni. |
| Qwen2.5-Omni 7B GPTQ-int4 | text and image on RTX 3090 | A | Installer/runtime repaired and unit-covered. Four shards loaded with the Triton v2 GPTQ kernel at 12.7 GB VRAM; exact text and small-image inference passed; exact worker cleanup returned the 3090 to baseline. |
| Qwen2.5-Omni 7B base | installed fp16 checkpoint | C | Installed but not loaded in this campaign; prefer the verified GPTQ variant unless a full-precision comparison is necessary. |
| MiniCPM-o 2.6 text/image/audio input | BF16 on RTX 3090, 17.9 GB VRAM | A | Exact text response, accurate small-image description, and accurate 16 kHz speech transcription passed through the API. Cold loading under host CPU contention took about 8 minutes; warm loading took about 16 seconds. Video is explicitly rejected rather than ignored. |
| MiniCPM-o 2.6 speech output | existing `/api/tts/minicpm_o` handler | D | TTS reaches the model's audio decoder but remains blocked by upstream-version incompatibility: the decoder receives a Python list where it expects an object exposing `get_mask_sizes()`. Do not advertise this route as working yet. |
| Moshi 7B | installed moshiko BF16, about 16 GB VRAM | D | Omni's batch handler is a hard 501, Moshi is absent from the stream-handler registry, and the UI hides it because the current session API cannot carry full-duplex audio. Loading weights cannot make the present API functional. |
| AnyGPT 7B | installed chat variant, about 16 GB | D | Source exposes only text generation and ignores speech/music/image inputs. The fast-tokenizer protobuf failure was fixed with a scoped slow-tokenizer load, but a cold load then stayed CPU-side for over 13 minutes, reached about 16 GB RAM, outlived the bridge request, and had to be killed before ready. No inference capability is live-verified. |
| Qwen3-Omni 30B | installed instruct, about 60 GB VRAM | D | Standalone auto/manual sharding now exists, but the installed variants still exceed the safe combined 32–33 GB GPU budget. Live analysis returns an invalid plan and spawn hard-stops before process creation. Audio/video are explicitly 501; text/image remain untested because no fitting variant exists. |
| Nemotron Nano Omni | NVFP4, FP8, BF16 placement | C | Live no-weight analysis fits NVFP4 on the 3090 alone. FP8 is blocked GPU-only by about 1.08 GB but plans validly across both GPUs with an explicit 2 GB CPU spill under the 21 GiB workload child. Exact checkpoint load/inference is not yet qualified. |
| Nemotron Nano Omni 30B | installed BF16, about 62 GB VRAM | D | BF16 exceeds combined capacity. FP8 at roughly 33 GB is a borderline two-GPU candidate dependent on live headroom; NVFP4 at roughly 21 GB should prefer the 3090 alone. Neither quantized variant is live-verified here. Audio/video are explicitly 501 in the current shared handler. |

## Current focused regression evidence

The current confidence pass completed these model-free or bounded suites:

- Comfy placement: 18 passed.
- Comfy multi-GPU routing: 13 passed.
- Workflow requirements: 11 passed.
- Comfy extensions: 16 passed.
- Comfy discovery/search: 12 passed.
- Comfy startup options: 8 passed.
- MOSS-SFX output contract: 4 passed.
- MOSS-SFX GPU/runtime policy: 3 passed.
- Standalone Omni placement/router/worker integration: 27 passed.
- Workload cgroup isolation and scoped cache cleanup: 5 passed.

Run the full suite only in an isolated CI/test process whose stdout is
disposable. `test_comfy_recovery.py` previously closed an interactive command
host's stdout. Gateway imports now preserve the host's signal handlers, but
process-lifecycle tests still warrant a separate test host. Use the targeted
AGENTS.md suites for interactive checks.

## Rollout order for remaining libraries

1. Listen to and score the completed paired MOSS-SFX and Stable Audio
   transient/ambience/mechanical outputs in the Media tab; objective metadata
   and cleanup are complete.
2. Treat the fresh Comfy analyze-only HiDream placement check as the current
   model-free baseline; repeat it after planner or Comfy upstream changes.
3. Wire Qwen2.5-Omni audio/video inputs and native speech output to the official
   processor/model path before live modality tests. Text and image already
   have qualified 3090 paths; those results do not verify audio or speech.
4. Treat Qwen2.5-Omni 7B GPTQ-int4 text/image and MiniCPM-o 2.6
   text/image/audio understanding as verified 3090 baselines. MiniCPM TTS is
   separately blocked in its decoder, and video is explicitly unsupported.
5. Do not load Moshi until Omni has a real full-duplex audio transport; the
   current batch and stream registries cannot expose it.
6. Treat AnyGPT as blocked pending a bounded/asynchronous loader and explicit
   multimodal handlers. Its current worker source implements text only, and
   even that path did not reach ready within the bridge window.
7. Qwen3-Omni and Nemotron now have loader-level CUDA capacity hard stops.
   Install and test Nemotron NVFP4 if 30B coverage is required on the 3090;
   Qwen3 needs a fitting quantized/offloaded implementation before live use.
8. Requalify MOSS-TTS only after the decoder/CUDA descriptor failure is
   addressed. Check exact worker retirement and cleanup before another run.

Every live phase starts with `GET /api/devices` and `GET /api/workers`, uses
one explicit device, persists and probes one small output, deletes the exact
worker, and rechecks both APIs before moving to the next library.

## 2026-09-25 source repair qualification

The [repair ledger](../reports/2026-09-25-full-read-only-code-audit.md#repair-campaign---authorized-2026-09-25)
records source changes and isolated regressions after the static audits. Those
checks use fake workers, temporary files, and small local subprocesses. They
do not upgrade the historical live-model grades above or qualify the current
installed distro. Runtime deployment, model quality, GPU memory behavior, and
Windows/WSL lifecycle behavior still require their separate bounded checks.

## 2026-09-25 documentation browser qualification

The [screenshot and browser report](../reports/2026-09-25-screenshots-and-browser-checks.md)
adds real Windows-to-WSL bridge/UI evidence after a source-only runtime refresh:
14 destination renders, 16 distinct bounded checks, and six newly corrected
source/documentation findings. No model weights or Comfy instances were loaded.
The historical model grades above remain unchanged. Complete packaged setup,
actual gateway restart recovery and heavyweight inference are still separate
checks; the report distinguishes the initial CRLF launch failure from the
subsequent successful source-refreshed gateway start.
