"""ACE-Step CLI — DiT music-generation management and inference.

Phase 1 surface: list, install/delete (models / LMs / VAEs / LoRAs / custom).
Generation / scoring / advanced modes arrive in later phases.

The group is mounted onto the root ``cli`` from ``cli/omni.py`` via
``cli.add_command(ace_step)``. It reuses the root's ``_common_options``
context (token / base_url / out_format).
"""

from __future__ import annotations

import base64
from pathlib import Path

import click

from cli.audio_lab_commands import _save_url_to
from cli.client import Client
from cli.formatters import emit


def _client(ctx) -> Client:
    return Client(base_url=ctx.obj["base_url"], token=ctx.obj["token"])


def _file_to_b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


@click.group("ace-step")
@click.pass_context
def ace_step(ctx: click.Context) -> None:
    """ACE-Step 1.5 DiT song generation lab (install / generate / score)."""


# ---------------------------------------------------------------------------
# listing / status
# ---------------------------------------------------------------------------
@ace_step.command("list")
@click.option("--kind", type=click.Choice(["all", "models", "lms", "vaes", "loras", "custom"]),
              default="all", show_default=True)
@click.pass_context
def ace_step_list(ctx: click.Context, kind: str) -> None:
    """List ACE-Step assets (registry + custom) and their install state."""
    c = _client(ctx)
    data = c.call("GET", "/api/ace_step/status")
    fmt = ctx.obj["fmt"]
    if kind == "all":
        emit(data, fmt)
        return
    block = data.get(kind, [])
    if kind == "custom":
        emit(block, fmt)
        return
    if kind == "loras":
        emit(block, fmt, columns=["name", "display", "tier", "size_gb", "enables_mode", "installed"])
    else:
        emit(block, fmt, columns=["variant_id", "display", "tier", "format", "size_gb", "installed"])


# ---------------------------------------------------------------------------
# Base DiT model install / delete
# ---------------------------------------------------------------------------
@ace_step.command("install-model")
@click.argument("variant_id")
@click.pass_context
def ace_step_install_model(ctx: click.Context, variant_id: str) -> None:
    """Install a registered ACE-Step base model (e.g. ace-xl-turbo).

    Returns immediately with a job_id. Poll with `omni-cli jobs show <id> --follow`.
    """
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/install-model",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@ace_step.command("delete-model")
@click.argument("variant_id")
@click.pass_context
def ace_step_delete_model(ctx: click.Context, variant_id: str) -> None:
    """Remove an ACE-Step base model's weights from disk."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/install-model/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# 5Hz LM install / delete
# ---------------------------------------------------------------------------
@ace_step.command("install-lm")
@click.argument("variant_id")
@click.pass_context
def ace_step_install_lm(ctx: click.Context, variant_id: str) -> None:
    """Install an ACE-Step 5Hz LM (e.g. ace-lm-1.7b)."""
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/install-lm",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@ace_step.command("delete-lm")
@click.argument("variant_id")
@click.pass_context
def ace_step_delete_lm(ctx: click.Context, variant_id: str) -> None:
    """Remove an LM's weights."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/install-lm/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# VAE swap install / delete
# ---------------------------------------------------------------------------
@ace_step.command("install-vae")
@click.argument("variant_id")
@click.pass_context
def ace_step_install_vae(ctx: click.Context, variant_id: str) -> None:
    """Install an ACE-Step VAE swap (e.g. scrag-vae)."""
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/install-vae",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@ace_step.command("delete-vae")
@click.argument("variant_id")
@click.pass_context
def ace_step_delete_vae(ctx: click.Context, variant_id: str) -> None:
    """Remove a VAE swap's weights."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/install-vae/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# LoRA install / delete
# ---------------------------------------------------------------------------
@ace_step.command("install-lora")
@click.option("--name", help="Registry name (e.g. synthpop) OR target name for ad-hoc install.")
@click.option("--repo", help="HF repo (e.g. someuser/some-lora) — pass alongside --name to install ad-hoc.")
@click.pass_context
def ace_step_install_lora(ctx: click.Context, name: str | None, repo: str | None) -> None:
    """Install an ACE-Step LoRA.

    Two paths:
      • Registry lookup: pass --name only.
      • Ad-hoc community LoRA: pass --repo (and optionally --name).
    """
    if not name and not repo:
        raise click.UsageError("Provide --name (registry) or --repo (ad-hoc).")
    body: dict = {}
    if name:
        body["name"] = name
    if repo:
        body["repo"] = repo
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/install-lora",
                json=body, expect=(200, 202)),
         ctx.obj["fmt"])


@ace_step.command("delete-lora")
@click.argument("name")
@click.pass_context
def ace_step_delete_lora(ctx: click.Context, name: str) -> None:
    """Remove an installed LoRA's weights."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/install-lora/{name}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Custom HF repo install / delete (sandboxed under custom/<kind>/<name>)
# ---------------------------------------------------------------------------
@ace_step.command("install-custom")
@click.option("--repo", required=True, help="HF repo, e.g. someuser/some-ace-tune")
@click.option("--name", default=None, help="Local name (defaults to repo with '/' → '_')")
@click.option("--kind", type=click.Choice(["model", "lm", "vae", "lora"]), default="model",
              show_default=True)
@click.pass_context
def ace_step_install_custom(ctx: click.Context, repo: str, name: str | None, kind: str) -> None:
    """Install any HF repo as an ACE-Step asset.

    Lands under MODELS_DIR/ace_step/custom/<kind>/<name>/. Appears in the
    same UI dropdowns as registry variants, tagged 'Custom'.
    """
    body = {"repo": repo, "kind": kind}
    if name:
        body["name"] = name
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/install-custom",
                json=body, expect=(200, 202)),
         ctx.obj["fmt"])


@ace_step.command("delete-custom")
@click.option("--kind", type=click.Choice(["model", "lm", "vae", "lora"]), required=True)
@click.argument("name")
@click.pass_context
def ace_step_delete_custom(ctx: click.Context, kind: str, name: str) -> None:
    """Remove a custom-installed asset."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/install-custom/{kind}/{name}"), ctx.obj["fmt"])


# ===========================================================================
# Phase 2+: lifecycle, generation, advanced modes
# ===========================================================================


def _read_lyrics(path_or_inline: str | None) -> str | None:
    """Resolve a lyrics input: @path.txt reads the file; else use the literal string."""
    if path_or_inline is None:
        return None
    if path_or_inline.startswith("@"):
        lyrics_path = path_or_inline[1:]
        try:
            return Path(lyrics_path).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as e:
            raise click.ClickException(f"could not read lyrics file '{lyrics_path}': {e}")
    return path_or_inline


def _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                scheduler, seed, guidance_interval, no_overlapped_decode,
                extra=None):
    body = {
        "prompt": prompt,
        "duration_s": duration,
        "scheduler": scheduler,
    }
    if lyrics is not None:
        body["lyrics"] = _read_lyrics(lyrics)
    if negative_prompt:
        body["negative_prompt"] = negative_prompt
    if steps is not None:
        body["steps"] = steps
    if cfg is not None:
        body["cfg_scale"] = cfg
    if seed is not None:
        body["seed"] = seed
    if guidance_interval:
        a, b = guidance_interval.split(":")
        body["guidance_interval"] = [float(a), float(b)]
    if no_overlapped_decode:
        body["overlapped_decode"] = False
    extra = extra or {}
    if extra.get("shift") is not None:
        body["shift"] = extra["shift"]
    if extra.get("bpm") is not None:
        body["bpm"] = extra["bpm"]
    if extra.get("keyscale"):
        body["keyscale"] = extra["keyscale"]
    if extra.get("timesignature"):
        body["timesignature"] = extra["timesignature"]
    return body


_GEN_OPTIONS = [
    click.option("--prompt", required=True, help="Style prompt."),
    click.option("--lyrics", default=None,
                 help="Lyrics with [verse]/[chorus]/[bridge] tags. Prefix with @ to read from a file."),
    click.option("--negative-prompt", default=None),
    click.option("--duration", "duration", type=float, default=60.0, show_default=True,
                 help="Output duration in seconds."),
    click.option("--steps", type=int, default=None,
                 help="Diffusion steps (defaults to the model's recommended value)."),
    click.option("--cfg", "cfg", type=float, default=None,
                 help="CFG scale (defaults to the model's recommended value)."),
    click.option("--scheduler", type=click.Choice(["euler", "heun", "dpmpp"]),
                 default="euler", show_default=True),
    click.option("--seed", type=int, default=None),
    click.option("--guidance-interval", default=None, metavar="START:END",
                 help="CFG schedule interval like 0.3:0.7"),
    click.option("--shift", type=float, default=None,
                 help="Timestep shift 1-5. Turbo default 3.0; SFT/base default 1.0."),
    click.option("--bpm", type=int, default=None, help="Target BPM 30-300."),
    click.option("--keyscale", default=None, help='Musical key, e.g. "D minor".'),
    click.option("--timesignature", default=None, help='Time signature, e.g. "4" for 4/4.'),
    click.option("--no-overlapped-decode", is_flag=True,
                 help="Disable overlapped VAE decoding (slightly slower but lower VRAM peak)."),
]


def _apply_gen_options(fn):
    for opt in reversed(_GEN_OPTIONS):
        fn = opt(fn)
    return fn


# ---------------------------------------------------------------------------
# Lifecycle / state / cancel
# ---------------------------------------------------------------------------
@ace_step.command("load")
@click.option("--model-variant", help="Base model to load (e.g. ace-xl-turbo).")
@click.option("--lm-variant", help="5Hz LM to load (e.g. ace-lm-1.7b).")
@click.option("--vae-variant", default="default", show_default=True,
              help="VAE swap (default | scrag-vae | hot-step-cpp | ace-1d-sa-format).")
@click.option("--cpu-offload", is_flag=True, help="Enable CPU offload (lower VRAM, slower).")
@click.option("--int8", is_flag=True, help="Dynamic INT8 quantize the DiT (saves ~5GB on XL).")
@click.option("--torch-compile", is_flag=True, help="torch.compile the transformer (slower first run).")
@click.option("--fp32", is_flag=True, help="Use fp32 instead of bf16 (rarely needed).")
@click.pass_context
def ace_step_load(ctx, model_variant, lm_variant, vae_variant, cpu_offload,
                  int8, torch_compile, fp32):
    """Load model + LM + VAE onto the ACE-Step worker."""
    body = {"vae_variant": vae_variant, "bf16": not fp32,
            "cpu_offload": cpu_offload, "int8": int8, "torch_compile": torch_compile}
    if model_variant:
        body["model_variant"] = model_variant
    if lm_variant:
        body["lm_variant"] = lm_variant
    if not (model_variant or lm_variant) and vae_variant == "default":
        raise click.UsageError("Provide --model-variant, --lm-variant, or a non-default --vae-variant.")
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/load", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("unload")
@click.option("--component", type=click.Choice(["model", "lm", "vae", "all"]), default="all", show_default=True)
@click.pass_context
def ace_step_unload(ctx, component):
    """Unload component(s) from VRAM."""
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/unload", json={"component": component}), ctx.obj["fmt"])


@ace_step.command("state")
@click.option("--autospawn", is_flag=True, help="Boot the worker if not running.")
@click.pass_context
def ace_step_state(ctx, autospawn):
    """Show worker status and what's loaded."""
    c = _client(ctx)
    path = "/api/ace_step/state" + ("?autospawn=true" if autospawn else "")
    emit(c.call("GET", path), ctx.obj["fmt"])


@ace_step.command("cancel")
@click.pass_context
def ace_step_cancel(ctx):
    """Set the worker's cancel flag. Mid-step diffusion can't be interrupted,
    but pending candidates / scoring rounds will abort."""
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/cancel"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# LoRA stack
# ---------------------------------------------------------------------------
@ace_step.group("lora")
def ace_step_lora():
    """LoRA stack management (attach / detach / list)."""


@ace_step_lora.command("attach")
@click.option("--name", required=True)
@click.option("--multiplier", type=float, default=1.0, show_default=True)
@click.pass_context
def ace_step_lora_attach(ctx, name, multiplier):
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/lora/attach",
                json={"name": name, "multiplier": multiplier}), ctx.obj["fmt"])


@ace_step_lora.command("detach")
@click.option("--name", required=True)
@click.pass_context
def ace_step_lora_detach(ctx, name):
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/lora/detach", json={"name": name}), ctx.obj["fmt"])


@ace_step_lora.command("list")
@click.pass_context
def ace_step_lora_list(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/ace_step/lora/list"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
@ace_step.command("generate")
@_apply_gen_options
@click.option("--out", "out_path", default=None,
              help="Write the resulting WAV to this path (else just print the URL).")
@click.pass_context
def ace_step_generate(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                      scheduler, seed, guidance_interval, no_overlapped_decode, out_path,
                      **extra):
    """Single T2M generation. Returns one wav + URL."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    c = _client(ctx)
    data = c.call("POST", "/api/ace_step/generate", json=body, expect=(200,))
    if out_path:
        # Stream the wav file via the URL and write to disk.
        url = (data.get("results") or [{}])[0].get("url") or data.get("url")
        if url:
            if _save_url_to(c, url, Path(out_path)):
                data["written_to"] = out_path
        else:
            click.echo("server response missing 'url'", err=True)
    emit(data, ctx.obj["fmt"])


@ace_step.command("generate-ranked")
@_apply_gen_options
@click.option("--n", type=int, default=4, show_default=True)
@click.option("--score-prompt", default=None,
              help="Override the prompt used for CLAP ranking (defaults to --prompt).")
@click.option("--no-clap", is_flag=True, help="Skip CLAP scoring even if audio_lab is running.")
@click.pass_context
def ace_step_generate_ranked(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                             scheduler, seed, guidance_interval, no_overlapped_decode,
                             n, score_prompt, no_clap, **extra):
    """Fan-out N candidates, CLAP-rank via the audio_lab worker, return ranked manifest.

    Falls back to unranked output if audio_lab isn't running or has no CLAP loaded.
    """
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    body["n"] = n
    if score_prompt:
        body["score_prompt"] = score_prompt
    if no_clap:
        body["score_with_clap"] = False
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/generate-ranked", json=body, expect=(200,)),
         ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Advanced modes
# ---------------------------------------------------------------------------
def _add_audio_to_body(body, audio_path, audio_url, init_sample_rate):
    if audio_path and audio_url:
        raise click.UsageError("Pass --audio or --audio-url, not both.")
    if audio_path:
        body["init_audio_base64"] = _file_to_b64(audio_path)
        if init_sample_rate:
            body["init_sample_rate"] = init_sample_rate
    elif audio_url:
        body["init_audio_url"] = audio_url
    else:
        raise click.UsageError("Provide --audio <path> or --audio-url <url>.")


@ace_step.command("a2a")
@_apply_gen_options
@click.option("--audio", "audio_path", default=None, help="Reference audio path.")
@click.option("--audio-url", default=None, help="ACE-Step outputs URL.")
@click.option("--init-sample-rate", type=int, default=None)
@click.option("--noise", "noise", type=float, default=0.6, show_default=True,
              help="init_noise_level (0=preserve, 1=ignore init).")
@click.pass_context
def ace_step_a2a(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                 scheduler, seed, guidance_interval, no_overlapped_decode,
                 audio_path, audio_url, init_sample_rate, noise, **extra):
    """Audio-to-audio style transfer."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    body["init_noise_level"] = noise
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/a2a", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("repaint")
@_apply_gen_options
@click.option("--audio", "audio_path", default=None)
@click.option("--audio-url", default=None)
@click.option("--init-sample-rate", type=int, default=None)
@click.option("--mask", "mask", required=True, metavar="START:END",
              help="Mask region in seconds, e.g. 30:60.")
@click.pass_context
def ace_step_repaint(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                     scheduler, seed, guidance_interval, no_overlapped_decode,
                     audio_path, audio_url, init_sample_rate, mask, **extra):
    """Masked-segment regeneration."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    try:
        a, b = mask.split(":")
        body["mask_start_s"] = float(a)
        body["mask_end_s"] = float(b)
    except (ValueError, IndexError):
        raise click.UsageError("--mask must be START:END in seconds (e.g. 30:60)")
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/repaint", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("edit")
@_apply_gen_options
@click.option("--audio", "audio_path", default=None)
@click.option("--audio-url", default=None)
@click.option("--init-sample-rate", type=int, default=None)
@click.option("--mode", type=click.Choice(["only_lyrics", "remix"]), required=True)
@click.pass_context
def ace_step_edit(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                  scheduler, seed, guidance_interval, no_overlapped_decode,
                  audio_path, audio_url, init_sample_rate, mode, **extra):
    """Edit only_lyrics (same melody, new words) or remix (style change)."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    body["edit_mode"] = mode
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/edit", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("extend")
@_apply_gen_options
@click.option("--audio", "audio_path", default=None)
@click.option("--audio-url", default=None)
@click.option("--init-sample-rate", type=int, default=None)
@click.option("--mode", type=click.Choice(["prepend", "append"]), default="append", show_default=True)
@click.option("--extend-duration", type=float, default=30.0, show_default=True,
              help="Seconds to add (before or after existing track).")
@click.pass_context
def ace_step_extend(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                    scheduler, seed, guidance_interval, no_overlapped_decode,
                    audio_path, audio_url, init_sample_rate, mode, extend_duration,
                    **extra):
    """Prepend or append to an existing track."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    body["extend_mode"] = mode
    body["extend_duration_s"] = extend_duration
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/extend", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("cover")
@_apply_gen_options
@click.option("--audio", "audio_path", default=None)
@click.option("--audio-url", default=None)
@click.option("--init-sample-rate", type=int, default=None)
@click.pass_context
def ace_step_cover(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                   scheduler, seed, guidance_interval, no_overlapped_decode,
                   audio_path, audio_url, init_sample_rate, **extra):
    """Re-sing an existing track in a new style."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/cover", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("vocal2bgm")
@click.option("--audio", "audio_path", default=None, help="Vocals track (path).")
@click.option("--audio-url", default=None)
@click.option("--init-sample-rate", type=int, default=None)
@click.option("--prompt", default=None,
              help="Optional style hint for the generated accompaniment.")
@click.option("--duration", "duration", type=float, default=None,
              help="Override duration; defaults to the input track length.")
@click.option("--steps", type=int, default=None)
@click.option("--cfg", "cfg", type=float, default=None)
@click.option("--scheduler", type=click.Choice(["euler", "heun", "dpmpp"]),
              default="euler", show_default=True)
@click.option("--seed", type=int, default=None)
@click.pass_context
def ace_step_vocal2bgm(ctx, audio_path, audio_url, init_sample_rate,
                       prompt, duration, steps, cfg, scheduler, seed):
    """Strip vocals from input, generate instrumental accompaniment."""
    body = {"scheduler": scheduler}
    _add_audio_to_body(body, audio_path, audio_url, init_sample_rate)
    if prompt:
        body["prompt"] = prompt
    if duration is not None:
        body["duration_s"] = duration
    if steps is not None:
        body["steps"] = steps
    if cfg is not None:
        body["cfg_scale"] = cfg
    if seed is not None:
        body["seed"] = seed
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/vocal2bgm", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("lyric2vocal")
@_apply_gen_options
@click.pass_context
def ace_step_lyric2vocal(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                         scheduler, seed, guidance_interval, no_overlapped_decode,
                         **extra):
    """Vocal-only generation. Requires the lyric2vocal LoRA installed."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/lyric2vocal", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("text2samples")
@_apply_gen_options
@click.pass_context
def ace_step_text2samples(ctx, prompt, lyrics, negative_prompt, duration, steps, cfg,
                          scheduler, seed, guidance_interval, no_overlapped_decode,
                          **extra):
    """Short instrument-sample generation. Requires the text2samples LoRA installed."""
    body = _gen_kwargs(prompt, lyrics, negative_prompt, duration, steps, cfg,
                       scheduler, seed, guidance_interval, no_overlapped_decode,
                       extra)
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/text2samples", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("analyze")
@click.option("--audio", "audio_path", default=None)
@click.option("--audio-url", default=None)
@click.option("--sample-rate", type=int, default=None)
@click.pass_context
def ace_step_analyze(ctx, audio_path, audio_url, sample_rate):
    """BPM / key / loudness analysis (librosa, no DiT required)."""
    body = {}
    if audio_path and audio_url:
        raise click.UsageError("Pass --audio or --audio-url, not both.")
    if audio_path:
        body["audio_base64"] = _file_to_b64(audio_path)
        if sample_rate:
            body["sample_rate"] = sample_rate
    elif audio_url:
        body["audio_url"] = audio_url
    else:
        raise click.UsageError("Provide --audio <path> or --audio-url <url>.")
    c = _client(ctx)
    emit(c.call("POST", "/api/ace_step/analyze", json=body, expect=(200,)), ctx.obj["fmt"])


@ace_step.command("jobs")
@click.option("--mode", default=None)
@click.option("--model-variant", default=None)
@click.option("--limit", type=int, default=100, show_default=True)
@click.pass_context
def ace_step_jobs(ctx, mode, model_variant, limit):
    """List recent inference jobs (manifests on disk)."""
    q = f"limit={limit}"
    if mode:
        q += f"&mode={mode}"
    if model_variant:
        q += f"&model_variant={model_variant}"
    c = _client(ctx)
    emit(c.call("GET", f"/api/ace_step/jobs?{q}"), ctx.obj["fmt"])


@ace_step.command("delete-job")
@click.argument("job_id")
@click.pass_context
def ace_step_delete_job(ctx, job_id):
    """Delete all files for a given job_id."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/ace_step/jobs/{job_id}"), ctx.obj["fmt"])
