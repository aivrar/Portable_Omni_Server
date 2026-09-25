# Stable Audio Tools inventory vs Omni Audio Lab

Date: 2026-08-18  
Workspace: `E:\linux\template\app\apps\Omni_Studio`  
Live package: `/opt/omni_studio/venv/lib/python3.12/site-packages/stable_audio_tools`  
Official repo: [Stability-AI/stable-audio-tools](https://github.com/Stability-AI/stable-audio-tools)

This report is the SAT **tool kit** (scripts, inference functions, conditioners, VAE, LoRA, training), mapped onto what Omni Audio Lab actually calls. It is not a generate-slider checklist. That knob audit is in the same conversation as this file.

**Scope of this install.** Audio Lab is Stable Audio **Open** (Open 1.0 + Open Small + community SAT/Diffusers checkpoints). It is not commercial Stable Audio 2.0 / 2.5 / 3.

**Venv vs GitHub `main`.** The wheel in this venv is slightly older than current GitHub `main`. It has `generate_diffusion_cond_inpaint` and `build_mask`. It does **not** ship `inference/inversion.py` or `models/lora/`. Newer SAT tools (RF-Inversion, official LoRA package) are documented upstream but are not in this environment.

---

## 1. Official SAT command-line tools

These are the programs in the SAT repo root / `scripts/`.

| Tool | Job | In this venv | Omni Audio Lab |
|---|---|---|---|
| `run_gradio.py` | Load a SAT checkpoint and run the official inference UI | Library present, no launcher | No. Audio Lab is a thin substitute. |
| `run_control_gradio.py` | Control demo: audio conditioners, envelopes, chords, stem mix | Not in this venv | No |
| `train.py` | Train / fine-tune (Lightning, multi-GPU, W&B) | `training/` installed | No |
| `unwrap_model.py` | Strip Lightning wrapper so a ckpt is inference-sized | Not exposed | No |
| `pre_encode.py` | Encode a dataset to `.npy` latents for faster training | Not exposed | No |
| `scripts/ds_zero_to_pl_ckpt.py` | DeepSpeed ZeRO → Lightning checkpoint | Not exposed | No |

Training, unwrap, and pre-encode are first-class SAT tools if you fine-tune. Omni is inference-only. That is a product choice, not a missing generate knob.

---

## 2. Inference tools (library API)

Installed module: `stable_audio_tools.inference.generation`.

Confirmed in this venv:

```
generate_diffusion_cond
generate_diffusion_uncond
generate_diffusion_cond_inpaint
build_mask
prepare_audio
sample / sample_k / sample_rf
```

No `invert_audio` in this venv.

| Function | Job | Omni calls it? |
|---|---|---|
| `generate_diffusion_cond` | Text (and other cond) → audio; also init-audio variation | **Yes** — Generate, A2A, Uncond (empty prompt), and the native “inpaint” path |
| `generate_diffusion_uncond` | No conditioner | **No.** Uncond still goes through `generate_diffusion_cond` with `prompt=""` |
| `generate_diffusion_cond_inpaint` | Real latent inpaint (`inpaint_audio` + `inpaint_mask`) | **No.** Official inpaint tool. Unused. |
| `build_mask` | Soft mask from **percent** start/end + softness L/R + marination | **Shipped, unused.** Omni builds `mask_args` with **seconds** and passes them into `generate_diffusion_cond`, which does not call `build_mask` in this venv. |
| `sample` / `sample_k` / `sample_rf` | v-diffusion vs k-diffusion vs rectified-flow | Indirectly, via `generate_diffusion_cond` |
| `prepare_audio` | Resample / pad / channel-match init audio | Indirectly |
| `invert_audio` (newer SAT) | RF-Inversion | Not in this venv |

Of SAT’s three generate tools, Omni uses **one**. Diffusers Open 1.0 inpaint is a homemade regenerate + crossfade splice (`_splice_inpaint`). Native inpaint does not call `generate_diffusion_cond_inpaint`.

`generate_diffusion_cond` signature in this venv:

```
steps, cfg_scale, conditioning, negative_conditioning, batch_size,
sample_size, sample_rate, seed, device, init_audio, init_noise_level,
return_latents, **sampler_kwargs
```

Newer GitHub `main` also documents `inversion_params`, `apg_scale`, `dist_shift`, duration-adapt flags. Those are not named parameters here; some may only exist if forwarded as `**sampler_kwargs`.

---

## 3. Conditioner tools

SAT conditioners turn metadata into tensors. Official docs (`docs/conditioning.md`) list these types.

### Text

| Type | Role |
|---|---|
| `t5` | Frozen T5 prompt encoder (Open 1.0) |
| `t5gemma` | T5Gemma prompt encoder |
| `causal_lm` | Causal LM (e.g. Gemma-2) prompt encoder |
| `clap_text` | LAION CLAP text encoder (sequence or single embedding) |
| `sat_clap_text` | SAT’s own CLAP text wrapper |
| `phoneme` | English phonemes via `g2p_en` (speech / singing) |
| `lut` | Lightweight tokenizer lookup table |

### Numbers / labels

| Type | Role |
|---|---|
| `int` | Discrete embedding. Open 1.0 uses this for **`seconds_start`**. |
| `number` | Fourier embedding of a float. Open 1.0 uses this for **`seconds_total`**. |
| `list` | Fixed string set (e.g. genre) |

### Audio

| Type | Role |
|---|---|
| `clap_audio` | CLAP audio encoder → one multimodal embedding |
| `sat_clap_audio` | SAT CLAP audio wrapper |
| `pretransform` | Encode a reference through the VAE for latent conditioning |
| `source_mix` | Mix projected stems (vocals, drums, bass, …) |

Open 1.0 / Small only require `prompt` + `seconds_start` + `seconds_total`.

**Omni:** always sends `seconds_start: 0` and `seconds_total: duration`. No API/UI for start offset. No path to drive CLAP-audio, phoneme, stem-mix, or pretransform conditioners even if a control fine-tune is loaded.

Registry entry `fundwotsai-control` (uninstalled) is the kind of model `run_control_gradio.py` + `source_mix` / `pretransform` exist for. Audio Lab cannot drive those conditioners.

Omni CLAP (`larger-clap-music`, `larger-clap-general`) is a **separate scoring worker**. It is not SAT’s `clap_text` / `clap_audio` conditioner.

---

## 4. Autoencoder / VAE tools

SAT autoencoders are their own subsystem (`docs/autoencoders.md`, `docs/pretransforms.md`, `docs/pre_encoding.md`).

Architectures: Oobleck, DAC, SEANet.  
Bottlenecks: VAE, tanh, Wasserstein, L2, RVQ, DAC-RVQ.  
Also: chunked encode/decode, latent noise, n_quantizers, `pre_encode.py`.

| SAT tool | Omni |
|---|---|
| Encode waveform → latent | `POST /api/audio_lab/vae/encode` (download `.pt`) |
| Decode latent → waveform | `POST /api/audio_lab/vae/decode` |
| Reconstruct (encode+decode) | `POST /api/audio_lab/vae/reconstruct` |
| Swap pretransform / tuned decoder | Load `sao-vae-tuned-100k` (installed) |
| Latent noise slider | No |
| n_quantizers (RVQ) | No |
| Chunked encode (`chunked`, `overlap`, `chunk_size`) | No |
| `pre_encode.py` dataset encoder | No |
| Train a VAE | No |

VAE Lab is the three inference calls on the **loaded SA VAE**, not the training/pre-encode tools.

---

## 5. LoRA tools

Official SAT (current docs + Gradio) treats LoRA as a first-class tool:

- Train LoRA / DoRA / LoRA-XS (`docs/lora.md`)
- `run_gradio.py --lora-ckpt-path a.safetensors b.safetensors`
- Per-LoRA strength, interval (when in the noise schedule it is on), layer filter
- `set_lora_strength`, merge LoRAs into the base, convert `.ckpt` → `.safetensors`

**This venv has no `stable_audio_tools.models.lora` package.**  
Omni has `sa_lora_stack` on Audio Lab worker state and never fills it. There is no Audio Lab LoRA install/attach API.

SAT LoRA is absent twice: not in the installed wheel’s public module tree, and not in Omni.

---

## 6. Training tools

SAT `training/`: diffusion wrapper, autoencoder wrapper, ARC (adversarial), LM wrapper, losses (auraloss, semantic), datasets (local folder, S3 WebDataset, pre-encoded).

Optimizers/schedulers: `docs/optims.md`. Fine-tune flags on `train.py`: `--ckpt-path`, `--pretrained-ckpt-path`, `--pretransform-ckpt-path`.

Omni does none of this. You can **load** community fine-tunes someone else trained (pianos, EDM, instrumental, Nekochu, …). You cannot run SAT’s training tools from Audio Lab.

---

## 7. Model-type tools vs what you load

| SAT `model_type` | Official tool path | This install |
|---|---|---|
| `diffusion_cond` | `generate_diffusion_cond` + Gradio Generation | Open 1.0 (Diffusers), Small, most community ckpts |
| `diffusion_cond_inpaint` | `generate_diffusion_cond_inpaint` + Inpaint accordion | No checkpoint. Function unused. |
| `diffusion_uncond` | `generate_diffusion_uncond` | Function unused |
| `autoencoder` / `diffusion_autoencoder` | Gradio Process + `pre_encode` | Only encode/decode/reconstruct on the loaded SA VAE |
| `lm` | Gradio temp / top-p / top-k | Unused |
| `clap` (wrapper type) | Conditioner / SAT CLAP | Separate LAION CLAP worker for scoring only |

Installed Open models (live `GET /api/audio_lab/status`, 2026-08-18):

- Official: `sao-open-1.0` (Diffusers, ≤47 s), `sao-open-small` (native, ≤11 s)
- Community on disk: Foundation-1 Diffusers, RC Infinite Pianos, RC Vocal Textures, SAO Instrumental, Audialab EDM, Nekochu Music
- VAE: default + `sao-vae-tuned-100k`
- CLAP: `larger-clap-general`, `larger-clap-music`

---

## 8. What Omni actually uses

Audio Lab calls **one SAT inference tool** (`generate_diffusion_cond`) plus:

- a homemade Diffusers splice for Open 1.0 inpaint
- a homemade VAE shim for encode/decode/reconstruct
- a separate LAION CLAP worker for ranked scoring

SAT tools **not** used, even though they are what the library is:

1. `generate_diffusion_cond_inpaint` — real inpaint  
2. `generate_diffusion_uncond` — real uncond  
3. `build_mask` — official soft-mask (percent + softness + marination)  
4. Conditioner suite beyond prompt + duration, especially **`seconds_start`**  
5. LoRA load / apply / strength  
6. `run_gradio` / `run_control_gradio`  
7. `train` / `unwrap` / `pre_encode`  
8. RF-Inversion (newer SAT; not in this venv)

---

## 9. If Omni should “have the SAT tools”

Inference first, on this box:

1. Call `generate_diffusion_cond_inpaint` for native inpaint (or stop calling the unused `mask_args` path).  
2. Expose `seconds_start` (and keep `seconds_total` independent of clip length if wanted).  
3. Add SAT LoRA load — requires a newer wheel than this venv, or a backport of `models/lora`.  
4. Optionally call `generate_diffusion_uncond` for the Uncond tab.

Training tools (`train`, `unwrap`, `pre_encode`, control Gradio) are a different product surface.

Related knob/UI gaps (Generate requires CLAP, step slider min 10 vs Small’s 8, init URL only Audio Lab outputs) are in the conversation audit from the same day; this file is the tool inventory only.

---

## 10. Source map

| Path | Role |
|---|---|
| `server/audio_lab_loaders.py` | Native `generate_diffusion_cond`, Diffusers splice inpaint, VAE shim, empty `sa_lora_stack` |
| `server/routers/audio_lab.py` | Gateway: generate, ranked, a2a, inpaint, uncond, VAE, score, jobs |
| `server/static/tab-audio-lab.js` | Audio Lab UI (Generate / A2A / Inpaint / Uncond / VAE / Score) |
| `cli/audio_lab_commands.py` | CLI mirrors the API |
| `server/config.py` | `STABLE_AUDIO_MODELS`, VAEs, CLAPs, sampler families |
| Official docs (upstream) | `docs/conditioning.md`, `docs/lora.md`, `docs/diffusion.md`, `docs/autoencoders.md`, `docs/pre_encoding.md`, `docs/pretransforms.md`, `docs/datasets.md`, `docs/optims.md` |
