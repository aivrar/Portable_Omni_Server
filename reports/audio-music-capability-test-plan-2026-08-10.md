# Omni Studio Local Music Capability Test Plan

Date: 2026-08-10

## Goal

Qualify Omni Studio as a dependable, API-driven local music workstation using
ACE-Step 1.5 and Stable Audio. Produce original instrumental and vocal music,
identify the best settings for each engine, exercise the useful LoRAs and edit
modes, determine practical song-length limits, and establish a reliable way to
build long-form music from shorter generated material.

This is a staged qualification plan, not a promise that every community model
or LoRA is compatible. A capability is documented as working only after its
output, resource use, unload behavior, and API response have all been checked.

## Confirmed starting state

- ACE-Step is installed with `ace-1.5`, `ace-xl-turbo`, the 0.6B and 1.7B
  language models, and the default VAE.
- The previous short qualification produced valid 48 kHz stereo audio with both
  the 2B/0.6B and XL/1.7B combinations.
- No ACE-Step LoRAs are installed yet.
- Audio Lab is installed with Stable Audio Open 1.0, Stable Audio Open Small,
  the default VAE, and the general LAION CLAP model.
- Neither ACE-Step nor Audio Lab currently has a resident worker or loaded
  model. This is the correct idle state.
- Omni already exposes load, unload, cancel, generation, ranking, audio-to-audio,
  inpainting/repainting, VAE, installation, output, and job APIs for the relevant
  engine.
- Omni validates Stable Audio duration against the loaded model: 47 seconds for
  Stable Audio Open 1.0 and 11 seconds for Open Small.
- Omni does not yet expose a general audio timeline/composition API for
  beat-aligned assembly, repeated loops, multi-track mixing, production fades,
  or loudness normalization. The existing Stable Audio inpaint fallback only
  performs very short boundary crossfades.

## Engine roles

### ACE-Step 1.5

Use ACE-Step for complete songs, instrumentals with structure, singing, lyrics,
cover/remix/repaint operations, and long-range musical development. Its language
model can plan material from short loops through ten-minute compositions. Omni's
generation schema permits up to 600 seconds and its extension API can prepend or
append up to 300 seconds per operation.

### Stable Audio

Use Stable Audio for short production elements: drum and percussion loops,
bass ideas, pads, textures, risers, transitions, ambiences, sound design, and
instrumental samples. Stable Audio Open 1.0 is limited to 47 seconds and its
official model card says it cannot generate realistic vocals. It is not the
primary full-song or singing engine.

### Local TTS

Use Omni's local TTS to create spoken phrases, narration, whispers, vocoder
source material, and test references. TTS is not expected to sing by itself.
Test it as input to ACE audio-to-audio, cover, repaint, and vocal-to-background
workflows and as a spoken layer in a composed track.

## Execution rules

1. Use Omni APIs for installation, lifecycle, generation, outputs, cancellation,
   and cleanup.
2. Run one resource-owning audio engine at a time. Unload its models and remove
   the worker before handing the GPU to the other engine when required.
3. Keep probes short and sequential. Do not run a large Cartesian parameter
   sweep.
4. Change one variable at a time with a fixed seed before comparing variants.
5. Poll the job/progress API at a modest interval; do not run broad process or
   filesystem scans during inference.
6. After every new model or LoRA, run one short output test, validate the file,
   unload, and check that GPU memory returns before continuing.
7. Stop a test ladder on malformed output, non-finite audio, repeated worker
   failure, an invalid model/task combination, OOM, cancellation failure, or
   persistent resource retention.
8. All lyrics and prompts will be newly written for these tests. Do not imitate
   a living artist or use copyrighted lyrics.

## Phase 1: ACE-Step baseline songs

Start with `ace-1.5` plus the 0.6B LM, BF16, no CPU offload, eight steps, Euler,
CFG 1, fixed seeds, and 30-second outputs. This is the low-cost baseline. Each
result must play in the Media Library and have a valid manifest before advancing.

Create this starter set:

| ID | Test | Musical target | Vocal target |
| --- | --- | --- | --- |
| A1 | Minimal techno | 124 BPM, dry kick, restrained sub bass, sparse clicks, hypnotic development | Instrumental; explicitly no voice |
| A2 | Psychedelic downtempo | 98 BPM, modulated synths, reversed textures, evolving stereo space | Instrumental |
| A3 | Dark ambient | Slow, spacious drones, granular details, coherent rise and resolution | Instrumental |
| A4 | Synthpop | Verse and chorus, bright arpeggio, electronic drums, memorable hook | Clear English lead vocal |
| A5 | Hard techno | 142 BPM, distorted percussion, controlled low end, short breakdown | Short original chant |
| A6 | Psychedelic vocal | Trippy electronic ballad, unusual phrasing and vocal texture | Original sung English lyric |

For singing tests, lyrics will include explicit section labels such as intro,
verse, pre-chorus, chorus, bridge, and outro. Instrumental tests omit lyrics and
also state `instrumental, no singing, no spoken voice` in the musical brief.

## Phase 2: ACE-Step quality and model screening

Use the best baseline instrumental and vocal seed. Test one factor at a time:

1. LM: 0.6B versus 1.7B. Add the 4B LM only if planning, lyric alignment, or
   long-form coherence materially improves enough to justify its download and
   memory cost.
2. DiT: `ace-1.5` versus `ace-xl-turbo`. Measure cold load time separately from
   warm generation time.
3. Scheduler: Euler, Heun, and DPM++ at the checkpoint's intended step count.
4. Steps: keep turbo checkpoints at their intended eight-step regime; test
   higher-step base/SFT checkpoints only after they are installed and their task
   support is verified.
5. CPU offload and overlapped decode: compare only after a quality baseline;
   evaluate wall time, VRAM, system RAM, and unload behavior.
6. Ranked generation: generate four candidates and CLAP-rank them, then compare
   the automated winner with a human listening choice.

Avoid a full grid. A setting survives only if it improves the same fixed-seed
example, then it is checked once on a second genre.

## Phase 3: ACE-Step modes and LoRAs

Install and qualify official mode-enabling LoRAs first:

1. `lyric2vocal`: generate isolated sung vocals from original lyrics, check
   intelligibility, timing, background leakage, and suitability as a guide vocal.
2. `text2samples`: create kick/percussion loops, bass phrases, psychedelic FX,
   synth stabs, and transition elements.
3. Verify that each mode attaches the required LoRA for the job and detaches it
   cleanly afterward.

Then screen the curated style LoRAs in this order:

1. `synthpop`
2. `pop-electro`
3. `lofi`
4. `raspy-vocal-pack`
5. `acoustic-guitar`
6. `raga`

For each LoRA, test the documented trigger information, then multipliers 0.6,
0.9, and 1.1 on one fixed 30-second seed. Reject settings that overpower lyrics,
collapse the mix, introduce silence, or make unloading unreliable. The registry
does not currently contain a dedicated techno LoRA; techno is prompt-driven
until a candidate LoRA has been researched and independently qualified.

Exercise the advanced modes with short source material:

- Audio-to-audio at low, medium, and high transformation strengths.
- Repaint a single eight-bar region while preserving the rest.
- Edit one lyric phrase, then perform two sequential small edits rather than a
  large replacement.
- Cover/remix an original locally generated instrumental.
- Convert an original isolated vocal or TTS phrase into a background-music test.
- Compare the default VAE with compatible alternative VAEs in isolated tests;
  do not combine a new VAE and LoRA in the same first test.

## Phase 4: ACE-Step duration and continuity

Use the strongest minimal-techno instrumental and vocal-song settings. Advance
through 30, 60, 120, 240, 360, and 600 seconds. A duration advances only if the
previous result has valid audio, no severe tempo collapse, no long silence, an
intentional ending or continuation point, and acceptable resource recovery.

Compare three long-form methods:

1. Direct generation: one LM-planned track at the final duration.
2. Native extension: generate a strong seed section and append/prepend new
   sections with deliberate musical instructions.
3. Section assembly: independently generate intro, groove, breakdown, peak, and
   outro sections and arrange them on a beat-aligned timeline.

For native extension, test 30- and 60-second additions first. Preserve several
seconds of musical context and compare continuation quality, tempo drift, key
drift, repeated motifs, vocal identity, and boundary artifacts. Do not assume
that chaining technically valid extensions produces a musically coherent song.

## Phase 5: Stable Audio baseline and toolkit

Use Open Small for cheap 8- and 11-second API/toolkit checks. Use Open 1.0 for
16-, 32-, and 47-second quality tests.

Starter material:

- 124 BPM minimal-techno kick and percussion loop.
- Separate minimal-techno bass loop.
- Psychedelic granular transition and stereo texture.
- Dub-techno chord stab and delay tail.
- Dark ambient pad.
- Hard-techno riser and impact.
- Acoustic or piano motif for contrast.

For Open 1.0, begin near the official reference settings: 100 steps, CFG 7,
sigma 0.3 to 500, and DPM++ 3M SDE. Screen 50 versus 100 steps, CFG 5/7/9, and
one alternate compatible sampler using fixed seeds and one-factor changes. Use
four-candidate CLAP ranking only after single generation is dependable.

Toolkit qualification:

- Audio-to-audio at three transformation strengths.
- Inpaint a bounded region and inspect both edit boundaries.
- Unconditional generation as a control, not a preferred production mode.
- VAE encode/decode/reconstruct round trip and audible degradation check.
- Default VAE versus the tuned VAE on the same source.
- General CLAP versus music-and-speech CLAP on a small shared candidate set.

After the official models pass, install and test community models sequentially:

1. SAO Instrumental.
2. Audialab EDM Elements.
3. Foundation-1 diffusers port.
4. Nekochu Music.
5. RC Vocal Textures and RC Infinite Pianos only after the core electronic-music
   cases are stable.

Every community checkpoint is treated as untrusted until load, generation,
output validation, cancellation, and unload all pass. Untested registry entries
are excluded from the first qualification round.

## Phase 6: long-form audio composition

The preferred coherent-song path is direct ACE-Step generation or ACE native
extension. Stable Audio material becomes a long track through deterministic
production operations, not by asking the 47-second model for a longer clip.

The required assembly behavior is:

- Trim clips to beat/bar boundaries using a declared or detected BPM.
- Repeat exact loops without accumulating resampling error.
- Place clips and stems on a common timeline.
- Apply per-clip gain, pan, fade-in, fade-out, and equal-power crossfade.
- Overlap one to four bars when blending generated continuations.
- Resample sources explicitly to one project rate and channel layout.
- Normalize the completed mix by loudness, preserve headroom, and prevent
  clipping; retain a lossless master plus a Media Library preview.
- Persist a composition manifest containing source asset IDs, offsets, trims,
  fades, gains, seeds, model settings, BPM, and output hash.

Omni does not currently expose this full operation. After the short engine tests
prove the source material is useful, add one fundamental composition API rather
than many special-case endpoints. Its implementation must be deterministic,
bounded, cancellable, path-safe, job-backed, and usable without a model worker.
It should accept existing Omni media assets rather than arbitrary host paths.

Long-form experiments after that capability exists:

1. Five-minute minimal techno from 8- or 16-bar Stable Audio loops, with evolving
   percussion, bass, effects, breakdown, and re-entry.
2. Ten-minute psychedelic ambient piece built from long pads and overlapped
   generative transitions.
3. ACE direct five-minute song versus an ACE seed plus extensions.
4. Hybrid vocal track: ACE song and vocal material with Stable Audio transitions
   and locally generated spoken TTS texture.
5. One-hour background instrumental built from a larger pool of sections with
   controlled recurrence, not one short loop repeated for an hour.

## Measurements and acceptance criteria

Record for every output:

- API request with secrets removed, model/LM/VAE/LoRA identifiers, seed, prompt,
  lyrics, sampler, scheduler, steps, CFG, and duration.
- Cold load time, warm generation time, output duration, sample rate, channels,
  format, file size, and hash.
- Peak VRAM by GPU, peak system RAM, and whether memory returns after unload and
  worker removal.
- Peak level, clipping count, integrated loudness, silence ratio, BPM estimate,
  tempo drift, and loop-boundary discontinuity where applicable.
- CLAP score when ranking is used.
- Human notes for arrangement, genre adherence, lyric intelligibility, vocal
  consistency, musical development, mix clarity, artifacts, boundary quality,
  and ending quality.

Minimum technical acceptance:

- The API job completes or cancels predictably.
- The output decodes and plays in Omni's Media Library.
- Duration and audio format match the manifest.
- No NaN/non-finite samples, hard clipping, unexplained long silence, or severe
  boundary click is present.
- The loaded model and worker can be released without restarting the whole app.
- A repeat with the same seed and settings is reproducible to the extent the
  backend supports.

## Findings, documentation, and skills

Maintain a separate findings report while tests run. Record failures as well as
successes, including upstream source locations for model/toolkit defects; do not
patch upstream source merely to hide a failed qualification.

After settings are proven:

- Create an `omni-audio-api` skill that teaches future agents lifecycle,
  installation, generation, ranked generation, editing, LoRAs, Stable Audio
  toolkit use, long-form composition, output retrieval, cancellation, and safe
  resource handoff.
- Add compact human/API documentation with tested examples generated from the
  typed request models.
- Route the skill to separate ACE-Step, Stable Audio, and composition references
  so an agent reads only what the task requires.
- Never put the Omni API token in the skill, reports, examples, or logs.

## Primary upstream references

- ACE-Step project: https://github.com/ace-step/ACE-Step
- ACE-Step 1.5 model card: https://huggingface.co/ACE-Step/Ace-Step1.5
- Stable Audio Tools: https://github.com/Stability-AI/stable-audio-tools
- Stable Audio Open 1.0 model card: https://huggingface.co/stabilityai/stable-audio-open-1.0

