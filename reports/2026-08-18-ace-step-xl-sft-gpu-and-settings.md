# ACE-Step XL SFT campaign: GPU layout, working settings, and UI

Date: 2026-08-18  
Workspace: `E:\linux\template\app\apps\Omni_Studio`  
Live distro: WSL `linbox-Omni_Studio`, `/opt/omni_studio`  
Local API: `http://127.0.0.1:9200` (bridge) → gateway `:8200`  
Constraint: ACE-Step only. Turbo was not used unless asked. No C: scratch; work files live under `cache/agent-work/omni-techno/`.

This is a handoff for the ACE-Step 1.5 XL SFT work that ran across the techno / official-params / dual-GPU / UI sessions. It is meant so a later session can load the same stack, use the same generation recipe, and not repeat the settings that garbled SFT.

Evidence terms:

- **Verified:** a live generate or load completed and the result was inspected (listen, VRAM, or API state).
- **Implemented:** source in this tree (and copied to `/opt` when the live gateway was used).
- **Found, not used:** discovered on Hugging Face or disk, not attached for a listen take.
- **Do not repeat:** a live path that produced unusable audio or an OOM.

---

## 1. Hardware and device map

Two NVIDIA GPUs, as seen from WSL:

| Host id | Card | VRAM |
|---|---|---|
| `cuda:0` | RTX 3060 | 12 GB (12287–12288 MiB) |
| `cuda:1` | RTX 3090 | 24 GB (24575–24576 MiB) |

An ACE worker spawned on a single GPU remaps that GPU to `cuda:0` inside the process (`gpu_device_map`). Logs that say “loaded on cuda:0” while `GET /api/workers` shows `device: cuda:1` mean the 3090. Do not assume the worker’s internal `cuda:0` is the 3060.

Windows RAM on this box is ~64 GB. WSL is given ~32 GB. That is separate from GPU VRAM.

---

## 2. What ACE-Step 1.5 actually is here

ACE-Step 1.5 is a hybrid, not a single diffusion graph:

1. **5 Hz LM (planner)** — Qwen3-based. Turns prompt + lyrics into semantic audio codes when Thinking is on.
2. **DiT renderer** — denoises those codes into 48 kHz stereo.
3. **Shared 1D VAE** — latent ↔ waveform.
4. **Optional CLAP** — only used by `POST /api/ace_step/generate-ranked` to score N takes. A single `generate` does not call CLAP.

Official DiT family used in this campaign:

| Omni id | Upstream | Role |
|---|---|---|
| `ace-xl-sft` | `ACE-Step/acestep-v15-xl-sft` | **Best quality renderer.** ~4B DiT, CFG, 50-step ODE. |
| `ace-xl-base` | `ACE-Step/acestep-v15-xl-base` | Training/base. Needed for extract / lego / complete. Not the listen default. |
| `ace-xl-turbo` | `ACE-Step/acestep-v15-xl-turbo` | 8-step, CFG ~1, shift 3. Installed. **Not used** for the good SFT takes. |

Official planners:

| Omni id | Size | VRAM (weights, bf16) |
|---|---|---|
| `ace-lm-0.6b` | 0.6B | ~2 GB |
| `ace-lm-1.7b` | 1.7B | ~4 GB |
| `ace-lm-4b` | 4B | ~9 GB |

**Best quality pairing that was run:** XL SFT DiT + 4B planner.  
**Longest pairing that was run:** XL SFT DiT + 1.7B planner (7:00).

ACE official surface is two handlers (DiT + LM), not one Comfy-style multi-GPU graph. Omni therefore places them as **components**, not Hugging Face layer shards. That is `COMPONENT_SPLIT_MODELS = {"ace_step"}` in `server/omni_placement.py`.

---

## 3. GPU layouts that were tried

### 3.1 Final layout the user asked to keep (verified)

Both ACE pieces on the 3090. CLAP alone on the 3060.

| Worker | Model | Host device | Typical occupancy |
|---|---|---|---|
| `ace_step-1` | XL SFT DiT + 4B LM | `cuda:1` (3090) | ~20.3 / 24 GB (~5 GB free) |
| `audio_lab-1` | `larger-clap-music` | `cuda:0` (3060) | ~1.9 / 12 GB |

Load bodies used:

`cache/agent-work/omni-techno/load-xl-sft-4b-3090.json`

```json
{
  "model_variant": "ace-xl-sft",
  "lm_variant": "ace-lm-4b",
  "bf16": true,
  "cpu_offload": false,
  "int8": false,
  "torch_compile": false,
  "device": "cuda:1"
}
```

`cache/agent-work/omni-techno/load-clap-3060.json`

```json
{
  "clap_variant": "larger-clap-music",
  "device": "cuda:0"
}
```

When the ACE worker is spawned only on `cuda:1`, WSL shows that worker a single GPU. The LM therefore stays on the same card. That is how both ACE models sit on the 3090 without passing `lm_device`.

This is the layout to restore after a restart:

1. `POST /api/ace_step/load` with the 3090 JSON above.
2. `POST /api/audio_lab/load` with the CLAP JSON.

### 3.2 Dual-GPU ACE split (implemented, then taken down)

The user asked to use both GPUs for the “best” ACE setup, like a Comfy multi-GPU graph. Omni did not already do that for ACE, so the following was implemented:

- `server/omni_placement.py` — `ace_step` is a **component split**, not an HF shard.
- `server/routers/ace_step.py` — `_ace_split_placement()` and `_ensure_ace_step_worker(..., lm_device=...)`. If `lm_device != device`, the worker is spawned with a two-GPU pool.
- `server/ace_step_loaders.py`:
  - `_resolve_lm_runtime_device()` — if the worker sees two CUDA devices and `lm_device` is omitted, default the LM to the worker’s `cuda:1`.
  - `_DirectDeviceLLMHandler` — load the 4B planner in **bf16 first**, then `.to(device)`. Upstream ACE did `from_pretrained()` in FP32 then `.to(device).to(dtype)`. That transient FP32 copy is ~16 GB and will not fit a 12 GB 3060.

Tried live: DiT on 3090, 4B LM on 3060 (`load-xl-sft-4b-split.json`, `device: cuda:1`, `lm_device: cuda:0`).

That load is **possible** if CLAP is **not** on the 3060. The 4B planner is ~9–10 GB; CLAP is ~2 GB; together they do not leave a safe 12 GB card.

The user then saw ~9.3 GB on the 3060 and ~11 GB on the 3090 and asked to **put both ACE models back on the 3090 and CLAP on the 3060**. Dual-GPU ACE spawn remains in the code for later. It is not the current operating layout.

### 3.3 Layouts that failed or must not be used blindly

| Attempt | Result |
|---|---|
| 4B planner onto the 3060 using stock ACE `.to(cuda).to(bf16)` | Failed. Transient FP32 ~16 GB; “device not ready”. Fixed by `_DirectDeviceLLMHandler`. |
| 4B planner + CLAP both on the 3060 | Does not fit. ~10 GB + ~2 GB on 12 GB. |
| 480 s XL SFT + 1.7B, CFG on | VRAM preflight failed near 8.5 vs ~8.3 GB free. GPU looked ~63% full because **weights** occupy that; the rest is the denoise workbench. |
| 240 s XL SFT + 4B colocated | Failed preflight (need ~4.5 GB free, had ~4.2). |
| 210 s XL SFT + 4B | Failed on a `4.0 == 4.0` float compare at the edge. |
| 200 s (3:20) XL SFT + 4B ranked × 5 | **Succeeded.** Practical max for that stack. |

VRAM leftover at 63% is not unused headroom you can “fill” with a bigger model. It is activation / CFG / duration workspace.

---

## 4. Precision — already half precision

Native ACE on Ampere (3060 and 3090) loads **bfloat16** by itself. Official `AceStepHandler.initialize_service` picks:

- CUDA Ampere+ → `torch.bfloat16`
- Older CUDA → `float16`
- CPU → `float32`

The 4B LM is also forced to `torch.bfloat16` in `_DirectDeviceLLMHandler` so it never parks a full FP32 copy on the GPU.

**FP16 would not free VRAM.** Both formats are 2 bytes per weight. The Omni Load checkbox “BF16” is real for the Diffusers port. For native XL SFT, ACE ignores that flag and still uses bf16 on these cards.

Smaller than bf16:

| Option | On Load… | Effect |
|---|---|---|
| Dynamic INT8 | Yes | Roughly half the weight VRAM again. Needs `torchao`. Slightly lower fidelity. Not used on the good SFT takes. |
| CPU offload | Yes | Parks idle weights in system RAM. Slower. Not used. |

CPU offload was **not** why Windows RAM looked full. See §10.

---

## 5. Best ACE generation settings that were actually run

This is the recipe that stopped the garble and produced listenable XL SFT. Treat it as the default unless the user asks otherwise.

| Knob | Working SFT value | Why |
|---|---|---|
| DiT | `ace-xl-sft` | Official top local quality model. |
| Planner | `ace-lm-4b` when duration ≤ ~3:20; `ace-lm-1.7b` for 4–7 min | 4B is the better planner; 1.7B frees workbench. |
| Thinking | **on** | Needed for 5 Hz codes. Off = no planner codes. |
| `vocal_language` | **`en`** | Omit → ACE tags “# Languages unknown” and vocals collapse. |
| Steps | **50** | Official SFT. |
| CFG | **7.0** | Official SFT. Registry `cfg_default` for XL SFT/base was raised from 4.0 to 7.0 in `server/config.py`. |
| Shift | **1.0** | Official SFT/base. Turbo uses 3.0. |
| Infer method | **`ode`** | Official. `sde` garbled SFT. |
| Sampler | **`euler`** | Official. Heun was tried and hated. |
| ADG | **off** | Official default false. |
| DCW | **off for SFT** | Official Gradio: DCW on for Turbo, off for SFT/base. Python `GenerationParams.dcw_enabled` defaults **True**, so Omni used to run SFT with DCW unless overridden. That was a root cause of garble. Loaders now default DCW off unless the model is Turbo. |
| CoT caption rewrite | **off** | Official Gradio default off. On rewrote the caption and garbled SFT. |
| CoT metadata rewrite | **off** | Official Gradio default on. Left off so typed BPM/key/meter stay. |
| CoT language rewrite | **off** | Official Gradio default on. Left off so `vocal_language=en` stays. |
| CoT lyrics rewrite | **off** | Official default off. |
| Constrained decoding | on (UI default) | Official LM grammar. |
| LM temperature / CFG / top-k / top-p | 0.85 / 2.0 / 0 / 0.9 | Official GenerationParams. |
| Scheduler mapping | `scheduler` → ACE `sampler_mode` | DPM++ is forwarded, not remapped to Euler. |
| Seed | same seed = same song | ACE seeds **both** the LM codes and the DiT. Unlike Stable Audio, same seed is not a variation knob. Use ranked N or change the seed. |
| CLAP | ranked generate only | `generate` scores nothing. `generate-ranked` makes N sequential takes, scores each, marks best. |

Canonical request (hypnotic minimal techno, 60 s, listen take 34):

```json
{
  "duration_s": 60,
  "steps": 50,
  "cfg_scale": 7.0,
  "scheduler": "euler",
  "shift": 1.0,
  "infer_method": "ode",
  "use_adg": false,
  "dcw_enabled": false,
  "thinking": true,
  "use_cot_caption": false,
  "use_cot_metas": false,
  "use_cot_language": false,
  "vocal_language": "en",
  "bpm": 126,
  "keyscale": "A minor",
  "timesignature": "4",
  "bf16": true,
  "overlapped_decode": true
}
```

That same knob set was reused for the 7:00 take (duration 420, 1.7B planner) and the RUN DMC ranked set (duration 200, 4B planner, `n: 5`).

### 5.1 What twisted the sound before that recipe

In order:

1. Gateway `extra=ignore` dropped new fields (`thinking`, `infer_method`, `output_name`) until a gateway restart. Worker defaults still applied some of them.
2. DCW left on for SFT (Python default True vs Gradio off).
3. `vocal_language` omitted → unknown language header.
4. CoT caption/metadata rewrite on SFT.
5. Heun sampler.
6. Unsolicited Turbo / CFG 1 (user banned that unless asked).

After DCW-off + `vocal_language=en`, takes 32 and 33 were listenable. 34 onward used the official SFT table above.

### 5.2 Prompt notes that mattered for techno

- Say **instrumental, no vocals** and put `[Instrumental]` in lyrics, or SFT still tries to sing.
- Ask for **music starts immediately / no long intro**. Early SFT takes had long dead intros.
- Richer fidelity language (analog warmth, full-frequency mix, velvet sub) was used on the 7:00 take. It helped density more than it invented a new genre.
- DJ names as style (Josh Wink) were accepted by the model. That is prompt steering, not a licensed style clone.

---

## 6. Duration ceilings (verified)

ACE registry `max_duration_s` is 600 (10 minutes). The real cap is VRAM of the loaded pair.

| Stack | Longest that worked | Failed just above |
|---|---|---|
| XL SFT + 1.7B on 3090 | **420 s (7:00)** | 480 s preflight |
| XL SFT + 4B on 3090 | **200 s (3:20)** ranked × 5 | 210–240 s |
| Extend 2:00 + 2:00 on XL+4B | Not run. Predicted fail | Total 4:00 exceeds ~3:20 |

Extend (`POST /api/ace_step/extend`, Append/Prepend) pads silence and **repaints only the new region**. VRAM is charged for the **whole padded file**, not just the added seconds. One chunk ≤ 300 s. Chain by: generate → Use as source → Extend → repeat.

For a 2+2+2 chain, swap the planner to **1.7B** first.

---

## 7. Named listen takes from this campaign

Bodies live in `cache/agent-work/omni-techno/`. WAV names as written at the time:

| Take | Length | Notes |
|---|---|---|
| 32-xl-sft-dcw-off-metal | short | First listenable after DCW-off |
| 33-xl-sft-dcw-off-codes | short | Thinking/codes path, still good |
| 34-xl-sft-hypnotic-minimal | 60 s | Hypnotic minimal techno recipe locked |
| 35-xl-sft-hypnotic-rich-7min | **420 s** | Same recipe, richer prompt, 1.7B |
| 36-xl-sft-4b-hypnotic-3m30 | 210 s attempt | 4B; 210 s was the edge |
| 37-xl-sft-4b-josh-wink | ~3 min class | DJ-name style |
| 38-run-dmc-rank1–5 | 60 s × 5 | ACE-written lyrics, CLAP ranked |
| 39-run-dmc-3m20-rank1–5 | **200 s × 5** | Longest 4B ranked set that succeeded |

CLAP ranking: N sequential generations, then score each against `score_prompt` (or the style prompt). The UI marks the best. CLAP does nothing on a single generate.

---

## 8. Official ACE API surface that was wired in Omni

Implemented in `server/ace_step_loaders.py`, `server/routers/ace_step.py`, `server/omni_worker.py`:

- Generation knobs listed in §5, plus LM sampling, DCW mode/scalers/wavelet, normalize, fades, batch 1–8, audio codes, timesteps, reference audio.
- Dual-GPU ACE spawn when `lm_device != device`.
- `POST` create-sample, format-sample, understand, simple, extract, lego, complete.
- Extend prepend/append via pad + repaint (SFT has no native prepend).
- Cover strength and cover noise now override the hardcoded 0.8 / 0.0 when the client sends them (`audio_cover_strength`, `cover_noise_strength`).
- Velocity norm / EMA forwarded onto official `GenerationParams`.

Still not available / not switched on:

- `flash_attn`
- AutoGen
- Lyric2Vocal / Text2Samples (upstream weights unreleased)
- vLLM LM backend (API accepts `pt` / `vllm`; live path stayed `pt`)
- LyCORIS LoHA load the Gradio way (pop-electro)

Gateway Pydantic `extra=ignore` **drops unknown fields** until the gateway process is restarted after a schema change. After the 2026-08-18 field adds, start Omni once so the running gateway has `audio_cover_strength` and friends.

---

## 9. Music tab UI (2026-08-17 / 18)

`server/static/tab-ace-step.js?v=11` (live copy under `/opt/omni_studio/server/static/`).

Completed so official params are sliders and selectors, not blank boxes:

- Song: duration, BPM, key, time signature, vocal language (default English).
- Sampling: steps/CFG/shift prefilled from the loaded model (50 / 7 / 1.0 on XL SFT), Euler/Heun/DPM++, ODE/SDE, ADG, Thinking, velocity sliders.
- Guidance interval 0→1.
- DCW enable (off on SFT, on on Turbo), mode, wavelet, scalers.
- LM: temperature, CFG, top-k, top-p, constrained decoding, four CoT toggles (rewrites default **off**).
- Output: normalize, target dB, fades, batch, random-seed toggle, BF16, overlapped decode, timestep schedule, optional audio codes.
- **Style reference** upload on Generate, Simple, Cover, A2A, Edit, Extend, Extract, Lego, Complete, Vocal→BGM. This is official `reference_audio` (new song colored by a track), not the source file those modes rewrite.
- Cover strength + cover noise sliders.
- Results: **Use as source** and **Use as style reference**.
- **Planner** tab: create-sample, format-sample, understand; send caption/lyrics to Generate.
- **LoRA attach** on the live worker card (pack, adapter file, multiplier).
- Load modal: DiT GPU, LM GPU, `pt`/`vllm`, BF16, INT8, CPU offload, torch.compile.
- Simple, Extract, Lego, Complete modes.

Hard-refresh Music after start so the browser does not keep `?v=9` / `?v=10`.

---

## 10. System RAM that looked “full”

Measured while ACE + CLAP were loaded:

- WSL `free`: **5.1 GB** process use, **25 GB page cache**, **26 GB available**, ~1 GB “free”.
- Windows Task Manager: `vmmem` ~**31.7 GB** of 64 GB.

That 25 GB is Linux file cache (ACE checkpoints and WAVs), not CPU offload and not a leak. WSL does not give page cache back to Windows quickly, so Task Manager looks full. Swap was ~20 MB. Restarting the distro or dropping caches returns it to Windows; it will refill the next time ACE reads weights.

Agent-started leftover processes: after the user asked to start Omni themselves, the `/opt/omni_studio/bridge.py` instance was killed. A later check showed **one** official stack: one `Omni_Studio.exe`, one watchdog, one `E:\...\bridge.py`, one gateway, no ACE worker until they load models.

---

## 11. LoRAs

Omni attach is PEFT: directory must have `adapter_config.json`, and ACE looks for `adapter_model.safetensors`. Packs that ship another filename need `adapter_file` so Omni remaps it.

| Pack | Status | Usable on XL SFT? |
|---|---|---|
| `ryanontheinside/techno-acestep1.5-xl-v1` | **Found. Not installed.** PEFT, has `adapter_config.json`, weights `techno-xl-v1.safetensors` (~160 MB). Trained on XL Turbo (same 4B XL DiT). Trigger `roti-t3kn0`. Community says XL LoRAs load on Turbo, SFT, or merges. Attach with `adapter_file: "techno-xl-v1.safetensors"`. Keep SFT 50/7/1.0; do not switch to Turbo 8-step because the card says 8. | Yes, after install. |
| `rain-techno` | Downloaded to `models/ace_step/loras/rain-techno/`. Weights only, no `adapter_config.json`. 2B / Comfy pack. | No, not without inventing config or swapping to 2B. |
| `Nekochu/ACE-Step-xl-base-pop-electro-lora` | Installed as `pop-electro`. LoHA on **xl-base**, file `loha_weights.safetensors`. ACE Gradio PEFT loader will not attach it. | No on the Omni PEFT path. |
| Official chinese-new-year, lofi, raga, acoustic-guitar, raspy-vocal-pack | Installed, attachable PEFT. | Not techno. |

HF search `kind=lora` only returns PEFT-tagged repos. That is why rain-techno and most Comfy ACE LoRAs miss the filter. Search cannot yet restrict “LoRAs for the currently loaded model.”

There is no official ACE LoRA tagged `acestep-v15-xl-sft` for techno.

---

## 12. Code and file map

| Path | What changed or matters |
|---|---|
| `server/ace_step_loaders.py` | DCW default off for non-Turbo; `vocal_language` default `en`; 4B bf16-then-`.to()`; LM device; official GenerationParams overrides; extend pad+repaint; cover strength override; velocity forward. |
| `server/routers/ace_step.py` | Load `lm_device` / `lm_backend`; official helper routes; dual-GPU spawn; reference URL resolve on generate / ranked / init modes; new cover/velocity fields. |
| `server/omni_worker.py` | ACE infer extras; `extra=allow` on worker gen request. |
| `server/omni_placement.py` | `COMPONENT_SPLIT_MODELS={"ace_step"}`. |
| `server/config.py` | XL SFT/base `cfg_default` 7.0. |
| `server/static/tab-ace-step.js` | Full Music UI (`?v=11`). |
| `server/static/index.html` | Cache bust `tab-ace-step.js?v=11`. |
| `tests/test_ace_step_generation_params.py` | Official knobs, helper fields, cover strength / velocity. |
| `docs/audio-api.md`, `docs/omni-model-api.md` | Dual-GPU ACE and new routes. |
| `cache/agent-work/omni-techno/` | Load JSONs, gen bodies, ranked runners. |

Live gateway reads `/opt/omni_studio/server/...`. After editing the Windows tree, copy those files into `/opt` (they are not a live bind-mount).

Client used throughout: `python skills/omni-audio-api/scripts/omni_audio_api.py --timeout N request METHOD PATH --body file.json`. Never print `X-Omni-Token`.

---

## 13. Restore checklist (next session)

1. Confirm a single Omni instance (one watchdog + one gateway).
2. Hard-refresh Music (`tab-ace-step.js?v=11`).
3. Load ACE: `POST /api/ace_step/load` with `load-xl-sft-4b-3090.json` (both ACE pieces on 3090).
4. Load CLAP: `POST /api/audio_lab/load` with `load-clap-3060.json`.
5. Generate with the §5 table. Do not turn DCW or CoT rewrite on for SFT.
6. For >3:20, unload 4B and load `ace-lm-1.7b` on the same 3090 worker, or generate 2:00 and Extend in 30–60 s chunks after switching planner.
7. Techno LoRA: install `ryanontheinside/techno-acestep1.5-xl-v1` as an ad-hoc pack, attach with `adapter_file=techno-xl-v1.safetensors`, put `roti-t3kn0` in the prompt. Not done yet.

---

## 14. Open items (not finished in these sessions)

- Techno XL LoRA not downloaded or attached.
- Models were unloaded when the user last started Omni themselves; they must be loaded again.
- Dual-GPU ACE (DiT 3090 / LM 3060) works in code but was reverted as the operating layout.
- Extract / Lego / Complete UI exists; those tasks need **XL Base**, not XL SFT.
- No listen take yet using Generate + style-reference upload (API and UI are wired; not exercised end-to-end after the UI landing).
- Browser click-through of the new Music tab was not done in-agent (no browser tools on that turn). Static JS was confirmed served with the new strings.
