# Workflows and GPUs

## Requirements

Comfy queueing requires API-format graph JSON. UI-format graphs must be
exported to API format. Use:

- `GET /api/workflows/requirements`
- `GET /api/workflows/{filename}/requirements`
- `POST /api/workflows/analyze` for inline graphs and placement preview

Require `ready_to_run: true`. If placement was requested, also require
`placement_plan.valid: true`.

Plans use current free VRAM. Existing allocations owned by the selected Comfy
PID are not assumed reusable because they may be CUDA context overhead, routed
deep clones, or weights from another graph. If a warm rerun is blocked, unload
resident models and analyze again rather than overriding the plan.

## Placement request

```json
{
  "mode": "auto",
  "eligible_devices": ["GPU-...", "GPU-..."],
  "primary_device": "GPU-...",
  "reserve_mb": 1024,
  "device_reserve_mb": {},
  "overrides": {},
  "require_all": false,
  "allow_cpu": false
}
```

- `single`: all components target `primary_device`.
- `auto`: deterministic best-fit placement across eligible GPUs.
- `manual`: locks overrides first and plans the remainder.
- Only an instance explicitly started in `low` or `none` VRAM mode may plan a
  bounded CPU-offload deficit. Check `summary.estimated_cpu_offload_mb` against
  `summary.cpu_offload_budget_mb`; a larger deficit is a hard blocker. Normal
  VRAM mode always requires the component peak to fit current GPU headroom.
- Ordinary graphs also report `summary.host_weight_footprint_mb` and
  `summary.host_model_budget_mb`. The footprint counts every unique referenced
  installed weight, including LoRAs and model patches. A larger footprint is a
  hard blocker even when all GPU assignments fit; forcing it can exhaust WSL
  socket buffers while models are mapped/offloaded.
- Staged graphs also require the aggregate host-mapping check. Stage-level GPU
  unloading does not remove the host-memory admission requirement.
- Overrides are honored in auto mode too.
- `require_all` forces every eligible GPU to receive a component; leave it off
  for normal “use all available GPUs” behavior.

Use component IDs returned by analyze, such as `12:model`, `7:clip`,
`19:video_vae`, or `19:audio_vae`. UUIDs are preferred targets.

## Apply and run

- Inline: `POST /api/workflows/run` with `workflow`, `instance_id`, optional
  `client_id`, and the same `placement` object.
- Saved/parameterized: `POST /api/workflows/{filename}/run` with `params`,
  `instance_id`, and optional `placement`.
- Saved default: store `placement_policy` through
  `PUT /api/workflows/{filename}/metadata`, then use the saved run/queue path.

The server recalculates the plan and transforms a copy of the graph at run
time. Never rely on a stale client transformation.

## Execution semantics

- Fully staged LTX 2.5 graphs use `OmniLTXStageConditioning`,
  `OmniLTXStageEmptyAVLatent`, `OmniLTXStageSampler`, and
  `OmniLTXStageAVDecode`. The conditioner preserves the complete in-memory
  Gemma conditioning options; do not replace it with the upstream
  `LTXVSaveConditioning`/`LTXVLoadConditioning` pair, which drops non-mask
  options and produced colored-noise video in the 2.5 qualification run.
  The empty-audio and both decode phases can target an auxiliary GPU while
  Gemma and the diffusion transformer target the primary GPU. Use the official
  distilled sigma string exposed by the installed template for distilled
  weights.
- For LTX 2.5 reference-image/video work, chain `OmniLTXStageGuide` nodes
  between the empty AV latent and sampler. Each guide preserves native
  `LTXVAddGuide` semantics (`frame_idx`, `strength`, optional attention mask,
  and optional IC-LoRA parameters) while loading and unloading its selected
  video VAE as a distinct placement stage. Use one node for first-frame I2V,
  two for first/last guidance, or more for native multi-keyframe guidance. The
  staged sampler splits combined AV output, crops appended guide tokens only
  from video, then recombines untouched audio; do not add a second external
  crop node.
- In an instance started with physical RTX 3090 first and RTX 3060 second,
  Comfy logical `cuda:0` is the 3090 (`primary`) and logical `cuda:1` is the
  3060 (`auxiliary:1`). Always resolve this from the live placement plan;
  never persist logical indices as physical identities.
- The official `ltx-2.5-video-vae-bf16.safetensors` is the 2.5
  `NADiffusionDecoder`, not the lighter convolutional decoder. It may fall
  back to Triton/eager neighborhood attention when NATTEN is unavailable.
  `OmniLTXStageAVDecode` uses tiled video decode and unloads the video and
  audio VAEs sequentially.
- Treat final LTX 2.5 decode as a quality/speed policy. The official
  `ltx-2.5-video-vae-conv-bf16.safetensors` is the qualified fast decoder;
  the diffusion VAE retains slightly sharper fine texture. On the tested
  121-frame 1024x640 latent, cached final decode/audio/save took 60.58 seconds
  with conv VAE, `tile_size=512`, `temporal_size=64`, and video decode on the
  unloaded RTX 3090, versus 157.42 seconds for diffusion VAE t64 on the RTX
  3060. Diffusion VAE t2048 completed in 129.51 seconds on the RTX 3090 but
  OOMed the 12 GB RTX 3060. Conv t2048 was slightly slower than conv t64 on
  the same 3090. Prefer conv/t64 on the largest free staged GPU for speed;
  prefer diffusion/t64 for maximum decoder fidelity. Never copy a large
  temporal tile without analyzing resolution, frames, selected GPU, and
  current free VRAM.
- Qualification baseline: W4A8 mixed transformer, INT8 ConvRot Gemma,
  512x320, 49 frames, 24 fps, CFG 1, `euler_ancestral`, official 8-sigma
  schedule. It completed as coherent H.264 plus 48 kHz stereo AAC while using
  the 3090 for conditioning/sampling and 3060 for latent setup/decoding.
- A qualified lower-residency LTX 2.5 option uses
  `gemma4-12b-with-proj-ltx-2.5-w4a8_convrot.safetensors` and
  `ltx-2.5-22b-distilled-transformer-w4a8_convrot.safetensors` for the first
  pass. They loaded at 10065.76 MB and 11919.34 MB respectively and produced
  coherent 512x320 and 1024x640 results. For the high-quality path, keep the
  qualified tsolful W4A8 dev transformer for refinement; the smaller repo has
  no matching dev weight. The current loader prints a large unexpected
  `.comfy_quant` helper-tensor list for this exact pair, but detects native
  W4A8 operations and loads the executable weights completely. Treat that as
  a qualified exception for these exact filenames, not evidence that another
  converter's unexpected keys are safe. The saved qualified hybrid graph is
  `ltx25_hybrid_lowvram_multishot_two_stage_1024x640_121f_api.json`.
- For the qualified native two-stage x2 path, insert
  `OmniLTXStageSpatialRefine` between the first sampler and final decode. It
  stages the video VAE and latent spatial upscaler together on the selected
  auxiliary GPU, unloads them, and then stages the dev transformer on the
  selected primary GPU. The verified 512x320 to 1024x640 graph used the
  official three-step refinement schedule and kept all heavy placement staged.
  Do not bypass Comfy's tiled-decode wrapper when creating the refinement
  guide; raw NADiffusionDecoder output is five-dimensional and must be
  normalized to the IMAGE batch expected by guide re-encoding.
- `OmniLTXStageDurationPredictor` accepts the lossless positive conditioning,
  a transformer, and a duration head. It loads the transformer and head on one
  selected GPU, predicts the raw seconds, snaps the requested output to LTX's
  valid `8k+1` frame grid within the supplied bounds, and unloads both. Treat
  the returned seconds as diagnostic and use the returned integer for latent
  length. The head belongs in `models/model_patches`.
- Native multi-shot is prompt-driven in LTX 2.5: describe a chronological shot
  list and explicit cuts in a single literal prompt. Auto-duration and
  multi-shot are separate capabilities; predicting a longer duration does not
  guarantee the model will produce every requested cut, so visually inspect
  shot transitions and identity continuity.
- Qualified multi-shot reference: 121 frames at 24 fps and 512x320 using the
  W4A8 distilled transformer produced a coherent three-composition 5.04-second
  clip with synchronized 5.01-second AAC. Use at least one frame from each
  requested shot plus the endpoint for review; a valid media container alone
  does not prove that cuts or identity continuity worked.
- The custom extension's prompt enhancer currently loads separate Hugging Face
  LLM/captioner repositories. Do not let it download outside Omni's managed
  inventory during a controlled qualification. The official Python
  `res_2s` HQ sampler is also not exposed by the current live Comfy node set;
  do not claim the verified Euler two-stage graph tested that HQ variant.
- LTX 2.5 DFR is a distinct detail-fidelity pipeline with keyframe/detailing
  passes and optional temporal refine; it is not a base-sampler boolean.
  The optional pixel-spatial detailing IC-LoRA is separately gated from the
  base LTX-2.5 repository. A successful base-model download does not prove
  access to that repository.
- Staged H3 nodes directly set text, diffusion, video-VAE, and audio-VAE
  devices. `OmniH3StageFL2VConditioning` can put ClipProj and reference VAE
  encoding on different selected GPUs. They return CPU intermediates and
  unload each heavy stage.
- `OmniH3ReferenceStrength` changes native visual/audio conditioning without
  loading a model. `OmniH3StageSampler` can optionally apply the installed
  Spectrum wrapper; keep it default-off unless the user requests approximate
  acceleration and use RAM-backed history/archive for the first comparison.
- Official H3 latents are a NestedTensor pair (video, audio). Third-party
  latent upscalers and LTX AV split/concat nodes do not understand that
  packing. Use `OmniH3SplitAVLatent` before a video-only upscaler and
  `OmniH3JoinAVLatent` before native H3 decode. Do not send H3 latents
  through `LTXVSeparateAVLatent` / `LTXVConcatAVLatent`.
- `OmniStageVAEDecode` loads and releases its own named VAE. It does not unload
  upstream ordinary models or encoders; their residency overlaps decode and
  the planner must include that overlap in its peak estimate.
- Ordinary loader graphs receive or update OmniRoute MODEL/CLIP/VAE nodes.
  Components may remain resident together; heed the overlap warning.
- Component placement is not tensor/layer sharding of one model.
- If an ordinary loader cannot safely reload on another GPU, the Omni node
  raises an error instead of silently running elsewhere.

## Verify

1. Record `prompt_id` and `instance_id`.
2. Check queue/history through the instance API or UI.
3. Wait for completion/error, not merely queue acceptance.
4. Verify expected output files/media metadata.
5. Confirm models unload when the workflow promises staged unloading.

For API wiring tests, analyze only and confirm the queue stays empty.
