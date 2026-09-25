# Omni Studio Local Music Capability Findings

Date started: 2026-08-10

Companion plan: `reports/audio-music-capability-test-plan-2026-08-10.md`

## Test conditions

- API base: local Omni gateway.
- Test GPU: NVIDIA GeForce RTX 3060 12 GB (`cuda:0`).
- The RTX 3090 remained available to the existing Comfy instance.
- The user reported a separate CPU-heavy process during the run. Wall-clock
  timings below are operational observations, not clean performance benchmarks.
- Tests were sequential and API-driven. Long requests were detached and polled
  with short checks so they did not block the working session.
- All prompts and lyrics were written specifically for this qualification.

## ACE-Step 1.5 baseline

Configuration:

- Model: `ace-1.5`
- LM: `ace-lm-0.6b`
- Device: `cuda:0`
- BF16, no CPU offload, no int8, no torch compile
- Eight Euler steps, CFG 1, overlapped decode

Cold load completed successfully but took 357.99 seconds under the current
system load. The worker log shows that much of the time was checkpoint/LM
initialization rather than GPU inference.

| Test | Job | Duration | Generate time | Format |
| --- | --- | ---: | ---: | --- |
| Minimal techno instrumental | `51257e14ff4c491f82e147222aa43244` | 30 s | 49.951 s | PCM16 stereo, 48 kHz |
| Synthpop vocal | `0bb251ff7900491e950cd25f5a70d356` | 30 s | 46.519 s | PCM16 stereo, 48 kHz |
| Psychedelic downtempo instrumental | `3852822d5f1d4a7e87afd638a4fe9016` | 30 s | 41.388 s | PCM16 stereo, 48 kHz |
| Dark ambient instrumental | `4355de7674e347c68d941255024f218a` | 30 s | 48.446 s | PCM16 stereo, 48 kHz |
| Hard-techno chant | `41a68b4c8ebd41e08827ae38b180b740` | 30 s | 42.354 s | PCM16 stereo, 48 kHz |
| Psychedelic vocal | `fdd458f758a74d478502b71c56565617` | 30 s | 47.684 s | PCM16 stereo, 48 kHz |

All six API jobs completed, persisted manifests and WAV files, and exposed
output URLs. Sampled files passed `ffprobe` container checks. Subjective musical
and lyric-quality scoring is still pending.

ACE unload completed in about four seconds. Deleting the worker returned the
3060 to approximately its starting free VRAM with no Omni worker registered.

### ACE optional-toolkit observations

The worker currently reports:

- LyCORIS is not installed, so LoKr training/inference is unavailable.
- Lightning and Lightning Fabric are not installed, so the optional training
  toolkit is unavailable or reduced to the upstream basic loop.
- These warnings did not prevent standard inference. Standard LoRA inference
  has not yet been qualified.

## Stable Audio Open Small

Open Small loaded successfully with the default CLAP model. Its worker reported:

- Native format
- RF denoiser objective
- 44.1 kHz
- 11-second maximum
- Valid samplers: Euler, RK4, DPM++, and Pingpong

The first official-recipe request exposed an Omni schema error: upstream uses
eight steps, but both the gateway and worker required at least ten. After the
schema fix, the unchanged request succeeded:

| Test | Job | Duration | Wall time | Format |
| --- | --- | ---: | ---: | --- |
| Minimal-techno loop, 8 steps, CFG 1, Pingpong | `b82290e8d8a44c10abec946713654f6b` | 10.9598 s | 5.872 s | PCM16 stereo, 44.1 kHz |

## Stable Audio Open 1.0

The official diffusers model loaded successfully with:

- V-prediction objective
- 44.1 kHz
- 47-second maximum
- DPM++/K sampler family
- Default general CLAP scorer

| Test | Job | Duration | Wall time | Result |
| --- | --- | ---: | ---: | --- |
| Minimal-techno baseline, 100 steps | `6545aad24609439eaf642937f2c5f2ce` | 30 s | 38.659 s | Passed |
| Maximum-length psychedelic texture | `4798af78d9bf478587f820dcbaa321bb` | 47 s | 38.670 s | Passed |
| Acid/minimal audio-to-audio, noise 0.35 | `6cecbe988c154510a63589947a198e31` | 30 s | 40.127 s | Passed after dtype fix |
| Breakdown inpaint, seconds 12–18 | `a6cf8455298741029a67b87a88519c57` | 30 s | 38.770 s | Passed after dtype fix; splice method |
| Default VAE reconstruction | `b7293ffe0a9a4872be6c3adb5cec002b` | 29.9537 s | 2.991 s | Passed after dtype fix; diff RMS 0.05582 |
| Four-candidate CLAP-ranked loop | `ea1350a57dd2485aa0ad2d741d932740` | 4 × 10 s | 85.888 s | Passed after sequential-ranking fix |

The 48-second negative test was rejected in 0.80 seconds with the precise model
limit: `duration_s 48s exceeds the loaded model limit of 47s`.

Ranked candidate seeds were 124404–124407. CLAP scores were 0.42946, 0.42317,
0.39603, and 0.38403. The best persisted output is:

`/api/audio_lab/outputs/ea1350a57dd2485aa0ad2d741d932740/best_01_0.4295.wav`

## Omni defects fixed during qualification

### 1. Official Open Small step count rejected

Symptom:

`POST /api/audio_lab/generate` rejected the official eight-step Open Small
recipe because Pydantic schemas required `steps >= 10`.

Fix:

- Changed the Audio Lab generate/unconditional minimum to one step in the
  gateway and worker schemas.
- Eight steps now passes; zero still fails validation.

Files:

- `server/routers/audio_lab.py`
- `server/omni_worker.py`

### 2. Diffusers A2A and inpaint fp16 mismatch

Symptom:

`Input type (float) and bias type (c10::Half) should be the same`

Cause:

Decoded WAV input was float32 while Stable Audio Open 1.0's diffusers VAE was
loaded in float16. `StableAudioPipeline` does not cast
`initial_audio_waveforms` before its first VAE convolution.

Fix:

- Added component dtype discovery and explicit input casting.
- Applied it to diffusers A2A and the diffusers inpaint fallback.

File: `server/audio_lab_loaders.py`

### 3. VAE-only fp16 mismatch

Symptom:

The same float32/fp16 error occurred in VAE encode/reconstruct.

Fix:

- Applied component-aware casting to VAE encode, decode, and reconstruct.
- The unchanged reconstruction request now succeeds.

File: `server/audio_lab_loaders.py`

### 4. Ranked batch exhausts a 12 GB GPU

Symptom:

Four-waveform generation completed all diffusion steps but failed during decode
with `CUDA driver error: device not ready`. Only 169 MB VRAM remained.

Cause:

Despite the function's sequential-fan-out documentation, the implementation
requested one batch with `num_waveforms_per_prompt=4`, multiplying peak VAE
decode memory.

Fix:

- Generate one candidate at a time.
- Use deterministic incrementing seeds when a base seed is supplied.
- Preserve cancellation checks, partial candidates, failure records, and serial
  CLAP scoring.

Verification:

- The unchanged four-candidate request completed with four outputs and no
  failures.
- The loaded worker retained about 4.1 GB free VRAM afterward.

File: `server/routers/audio_lab.py`

## Other observations

- Audio Lab autoloads the default CLAP scorer when starting. This makes ranking
  immediately available but adds startup time and memory even for unranked jobs.
- Flash Attention is not installed in the shared runtime. Native Stable Audio
  correctly falls back to the standard attention path.
- TorchSDE emits small floating-boundary warnings around sigma 0.3/500 during
  DPM++ 3M SDE. Generation still completes; no source change was made.
- Diffusers inpaint is a full regeneration followed by a 40 ms splice at mask
  boundaries, not true latent-mask inpainting. Boundary quality requires
  listening tests.
- VAE reconstruction shortened a 30-second source by about 46 ms. Timeline
  composition must use the returned duration rather than assuming sample-exact
  preservation.

## Current runtime state

At the end of this recorded block, Audio Lab was explicitly unloaded and its
worker removed before moving to ACE LoRA installation/testing.

## ACE-Step LoRA qualification

### Upstream mode adapters are not downloadable

The registry previously presented `lyric2vocal` and `text2samples` as
installable official LoRAs. ACE-Step documents these modes, but has not
released either adapter. Both entries now report `available: false`, a null
repository, and a specific upstream-unreleased reason. Install attempts fail
fast with HTTP 409 instead of starting a doomed Hugging Face job.

Files: `server/config.py`, `server/routers/setup.py`,
`server/ace_step_loaders.py`, `server/install_model.sh`

### Community adapter compatibility differs by package

- `daydreamlive/synthpop` downloads a PEFT-style safetensors file but provides
  no `adapter_config.json` and no recoverable LoRA alpha metadata. Native ACE
  correctly refused to guess the missing configuration. The registry now marks
  this package unavailable with that reason, preventing repeat installs. The
  incompatible copy installed during this test was deleted through the API;
  that deletion is not recoverable locally (the upstream repository remains).
- `DisturbingTheField/ACE-Step-v1.5-raspy-vocal-and-instrumental-5-LoRAs`
  installed and attached successfully through the API at multiplier 0.5.
- A controlled 30-second raspy-vocal render completed successfully as job
  `c0ad6209ffb34494acc1b3eb044cfdc0`: 48 kHz stereo, seed 104091, 8 Euler
  steps, CFG 1, 49.163 seconds inference time.

This establishes that the standard PEFT LoRA path works and that the synthpop
failure is repository-specific rather than a general attachment defect.

### Final LoRA detach left ACE in a broken PEFT state

Symptom:

- Detach returned an empty Omni LoRA list.
- The next base-model generation failed with `KeyError('raspy-vocal-pack')`.

Cause:

ACE-Step 1.5 deleted the final PEFT adapter, then attempted
`decoder.get_base_model()`. The installed PEFT version consulted the stale
active-adapter name during that call and raised after deletion. This left an
empty PEFT wrapper active and retained an approximately 3 GB CPU decoder
backup.

Fix:

- When this precise half-completed final-detach state occurs, Omni now reaches
  PEFT's underlying base module directly, restores ACE's saved base weights,
  clears the upstream LoRA registries and active name, and releases the CPU
  backup.
- A failed removal that cannot be recovered now propagates as an API error
  instead of silently deleting Omni's stack entry.
- Added focused regression coverage for both the empty-wrapper success response
  and the observed partial-failure response.

Verification:

- Live attach at 0.5 and detach triggered the known upstream error and Omni's
  recovery path.
- A same-prompt, same-seed base-model control then completed successfully as
  job `bb23768656ca4a41823c50b6b9c15355` in 26.059 seconds.

Files: `server/ace_step_loaders.py`, `tests/test_ace_step_staging.py`

### Multi-adapter packs were only partially addressable

The installed raspy pack contains five safetensors adapters sharing one
`adapter_config.json`. Its author instructs users to rename the selected file
to `adapter_model.safetensors`, but Omni's attach request previously exposed
only the repository's default merged file.

Fix:

- Extended the existing `/api/ace_step/lora/attach` request with optional
  `adapter_file`; no new endpoint was added.
- The filename is restricted to a safetensors basename, checked against the
  registry's declared pack variants, and verified inside the installed LoRA
  directory.
- Omni builds a small symlinked PEFT view beneath that LoRA's distro directory,
  avoiding duplicate weight files.
- `/api/ace_step/status` now exposes each selectable file, description, default,
  and the upstream author's recommended 0.2–0.7 multiplier range.
- `/api/ace_step/lora/list` reports the exact active `adapter_file`.

Verification:

- The pure instrumental adapter attached at multiplier 0.4 through the live API.
- A 30-second dark psychedelic instrumental completed as job
  `996558ed350f4c7ebfdf9627c24cdf51` in 28.473 seconds.
- Final detach reduced worker RSS from 5,672,428 kB to 2,617,364 kB, confirming
  the approximately 3 GB CPU decoder backup was released. The base ACE model
  intentionally remained loaded on the GPU for subsequent work.

Files: `server/config.py`, `server/routers/setup.py`,
`server/routers/ace_step.py`, `server/omni_worker.py`,
`server/ace_step_loaders.py`, `tests/test_ace_step_staging.py`

## ACE-Step duration gates

- 60-second minimal-techno instrumental: job
  `15428836245947579cf6f6df43aa9ae1`, 38.376 seconds inference.
- 120-second psychedelic minimal-techno instrumental: job
  `4c3e2734a83143779904c0c6cb53949c`, 63.785 seconds inference.

Both completed at 48 kHz stereo with the 2B turbo bundle, 8 Euler steps,
CFG 1, BF16, overlapped decode, and no CPU offload. These establish functional
duration support through two minutes. They do not yet prove musical coherence
or non-repetition; those require listening, and current wall-clock figures are
not clean benchmarks because of unrelated host CPU load.

## Long-form output composition API

Gap:

The outputs API could list and stream ACE, Stable Audio, and TTS artifacts but
could not assemble them. Long music therefore required one very large model
generation or manual work outside Omni.

Implementation:

- Added the fundamental background-job route
  `POST /api/outputs/audio/compose` under the existing outputs API.
- Inputs are exact paths returned by `GET /api/outputs`; every path is resolved
  through the configured output root and must be an audio file.
- Per-segment trim, gain, and label are supported along with common resampling,
  sequential equal-power-style triangular crossfades, optional one-pass EBU
  loudness normalization, 16/24-bit WAV output, and a 1–4 thread limit
  (default 2).
- Output and a provenance manifest are written under
  `OUTPUT_DIR/omni/compositions/<composition_id>/`, so the media library and
  range-aware outputs API see the master immediately.
- Composition runs through the unified cancellable jobs API. The initial live
  request revealed that `audio_compose` was missing from the job-kind allowlist;
  it is now registered and regression-tested.

Verification:

- Job `feb93dcc` composed the 60-second and 120-second techno gates with an
  8-second crossfade into exactly 172.0 seconds.
- Output is 48 kHz, 24-bit PCM WAV, 49,536,102 bytes, and completed in 1.3
  seconds with two CPU threads.
- The finished master is discoverable through `GET /api/outputs?kind=omni`
  at `compositions/1dd6caf8d5994ccca209f3c03f26ca08/`.

Files: `server/routers/outputs.py`, `server/jobs.py`,
`tests/test_audio_compose.py`

## ACE-Step transformation and editing modes

- Cover: psychedelic vocal to acoustic dream-folk, job
  `8fe1c2e727ba48c9852bdba23a0425ed`, 30 seconds, 4.956 seconds inference.
- Repaint: seconds 10–18 of that cover, job
  `89f8fda9420545b7b51db4a4076f74da`, 30 seconds, 4.055 seconds inference.
  Normalized source/output difference was about 0.109 before and after the
  mask versus 1.526 inside it, supporting true localized regeneration rather
  than an unrelated full replacement.
- A2A: acoustic cover to dub-techno at `init_noise_level: 0.45`, job
  `64c420ba6180470fa63f4bf950eec075`, 30 seconds, 3.875 seconds inference.
- Lyric-only flow edit: job `e2825c1743be45d18f725e23fda1fa66`,
  30 seconds, 5.732 seconds inference.

### Lyric edit contract omitted required source conditioning

ACE's flow edit builds separate original and target conditioning branches from
`flow_edit_source_caption` and `flow_edit_source_lyrics`. Omni's internal
loader already consumed `source_prompt` and `source_lyrics`, but its gateway
and worker request models omitted them, so API clients could not supply the
original branch.

Both fields are now part of the existing `/api/ace_step/edit` typed request and
were used in the successful live lyric-only edit above.

Files: `server/routers/ace_step.py`, `server/omni_worker.py`

## ACE base-model completion

- Installed `ace-base` through the API; download completed in 61.5 seconds.
- Loaded BF16 on `cuda:0` with the 0.6B LM. The base model reports upstream
  tasks `extract`, `lego`, and `complete` in addition to generation/cover/repaint;
  its defaults are 50 steps and CFG 4.
- Appending 15 seconds to a 30-second instrumental completed as job
  `1c461887a37f439ca67b3cdf0957b199` in 43.271 seconds and returned exactly
  45 seconds.
- The original 29.5-second region was not sample-preserved
  (`normalized_diff: 1.547`). Treat ACE completion as regenerative musical
  continuation. Use the output-composition API when the source audio must
  remain bit-for-bit or perceptually unchanged.

## MOSS-TTS and cross-tool audio handoff

### Custom processor leaked a tokenizer-only argument into the codec

Initial MOSS load failed with:

`MossAudioTokenizerModel.__init__() got an unexpected keyword argument 'fix_mistral_regex'`

Omni had injected Transformers' Mistral-tokenizer repair flag at
`AutoProcessor`; MOSS forwards processor kwargs to both its text tokenizer and
audio-tokenizer `AutoModel`. The fix now applies the flag only around
`AutoTokenizer.from_pretrained` and explicitly strips it from processor kwargs.
The tokenizer warning disappeared and the codec constructor loaded.

### Placement and cold-load behavior

- Native TTS auto-spawn used the global default `cuda:0`; the shared worker
  manager does not size models automatically.
- MOSS is documented by Omni as a 12–16 GB worker, so the first automatic
  placement on a 12 GB RTX 3060 was borderline.
- An explicit worker on physical `cuda:1` (RTX 3090 24 GB) eventually became
  ready. Under concurrent host CPU load, cold initialization took about 8.5
  minutes, read roughly 10.25 GB, and settled at about 16.2 GB GPU memory.
- The controlled TTS request then succeeded in about 42 seconds, returning
  2,485,542 bytes of 48 kHz stereo WAV (12.945 seconds) and persisting
  `tts/moss_tts/2026-08-10T12-50-14.wav`.
- The worker was explicitly deleted afterward; RTX 3090 application memory
  returned to 0 MB.

The existing native TTS request now accepts `device`, and binary responses
include `X-Omni-Output-Path`, `X-Omni-Output-Ref`, and
`X-Omni-Output-URL` headers so API clients can deterministically reference the
persisted artifact.

Files: `server/moss_tts_loaders.py`, `server/routers/audio.py`,
`tests/test_moss_tts_contract.py`

### ACE can now consume general output-library audio URLs

ACE transformation routes previously accepted only
`/api/ace_step/outputs/...`, forcing TTS and Stable Audio handoffs through
large base64 fields. The existing `init_audio_url` now also accepts safe local
`/api/outputs/<path>?kind=<kind>` URLs for audio files, with root containment,
extension, existence, and size validation.

Verification:

- ACE base consumed the MOSS TTS output directly from the general outputs API.
- `vocal2bgm` completed as job `43ad15eae4114ed8a42b909e9a3e8356`
  at the exact 12.945125-second source duration, 50 steps, CFG 4, in 26.28
  seconds.

Files: `server/routers/ace_step.py`,
`tests/test_ace_step_audio_sources.py`

## Timing caution

An unrelated host process was producing heavy CPU load during the later ACE
tests. Output validity and GPU stability remain meaningful, but these wall-clock
times should not be treated as clean performance benchmarks.

## Durable API guidance and agent skill

Created a project-local `omni-audio-api` skill and canonical guide so future
agents and users can reproduce the tested workflows without reconstructing the
API from source or conversation history.

- `skills/omni-audio-api/SKILL.md` routes installation, ACE-Step, Stable Audio,
  TTS, composition, GPU lifecycle, and verification work.
- `skills/omni-audio-api/agents/openai.yaml` enables implicit discovery and a
  user-facing invocation prompt.
- `skills/omni-audio-api/scripts/omni_audio_api.py` provides token-safe JSON
  requests, binary-response saving, and generalized job polling. A live
  `GET /api/workers` call verified the client. The client prefers the current
  loopback-only `/api/session` token so a stale runtime token file cannot cause
  unexplained 403 responses.
- `docs/audio-api.md` is the canonical human/API guide. It distinguishes
  install jobs from synchronous inference result IDs and records verified
  starting settings, limitations, output handoffs, and cleanup.
- `AGENTS.md` now requires this skill before operating or modifying Omni local
  audio features.

The skill passed `quick_validate.py`; its client and the two documentation-only
router corrections passed Python compilation. Focused WSL tests passed:

- `test_audio_compose.py`: 3
- `test_ace_step_audio_sources.py`: 2
- `test_moss_tts_contract.py`: 4
- `test_ace_step_staging.py`: 4

Final live cleanup verification reported no Omni model workers and no GPU
compute PIDs. The RTX 3060 reported 11,045 MB free of 12,288 MB, and the RTX
3090 reported 24,326 MB free of 24,576 MB. Baseline display/driver allocation
accounts for the remaining difference.

## Sound-effect confidence pass

The two dependable local SFX engines are dedicated MOSS-SoundEffect v2.0 and
Stable Audio. ACE Text2Samples is not counted because its official adapter is
still unreleased.

### Transient comparison

Both engines received the same fixed-seed prompt for one dry wooden door slam:

- MOSS-SFX ran explicitly on `cuda:1` (RTX 3090) and returned exactly 3.0
  seconds of 48 kHz, 16-bit mono WAV (288,044 bytes). The improved binary API
  returned its persisted path, ref, and media URL; the output metadata route
  confirmed duration, sample rate, channels, and sample width.
- Stable Audio Open Small ran explicitly on `cuda:0` (RTX 3060) with the
  verified 8-step, CFG 1, `pingpong` recipe. Job
  `c0bbccbbfbbe44a2b265cbcb1af4d482` returned 2.972154 seconds of 44.1 kHz,
  16-bit stereo WAV (524,332 bytes), and the same facts were confirmed through
  the general media metadata API.

Both files are discoverable and playable through `kind=omni`. The lightweight
output-list route deliberately leaves duration null; callers that require
media facts must use `/api/outputs/metadata/<path>?kind=omni`.

The MOSS route now emits the same `X-Omni-Output-Path`,
`X-Omni-Output-Ref`, and `X-Omni-Output-URL` discovery headers as TTS.
Contract and device-policy tests passed (4 + 3). Subjective event accuracy
still requires listening in the Media tab.

### Ambience and mechanical comparison

The same two engines also completed matched semantic tests for a continuous
night-forest ambience and one pneumatic-press cycle:

- MOSS ambience: exactly 8.0 seconds, 48 kHz mono, 768,044 bytes,
  `sfx/moss_sfx/2026-08-10T14-52-20.wav`.
- Stable ambience: 7.987664 seconds, 44.1 kHz stereo, job
  `fd43a1bb11934346845e512e8f790566`.
- MOSS mechanical: exactly 5.0 seconds, 48 kHz mono, 480,044 bytes,
  `sfx/moss_sfx/2026-08-10T14-52-52.wav`.
- Stable mechanical: 4.969070 seconds, 44.1 kHz stereo, job
  `c579e06615db4b7ab2a5628b4c67c337`.

All four outputs were independently probed through the media metadata API.
Each worker was kept resident only for its paired calls, then explicitly
unloaded/deleted. MOSS cold load held roughly 13 GB of host RSS and reached
about 11.4 GB GPU use; host CPU contention stretched the second cold load to
about 6.5 minutes. Upstream currently emits deprecation warnings for
`torch_dtype`, `weight_norm`, and an ignored `output_hidden_states` generation
flag during MOSS load. These did not prevent inference but should be revisited
when the upstream pipeline changes.

Files: `server/omni_outputs.py`, `server/routers/moss.py`,
`tests/test_moss_sfx_contract.py`, `docs/audio-api.md`.

## Fresh Comfy confidence check

A model-free Comfy instance started on physical `cuda:1` with primary-first
pool `[cuda:1, cuda:0]` and `cache_policy=ram_8`. Analyze-only validation of
the installed HiDream O1 768 graph reported:

- `ready_to_run: true`
- zero missing models and nodes
- `placement_plan.valid: true`, no blockers
- diffusion model: RTX 3090 (`cuda:1`, logical `cuda:0`), estimated 7,486 MB
- CLIP and VAE: RTX 3060 (`cuda:0`, logical `cuda:1`), estimated 2,095 MB and
  1,550 MB

The targets were resolved from stable GPU UUIDs. Direct read-only Comfy queue
status remained `queue_running: []` and `queue_pending: []`; nothing was
queued and no model weights were loaded. The exact instance was then stopped.

Focused Comfy regression results in this pass: 17 placement, 13 multi-GPU, 11
requirements, 16 extensions, 12 discovery/search, and 8 startup tests passed.
Do not rerun `test_comfy_recovery.py` inside the interactive command host: its
process-isolation behavior closed that host's stdout and interrupted the agent
session. Reserve it for an isolated CI/test process.

## Qwen2.5-Omni 3B first live pass

The installed base variant loaded explicitly on the RTX 3060 and reported
ready, but occupied essentially all 12 GB of VRAM. Deterministic text inference
completed successfully with the exact requested response `QWEN3B_TEXT_OK`.

The existing image path accepted and decoded a small PNG request, then failed
inside the worker with `CUDA driver error: device not ready`. Because the base
worker had no practical VRAM headroom, this is recorded as a 3060 capacity
failure rather than a request-contract failure. The worker remained responsive
and was deleted exactly; `/api/workers` then returned an empty list.

Source inspection found a more important contract gap: Qwen's current worker
implementation consumes text and image, but ignores the typed `audio` and
`video` fields. The generic TTS router also does not register Qwen2.5-Omni, so
native Talker output currently returns 501 through Omni. Those advertised
modalities must not be claimed as operational until the official multimodal
processor and speech-output paths are wired and live-tested.

The skill client now accepts repeatable `--base64-field image=PATH`,
`audio=PATH`, or `video=PATH` arguments. This keeps binary API tests token-safe
and avoids materializing very large base64 JSON files; no endpoint changed.

## Qwen2.5-Omni 7B GPTQ installer and loader repair

The installed `gptq-int4` checkpoint initially could not load because the Qwen
overlay contained neither GPTQModel nor AutoGPTQ. The registry exposed the
variant while the setup script explicitly assumed only dynamic bitsandbytes
quantization. The repair keeps the shared Torch/Transformers stack unchanged:

- Qwen's atomic override installer now builds GPTQModel 4.2.5, the release line
  compatible with Omni's Torch 2.7 and Transformers 4.57 pins and with explicit
  Qwen2.5-Omni support.
- The build uses isolated temporary Setuptools tooling and removes it before
  swapping the override. This avoids changing the shared `setuptools<70`
  compatibility policy.
- Undeclared upstream runtime imports `logbar`, `tokenicer`, and `device-smi`
  are installed explicitly. A cheap import check passed with GPTQModel 4.2.5
  and qwen-omni-utils 0.0.9.
- Shared Wheel is pinned to the compatible 0.46.2/0.46.3 line rather than
  0.47, which removed legacy `bdist_wheel` support.
- Optimum's generic block detector does not recognize Qwen2.5-Omni's nested
  decoder. The worker now injects GPTQModel's canonical
  `thinker.model.layers` path into the in-memory quantization config without
  modifying the checkpoint.

Three focused GPTQ-config tests pass. The repaired worker selected GPTQModel's
Triton v2 kernel, loaded all four shards on the RTX 3090, and reported ready at
12,701 MB VRAM used. Deterministic text inference returned the exact requested
`QWEN7B_GPTQ_TEXT_OK`; image inference correctly described the small test frame
as a woman with a prosthetic leg bending over. The exact worker was deleted,
`/api/workers` returned empty, no compute PIDs remained, and the 3090 returned
to 24,326 MB free. Audio/video input and native Talker output remain the shared
Qwen2.5-Omni source-wiring gaps described above.

## MiniCPM-o 2.6 installer and loader repair

The first MiniCPM load failed before weights because its override retained a
literal `nvidia/` package tree from a transitive Torch install. That CUDA 12.6
tree shadowed Omni's shared Torch 2.7 CUDA 12.8 libraries and produced an
undefined `libnvJitLink` symbol. The generic cleanup removed `nvidia_*`
metadata but had missed the literal directory.

The installer now removes both forms, and MiniCPM uses a dedicated atomic
override builder that installs its seven small unique packages with
`--no-deps`. This reuses shared Torch/Torchaudio/NumPy and avoids downloading
and then deleting an 821 MB duplicate Torch wheel. A cancelled superseded job
proved that waste; the corrected install completed without any NVIDIA entries.

Two additional upstream compatibility defects were repaired in the worker:

- Transformers' dynamic-module copier omitted MiniCPM relative-import files.
  The worker now stages all trusted local checkpoint `.py` files into the
  normal Transformers cache. The previous incomplete cache was moved to the
  recoverable quarantine path
  `/opt/omni_studio/cache/quarantine/minicpm_hyphen_o_stale_20260810`. That
  quarantine was removed during the 2026-08-10 tree-hygiene pass after the
  repaired worker and compatibility tests had been verified.
- MiniCPM imports the removed `WHISPER_ATTENTION_CLASSES` registry. A narrow
  compatibility map points the old eager/SDPA/Flash keys to Transformers
  4.57's unified `WhisperAttention`, whose constructor and dynamic backend
  behavior match the call site.

Eleven focused Qwen/MiniCPM compatibility tests now pass across the two test
files. The repaired MiniCPM worker loaded all four BF16 shards on the RTX 3090
and reported 17,889 MB VRAM used. A cold load under unrelated host CPU
contention took about eight minutes; warm-cache reloads completed in roughly
16 seconds.

Live API results were positive for the model's understanding paths:

- deterministic text returned the exact requested `MINICPM26_TEXT_OK`;
- the image path accurately described the teal blazer, black clothing and
  gold prosthetic legs in the small test frame;
- the audio path accurately transcribed the complete local voice-test WAV;
- video input is now explicitly rejected with 501 rather than being silently
  ignored, and combined image-plus-audio is explicitly rejected with 400.

The existing `/api/tts/minicpm_o` route was wired and reached native model
audio generation, but speech output is not production-ready. Transformers
4.57 returns an all-`None` generation hidden-state history for this trusted
remote-code model. Omni now captures the prompt state before MiniCPM discards
that context, but the next native decoder stage still fails because it passes
a Python list to code expecting an object with `get_mask_sizes()`. Further
live patching was stopped to avoid an open-ended upstream compatibility chain;
this exact decoder mismatch is the remaining TTS blocker. The exact worker was
deleted after testing and `GET /api/workers` returned an empty list.

## Moshi API exposure audit

Moshiko BF16 weights and a loader are installed, but the current Omni API does
not expose Moshi's defining full-duplex interaction. The batch inference
handler unconditionally returns 501, Moshi is intentionally absent from the
streaming handler registry, and the Chat UI hides it because the session
transport cannot carry streaming audio in both directions. A 16 GB live load
would only prove weight residency and would still end at the same hard 501, so
it was intentionally skipped. Moshi is installed but not usable through Omni.

## AnyGPT loader and modality audit

The installed `fnlp/AnyGPT-chat` loader initially failed before weights because
Transformers' fast Llama-tokenizer conversion imported a generated
SentencePiece protobuf module that requires `google.protobuf.internal.builder`;
the shared protobuf 3.19.6 lacks that symbol. Omni now requests the checkpoint's
slow SentencePiece tokenizer with `use_fast=False`, avoiding a risky shared
protobuf upgrade. A focused loader test covers the flag.

The corrected worker passed tokenizer setup but exposed a larger resource and
API trap. It remained in `loading` for more than 13 minutes under current host
contention, grew from roughly 1 GB to about 16 GB resident CPU RAM, kept GPU
VRAM near baseline, and never reached ready. The synchronous spawn outlived the
bridge's reachability window and returned `API server unreachable` while the
high-RAM worker remained registered. The exact `anygpt-2` worker was killed;
workers and compute PIDs returned empty and the 3090 returned to 24,326 MB free.

AnyGPT is also not any-to-any in the current worker implementation. Its handler
uses `AutoModelForCausalLM` and a tokenizer for text generation only; typed
audio, music and image inputs are ignored. Because the worker never reached
ready, even text inference remains unverified. Do not advertise multimodal
AnyGPT support or use a synchronous cold spawn until loader lifecycle and
modality handlers are redesigned.

## Qwen3-Omni and Nemotron 30B capacity audit

The installed Qwen3-Omni Instruct and Nemotron Nano Omni BF16 checkpoints are
about 60 GB and 62 GB respectively. Their original loaders accepted one CUDA
device and started import/checkpoint work without enforcing the documented
capacity, even though the largest detected GPU has only 24 GB. Qwen3 logged
free memory but did not act on it; Nemotron did not check it at all.

Both loaders now enforce variant-specific free-VRAM requirements before heavy
Transformers imports or checkpoint deserialization. Live API spawn checks on
the 3090 rejected Qwen3 at 60,000 MB and Nemotron BF16 at 62,000 MB in under six
seconds, with explicit guidance to choose a fitting quantized variant or CPU.
No weights were loaded and the worker registry returned empty afterward.
Nemotron FP8 remains above this GPU at roughly 33 GB; its configured NVFP4
variant is about 21 GB and is the only listed 30B candidate expected to fit,
but it is not live-verified. All listed Qwen3 variants remain about 60 GB.

The shared Qwen inference path also consumes text and optional image only.
Typed audio/video fields previously disappeared silently; both batch and
stream paths now return an explicit 501 for those unwired modalities. This
applies to Qwen2.5-Omni, Qwen3-Omni, and Nemotron until their official
audio/video processors are integrated and tested.

## Standalone Omni multi-GPU placement

Standalone model workers previously isolated one physical GPU with
`CUDA_VISIBLE_DEVICES`, so even Hugging Face `device_map="auto"` could see only
one card. The existing worker spawn contract now accepts a nested placement
policy, and the fundamental no-weight `POST /api/workers/analyze` route previews
the same fresh plan enforced by spawn.

The planner resolves stable UUIDs, keeps the primary GPU first, supports
single/auto/manual modes, applies global and per-device reserves/caps, reports
foreign compute PIDs, and selects the smallest viable auto subset unless
`require_all` is explicit. Worker-local CUDA mapping and Hugging Face
`max_memory` are bounded per selected card. Manual maps accept exact model
module names. Registry and health state expose the full physical pool and
per-visible-GPU memory.

CPU offload requires an explicit budget and is capped by current app-cgroup
headroom. Live analysis reported a 24,576 MB cgroup limit, 11,175 MB
reclaimable file cache, only 438 MB effective non-cache usage, and a 20,042 MB
safe worker budget after reserve. The cache was not globally dropped because
it is reclaimable and doing so would disrupt unrelated processes and cold-load
performance.

Live no-weight analysis produced a valid Qwen2.5-Omni 7B base two-GPU plan at
about 23.3 GB plus 9.6 GB budgets. Qwen3 Instruct produced an invalid plan at
about 32.9 GB combined versus 61,440 MB required; a spawn request returned 409
in two seconds and created no worker. Twenty-seven focused placement, route,
registry, loader-argument, and input-device tests pass. Actual cross-device
inference remains a per-checkpoint verification task and must not be inferred
from a valid capacity plan alone.

## Post-restart bounded probes (2026-08-11)

- ACE-Step XL Turbo/1.7B, Stable Audio Open Small, and MOSS-SFX each received
  one sequential, time-bounded cold-load attempt on the RTX 3090. The workers
  were deleted at the bounded stop point when readiness did not arrive; no
  worker or GPU allocation remained afterward. These results are classified as
  cold loader/page-in stalls under the restarted environment. Earlier warm
  success records in this report remain authoritative for actual generation.
- The output metadata API revalidated the existing ACE 60 s and 120 s tracks,
  MOSS TTS, MOSS SFX, Stable Audio, and 172 s composed instrumental. A separate
  API composition gate produced a 60 s, 48 kHz, 16-bit WAV from five short TTS
  slices. This validates long-form composition and persistence, not natural
  long-form TTS synthesis.
- Final teardown left no audio workers; Comfy was also stopped separately. WSL
  `MemAvailable` was 31.28 GB with 7.42 GB reclaimable file cache and 8.35 GB
  swap free. No global cache drop was used.

## Fresh API capability matrix (2026-08-11 continuation)

### ACE-Step 1.5 and long-form composition

- The installed `ace-1.5` 2B turbo core plus `ace-lm-0.6b` cold-loaded on the
  RTX 3060 in 377.4 s with BF16, no CPU offload, no int8, and no compilation.
- A 60 s minimal-techno instrumental completed in 64.902 s as exact 48 kHz
  audio (`76fbb04a863a4157a1f1a0a8ff48b1dc`). Objective analysis reported
  60.0 s, peak 0.8607, RMS -21.59 dB, and a low-confidence 140.62 BPM estimate
  versus the requested 126 BPM; tempo text is therefore guidance, not a lock.
- The installed raspy-vocal community pack attached with the declared
  `male_vocals_adapter_model.safetensors` at 0.4. A contrasting 60 s vocal
  song completed in 54.752 s (`470081714a8a4640b6c7de15c434717f`). The LoRA
  detached cleanly.
- The pack's declared instrumental adapter attached at 0.4. A 60 s
  psychedelic progressive-techno instrumental completed in 55.391 s
  (`61e7cce751214f5e8f04f68d89cba3d8`).
- Four compatible 60 s instrumental sections were composed A/B/A/B with
  three 8 s crossfades into a 216.0 s, 48 kHz, 24-bit master. Two-thread LUFS
  and true-peak normalization completed in 12.6 s; composition ID
  `33b0aab8c7a048e4b19dea921651c994` is media-visible. This is the dependable
  path for arbitrarily longer arrangements, subject to the 64-segment API
  bound and phrase-alignment listening.
- The ACE LoRA was detached, all components unloaded, and exact worker
  `ace_step-1` deleted after testing.

### Stable Audio Open Small and 1.0

- Open Small cold-loaded on the RTX 3060 in 233.1 s. Its live contract reports
  44.1 kHz, 11 s maximum, and `euler`, `rk4`, `dpmpp`, `pingpong` samplers.
- Warm runs passed three sound-effect classes: one 2.972 s dry door slam at
  10 steps (`69e61763a96746ed8f904aaa0e8b0628`), a 10.495 s rainforest ambience
  (`8dbc129fd6544c73bc8046159ecfdda5`), and a 7.988 s layered freight-elevator
  scene (`2fa54f3336cc412c839c818b2bb779a4`).
- The same worker hot-swapped to Open 1.0 in 158.9 s. A maximum-duration 47.0 s
  cavern ambience completed at 100 steps in 38.5 s
  (`50ac66bce3f747a6913dfc4ba21c0753`).
- `larger-clap-general` loaded explicitly. Ranked generation produced two
  sequential six-second thunder candidates with scores 0.32264 and 0.31561,
  and persisted the best copy (`0ae170d1c2fa4b5f86f5ae9c18498e96`).
- Audio Lab unloaded SA and CLAP and deleted exact worker `audio_lab-1`.

### MOSS sound effect and TTS failure containment

- MOSS-SFX cold-loaded on the RTX 3090 in about 490 s and then produced a
  persisted door-slam fixture at
  `sfx/moss_sfx/2026-08-11T20-04-17.wav`. The repaired metadata route verifies
  integrity OK, exact 3.0 s, mono, 48 kHz PCM16. Exact worker `moss_sfx-1` was
  deleted afterward.
- The typed TTS route is `/api/tts/moss_tts`; the stale hyphenated example was
  corrected in the canonical guide, plan, and agent skill. `moss_tts` loaded
  successfully on the RTX 3090 in about 424 s, but inference failed in the
  upstream decoder with `CUDA driver error: unknown error`.
- That CUDA fault leaked exactly 1,024 `anon_inode:dxgresource` descriptors and
  exhausted the worker's file limit, causing an asyncio socket-accept storm.
  Omni now retires any TTS worker that returns a 5xx instead of returning the
  poisoned process to the ready pool. The focused regression test passes, the
  failed exact worker was deleted, and no new TTS output is claimed.
- Final teardown verified zero audio/model workers, stopped Comfy separately,
  synced persisted state, and terminated only the Omni distro to release its
  reclaimable cache.
