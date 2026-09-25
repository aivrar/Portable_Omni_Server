# Omni Future Enhancers

## Current assessment

Omni Studio currently has partial audio and video post-processing, but it does
not yet provide a complete mastering, restoration, or finished-media
enhancement system.

The recommended design is hybrid:

1. Implement dependable, model-free media processing directly in Omni's API.
2. Use ComfyUI workflows for GPU-based AI enhancement and restoration.
3. Keep the ComfyUI operations callable through Omni's existing workflow API
   using saved, tested API-format workflows.

This means enhancement should not depend entirely on ComfyUI.

## Native Omni audio capabilities available now

Omni's audio composer already runs independently of ComfyUI. It can:

- Trim source segments.
- Apply per-segment gain from -48 dB to +24 dB.
- Convert inputs to stereo.
- Resample from 8 kHz through 192 kHz.
- Produce 16-bit or 24-bit PCM WAV output.
- Crossfade between 2-64 segments.
- Optionally normalize loudness to a target between -30 and -5 LUFS.
- Apply a configurable true-peak ceiling between -9 and 0 dBTP as part of
  loudness normalization.
- Limit FFmpeg processing to 1-4 CPU threads.

The main limitation is that the composer requires at least two segments. It is
therefore not a convenient general-purpose processor for one finished clip.

Additional existing audio capabilities include:

- ACE-Step audio-to-audio transformation.
- ACE-Step repainting of a selected interval.
- ACE-Step remix and lyrics editing.
- ACE-Step cover and style transformation.
- ACE-Step generated extension or continuation.
- ACE-Step vocal-to-accompaniment generation.
- Stable Audio audio-to-audio transformation.
- Stable Audio interval inpainting.
- Stable Audio VAE reconstruction.
- ACE-Step analysis of BPM, key, loudness, duration, and related facts.
- FFmpeg tempo adjustment for generated TTS.

ACE-Step and Stable Audio transformations are generative. They can alter the
composition, voice, timbre, and retained source material. They should not be
presented as lossless cleanup or conventional mastering.

Loudness normalization may improve the apparent volume of a generated song,
but it will not repair poor composition, muddy instrumentation, malformed
vocals, or a loss of musical coherence.

## Missing native audio enhancement

Omni does not currently expose a straightforward single-clip processing API
for:

- Loudness normalization.
- Simple gain or volume boosting.
- True-peak limiting.
- Dynamic-range compression.
- Parametric or graphic EQ.
- High-pass and low-pass filtering.
- Noise reduction.
- Dereverberation.
- Declipping.
- De-essing.
- Stem separation or vocal isolation.
- Pitch correction.
- General-purpose time stretching.
- Audio extraction from a finished video.
- Replacing or remuxing a video's audio track.
- A repeatable mastering chain.

The clearest missing fundamental capability is a native single-input media
processing facility. It should operate without ComfyUI, without loading a
model, and without requiring a GPU.

A useful native audio processor should support:

- One exact media-library input.
- Trim and fades.
- Gain adjustment.
- Target-LUFS normalization.
- True-peak limiting.
- Compression.
- EQ and high-pass/low-pass filtering.
- Resampling and channel conversion.
- Bit-depth selection.
- Optional conservative FFmpeg noise reduction.
- Bounded CPU thread usage.
- A persisted output WAV and manifest containing every applied setting.

## Native video and media-processing gaps

Omni does not currently expose a general native finished-video enhancement
facility. Useful model-free operations would include:

- Extracting an audio track from a video.
- Replacing or adding a video's audio track.
- Remuxing without unnecessary video re-encoding.
- Audio loudness normalization during remuxing.
- Frame-rate conversion.
- Resolution scaling with conventional FFmpeg filters.
- Basic sharpening, denoising, color adjustment, and stabilization where the
  installed FFmpeg build supports them.
- Persisted output and processing manifests in the media library.

These operations belong in native Omni processing because they are dependable,
do not need model weights, and should not require ComfyUI to be running.

## Video enhancement currently available through ComfyUI

The strongest qualified video quality path is LTX-2.5 two-stage generation:

1. Generate a 512x320 first pass.
2. Apply the official latent spatial x2 upscaler.
3. Refine it with the development transformer.
4. Decode the result at 1024x640.

Both LTX-2.5 spatial and temporal x2 latent-upscaler weights are installed.
The spatial two-stage path has been successfully qualified. The temporal asset
is present, but it does not yet have the same verified general post-processing
workflow coverage.

Saved LTX-2.5 material also includes generative workflows or upstream
templates for:

- Video-to-video transformation.
- Video inpainting.
- Video outpainting.
- Motion tracking and guidance.
- First-frame and first/last-frame guidance.
- Multi-keyframe guidance.
- Detail refinement and two-stage generation.

These are generative transformations rather than conventional restoration
filters. They may modify identity, motion, details, composition, or timing.

## Video enhancement not currently installed or qualified

The live installation did not contain qualified general-purpose assets or
workflows for:

- Real-ESRGAN or UltraSharp pixel upscaling.
- SeedVR2 video restoration and upscaling.
- RIFE or FILM frame interpolation.
- General video denoising, deblocking, or deblurring.
- Video stabilization.
- Deflickering.
- Color restoration or matching.
- Face restoration.
- A native finished-video enhancement endpoint.

The installed-model search found the two LTX-2.5 latent upscalers but no
installed Real-ESRGAN, UltraSharp, RIFE, or general frame-interpolation model.
The installed custom-node list also did not show a dedicated general video
upscaler, frame-interpolation suite, or audio-restoration extension.

## Available ComfyUI enhancement candidates

ComfyUI Manager's catalog exposes several promising extensions. Discovery in
the catalog means they are available to investigate; it does not mean they are
installed, compatible, safe, or qualified in Omni.

### Audio candidates

#### Egregora Audio Super-Resolution

The catalog describes capabilities including:

- FlashSR audio super-resolution.
- Fat Llama spectral enhancement on GPU or CPU.
- DeepFilterNet denoising.
- RNNoise denoising.
- WPE dereverberation.
- BS.1770 loudness measurement and gain matching.
- High-quality resampling.
- Alignment, null testing, plotting, and objective comparison metrics.

This appears to be the broadest candidate for music enhancement and objective
A/B testing.

#### ClearVoice and Resemble Enhance

Catalog candidates include ClearVoice, Resemble Enhance, and VoiceFixer-based
processing. These are primarily appropriate for:

- Speech cleanup.
- Speech denoising.
- Speech restoration.
- Speech super-resolution.

They should not automatically be assumed suitable for complete mixed music.

#### Audio Quality Enhancer

The Manager catalog also contains a general audio-quality enhancer extension.
Its exact processing quality, dependencies, model behavior, and API
repeatability remain unqualified.

### Video candidates

#### SeedVR2

SeedVR2 is a promising AI video restoration and upscaling candidate. It would
need installation, model discovery, API-format workflow construction, memory
analysis, and controlled quality/speed qualification.

#### Real-ESRGAN and conventional image upscalers

Frame-by-frame Real-ESRGAN or similar pixel upscaling can provide a simpler
baseline than a large generative video-restoration model. Temporal consistency
must be checked because independent frame enhancement can produce flicker.

#### RIFE, FILM, and other frame interpolation

The catalog exposes multiple interpolation implementations, including RIFE,
FILM, GIMM-VFI, and TensorRT-oriented variants. These could increase frame
rate and perceived smoothness, but must be checked for:

- Motion warping.
- Duplicate or malformed frames.
- Scene-cut behavior.
- Audio duration synchronization.
- VRAM use and processing speed.

#### Wavelet Color Fix

Wavelet color correction is a useful candidate for restoring color after
generative processing, frame interpolation, or upscaling. It should be tested
as a finishing stage rather than assumed to improve every clip.

## Recommended architecture

### Native Omni processing

Use native Omni routes for predictable media operations:

- Normalize and boost audio.
- Apply limiting, compression, EQ, and filtering.
- Trim, fade, resample, and convert formats.
- Extract or replace video audio.
- Remux media.
- Apply safe conventional FFmpeg video filters.
- Write complete processing manifests.

These operations should remain usable while ComfyUI is stopped.

### ComfyUI AI enhancement

Use ComfyUI for model-driven processing:

- Audio super-resolution and learned restoration.
- Speech restoration.
- AI video restoration and upscaling.
- Frame interpolation.
- Face restoration.
- Generative color/detail refinement.
- Video-to-video correction or regeneration.

Each supported operation should have a saved, tested API-format workflow and
should be run through Omni's existing workflow analyze/run lifecycle. A
catalog listing or UI-format example is not sufficient proof that a capability
works.

## Qualification requirements

Before advertising an enhancer as supported:

1. Resolve its exact extension and model dependencies through the API.
2. Install only managed assets into the correct Comfy tree.
3. Convert or construct an API-format workflow.
4. Analyze placement without loading weights.
5. Respect any `valid: false` or readiness blocker.
6. Run a short representative clip first.
7. Compare the original and processed media by listening or visual inspection.
8. Record duration, sample rate, channels, resolution, frame rate, codec, file
   size, processing time, VRAM use, and CPU impact.
9. Check synchronization and temporal artifacts for video.
10. Persist the output beside a readable manifest.
11. Unload models and delete the exact worker when residency is not wanted.

## Priority recommendation

The recommended implementation order is:

1. Native single-clip audio normalization, gain, limiting, and resampling.
2. Native video audio extraction, replacement, normalization, and remuxing.
3. Native compression, EQ, filtering, fades, and conservative denoising.
4. A qualified music-oriented AI audio restoration workflow.
5. A qualified speech-restoration workflow.
6. A conventional Real-ESRGAN-style video-upscale baseline.
7. SeedVR2 or another temporally aware video-restoration workflow.
8. A qualified RIFE/FILM interpolation workflow.
9. Color correction and optional face-restoration finishing stages.

This sequence provides useful everyday enhancement first while retaining
ComfyUI for operations that materially benefit from learned GPU models.
