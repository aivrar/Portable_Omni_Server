# Command-line interface

Omni Studio ships a first-party CLI that is a thin wrapper over the same
HTTP API the UI uses. On Windows you run **`omni-cli.bat`**. Inside the
Linux distro you run **`omni-cli`**.

The CLI does not start the app. **Double-click `Omni_Studio.exe` first**
(or have the gateway already up). Then issue commands against
`http://127.0.0.1:9200` (override with `--base-url` or `OMNI_BASE_URL`).

## Windows: `omni-cli.bat`

The file sits next to `Omni_Studio.exe`. It is a shim:

```bat
set "DISTRO=%OMNI_WSL_DISTRO%"
if "%DISTRO%"=="" set "DISTRO=linbox-Omni_Studio"
wsl -d "%DISTRO%" -- /usr/bin/env PYTHONPATH=/opt/omni_studio /opt/omni_studio/venv/bin/python3 -m cli.omni %*
```

If `OMNI_WSL_DISTRO` is set, that name is used instead of
`linbox-Omni_Studio`. Leave it unset unless you know you renamed the distro.

**Do this from Command Prompt or PowerShell in the app folder:**

```bat
omni-cli.bat --help
omni-cli.bat status
omni-cli.bat devices list
```

You can pass a full path to `omni-cli.bat` from any cwd. Keep the app
folder together so the distro name in the bat file still matches the VHDX
you launched.

## Linux: `omni-cli`

Inside the distro (`wsl -d linbox-Omni_Studio`):

```bash
omni-cli --help
# or
/opt/omni_studio/venv/bin/python3 -m cli.omni --help
```

The POSIX shim looks for `/opt/omni_studio/venv/bin/python3` and falls back
to `python3`.

## Authentication

Every command accepts:

- `--token` or `OMNI_API_TOKEN`
- `--base-url` or `OMNI_BASE_URL`
- `--out-format text|json|jsonl`

If you omit the token, the client bootstraps from the loopback session the
same way the UI does, or from `~/.config/omni-studio/token` after:

```bat
omni-cli.bat session refresh
omni-cli.bat session show
```

`session show` prints a token. Do not capture that output into tickets,
screenshots, or git. Never put the token in source.

## Command groups

Top-level groups (plus `audio-lab` and `ace-step`):

| Group | What you do with it |
|---|---|
| `status` | One-shot system + workers + Comfy overview |
| `session` | Show/refresh cached token |
| `devices` | `list` GPUs |
| `workers` | `list`, `spawn MODEL`, `kill ID`, `kill-all`, `logs ID` |
| `chat` | `send MODEL PROMPT` with `--image/--audio/--video/--stream`; `cancel` |
| `comfy` | `list`, `start`, `stop`, `stop-all`, `logs`, `interrupt`, `free`, `watch` |
| `comfy assets` | `list`, `install`, `delete`, `upload` |
| `comfy nodes` | `list`, `install`, `delete` |
| `workflows` | `list`, `show`, `save`, `delete`, `import`, `run` |
| `outputs` | `list`, `get`, `delete`, `zip`, `prune` |
| `setup` | `install`, `install-lora`, `hf-token`, `search`, `status` |
| `jobs` | `list`, `show` (optional `--follow`), `cancel` |
| `system` | `info`, `disk`, `gpu`, `refresh`, `restart`, `shutdown` |
| `maintenance` | `status`, `run TASK`, `policy show|set` |
| `audio-lab` | install/load/unload/generate/score/A2A… |
| `ace-step` | install/load/unload/generate/transform/LoRA… |

`omni-cli.bat --help` and `omni-cli.bat comfy --help` are the live trees.
`install-completion bash|zsh|fish` is for shells inside the distro.

## Recipes

### Health

```bat
omni-cli.bat status
omni-cli.bat devices list
omni-cli.bat workers list
omni-cli.bat comfy list
```

### HuggingFace token and a model install

```bat
omni-cli.bat setup hf-token
omni-cli.bat setup install qwen_omni_7b --variant gptq-int4
omni-cli.bat jobs show JOB_ID --follow
```

The token command prompts with hidden input. For automation, use `--stdin`
with a protected input stream; do not put the token in command arguments.
Replace variant ids with what `setup status` prints on your disk.

### Spawn and chat

```bat
omni-cli.bat workers spawn qwen_omni_7b --device cuda:0 --variant YOUR_VARIANT
omni-cli.bat chat send qwen_omni_7b "Reply with OK." --max-tokens 32
```

`--image path.png` attaches vision **only if that worker actually consumes
images**. Qwen audio/video flags will 501. See Feature compatibility.

### Comfy start, analyze-before-run, queue

```bat
omni-cli.bat comfy start --device cuda:0 --gpu-pool cuda:0,cuda:1 --vram normal
omni-cli.bat workflows list
```

The CLI `workflows run --name file.json` queues a saved graph. For a full
placement plan with `valid: false` checking, prefer the Workflows UI or a
JSON POST to `/api/workflows/analyze` (see GPU placement). Do not queue a
graph whose UI plan is blocked.

```bat
omni-cli.bat comfy watch INSTANCE_ID
omni-cli.bat comfy interrupt INSTANCE_ID
omni-cli.bat comfy free INSTANCE_ID --unload-models
```

### Audio Lab generate

```bat
omni-cli.bat audio-lab load --sa-variant sao-open-small --clap-variant larger-clap-general
omni-cli.bat audio-lab generate --prompt "dry wooden knock, one hit" --duration 4 --out knock.wav
omni-cli.bat audio-lab unload --component all
```

### ACE-Step generate

```bat
omni-cli.bat ace-step load --model-variant ace-1.5 --lm-variant ace-lm-0.6b
omni-cli.bat ace-step generate --prompt "lofi beat, dusty drums" --duration 30
```

`lyric2vocal` and `text2samples` CLI commands exist and will fail without
released assets. Do not wrap them in a retry loop.

### Media retrieval

```bat
omni-cli.bat outputs list --kind omni --media-kind audio --limit 20
omni-cli.bat outputs get path/inside/distro.wav --kind omni --out /mnt/d/take/out.wav
omni-cli.bat outputs zip path1 path2 --kind omni --out bundle.zip
```

`get`, `delete`, `zip`, and `prune` accept `--kind output|input|temp|omni`.
`omni-cli.bat` launches Python inside WSL, so every local input/output path
uses Linux spelling even when the command is entered in Windows. Use
`/mnt/d/...` for the Windows D: drive and create the destination folder first.
The CLI does not convert `D:\...` automatically.

### Gateway restart versus app shutdown

```bat
omni-cli.bat system restart
omni-cli.bat system shutdown
```

Both send `{"confirm": true}`. `system shutdown` is gateway shutdown.
The WebView **Shutdown** button is `/api/app/shutdown` (full host stop).
Use the button when you want the Windows window to close.
While the bridge is running, it supervises and may relaunch a gateway stopped
with `system shutdown`. A normal `system restart` also stops workers and
Comfy instances during gateway teardown; start them again after reconnecting.

## Output formats

`--out-format json` is for scripts. Default `text` is tables. Do not parse
text tables in automation.

## Related pages

- [Launch and first run](02-launch-and-first-run.md)
- [GPU placement](19-gpu-placement.md)
- [Jobs, tokens, composition, and media retrieval](20-jobs-huggingface-and-special-usages.md)
- [Shutdown](17-shutdown.md)
