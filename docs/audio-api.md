# Omni Studio local audio API

This is the canonical human and agent guide for Omni Studio's local audio
features: ACE-Step music, MiniMax Music 3, Stable Audio Lab, MOSS-TTS, the shared output
library, and long-form composition. The typed Pydantic models in
`server/routers/` remain the machine-readable contract. If this guide and a
typed route differ, follow the typed route and correct this guide.

For app-wide verification grades and the remaining model rollout order, see
`docs/capability-confidence.md`.

## Contents

- [Contract, authentication, and call behavior](#contract-authentication-and-call-behavior)
- [Safe discovery sequence](#safe-discovery-sequence)
- [Asset status and installation](#asset-status-and-installation)
- [MiniMax Music 3](#minimax-music-3)
- [ACE-Step music](#ace-step-music)
- [Stable Audio Lab](#stable-audio-lab)
- [Sound effects with two engines](#sound-effects-with-two-engines)
- [Local TTS and cross-tool audio](#local-tts-and-cross-tool-audio)
- [Long-form composition and outputs](#long-form-composition-and-outputs)
- [Devices, workers, and cleanup](#devices-workers-and-cleanup)
- [Verified behavior and limitations](#verified-behavior-and-limitations)

## Contract, authentication, and call behavior

- Normal gateway: `http://127.0.0.1:9200`
- Auth header: `X-Omni-Token: <runtime token>`
- Never print or commit the token. Local clients may bootstrap it from the
  loopback-only `GET /api/session`, read the runtime token file, or use
  `OMNI_API_TOKEN`.
- OpenAPI is available at `GET /openapi.json` only when the gateway was
  deliberately started with `OMNI_ENABLE_DOCS=1`.
- Install and composition calls are background jobs. Poll their returned
  `job_id` with `GET /api/jobs/{job_id}`.
- ACE-Step, MiniMax Music 3, and Audio Lab generation calls are synchronous. Their result also
  has a `job_id`, but that identifier names the persisted output directory; it
  is not a background job to poll.
- Use a long client timeout for model loading and inference. Do not retry a
  timed-out mutation until state and outputs have been checked. During an
  ACE-Step or Audio Lab inference the exact worker reports `busy`; if the
  server-side timeout fires, Omni retires that worker instead of falsely
  returning it to `ready`.

The bundled client keeps the token out of command output:

```text
python skills/omni-audio-api/scripts/omni_audio_api.py request GET /api/devices
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/ace_step/generate --body request.json --timeout 900
python skills/omni-audio-api/scripts/omni_audio_api.py request POST /api/chat/qwen_omni_3b?autospawn=false --body chat.json --base64-field image=frame.png --timeout 900
python skills/omni-audio-api/scripts/omni_audio_api.py wait-job JOB_ID
```

Use repeatable `--base64-field image=PATH`, `audio=PATH`, or `video=PATH`
arguments when a typed request expects raw base64. This keeps large binary
content out of hand-written JSON and still sends the existing API contract;
it does not upload to a different endpoint.

Typed input fields do not prove a model consumes that modality. The current
Qwen2.5-Omni worker has verified text/image paths (7B GPTQ on the 3090), but
explicitly rejects unwired `audio` and `video` with 501, and native Talker/TTS
is not registered. See
`docs/capability-confidence.md` before exercising general multimodal workers.

MiniCPM-o 2.6 is separately verified for text, image, and audio understanding
on the 3090. Send one input modality per request to
`POST /api/chat/minicpm_o?autospawn=false`; use `--base64-field image=PATH` or
`--base64-field audio=PATH` with the bundled client. Raw audio is decoded to
mono 16 kHz and limited to 60 seconds. Video returns 501, and a combined
image-plus-audio request returns 400 instead of silently dropping input.

## Safe discovery sequence

Use these cheap calls before loading weights:

1. `GET /api/devices`
2. `GET /api/workers`
3. `GET /api/ace_step/status`, `GET /api/minimax_music3/status`, or `GET /api/audio_lab/status`
4. `GET /api/ace_step/state?autospawn=false`,
   `GET /api/minimax_music3/state?autospawn=false`, or
   `GET /api/audio_lab/state?autospawn=false`

`/status` reports installed assets and registry metadata. `/state` reports a
running worker and loaded components. A cheap state call returning
`{"running": false}` is normal and must not autospawn a model worker.

Choose an explicit current `cuda:N` for a GPU-sensitive load. Device indices
can change across boots or visibility masks, so derive them from the current
device response rather than memory. For MOSS-TTS on unequal GPUs, explicitly
select a device with adequate free VRAM; generic autospawn is not
capacity-aware.

## Asset status and installation

### ACE-Step

- Status: `GET /api/ace_step/status`
- Install model: `POST /api/ace_step/install-model`
  with `{"variant_id":"ace-1.5"}`
- Install LM: `POST /api/ace_step/install-lm`
  with `{"variant_id":"ace-lm-0.6b"}`
- Install VAE: `POST /api/ace_step/install-vae`
- Install registry LoRA: `POST /api/ace_step/install-lora`
  with `{"name":"raspy-vocal-pack"}`
- Install ad-hoc Hugging Face LoRA: the same endpoint with
  `{"repo":"org/repository","name":"local-name"}`
- Install a custom model, LM, VAE, or LoRA:
  `POST /api/ace_step/install-custom` with `repo`, optional `name`, and
  `kind` (`model`, `lm`, `vae`, or `lora`).

### Stable Audio Lab

- Status: `GET /api/audio_lab/status`
- Install model: `POST /api/audio_lab/install-model`
  with `{"variant_id":"sao-open-1.0"}`
- Install VAE: `POST /api/audio_lab/install-vae`
- Install CLAP: `POST /api/audio_lab/install-clap`
  with `{"variant_id":"larger-clap-general"}`
- Install a custom model, VAE, or CLAP:
  `POST /api/audio_lab/install-custom` with `repo`, optional `name`, and
  `kind` (`model`, `vae`, or `clap`).

### MOSS-TTS

- General setup state: `GET /api/setup/status`
- Install: `POST /api/setup/install/moss-tts`

### MiniMax Music 3

- Status: `GET /api/minimax_music3/status`
- Install the official selective snapshot: `POST /api/setup/install-variant`
  with `{"model":"minimax_music3","variant_id":"official-diffusers"}`
- Discover models or future LoRAs:
  `GET /api/search/hf?q=MiniMax%20Music3&kind=model&family=minimax_music3`
  (repeat with `kind=lora`)

The installer pins the official revision and downloads only the Diffusers
component layout, about 28 GB, excluding the duplicate SGLang weights. Only a
search result labelled `official` is currently runtime-compatible. Known
mirrors, hardware-specific compiled supplements, test fixtures, and Comfy
packages receive explicit non-compatible labels. No verified Music 3 LoRA
attachment contract is currently exposed.

Every install returns `status: "running"` and a background `job_id`. Poll:

```text
GET /api/jobs/JOB_ID
```

Terminal states are `done`, `error`, and `cancelled`. Read the returned error
and log tail before retrying. Some official Stable Audio repositories are
gated; accept their license and configure the Hugging Face token first.

Deletion routes mirror the install route and delete immediately. For example,
`DELETE /api/ace_step/install-lora/NAME` and
`DELETE /api/audio_lab/install-model/VARIANT`. Resolve the exact registry ID
and confirm that no worker has it loaded first. Model and LoRA deletion is not
recoverable through Omni.

## MiniMax Music 3

MiniMax Music 3 is an independent standalone worker, not a ComfyUI workflow or
an ACE-Step variant. Load it on an explicit current device:

```json
POST /api/minimax_music3/load
{
  "model_variant": "official-diffusers",
  "device": "cuda:0",
  "bf16": true,
  "cpu_offload": true,
  "cpu_memory_mb": 16000,
  "reserve_mb": 1024
}
```

The load route analyzes current GPU and app-cgroup budgets and passes the valid
plan into worker spawn. A `valid:false` placement is a hard stop. CPU offload
is the safe default for a 24 GB GPU, but its explicit `cpu_memory_mb` must fit
the current safe workload budget. This value is an **admission estimate**,
not an enforced per-worker ceiling. Runtime offload is constrained by the
shared workload cgroup. The worker does not claim
component sharding across the 3090 and 3060; choose the 3090 and use CPU
offload rather than implying that independent CUDA processes pool VRAM.

Generate with both a detailed music description and nonempty lyrics:

```json
POST /api/minimax_music3/generate
{
  "prompt": "Genre: minimal techno. BPM: 126. Key: D minor. Hypnotic and spacious. Arrangement: deep kick, rolling sub bass, sparse metallic percussion and evolving acid texture.",
  "lyrics": "[intro]\n(instrumental)\n\n[instrumental]\nSlow filter evolution\n\n[outro]\n(instrumental)",
  "duration_s": 60,
  "seed": 7,
  "autospawn": false
}
```

Valid duration is 1 through 360 seconds and is an upper bound: the language
model may stop earlier. Put `[intro]`, `[verse]`, `[pre-chorus]`, `[chorus]`,
`[post-chorus]`, `[bridge]`, `[instrumental]`, `[solo]`, and `[outro]` tags on their own lines.
Text on the same line as a leading tag is dropped. Instrumentals still require
nonempty lyrics.

The combined tokenized text is limited to 5,000 tokens. The gateway also uses
a conservative 20,000-character bound before the loaded worker performs the
exact tokenizer check. Streaming is not supported.

The result is a native 44.1 kHz stereo PCM-24 WAV written directly to
`outputs/omni/minimax_music3/JOB_ID/01.wav`, with an adjacent manifest visible
in the Media library. Direct storage avoids holding a multi-minute WAV as
base64 in gateway RAM. Use `GET /api/minimax_music3/jobs` for history,
`POST /api/minimax_music3/unload` to release weights, and
`POST /api/minimax_music3/cancel` to terminate an active generation. Cancel
retires the exact worker and therefore unloads the model. Supply `worker_id`
in load/generate bodies, or as a state/unload/cancel query parameter, when
more than one MiniMax worker exists; ambiguous selection returns 409.

The official checkpoint uses the MiniMax-Music3 Community License. Preserve
visible `MiniMax-Music3` attribution and review its commercial revenue,
acceptable-use, and safeguard terms before deployment.

## ACE-Step music

### Select and load

ACE admission checks DiT memory on the primary and LM memory on its actual
auxiliary device. Free memory on an unrelated GPU cannot cover a component
that does not fit. Exact variants are checked again before loading; FP32 has
a larger conservative estimate. Unknown custom variants have no proven
capacity estimate and are blocked from automatic admission. Existing resident
weights are not credited as free memory; unload before a blocked hot swap.


Use the 2B turbo core for fast general song generation:

```json
{
  "model_variant": "ace-1.5",
  "lm_variant": "ace-lm-0.6b",
  "bf16": true,
  "cpu_offload": false,
  "int8": false,
  "torch_compile": false,
  "device": "cuda:1"
}
```

For the official highest-quality stack on two GPUs, load XL SFT on the
3090 and the 4B planner on the 3060. ACE initializes DiT and LM as
separate handlers; Omni exposes both cards to one ACE worker and places
them independently:

```json
{
  "model_variant": "ace-xl-sft",
  "lm_variant": "ace-lm-4b",
  "bf16": true,
  "cpu_offload": false,
  "device": "cuda:1",
  "lm_device": "cuda:0"
}
```

The 4B LM needs the 3060 cleared of other residents (including Audio Lab
CLAP). Send this to `POST /api/ace_step/load`, then verify
`GET /api/ace_step/state?autospawn=false`. Turbo defaults are 8 steps and CFG
1.0. The 2B base model `ace-base` defaults to 50 steps and CFG 4.0 and is the
choice for base-only tasks such as completion. Registry status declares each
model's supported tasks, default LM, VRAM estimate, and duration ceiling.

Only pass components that should change. A model hot-swap frees the prior
model before loading the next. `cpu_offload` trades GPU memory for RAM and
transfer overhead; do not enable it reflexively on a GPU that fits the model.

### Generate songs and instrumentals

`POST /api/ace_step/generate` accepts:

- required `prompt`
- optional `lyrics` and `negative_prompt`
- `duration_s` from 1 to 600
- optional `steps`, `cfg_scale`, two-value `guidance_interval`
- `scheduler`: `euler`, `heun`, or `dpmpp` (v1.5 forwards DPM++ as `sampler_mode` when the installed GenerationParams declares that field)
- optional `shift` from 1.0 to 5.0. Turbo defaults to `3.0`; SFT/base default to `1.0`
- `infer_method`: `ode` (official default, use this for SFT) or `sde` (known to garble SFT)
- `use_adg`: Adaptive Dual Guidance, official default false
- optional `thinking`: when false, skip LM CoT caption/metadata rewrite even if an LM is loaded
- optional typed `bpm` (30–300), `keyscale` (for example `D minor`), and `timesignature` (`4` for 4/4)
- optional fixed `seed`
- `bf16`, `overlapped_decode`, and `autospawn`
- official LM knobs: `lm_temperature`, `lm_cfg_scale`, `lm_top_k`, `lm_top_p`, `use_constrained_decoding`
- official extras: `audio_codes`, `timesteps`, `reference_audio_base64` / `reference_audio_url`, `batch_size`, DCW `dcw_mode` / scalers, `enable_normalization`, `fade_in_s` / `fade_out_s`

Official Simple Mode and planner helpers:

- `POST /api/ace_step/create-sample` — style query to caption, lyrics, BPM/key
- `POST /api/ace_step/format-sample` — expand a caption/lyrics pair
- `POST /api/ace_step/understand` — 5Hz codes to metadata
- `POST /api/ace_step/simple` — create-sample then generate in one call

Base-only arrangement (load `ace-xl-base` or `ace-base` first):

- `POST /api/ace_step/extract` with `track_name`
- `POST /api/ace_step/lego` with `track_name`
- `POST /api/ace_step/complete` with `track_names`

`POST /api/ace_step/extend` now supports `prepend` and `append` on SFT by padding silence and repainting the new region. Load `lm_backend` as `pt` (default) or `vllm`.

On ACE-Step v1.5, `negative_prompt` is applied as the LM negative prompt and
`guidance_interval` is applied as `cfg_interval_start` / `cfg_interval_end`.
Those fields were previously accepted by the route and then dropped.

Example vocal song:

```json
{
  "prompt": "psychedelic synthpop, warm analog bass, expressive female lead, spacious mix",
  "lyrics": "[Verse]\nNeon rivers bend through the night\n[Chorus]\nWe wake inside the light",
  "negative_prompt": "harsh clipping, muddy mix",
  "duration_s": 30,
  "steps": 8,
  "cfg_scale": 1.0,
  "scheduler": "euler",
  "shift": 3.0,
  "bpm": 126,
  "keyscale": "D minor",
  "timesignature": "4",
  "seed": 12031,
  "autospawn": false
}
```

For an instrumental, omit `lyrics` and say `instrumental, no vocals` in the
prompt. Use `POST /api/ace_step/generate-ranked` with `n` from 1 to 16 to make
multiple candidates. Add `score_prompt`; set `score_with_clap=false` to skip
ranking. ACE ranking uses a running Audio Lab CLAP worker when available and
otherwise records an unranked result.

The response persists WAV files and a manifest under an ACE output `job_id`.
Use the returned URL or:

```text
GET /api/ace_step/outputs/JOB_ID
GET /api/ace_step/outputs/JOB_ID/FILENAME
```

### Attach LoRAs

Load the base model first. List runtime attachments with
`GET /api/ace_step/lora/list`. Attach one installed adapter:

```json
{
  "name": "raspy-vocal-pack",
  "adapter_file": "male_vocals_adapter_model.safetensors",
  "multiplier": 0.5
}
```

Send to `POST /api/ace_step/lora/attach`. `adapter_file` is optional for a
single-adapter repository and required to choose among a declared pack. Use
the exact filenames and recommended multiplier range returned by
`GET /api/ace_step/status`; do not guess a file inside a repository. Detach
with `POST /api/ace_step/lora/detach` and `{"name":"..."}`.

The verified raspy and acoustic packs behave safely in the 0.2-0.7 range;
0.4-0.5 is a conservative starting point. The registry marks known
incompatible or unreleased adapters unavailable. Lyric2Vocal and Text2Samples
weights have not been released upstream, so their specialized endpoints are
not currently usable with official weights.

### Transform existing audio

These routes accept either `init_audio_base64` or `init_audio_url`:

- `POST /api/ace_step/a2a`: add `init_noise_level` from 0 to 1.
- `POST /api/ace_step/repaint`: add `mask_start_s` and `mask_end_s` inside
  `duration_s`.
- `POST /api/ace_step/edit`: add `edit_mode` (`only_lyrics` or `remix`) and
  optional `source_prompt` and `source_lyrics`.
- `POST /api/ace_step/extend`: add `extend_mode` (`prepend` or `append`) and
  `extend_duration_s`.
- `POST /api/ace_step/cover`: describe the target style in `prompt`.
- `POST /api/ace_step/vocal2bgm`: provide a voice or vocal input and an
  optional accompaniment prompt.

An `init_audio_url` may be an ACE output URL or a local output-library URL:

```text
/api/ace_step/outputs/JOB_ID/01.wav
/api/outputs/tts/moss_tts/TIMESTAMP.wav?kind=omni
```

Allowed output kinds are `output`, `input`, `temp`, and `omni`; the file must
remain inside that library and use a supported audio extension.

Completion and extension are generative, not lossless splices. In the current
base-model test, the retained portion changed materially. Use the composition
endpoint when existing audio must remain bit-for-bit or perceptually stable.

Analyze a persisted audio item with `POST /api/ace_step/analyze`, passing
`audio_url` or `audio_base64`. This returns objective metadata such as BPM,
key, and loudness; it does not prove musical coherence.

### Unload

Unload one component or all components:

```text
POST /api/ace_step/unload
{"component":"all"}
```

Valid components are `model`, `lm`, `vae`, and `all`. Then find the exact ACE
worker in `GET /api/workers` and delete it with
`DELETE /api/workers/WORKER_ID` when no resident process is wanted.

## Stable Audio Lab

### Load

`POST /api/audio_lab/load` accepts optional `sa_variant`, `vae_variant`,
`clap_variant`, `worker_id`, and `device`. Pass at least one component. Example:

```json
{
  "sa_variant": "sao-open-1.0",
  "clap_variant": "larger-clap-general",
  "device": "cuda:0"
}
```

List all Audio Lab workers with `GET /api/audio_lab/workers`. Target an exact
worker with `worker_id` when more than one exists.

### Generate and rank

`POST /api/audio_lab/generate` accepts `prompt`, optional `negative_prompt`,
`duration_s`, `steps`, `cfg_scale`, optional sigma bounds, optional `sampler`,
optional `seed`, `autospawn`, and optional `worker_id`.

Known-good official starting recipes:

- `sao-open-small`: 8-10 steps, duration at or below 11 seconds. Eight steps
  is valid even though an older Omni minimum rejected it.
- `sao-open-1.0`: duration at or below 47 seconds. A 30-second or 47-second
  request has been verified locally.

Native sampler, sigma bounds, and A2A `init_noise_level` controls apply only
to native Stable Audio checkpoints. Omit them for diffusers checkpoints;
explicit unsupported controls return an error instead of being ignored. Zero
CFG and zero native A2A noise are preserved. Source audio is bounded to
600 seconds, 8 channels and 32 million decoded samples before allocation.
A supplied `clap_variant` must match the loaded scorer.

Query `GET /api/audio_lab/samplers` rather than assuming one sampler family is
valid for every checkpoint. Use `POST /api/audio_lab/generate-ranked` with
`n`, `clap_variant`, and optional `score_prompt`. The implementation generates
candidates sequentially to avoid needless concurrent GPU pressure. Load CLAP
before ranked generation.

### Audio-to-audio, inpaint, scoring, and VAE tools

- `POST /api/audio_lab/a2a`: generation fields plus source audio and
  `init_noise_level`.
- `POST /api/audio_lab/inpaint`: generation fields plus source audio,
  `mask_start_s`, and `mask_end_s`.
- `POST /api/audio_lab/score`: `text`, source audio, optional CLAP variant.
- `POST /api/audio_lab/uncond`: duration and sampling fields without a prompt.
- `POST /api/audio_lab/vae/encode`, `/vae/decode`, and `/vae/reconstruct`:
  inspect or round-trip the loaded VAE.

For these Audio Lab source-audio routes, `audio_url` or `init_audio_url`
currently accepts an Audio Lab output URL of the form
`/api/audio_lab/outputs/JOB_ID/FILENAME`; otherwise send base64. ACE-Step has
the broader shared-output URL support.

Diffusers inpaint currently regenerates audio and then applies a short splice
around the requested interval; it is not native latent masking. VAE
reconstruction is lossy and can shorten the waveform slightly. Judge both by
audio inspection and objective duration/difference checks.

Unload `sa`, `clap`, `vae`, or `all` with
`POST /api/audio_lab/unload`; include `worker_id` when needed. Resetting the
VAE reloads the active Stable Audio model with its default VAE. Delete the
exact worker afterward to release all process memory.

## Sound effects with two engines

Omni has two dependable local sound-effect paths. Test them independently;
they use different models, duration limits, prompts, and worker families.
ACE-Step Text2Samples is not one of the dependable paths because its official
mode adapter has not been released.

### MOSS-SoundEffect v2.0

Use `POST /api/moss/sfx`. The request accepts:

- required `prompt`
- `seconds` from 0.1 to 30, default 10
- `steps` from 1 to 200, default 100
- `cfg_scale` from 0 to 20, default 4
- `sigma_shift` from 0 to 20, default 5
- integer `seed`, optional `negative_prompt`
- `autospawn` and explicit `device` (`cuda:N` or `cpu`)

```json
{
  "prompt": "close-up dry wooden door slam in a quiet stone hallway, one impact, natural decay",
  "negative_prompt": "music, speech, repeated impacts, clipping",
  "seconds": 3.0,
  "steps": 50,
  "cfg_scale": 4.0,
  "sigma_shift": 5.0,
  "seed": 31415,
  "autospawn": true,
  "device": "cuda:1"
}
```

The response is binary 48 kHz WAV. Save the body. A successful persisted
response includes `X-Omni-Output-Path`, `X-Omni-Output-Ref`, and
`X-Omni-Output-URL`, pointing to `kind=omni` media. MOSS-SFX is estimated at
about 10-12 GB VRAM, so use the larger GPU when the smaller card lacks safe
headroom.

### Stable Audio sound effects

Use `POST /api/audio_lab/generate` with a concrete acoustic description.
Stable Audio Open Small is appropriate for quick effects at up to 11 seconds;
Stable Audio Open 1.0 supports effects up to 47 seconds and is useful for
longer ambiences. Example:

```json
{
  "prompt": "single cinematic thunder crack followed by a long natural valley echo, no music",
  "negative_prompt": "melody, speech, multiple thunder strikes, distortion",
  "duration_s": 8.0,
  "steps": 10,
  "cfg_scale": 7.0,
  "seed": 31415,
  "autospawn": false
}
```

Stable Audio persists a JSON manifest and WAV beneath its own output route.
Use Open Small for latency and iteration; use Open 1.0 for duration and richer
ambience. Do not compare seed numbers across the two engines as if they shared
a latent space.

For a meaningful comparison, hold the semantic event and approximate duration
constant, then verify actual duration, sample rate, clipping/peak, output
discoverability, worker cleanup, and subjective event accuracy. Use at least
one transient effect, one sustained ambience, and one layered mechanical or
environmental effect before declaring both paths production-ready.

## Local TTS and cross-tool audio

Generate local MOSS speech with `POST /api/tts/moss_tts`:

```json
{
  "text": "The city wakes beneath a violet sky.",
  "response_format": "wav",
  "speed": 1.0,
  "autospawn": true,
  "device": "cuda:1"
}
```

The response body is binary audio. Save it rather than trying to decode it as
JSON. Supported response-format names are `wav`, `mp3`, `ogg`, `flac`, and
`opus`; WAV is the dependable MOSS path and is required for gateway speed
retiming. `speed` ranges from 0.25 to 4.0.

Successful persisted responses include:

- `X-Omni-Output-Path`
- `X-Omni-Output-Ref`
- `X-Omni-Output-URL`

The output URL can be fed directly to ACE-Step `vocal2bgm`, `a2a`, `cover`, or
other source-audio modes. MOSS is a large model; on the tested machine it did
not fit the smaller auxiliary GPU but loaded on the larger GPU. Explicit
placement is therefore important.

Current limitation: a fresh MOSS-TTS load succeeded on the RTX 3090, but
inference failed in the upstream decoder with `CUDA driver error: unknown
error` and exhausted the worker's 1,024 WSL `dxgresource` descriptors. Omni now
retires a TTS worker automatically after any worker-side 5xx so that poisoned
CUDA/socket state is not returned to the ready pool. Do not blindly retry;
query workers and use a previously persisted fixture or another verified TTS
engine until the upstream CUDA boundary is fixed.

MiniCPM-o also has the existing `POST /api/tts/minicpm_o` contract. It accepts
WAV output only, requires `speed: 1.0`, and treats `voice` as a free-form style
description. Do not use it for production yet: the native decoder currently
fails at a model/dependency compatibility boundary (`get_mask_sizes`) after
text generation. MiniCPM text, image, and audio understanding are unaffected.

Moshi and AnyGPT require special caution. Moshi's installed loader does not
make it API-usable: the batch handler returns 501 and no full-duplex streaming
handler exists. AnyGPT's current worker implements text generation only; it
does not consume the typed speech, music, or image fields. Its cold load also
exceeded the bridge window and retained about 16 GB CPU RAM until the exact
worker was deleted. Do not autospawn either model as a capability probe.

Qwen3-Omni 30B and Nemotron Nano Omni 30B now enforce their CUDA capacity
before heavy imports or weight loading. The installed 60/62 GB variants fail
fast on a 24 GB GPU; do not bypass that blocker. Nemotron NVFP4 is configured
at about 21 GB and is the only listed 30B candidate expected to fit the 3090,
but it remains unverified. Their current shared handler supports text and image
only and returns 501 for audio/video.

For standalone model GPU pools, layer/expert sharding, CPU-offload budgets, and
no-weight worker analysis, use `docs/omni-model-api.md`. That placement system
is separate from ACE-Step and Audio Lab component workers.

## Long-form composition and outputs

List media-visible Omni audio:

```text
GET /api/outputs?kind=omni&media_kind=audio&limit=200
GET /api/outputs/PATH?kind=omni
```

The list route intentionally avoids probing every media file. Use
`GET /api/outputs/metadata/PATH?kind=omni` when exact duration, sample rate,
channel count, or dimensions are required.

Use `POST /api/outputs/audio/compose` to make a longer master from 2-64
existing library files. Each segment accepts `path`, `kind`, `start_s`,
optional `end_s`, `gain_db`, and optional `label`.

```json
{
  "segments": [
    {"path": "ace_step/FIRST_JOB/01.wav", "kind": "omni", "label": "A"},
    {"path": "ace_step/SECOND_JOB/01.wav", "kind": "omni", "label": "B"}
  ],
  "crossfade_s": 8.0,
  "sample_rate": 48000,
  "bit_depth": 24,
  "loudness_normalize": false,
  "target_lufs": -14.0,
  "true_peak_db": -1.0,
  "cpu_threads": 2,
  "title": "extended-track"
}
```

Use the exact `path` and `kind` returned by `GET /api/outputs`. `bit_depth` is
16 or 24. Composition uses triangular crossfades, optional loudness
normalization, and 1-4 CPU threads. It returns a background job; poll
`GET /api/jobs/JOB_ID`. The final WAV and manifest appear beneath
`kind=omni`, `compositions/<composition_id>/` and are visible in the media
library.

For many segments, total duration is approximately:

```text
sum(trimmed segment durations) - crossfade_s * (segment count - 1)
```

Crossfade composition preserves each source away from overlaps and is the
recommended way to assemble very long instrumentals. It does not guarantee
musical phrase alignment. Generate compatible tempo/key sections, trim on
phrase boundaries, use moderate crossfades, then listen and revise gains.

Output deletion is immediate:

```text
DELETE /api/outputs/PATH?kind=omni
```

Resolve the exact path first. Metadata routes can tag, pin, annotate, or group
results into collections without moving files.

## Devices, workers, and cleanup

Use this cleanup sequence after experiments:

1. Call the engine cancel route only if an inference is active.
2. Detach active ACE LoRAs.
3. Call the engine unload route with `component: "all"`.
4. Read `GET /api/workers` and delete only the exact audio worker IDs.
5. Recheck `GET /api/workers` and `GET /api/devices`.

Do not use `POST /api/workers/kill-all` unless every managed model worker is in
scope. Do not stop or restart ComfyUI for local audio lifecycle work; these are
separate worker families.

Keep diagnostics small and sequential. A busy host CPU can greatly inflate
load and inference wall time without changing functional correctness. Record
that contamination instead of diagnosing it as a model regression.

## Verified behavior and limitations

The dated detailed test record is
`reports/audio-music-capability-findings-2026-08-10.md`. The important current
conclusions are:

- ACE 2B turbo generated instrumental and vocal material across techno,
  synthpop, ambient, psychedelic, and chant prompts at 30, 60, and 120 seconds.
- ACE 2B base completion produced the requested longer duration but regenerated
  the nominally retained source; use composition for preservation.
- ACE cover, repaint, audio-to-audio, lyric edit, LoRAs, and MOSS-to-Vocal2BGM
  worked through public APIs.
- The raspy and acoustic multi-file LoRA packs can select exact adapter files.
- Official Lyric2Vocal and Text2Samples adapter weights remain unavailable;
  their registry entries fail clearly instead of installing nonexistent data.
- Stable Audio Open 1.0 and Open Small generation, ranked generation,
  audio-to-audio, inpaint, and VAE reconstruction worked after loader fixes.
- Earlier MOSS-TTS runs persisted media-library WAVs, but the current fresh
  inference hits the CUDA/dxgresource failure described above. Treat local
  MOSS speech generation as blocked, not production-ready.
- A two-segment composition with an 8-second overlap produced the mathematically
  expected 172-second, 48 kHz, 24-bit result.
- The current ACE 2B run added two 60-second instrumentals, one 60-second vocal
  song, two declared LoRA adapters, and a 216-second 48 kHz/24-bit four-segment
  composition. Open Small passed transient, ambience, and layered machinery;
  Open 1.0 passed its exact 47-second ceiling at 100 steps and CLAP ranked two
  candidates sequentially.
- The follow-up library wave qualified ACE Euler, Heun, and DPM++ scheduling,
  2B SFT and Turbo Continuous cores, ScragVAE, four working LoRA families, and
  a 240-second direct output. The 4B LM is not viable as a fully resident
  companion on the 24 GB card, and the installed Chinese Rap package is not a
  current PEFT adapter.
- Stable Audio community generation passed for SAO Instrumental, Audialab EDM,
  Nekochu Music, Infinite Pianos, and Vocal Textures. Tuned-100k VAE generation
  and reconstruction pass after the loader learned its declared stock Stable
  Audio 2.0 architecture; the measured reconstruction diff RMS was 0.02044.
  Unconditional generation and `larger-clap-music` scoring also pass.

Subjective claims such as vocal intelligibility, groove quality, transition
quality, and long-range coherence still require listening. Treat CLAP scores,
BPM, loudness, and waveform differences as diagnostics, not substitutes for
that review.

## Concurrency and result contracts

ACE-Step, Audio Lab, and MiniMax reject overlapping engine mutations with
409 before changing the progress record. Busy workers remain inspectable and
cancellable; no duplicate worker is spawned to service a busy engine. Audio
asset deletion requires stopping/deleting its engine workers and finishing
active installs. An unload alone leaves the worker present.

Native ACE batch generation persists every returned candidate. Legacy
diffusers batches larger than one fail explicitly. Ranked generation spools
candidates to disk, ranks unscored entries after all scored entries, and
retains an incomplete manifest if interrupted. Its `job_id` identifies stored
output, not a background JobStore task. Output ZIPs stream synchronously.
Use `POST /api/audio_lab/cancel?worker_id=ID` to cancel the selected worker.

OpenAI-compatible `/v1/audio/transcriptions` requires an explicit `model`;
select a currently supported STT model. Speech responses are converted to the
requested supported format when the worker produces WAV. Current model
qualification limits in the confidence matrix still apply.
