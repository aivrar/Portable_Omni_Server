# Jobs, HuggingFace, composition, and media retrieval

This page collects special usages that span multiple tabs: the HuggingFace
token, install/load/unload, background **jobs** versus synchronous inference
ids, long-form audio **compose**, and getting files out of the distro onto
Windows.

## HuggingFace token

Gated checkpoints (official Stable Audio, some Omni weights, many Comfy
files) need a token from the same account that clicked **Agree** on the
model card.

**In the UI:** Home → **Downloads, access token, and advanced setup** →
paste `hf_…` → **Save token**. Badge should become Connected.

**In the CLI:**

```bat
omni-cli.bat setup hf-token
omni-cli.bat setup search "stable audio" --kind model
```

The token is stored inside `linbox-Omni_Studio`. It is not an Omni Studio
cloud login. Rotate it on HuggingFace if it leaked; then Save again.

Without a token, non-gated installs still work. Failed gated jobs show
401/403 in the job Log.

## Install, load, unload (three different states)

| State | Meaning | Where you look |
|---|---|---|
| **Installed** | Files on the distro disk | Model library, engine Install tabs, `setup status` |
| **Loaded / ready worker** | Weights in VRAM (or CPU) | Runtime, audio live cards, `workers list` |
| **Persisted output** | A WAV/PNG/MP4 in the library | Media library, `outputs list` |

Install does not load. Load does not install. Killing a worker does not
delete weights. Deleting weights does not kill a worker — unload first.

**Install** (background job, poll it):

- Home defaults, Model library Download, audio Install sub-tabs
- `omni-cli setup install MODEL [--variant ID]`
- `omni-cli audio-lab install-model …`, `ace-step install-model …`
- Comfy assets / template catalog

**Load / spawn:**

- Runtime Spawn, Chat Spawn worker, Testing Spawn
- Audio Lab / Music / Music 3 / MOSS load buttons
- `workers spawn`, `audio-lab load`, `ace-step load`, Music 3 load API

**Unload:**

- Engine Unload Models (Comfy `/free`)
- Audio unload all / component
- Runtime Kill exact worker id
- Music 3 Unload / Cancel and unload

Model file **Delete** is immediate and `recoverable: false`. Resolve the
exact id/category/filename from the UI table. There is no undo.

## Jobs: when to poll, when not to

### Background jobs (poll)

Operations that return a generic `job_id` with `status: "queued"` or
`status: "running"`:

- model / LoRA / Comfy asset installs
- missing defaults
- Comfy core/Manager updates
- audio **compose**
- maintenance tasks

**Do this:**

```bat
omni-cli.bat jobs list
omni-cli.bat jobs show JOB_ID --follow
omni-cli.bat jobs cancel JOB_ID
```

Or watch Home / Model library **Install activity**. Terminal states:
`done` / `completed`, `error` / `failed`, `cancelled`. Read the log tail
before retrying.

Home and Model library also list setup jobs with Cancel + Log.

Output ZIP downloads also return synchronously, as archive bytes without a
generic job ID.

### Synchronous inference (do not poll the generic table)

ACE-Step generate, Audio Lab generate, Music 3 generate, MOSS TTS/SFX, and
Chat completions wait for the result in the HTTP call. They may still
**persist** media under a folder named with a `job_id`. That id is an
output directory, not a row to poll in `/api/jobs`.

If the HTTP call times out, **read state** (`workers`, engine `state`,
Media library) before retrying. A timeout is unknown, not "safe to
double-submit." ACE/Audio Lab workers that hit the server timeout are
retired.

### Comfy prompt_id

Workflows Run returns a Comfy `prompt_id`. That is queue acceptance. Watch
Engine & queue until the job finishes, then confirm the file in Media
library. Do not assume success from HTTP 200.

## Media retrieval

Windows does not have a supported `storage/` folder of models. To get
**output** onto NTFS:

1. Media library → select → **Export ZIP**
2. Media library lightbox download control when present
3. CLI:

```bat
omni-cli.bat outputs list --kind output --media-kind image --limit 50
omni-cli.bat outputs list --kind omni --media-kind audio
omni-cli.bat outputs get RELPATH --out /mnt/d/media/file.wav
```

`kind` is `output` | `input` | `temp` | `omni`. Use the relative path the
list printed. Prefix and `prompt_id` filters match the GUI.

ACE/Audio Lab also have `zip/{job_id}` and per-file output URLs. Those are
the same bytes the Omni source shows.

Do not rely on `\\wsl$\linbox-Omni_Studio\opt\...` for symlinked persist
paths. Copy through the API.

Deletion of an output is immediate. Pin first if maintenance prune is
enabled.

## Long-form composition

`POST /api/outputs/audio/compose` builds a master from **2–64** existing
library segments. This is the supported way to make a long instrumental
without trusting ACE extend to preserve audio.

Each segment: exact `path` + `kind` from `outputs list`, optional `start_s`,
`end_s`, `gain_db`, `label`.

Example body:

```json
{
  "segments": [
    {"path": "ace_step/JOB_A/01.wav", "kind": "omni", "label": "A"},
    {"path": "ace_step/JOB_B/01.wav", "kind": "omni", "label": "B"}
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

This returns a **background job**. Poll `/api/jobs/{job_id}`. The WAV and
manifest appear under Omni source `compositions/<id>/`.

Rules:

- `bit_depth` 16 or 24
- 1–4 CPU threads
- triangular crossfades
- optional loudness normalize
- total duration ≈ sum(trimmed lengths) − crossfade × (n − 1)
- composition is an edit, not a musical arranger; trim on phrase
  boundaries and listen

Use this when ACE complete/extend rewrote audio you needed to keep.

## Maintenance

Runtime-adjacent cleanup lives under `omni-cli maintenance`:

- `maintenance status` — cadence, last run, due
- `maintenance policy show` / `policy set TASK KEY VALUE`
- `maintenance run TASK` — one existing task as a background job

Pin Media library keepers before prune-like tasks. Do not invent task
names; only known policy keys apply.

## Related pages

- [Home](03-home.md)
- [Media library](05-media-library.md)
- [Model library](06-model-library.md)
- [Command-line interface](18-cli.md)
