# Omni Studio local-generation capability test plan

Date started: 2026-08-09

## Objective

Build a repeatable, API-driven validation program for Omni Studio's local
ComfyUI image/video workflows and its local audio tools. Determine usable and
optimal presets, identify model/custom-node/source bottlenecks, fix defects
owned by Omni Studio, and preserve the operating knowledge as app-local skills
and human documentation.

This program covers:

- MiniMax H3 T2VA, FL2VA, REF2VA, reference-strength control, ClipProj, and
  optional Spectrum acceleration
- Krea 2 OSS Raw and Turbo
- Ideogram 4.0 local inference
- HiDream-O1-Image
- BiRefNet background removal and alpha matting
- Wan Animate 2
- image animation and first/last-frame animation
- local TTS-driven speech animation and lip sync
- ACE-Step local singing/music generation followed by speech/singing animation

## Scope boundary

- Use Omni APIs for discovery, lifecycle, registry search, installation,
  workflow analysis, execution, upload, queue/history, media, and verification
  wherever the app exposes the capability.
- Add an API only when a broadly reusable automation primitive is genuinely
  absent. Extend existing routes when possible. Do not create one-off endpoints
  for a particular model or test case.
- Fix Omni-owned API, planner, routing, media, lifecycle, or UI defects.
- Inspect ComfyUI and custom-node source read-only. Record upstream defects,
  resource traps, and candidate changes; do not patch upstream source during
  this program.
- Keep every Comfy-specific model under
  `/opt/omni_studio/comfyui/models/<category>`.
- Keep tests small and sequential. Analyze before every first execution. A
  `valid: false` placement plan or `ready_to_run: false` dependency report is a
  hard stop.

## Initial machine and runtime inventory

- Primary performance GPU: RTX 3090, physical `cuda:1`, UUID
  `GPU-36feccef-50ef-2eaf-5c0c-5448e28a4d8a`, 24 GiB VRAM.
- Auxiliary GPU: RTX 3060, physical `cuda:0`, UUID
  `GPU-80c24f57-c604-b55c-5433-5d248304eec9`, 12 GiB VRAM.
- Host RAM reported by Comfy startup: approximately 32 GiB.
- Free Comfy-model filesystem space at inventory time: approximately 482.8
  GiB.
- Installed Comfy model inventory initially contains only four MiniMax H3 files
  totaling about 39.6 GiB: one diffusion model, one 32B text encoder, video
  VAE, and audio VAE.
- Local audio capabilities already installed include MOSS-TTS, Stable Audio
  Open, ACE-Step v1.5 Core, ACE-Step XL Turbo, and ACE 0.6B/1.7B language
  models.
- Comfy is started for discovery with the 3090 primary and the 3060 auxiliary.
  Starting Comfy does not load model weights.

## Preliminary API findings

1. `GET /api/workflows/search` scans approximately 600 template files. The
   first search exceeded a 15-second caller timeout; subsequent searches took
   about two seconds. The route synchronously enumerates distributions and
   reads/parses every candidate inside an async handler. This is an Omni-owned
   latency/event-loop risk. Optimize the existing route with a safely
   invalidated template/search index rather than adding a duplicate endpoint.
2. Most installed official templates are UI-format and correctly report
   `requires_api_export: true`. Full API automation needs a dependable way to
   materialize an API-format prompt from these templates. Audit Comfy's current
   frontend/backend capabilities before deciding whether a general conversion
   service is feasible; do not implement a model-specific converter.
3. Template analysis already returns exact Xet-backed model install actions.
   Use those actions rather than reconstructing filenames or directories.

## Reusable test assets

Create and retain a compact, rights-safe test pack beneath the app's normal
input/media storage. Every asset gets a stable descriptive name and provenance
note.

### Images

1. `portrait-neutral`: centered waist-up adult, visible lips, hands, hair, and
   textured clothing; neutral background. Used for identity, lip sync, and
   alpha-matting tests.
2. `portrait-profile`: three-quarter/profile face with loose hair. Used for
   difficult matting and facial-motion tests.
3. `fullbody-neutral`: complete subject and limbs with clear silhouette. Used
   for Wan Animate driving tests.
4. `object-complex-edge`: translucent/fine-edged object, such as glass plus
   plant leaves. Used for BiRefNet boundary testing.
5. `first-frame` and `last-frame`: same character/scene with a deliberate but
   plausible pose and camera change. Used for FLF/FL2VA adherence.
6. `style-reference`: clearly recognizable palette, medium, and lighting but no
   protected character. Used for Krea style-reference tests.
7. `typography-layout`: reference layout containing several text regions. Used
   for Ideogram typography and editing tests.

Generate candidates locally when practical. Store the chosen originals without
lossy recompression and derive resized inputs from them so comparisons use the
same source pixels.

### Audio

1. MOSS-TTS neutral phrase, 8-12 seconds, containing plosives, fricatives,
   long vowels, numbers, and one proper name.
2. MOSS-TTS expressive phrase with a question, pause, and emotional change.
3. ACE-Step short solo-vocal/singing passage with clear syllables and minimal
   backing for lip-sync evaluation.
4. ACE-Step full-mix passage to test whether lip-sync systems remain usable
   when vocals are not isolated.
5. A silence/control clip with the same duration and sample rate.

Normalize archival WAV inputs without clipping. Record sample rate, channels,
duration, peak, integrated loudness, and whether speech or lyrics were
generated from fixed text.

### Driving video

Create one short, low-resolution driving clip with head turns, blinking, arm
movement, and visible hands. Create a second faster-motion clip only after the
basic Wan Animate path succeeds.

## Test method shared by every capability

### Phase A: dependency and API readiness

1. Find the exact official/local template through the workflow-search API.
2. Fetch the exact live template and provenance.
3. Analyze without loading weights.
4. Classify it as local inference, paid/API-key use, UI-only, or API-runnable.
5. Record exact node classes and model files, repositories, sizes, licenses,
   gates, and Xet install actions.
6. Confirm selected models fit disk and expected GPU/RAM envelopes.
7. Install dependencies through existing asset/registry APIs, poll the job to
   terminal state, verify exact target paths, restart Comfy only when nodes
   require it, and re-analyze.
8. Never infer success merely from a queued download or repository directory.

### Phase B: smoke validation

Run the smallest representative output that remains within the model's intended
operating range. Confirm:

- queue acceptance and terminal success
- expected image/video/audio file and media-library discovery
- valid dimensions, duration, frame rate, codec, audio stream, and alpha channel
- no NaN/black-frame/silent-output condition
- model unload after the workflow or an explicit Comfy `free` operation
- GPU and host RAM return close to pre-run baseline

### Phase C: controlled quality search

Change one family of variables at a time. Use the same input, prompt, and seed
where the implementation supports determinism:

1. recommended/default preset
2. faster/lower-memory preset
3. higher-quality preset
4. one boundary preset expected to approach the safe resource limit

Do not run a full Cartesian product. Promote only settings that show a material
benefit in contact-sheet/frame/audio comparison.

### Phase D: resource and failure probes

Record separately:

- cold load, prompt encoding, sampling, decode, and save time
- peak and post-unload VRAM per GPU
- peak and post-unload host RAM/commit
- model file I/O or repeated reload behavior
- queue responsiveness during loading
- cancellation behavior and cleanup
- OOM/allocator/pinned-memory behavior
- malformed input, missing dependency, and invalid-placement responses

Do not intentionally reproduce the previously observed pinned-memory/low-VRAM
failure loop. Stop at the last safe setting and infer the boundary.

### Phase E: acceptance and optimal-preset selection

For each capability, retain:

- one `safe` preset for dependable use
- one `balanced` preset for normal quality
- one `quality` preset when the gain is meaningful
- explicitly unsupported or unsafe settings

An optimal preset must complete repeatedly, clean up memory, preserve required
conditioning, and improve a visible/audible metric enough to justify its time
and memory cost.

## Capability-specific matrices

### MiniMax H3

Continue the existing H3 report rather than repeating completed tests.

1. T2VA regression: rerun only one known-safe preset after integrations change.
2. FL2VA: fixed seed and first/last frames; test visual conditioning noise
   strengths `0.999`, `0.9`, `0.7`, and only if stable `0.5`.
3. REF2VA with reference audio: compare fixed reference audio with two seeds;
   preserve audio noise strength at `1.0`.
4. Generated-audio FL2VA: hold seed fixed and vary only visual reference
   strength; compare motion and the first seconds of audio.
5. ClipProj: same prompt/seed with native 32B, projected 4B, and projected 8B
   where reference-image testing warrants it. Measure text-stage load time,
   VRAM, proper names, speech text, non-English behavior, and identity.
6. Spectrum: only v0.2.5 or newer, CPU history/archive, exact-seed native A/B.
   Stop if motion/anatomy/audio changes outweigh the sampling-time reduction.

### Krea 2 OSS Raw

1. Establish the exact official checkpoint and template; do not confuse it
   with Flux.1 Krea Dev, Krea's hosted API, or Krea 2 Turbo.
2. Test square, portrait, and landscape aspect ratios around the template's
   recommended megapixel range.
3. Compare documented sampler/scheduler/steps only; then test one higher-step
   case to determine whether Raw benefits materially.
4. Evaluate photographic texture, skin, typography, prompt adherence, hands,
   and repetitive-detail artifacts.
5. Test prompt refinement off/on separately so model quality is not confused
   with Qwen prompt rewriting.

### Krea 2 OSS Turbo

1. Begin with the official int8-convrot workflow and its recommended low-step
   schedule.
2. Test native model precision variants only if the official repository offers
   them and disk/VRAM cost is justified.
3. Compare low-step speed against Raw at matching dimensions and prompt/seed.
4. Test the official style-reference workflow with the fixed style asset.
5. Test optional LoRA disabled first; then test one declared official LoRA so
   its effect and memory cost are isolated.

### Ideogram 4.0

1. Use the local `Ideogram4Scheduler` workflow, not `IdeogramV4` hosted API
   nodes, unless the user later asks to test paid APIs.
2. Test text rendering with short title, multi-line poster, punctuation,
   numerals, and one deliberately difficult proper name.
3. Test square, portrait poster, and landscape/banner ratios.
4. Compare default scheduler/steps with one fast and one quality setting.
5. Score exact text correctness, layout, cropping, prompt adherence, hands,
   and background detail.

### HiDream-O1-Image

1. Distinguish O1 release/dev checkpoints and the intended sampler for each.
2. Test text-to-image first, then single-reference editing, then multi-reference
   composition if supported by the installed native node.
3. Test reference preservation versus edit strength using the same source and
   prompt.
4. Test approximately 1 MP square, portrait, and landscape outputs before any
   higher-resolution boundary.
5. Record checkpoint-loader overlap, reference-image token/memory scaling, and
   sampler differences.

### BiRefNet

1. Test portrait-neutral, profile/hair, full-body, and complex translucent/fine
   edges.
2. Verify foreground RGB, mask polarity, alpha composition, and transparent PNG
   output separately.
3. Compare at least one fast/general model with the template default when the
   node exposes variants.
4. Test original resolution and one downscaled/upscaled path; inspect halos,
   missing hair, edge color contamination, holes, and shadow handling.
5. Measure CPU versus GPU execution if the node permits both and confirm model
   files remain in `background_removal`.

### Wan Animate 2

1. Confirm the exact model revision, model files, LoRAs, pose/detection helpers,
   and required native nodes from the official template.
2. Smoke-test one short clip using fullbody-neutral plus the gentle driving
   video.
3. Compare recommended resolution with one smaller/faster setting; length is
   increased only after the recommended resolution succeeds.
4. Test identity, face, hands, limb topology, clothing, background stability,
   motion amplitude, and driving-video adherence.
5. Test crop/resize and subject-mask behavior explicitly. Inspect all frames for
   boundary flicker and background leakage.
6. Route complete components across GPUs only when the placement analysis is
   valid. Do not describe this as layer sharding.

### Image animation and first/last-frame video

1. Test at least one dependable local image-to-video path using a shared source
   image and prompt.
2. Test LTX 2.3 first/last-frame workflow if its dependencies fit the available
   disk/VRAM budget; compare it with MiniMax FL2VA when both are operational.
3. Evaluate first-frame and last-frame adherence, transition plausibility,
   camera motion, identity drift, temporal flicker, generated audio, duration,
   and decode quality.
4. Change duration and resolution separately. Do not increase both in one
   boundary test.

### Local TTS, speech animation, and lip sync

1. Generate the neutral and expressive phrases through `/api/tts/{model}` with
   MOSS-TTS. Test WAV first, then media-library playback.
2. Record cold/warm load, voice controls, speed, sample rate, clipping, silence,
   pronunciation, and whether worker cleanup occurs.
3. Prefer a local speech-to-video/lip-sync workflow. Hosted Sync/ElevenLabs
   nodes are discovery references, not the target execution path.
4. Feed the same TTS clip and portrait into each viable local path. Evaluate
   phoneme timing, lip closure on plosives, teeth/tongue artifacts, head motion,
   identity, duration preservation, and A/V mux/playback.
5. If MiniMax REF2VA can preserve supplied audio while animating the portrait,
   include it as a local comparison even if it is not a dedicated lip-sync
   model.

### ACE-Step singing and singing animation

1. Test the installed 2B core/turbo path first, then XL Turbo only if it adds a
   meaningful quality gain and fits safely.
2. Generate one short solo-vocal phrase from fixed lyrics and one full mix.
3. Record model/LM variant, seed, steps, duration, language, BPM/key controls,
   cold/warm time, VRAM, output loudness, intelligibility, and lyric adherence.
4. Use the solo-vocal result for the primary singing lip-sync test. Use the full
   mix as a robustness test.
5. Do not claim a lip-sync model supports singing merely because it accepts the
   file; inspect consonant closures, sustained vowels, beat-related jitter, and
   drift across the clip.

## API coverage audit

Audit these reusable capabilities before adding routes:

1. Template search/fetch, efficient indexing, and exact provenance.
2. UI-template to API-prompt materialization, including embedded subgraphs and
   frontend-only widgets.
3. Exact dependency/size/license/gate reporting before download.
4. Model/custom-node install job polling and cancellation.
5. Comfy input upload for image, mask, video, and audio.
6. Queue/history/progress/error/output inspection without using the browser.
7. Explicit unload/free and post-run memory verification.
8. Media metadata and content playback for PNG/JPEG/WAV/MP3/MP4/WebM.
9. Saved workflow parameters and placement-policy persistence.
10. TTS/ACE job execution and terminal-status/output retrieval.

Prefer fixing or extending existing analyze, run, metadata, lifecycle,
registry, asset, proxy, audio, ACE, and media routes. Every fundamental API
change requires typed request/response behavior, authentication, bounds,
documentation, and targeted tests.

## Source-probe checklist

For each model/custom node, record without patching upstream:

- hard-coded devices or unconditional `.cuda()` calls
- resident model globals/caches and missing unload paths
- clone/copy/history retention proportional to frames or steps
- synchronous disk/network work on an async server path
- pinned-memory and mmap behavior under WSL
- duplicate model loads across nodes or stages
- precision casts that expand quantized weights
- CPU intermediates retained across decode stages
- unbounded image/video/audio batches
- temporary-file cleanup and media-container edge cases
- seed handling and determinism gaps
- defaults that differ from official templates/releases
- exception paths that leave models, workers, or queue state resident

Classify every finding as `Omni-owned`, `Comfy-core`, `custom-node`, `model`,
`environment`, or `expected tradeoff`, with exact file/function evidence.

## Documentation and skill deliverables

1. Update `omni-comfy-api` with any verified general API behavior and new
   fundamental routes.
2. Create a concise app-local capability-testing skill that routes agents to:
   dependency preparation, input assets, image tests, video tests, audio/lip
   sync tests, resource measurement, and report format.
3. Bundle deterministic scripts only for repeated API inventory, job polling,
   media inspection, and result-table generation. Keep tokens out of logs and
   artifacts.
4. Maintain this plan as the execution ledger and write per-family findings to
   focused report sections/files rather than inflating the skill with results.
5. Validate skills with the official quick validator and repository tests.

## Execution ledger

- [x] Read required Omni Comfy API and skill-authoring instructions.
- [x] Confirm gateway health and current devices through the API.
- [x] Confirm Comfy model storage is internal to its distro tree.
- [x] Record the initial four-file H3-only Comfy model inventory.
- [x] Confirm local MOSS-TTS, Audio Lab, and ACE-Step installations.
- [x] Discover templates for Krea, Ideogram, HiDream, BiRefNet, Wan Animate,
  first/last-frame video, and lip sync.
- [x] Finish exact dependency/size/license inventory for every chosen local
  workflow.
- [x] Resolve UI-template API materialization.
- [x] Create reusable test assets.
- [x] Install and verify dependencies.
- [x] Run capability matrices and record results.
- [x] Fix Omni-owned defects and verify regressions.
- [x] Finish skills, API guide, and final preset summary.

LTX 2.3 is an explicit bounded deferral, not an incomplete first/last-frame
test. Its UI-only blueprint has no materialized API graph, and its missing
29.53 GB checkpoint plus 9.45 GB encoder exceed this guest's total RAM before
runtime overhead. Staged MiniMax H3 qualified first/last-frame animation on the
target hardware without adding that redundant large-model stress test.
