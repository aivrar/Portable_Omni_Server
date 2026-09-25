"""Audio Lab CLI — Stable Audio + CLAP management and inference.

Phase 1 surface: list available variants, install/delete registry variants
(models / VAEs / CLAP), install custom HF repos. Generation / scoring /
A2A / inpaint / VAE-lab arrive in later phases.

The group is mounted onto the root ``cli`` from ``cli/omni.py`` via
``cli.add_command(audio_lab)``. It reuses the root's ``_common_options``
context (token / base_url / out_format) — no per-subgroup duplication.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import click

from cli.client import Client
from cli.formatters import emit


def _client(ctx) -> Client:
    return Client(base_url=ctx.obj["base_url"], token=ctx.obj["token"])


def _file_to_b64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


@click.group("audio-lab")
@click.pass_context
def audio_lab(ctx: click.Context) -> None:
    """Stable Audio + CLAP lab (install / generate / score)."""


# ---------------------------------------------------------------------------
# listing / status
# ---------------------------------------------------------------------------
@audio_lab.command("list")
@click.option("--kind", type=click.Choice(["all", "models", "vaes", "claps", "custom"]),
              default="all", show_default=True)
@click.pass_context
def audio_lab_list(ctx: click.Context, kind: str) -> None:
    """List Audio Lab assets (registry + custom) and their install state."""
    c = _client(ctx)
    data = c.call("GET", "/api/audio_lab/status")
    fmt = ctx.obj["fmt"]
    if kind == "all":
        emit(data, fmt)
        return
    block = data.get(kind, [])
    if kind == "custom":
        emit(block, fmt)
        return
    emit(block, fmt, columns=["variant_id", "display", "tier", "format", "size_gb", "installed"])


# ---------------------------------------------------------------------------
# Stable Audio model install / delete
# ---------------------------------------------------------------------------
@audio_lab.command("install-model")
@click.argument("variant_id")
@click.pass_context
def audio_lab_install_model(ctx: click.Context, variant_id: str) -> None:
    """Install a registered Stable Audio variant (e.g. sao-open-1.0).

    Returns immediately with a job_id. Poll with `omni-cli jobs show <id> --follow`.
    """
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/install-model",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@audio_lab.command("delete-model")
@click.argument("variant_id")
@click.pass_context
def audio_lab_delete_model(ctx: click.Context, variant_id: str) -> None:
    """Remove a Stable Audio variant's downloaded weights from disk."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/audio_lab/install-model/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# VAE swap install / delete
# ---------------------------------------------------------------------------
@audio_lab.command("install-vae")
@click.argument("variant_id")
@click.pass_context
def audio_lab_install_vae(ctx: click.Context, variant_id: str) -> None:
    """Install a Stable Audio VAE swap (e.g. sao-vae-tuned-100k)."""
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/install-vae",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@audio_lab.command("delete-vae")
@click.argument("variant_id")
@click.pass_context
def audio_lab_delete_vae(ctx: click.Context, variant_id: str) -> None:
    """Remove a VAE swap's weights."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/audio_lab/install-vae/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# CLAP install / delete
# ---------------------------------------------------------------------------
@audio_lab.command("install-clap")
@click.argument("variant_id")
@click.pass_context
def audio_lab_install_clap(ctx: click.Context, variant_id: str) -> None:
    """Install a CLAP scoring model (e.g. larger-clap-general)."""
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/install-clap",
                json={"variant_id": variant_id}, expect=(200, 202)),
         ctx.obj["fmt"])


@audio_lab.command("delete-clap")
@click.argument("variant_id")
@click.pass_context
def audio_lab_delete_clap(ctx: click.Context, variant_id: str) -> None:
    """Remove a CLAP model's weights."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/audio_lab/install-clap/{variant_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Custom HF repo install / delete
# ---------------------------------------------------------------------------
@audio_lab.command("install-custom")
@click.option("--repo", required=True, help="HuggingFace repo id (<org>/<name>)")
@click.option("--name", default=None, help="Local name (default: repo with / → _)")
@click.option("--kind", type=click.Choice(["model", "vae", "clap"]),
              default="model", show_default=True)
@click.pass_context
def audio_lab_install_custom(ctx: click.Context, repo: str,
                              name: str | None, kind: str) -> None:
    """Install any HF repo into the audio_lab tree as a custom asset.

    Loader auto-detects diffusers vs native format at load time.
    """
    body: dict = {"repo": repo, "kind": kind}
    if name:
        body["name"] = name
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/install-custom",
                json=body, expect=(200, 202)),
         ctx.obj["fmt"])


@audio_lab.command("delete-custom")
@click.argument("kind", type=click.Choice(["model", "vae", "clap"]))
@click.argument("name")
@click.pass_context
def audio_lab_delete_custom(ctx: click.Context, kind: str, name: str) -> None:
    """Remove a custom-installed Audio Lab asset by kind + name."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/audio_lab/install-custom/{kind}/{name}"),
         ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Lifecycle: load / unload / state
# ---------------------------------------------------------------------------
@audio_lab.command("state")
@click.option("--autospawn", is_flag=True, help="Spawn a worker if one isn't running")
@click.pass_context
def audio_lab_state(ctx: click.Context, autospawn: bool) -> None:
    """Report which SA / CLAP / VAE are loaded on the audio_lab worker."""
    c = _client(ctx)
    params = {"autospawn": "true"} if autospawn else None
    emit(c.call("GET", "/api/audio_lab/state", params=params), ctx.obj["fmt"])


@audio_lab.command("load")
@click.option("--sa-variant",   "sa_variant",   default=None, help="Stable Audio variant_id (e.g. sao-open-1.0)")
@click.option("--vae-variant",  "vae_variant",  default=None, help="VAE swap variant_id (or 'default')")
@click.option("--clap-variant", "clap_variant", default=None, help="CLAP variant_id (e.g. larger-clap-general)")
@click.pass_context
def audio_lab_load(ctx: click.Context, sa_variant: str | None,
                    vae_variant: str | None, clap_variant: str | None) -> None:
    """Load one or more Audio Lab components onto the worker (autospawns).

    At least one of --sa-variant / --vae-variant / --clap-variant is required.
    """
    body: dict = {}
    if sa_variant:   body["sa_variant"] = sa_variant
    if vae_variant:  body["vae_variant"] = vae_variant
    if clap_variant: body["clap_variant"] = clap_variant
    if not body:
        raise click.UsageError("Pass at least one of --sa-variant, --vae-variant, --clap-variant")
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/load", json=body), ctx.obj["fmt"])


@audio_lab.command("unload")
@click.option("--component", type=click.Choice(["sa", "clap", "vae", "all"]),
              default="all", show_default=True)
@click.pass_context
def audio_lab_unload(ctx: click.Context, component: str) -> None:
    """Free a loaded Audio Lab component (or everything)."""
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/unload",
                json={"component": component}), ctx.obj["fmt"])


@audio_lab.command("cancel")
@click.pass_context
def audio_lab_cancel(ctx: click.Context) -> None:
    """Set the cancel flag on the audio_lab worker. Best-effort: in-flight
    diffusion steps still finish, but the next candidate / CLAP window aborts."""
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/cancel"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Inference: generate / generate-ranked / score
# ---------------------------------------------------------------------------
def _gen_options(f):
    """Common Stable Audio sampling params for generate/generate-ranked."""
    f = click.option("--seed",            type=int,   default=None,
                     help="Fixed RNG seed (random if omitted)")(f)
    f = click.option("--sampler",         default=None, help="Sampler name (see /api/audio_lab/samplers)")(f)
    f = click.option("--sigma-max",       "sigma_max", type=float, default=None)(f)
    f = click.option("--sigma-min",       "sigma_min", type=float, default=None)(f)
    f = click.option("--cfg",             "cfg_scale", type=float, default=7.0, show_default=True,
                     help="Classifier-free guidance scale")(f)
    f = click.option("--steps",           type=int,   default=100, show_default=True)(f)
    f = click.option("--duration",        "duration_s", type=float, default=10.0, show_default=True,
                     help="Target length in seconds")(f)
    f = click.option("--negative-prompt", "negative_prompt", default=None,
                     help="Things to avoid in the output")(f)
    f = click.option("--prompt",          required=True, help="Text prompt to condition on")(f)
    return f


def _save_url_to(c, url: str, out_path: Path) -> bool:
    """Stream a file from the gateway to a local path. Returns True on
    success, False (and prints to stderr) on HTTP failure."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with c.stream("GET", url) as resp:
        if resp.status_code not in (200, 206):
            click.echo(f"[{resp.status_code}] GET {url}", err=True)
            return False
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    return True


@audio_lab.command("generate")
@_gen_options
@click.option("--out", "out_path",
              type=click.Path(dir_okay=False, writable=True),
              default=None, help="Local file to write the wav to (else print URL only)")
@click.pass_context
def audio_lab_generate(ctx: click.Context, prompt: str, negative_prompt: str | None,
                        duration_s: float, steps: int, cfg_scale: float,
                        sigma_min: float | None, sigma_max: float | None,
                        sampler: str | None, seed: int | None,
                        out_path: str | None) -> None:
    """Single text→audio generation. Persists one wav on the server."""
    body: dict = {
        "prompt": prompt, "duration_s": duration_s,
        "steps": steps, "cfg_scale": cfg_scale,
    }
    if negative_prompt: body["negative_prompt"] = negative_prompt
    if sigma_min is not None: body["sigma_min"] = sigma_min
    if sigma_max is not None: body["sigma_max"] = sigma_max
    if sampler:    body["sampler"] = sampler
    if seed is not None: body["seed"] = seed
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/generate", json=body)
    if out_path:
        url = data.get("url")
        if not url:
            raise click.ClickException("server response missing 'url'")
        if _save_url_to(c, url, Path(out_path)):
            click.echo(f"saved {out_path}")
    emit(data, ctx.obj["fmt"])


@audio_lab.command("generate-ranked")
@_gen_options
@click.option("--n", "n", type=int, default=4, show_default=True,
              help="Number of candidates to generate")
@click.option("--clap-variant", "clap_variant", default=None,
              help="Override the loaded CLAP variant for scoring")
@click.option("--score-prompt", "score_prompt", default=None,
              help="Prompt used for CLAP scoring (defaults to --prompt)")
@click.option("--out-dir", "out_dir",
              type=click.Path(file_okay=False, writable=True),
              default=None, help="Save all candidates locally to this directory")
@click.pass_context
def audio_lab_generate_ranked(ctx: click.Context, prompt: str,
                               negative_prompt: str | None,
                               duration_s: float, steps: int, cfg_scale: float,
                               sigma_min: float | None, sigma_max: float | None,
                               sampler: str | None, seed: int | None,
                               n: int, clap_variant: str | None,
                               score_prompt: str | None,
                               out_dir: str | None) -> None:
    """Generate N candidates, CLAP-score each, save ranked with best_ marker."""
    body: dict = {
        "prompt": prompt, "duration_s": duration_s,
        "steps": steps, "cfg_scale": cfg_scale, "n": n,
    }
    if negative_prompt: body["negative_prompt"] = negative_prompt
    if sigma_min is not None: body["sigma_min"] = sigma_min
    if sigma_max is not None: body["sigma_max"] = sigma_max
    if sampler:        body["sampler"] = sampler
    if seed is not None: body["seed"] = seed
    if clap_variant:   body["clap_variant"] = clap_variant
    if score_prompt:   body["score_prompt"] = score_prompt
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/generate-ranked", json=body)
    if out_dir:
        out_dir_p = Path(out_dir)
        saved = 0
        for r in data.get("results", []):
            url = r.get("url")
            filename = r.get("filename")
            if not url or not filename:
                raise click.ClickException("server result missing 'url' or 'filename'")
            if _save_url_to(c, url, out_dir_p / filename):
                saved += 1
            if r.get("best") and r.get("best_filename") and r.get("best_url"):
                _save_url_to(c, r["best_url"], out_dir_p / r["best_filename"])
        click.echo(f"saved {saved}/{len(data.get('results', []))} files to {out_dir}")
    emit(data, ctx.obj["fmt"])


@audio_lab.command("score")
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--prompt", required=True, help="Text to compare against the audio")
@click.option("--clap-variant", default=None,
              help="Override the loaded CLAP variant")
@click.pass_context
def audio_lab_score(ctx: click.Context, audio_path: str, prompt: str,
                     clap_variant: str | None) -> None:
    """CLAP cosine similarity between a text prompt and an audio file."""
    audio_b64 = _file_to_b64(audio_path)
    body: dict = {"text": prompt, "audio_base64": audio_b64}
    if clap_variant: body["clap_variant"] = clap_variant
    c = _client(ctx)
    emit(c.call("POST", "/api/audio_lab/score", json=body), ctx.obj["fmt"])


@audio_lab.command("samplers")
@click.pass_context
def audio_lab_samplers(ctx: click.Context) -> None:
    """List sampler names accepted by the Stable Audio backend."""
    c = _client(ctx)
    emit(c.call("GET", "/api/audio_lab/samplers"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Phase 3 — A2A / Inpaint / Uncond / VAE Lab
# ---------------------------------------------------------------------------
@audio_lab.command("a2a")
@_gen_options
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Init audio clip (the seed to vary)")
@click.option("--noise", "init_noise_level", type=float, default=0.7,
              show_default=True, help="0=keep init, 1=ignore init")
@click.option("--out", "out_path",
              type=click.Path(dir_okay=False, writable=True),
              default=None, help="Save the result here")
@click.pass_context
def audio_lab_a2a(ctx: click.Context, prompt: str, negative_prompt: str | None,
                   duration_s: float, steps: int, cfg_scale: float,
                   sigma_min: float | None, sigma_max: float | None,
                   sampler: str | None, seed: int | None,
                   audio_path: str, init_noise_level: float,
                   out_path: str | None) -> None:
    """Audio-to-audio: feed init clip + prompt, get a variation."""
    body: dict = {
        "prompt": prompt, "duration_s": duration_s,
        "steps": steps, "cfg_scale": cfg_scale,
        "init_audio_base64": _file_to_b64(audio_path),
        "init_noise_level": init_noise_level,
    }
    if negative_prompt: body["negative_prompt"] = negative_prompt
    if sigma_min is not None: body["sigma_min"] = sigma_min
    if sigma_max is not None: body["sigma_max"] = sigma_max
    if sampler:    body["sampler"] = sampler
    if seed is not None: body["seed"] = seed
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/a2a", json=body)
    if out_path:
        url = data.get("url")
        if not url:
            raise click.ClickException("server response missing 'url'")
        if _save_url_to(c, url, Path(out_path)):
            click.echo(f"saved {out_path}")
    emit(data, ctx.obj["fmt"])


def _parse_mask(s: str) -> tuple[float, float]:
    """'5.0:10.0' → (5.0, 10.0)."""
    if ":" not in s:
        raise click.BadParameter("mask must be START:END (seconds)")
    a, b = s.split(":", 1)
    try:
        return float(a), float(b)
    except ValueError as e:
        raise click.BadParameter(f"mask values must be floats: {e}")


@audio_lab.command("inpaint")
@_gen_options
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="Source clip to inpaint")
@click.option("--mask", "mask", required=True, callback=lambda c, p, v: _parse_mask(v),
              help="Mask region in seconds, format START:END (e.g. 5.0:10.0)")
@click.option("--out", "out_path",
              type=click.Path(dir_okay=False, writable=True),
              default=None, help="Save the result here")
@click.pass_context
def audio_lab_inpaint(ctx: click.Context, prompt: str, negative_prompt: str | None,
                       duration_s: float, steps: int, cfg_scale: float,
                       sigma_min: float | None, sigma_max: float | None,
                       sampler: str | None, seed: int | None,
                       audio_path: str, mask: tuple[float, float],
                       out_path: str | None) -> None:
    """Regenerate a time range of an existing clip."""
    mask_start, mask_end = mask
    body: dict = {
        "prompt": prompt, "duration_s": duration_s,
        "steps": steps, "cfg_scale": cfg_scale,
        "init_audio_base64": _file_to_b64(audio_path),
        "mask_start_s": mask_start, "mask_end_s": mask_end,
    }
    if negative_prompt: body["negative_prompt"] = negative_prompt
    if sigma_min is not None: body["sigma_min"] = sigma_min
    if sigma_max is not None: body["sigma_max"] = sigma_max
    if sampler:    body["sampler"] = sampler
    if seed is not None: body["seed"] = seed
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/inpaint", json=body)
    if out_path:
        url = data.get("url")
        if not url:
            raise click.ClickException("server response missing 'url'")
        if _save_url_to(c, url, Path(out_path)):
            click.echo(f"saved {out_path}")
    emit(data, ctx.obj["fmt"])


@audio_lab.command("uncond")
@click.option("--duration", "duration_s", type=float, default=10.0, show_default=True)
@click.option("--steps", type=int, default=100, show_default=True)
@click.option("--sampler", default=None)
@click.option("--sigma-min", "sigma_min", type=float, default=None)
@click.option("--sigma-max", "sigma_max", type=float, default=None)
@click.option("--seed", type=int, default=None)
@click.option("--out", "out_path",
              type=click.Path(dir_okay=False, writable=True),
              default=None, help="Save the result here")
@click.pass_context
def audio_lab_uncond(ctx: click.Context, duration_s: float, steps: int,
                      sampler: str | None, sigma_min: float | None,
                      sigma_max: float | None, seed: int | None,
                      out_path: str | None) -> None:
    """Unconditional generation (no prompt)."""
    body: dict = {"duration_s": duration_s, "steps": steps}
    if sampler:    body["sampler"] = sampler
    if sigma_min is not None: body["sigma_min"] = sigma_min
    if sigma_max is not None: body["sigma_max"] = sigma_max
    if seed is not None: body["seed"] = seed
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/uncond", json=body)
    if out_path:
        url = data.get("url")
        if not url:
            raise click.ClickException("server response missing 'url'")
        if _save_url_to(c, url, Path(out_path)):
            click.echo(f"saved {out_path}")
    emit(data, ctx.obj["fmt"])


@audio_lab.group("vae")
def audio_lab_vae() -> None:
    """VAE-only ops (encode, decode, reconstruct)."""


@audio_lab_vae.command("encode")
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_path", default=None,
              type=click.Path(dir_okay=False, writable=True),
              help="Save the latent (.pt) bytes here")
@click.pass_context
def audio_lab_vae_encode(ctx: click.Context, audio_path: str,
                          out_path: str | None) -> None:
    """Encode a wav → latent .pt bytes (base64-printed if --out omitted)."""
    body = {"audio_base64": _file_to_b64(audio_path)}
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/vae/encode", json=body)
    if out_path:
        latent_b64 = data.get("latent_base64")
        if not latent_b64:
            raise click.ClickException("server response missing 'latent_base64'")
        Path(out_path).write_bytes(base64.b64decode(latent_b64))
        click.echo(f"saved {out_path}  shape={data.get('shape')}")
    else:
        emit({"shape": data.get("shape"),
              "sample_rate": data.get("sample_rate")}, ctx.obj["fmt"])


@audio_lab_vae.command("decode")
@click.option("--latent", "latent_path", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_path", required=True,
              type=click.Path(dir_okay=False, writable=True))
@click.pass_context
def audio_lab_vae_decode(ctx: click.Context, latent_path: str, out_path: str) -> None:
    """Decode a latent (.pt) back to a wav."""
    body = {"latent_base64": _file_to_b64(latent_path)}
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/vae/decode", json=body)
    url = data.get("url")
    if not url:
        raise click.ClickException("server response missing 'url'")
    if _save_url_to(c, url, Path(out_path)):
        click.echo(f"saved {out_path}")
    emit(data, ctx.obj["fmt"])


@audio_lab_vae.command("reconstruct")
@click.option("--audio", "audio_path", required=True,
              type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_path", required=True,
              type=click.Path(dir_okay=False, writable=True))
@click.pass_context
def audio_lab_vae_reconstruct(ctx: click.Context, audio_path: str, out_path: str) -> None:
    """Round-trip audio → latent → audio (VAE quality check)."""
    body = {"audio_base64": _file_to_b64(audio_path)}
    c = _client(ctx)
    data = c.call("POST", "/api/audio_lab/vae/reconstruct", json=body)
    url = data.get("url")
    if not url:
        raise click.ClickException("server response missing 'url'")
    if _save_url_to(c, url, Path(out_path)):
        diff = data.get("diff_rms")
        diff_str = f"{diff:.6f}" if isinstance(diff, (int, float)) else "?"
        click.echo(f"saved {out_path}  diff_rms={diff_str}")
    emit(data, ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# Phase 5 — jobs listing + bulk download + delete
# ---------------------------------------------------------------------------
@audio_lab.command("jobs")
@click.option("--mode", default=None, help="Filter by mode (generate, a2a, inpaint, …)")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def audio_lab_jobs(ctx: click.Context, mode: str | None, limit: int) -> None:
    """List past Audio Lab inference jobs, newest first."""
    c = _client(ctx)
    params = {"limit": limit}
    if mode:
        params["mode"] = mode
    data = c.call("GET", "/api/audio_lab/jobs", params=params)
    emit(data.get("jobs", []), ctx.obj["fmt"],
         columns=["job_id", "mode", "created_at", "sa_variant", "n", "best_score"])


@audio_lab.command("download")
@click.argument("job_id")
@click.option("--out-dir", "out_dir", required=True,
              type=click.Path(file_okay=False, writable=True))
@click.pass_context
def audio_lab_download(ctx: click.Context, job_id: str, out_dir: str) -> None:
    """Download all artifacts for a job (as a ZIP, then extract)."""
    import zipfile
    out_p = Path(out_dir).resolve()
    out_p.mkdir(parents=True, exist_ok=True)
    target_dir = (out_p / job_id).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    c = _client(ctx)
    zip_path = out_p / f"{job_id}.zip"
    _save_url_to(c, f"/api/audio_lab/zip/{job_id}", zip_path)
    # Defend against zip-slip: validate each member's resolved path stays
    # inside target_dir before extracting. Reject absolute paths, ``..``
    # segments, and symlink-style names.
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or ".." in Path(name).parts:
                click.echo(f"refusing unsafe zip entry: {name}", err=True)
                zip_path.unlink(missing_ok=True)
                sys.exit(2)
            resolved = (target_dir / name).resolve()
            try:
                resolved.relative_to(target_dir)
            except ValueError:
                click.echo(f"zip entry escapes target dir: {name}", err=True)
                zip_path.unlink(missing_ok=True)
                sys.exit(2)
        zf.extractall(target_dir)
    zip_path.unlink(missing_ok=True)
    click.echo(f"extracted to {target_dir}")


@audio_lab.command("delete-job")
@click.argument("job_id")
@click.pass_context
def audio_lab_delete_job(ctx: click.Context, job_id: str) -> None:
    """Remove an Audio Lab job's files from disk."""
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/audio_lab/jobs/{job_id}"), ctx.obj["fmt"])
