"""Omni Studio CLI - first-party tool for the gateway HTTP API.

The CLI is a thin click wrapper over ``cli.client.Client``. Each subcommand
issues exactly one HTTP request (or one streaming connection) and renders
the result via ``cli.formatters.emit``.

Run ``omni-cli --help`` for the full command tree.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import click

from cli.client import Client, DEFAULT_BASE_URL, cache_user_token, resolve_token
from cli.formatters import emit


def _common_options(f):
    f = click.option("--out-format", type=click.Choice(["text", "json", "jsonl"]),
                     default="text", show_default=True,
                     help="Output format")(f)
    f = click.option("--token", default=None, envvar="OMNI_API_TOKEN",
                     help="API token (else env/file/loopback)")(f)
    f = click.option("--base-url", default=DEFAULT_BASE_URL, envvar="OMNI_BASE_URL",
                     help="Gateway/bridge base URL")(f)
    return f


def _client(ctx) -> Client:
    return Client(base_url=ctx.obj["base_url"], token=ctx.obj["token"])


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@_common_options
@click.pass_context
def cli(ctx: click.Context, base_url: str, token: str | None, out_format: str):
    """Omni Studio CLI."""
    ctx.ensure_object(dict)
    ctx.obj["base_url"] = base_url
    ctx.obj["token"] = token
    ctx.obj["fmt"] = out_format


# ---------------------------------------------------------------------------
# status / session
# ---------------------------------------------------------------------------
@cli.command()
@click.pass_context
def status(ctx):
    """One-shot health and capacity overview."""
    c = _client(ctx)
    info = c.call("GET", "/api/system/info")
    workers = c.call("GET", "/api/workers")
    instances = c.call("GET", "/api/comfy/instances")
    summary = {
        "system": info,
        "workers": workers.get("workers", []),
        "comfy_instances": instances.get("instances", []),
    }
    emit(summary, ctx.obj["fmt"])


@cli.group()
def session():
    """Inspect or refresh the cached API token."""


@session.command("show")
@click.pass_context
def session_show(ctx):
    c = _client(ctx)
    emit({"token": c.token, "base_url": c.base_url}, ctx.obj["fmt"])


@session.command("refresh")
@click.pass_context
def session_refresh(ctx):
    """Save the resolved token to ``~/.config/omni-studio/token`` for next run."""
    token = resolve_token(ctx.obj["token"], ctx.obj["base_url"], refresh=True)
    cache_user_token(token)
    emit({"status": "cached", "path": "~/.config/omni-studio/token"}, ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------
@cli.group()
def devices():
    """List host devices (GPUs, CPU)."""


@devices.command("list")
@click.pass_context
def devices_list(ctx):
    c = _client(ctx)
    data = c.call("GET", "/api/devices")
    emit(data.get("devices", data), ctx.obj["fmt"],
         columns=["index", "name", "vram_total_mb", "vram_used_mb"])


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------
@cli.group()
def workers():
    """Omni model worker lifecycle."""


@workers.command("list")
@click.pass_context
def workers_list(ctx):
    c = _client(ctx)
    data = c.call("GET", "/api/workers")
    emit(data.get("workers", []), ctx.obj["fmt"],
         columns=["worker_id", "model", "device", "port", "status"])


@workers.command("spawn")
@click.argument("model")
@click.option("--device", default=None)
@click.option("--variant", default=None)
@click.option("--lora", default=None)
@click.pass_context
def workers_spawn(ctx, model, device, variant, lora):
    c = _client(ctx)
    body = {"model": model}
    for k, v in [("device", device), ("variant", variant), ("lora", lora)]:
        if v:
            body[k] = v
    emit(c.call("POST", "/api/workers/spawn", json=body), ctx.obj["fmt"])


@workers.command("kill")
@click.argument("worker_id")
@click.pass_context
def workers_kill(ctx, worker_id):
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/workers/{worker_id}"), ctx.obj["fmt"])


@workers.command("kill-all")
@click.pass_context
def workers_kill_all(ctx):
    c = _client(ctx)
    emit(c.call("POST", "/api/workers/kill-all"), ctx.obj["fmt"])


@workers.command("logs")
@click.argument("worker_id")
@click.option("--lines", default=100, type=int)
@click.pass_context
def workers_logs(ctx, worker_id, lines):
    c = _client(ctx)
    data = c.call("GET", f"/api/workers/{worker_id}/logs", params={"lines": lines})
    for line in data.get("lines", []):
        click.echo(line)


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
@cli.group()
def chat():
    """One-shot or streaming chat against an Omni worker."""


def _file_to_b64(path: str) -> str:
    import base64
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


@chat.command("send")
@click.argument("model")
@click.argument("prompt")
@click.option("--image", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--audio", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--video", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--max-tokens", "max_new_tokens", type=int, default=512)
@click.option("--temperature", type=float, default=0.7)
@click.option("--top-p", "top_p", type=float, default=0.9)
@click.option("--stream", is_flag=True, help="SSE token stream")
@click.pass_context
def chat_send(ctx, model, prompt, image, audio, video,
              max_new_tokens, temperature, top_p, stream):
    c = _client(ctx)
    body = {
        "model": model, "text": prompt,
        "max_new_tokens": max_new_tokens,
        "temperature": temperature, "top_p": top_p,
    }
    if image:
        body["image"] = _file_to_b64(image)
    if audio:
        body["audio"] = _file_to_b64(audio)
    if video:
        body["video"] = _file_to_b64(video)
    if not stream:
        emit(c.call("POST", f"/api/chat/{model}", json=body), ctx.obj["fmt"])
        return
    # Stream path
    import signal as _signal
    job_id_holder = {"id": None}

    def _sigint(*_):
        if job_id_holder["id"]:
            try:
                c.post(f"/api/chat/{model}/cancel/{job_id_holder['id']}")
            except Exception:
                pass
        sys.exit(130)
    _signal.signal(_signal.SIGINT, _sigint)

    with c.stream("POST", f"/api/chat/{model}/stream", json=body) as resp:
        if resp.status_code != 200:
            click.echo(f"[{resp.status_code}] {resp.read().decode('utf-8', 'replace')[:300]}",
                       err=True)
            sys.exit(2)
        for raw in resp.iter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            payload = raw[len("data:"):].strip()
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "job_id" in evt:
                job_id_holder["id"] = evt["job_id"]
                click.echo(f"# job_id={evt['job_id']}", err=True)
            elif "delta" in evt:
                click.echo(evt["delta"], nl=False)
            elif evt.get("done"):
                click.echo("")
                break
            elif evt.get("cancelled"):
                click.echo("\n# cancelled", err=True)
                sys.exit(130)
            elif "error" in evt:
                click.echo(f"\n# error: {evt['error']}", err=True)
                sys.exit(2)


@chat.command("cancel")
@click.argument("model")
@click.argument("job_id")
@click.pass_context
def chat_cancel(ctx, model, job_id):
    c = _client(ctx)
    emit(c.call("POST", f"/api/chat/{model}/cancel/{job_id}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# comfy
# ---------------------------------------------------------------------------
@cli.group()
def comfy():
    """ComfyUI instance + asset management."""


@comfy.command("list")
@click.pass_context
def comfy_list(ctx):
    c = _client(ctx)
    data = c.call("GET", "/api/comfy/instances")
    emit(data.get("instances", []), ctx.obj["fmt"],
         columns=["instance_id", "device", "port", "vram_mode", "status"])


@comfy.command("start")
@click.option("--device", default=None)
@click.option(
    "--gpu-pool", default="",
    help="Comma-separated CUDA devices to expose; --device remains primary.",
)
@click.option("--vram", "vram_mode", default="normal",
              type=click.Choice(["normal", "low", "none", "cpu"]))
@click.pass_context
def comfy_start(ctx, device, gpu_pool, vram_mode):
    c = _client(ctx)
    body = {"vram_mode": vram_mode}
    if device:
        body["device"] = device
    if gpu_pool:
        body["gpu_pool"] = [item.strip() for item in gpu_pool.split(",") if item.strip()]
    emit(c.call("POST", "/api/comfy/start", json=body), ctx.obj["fmt"])


@comfy.command("stop")
@click.argument("instance_id")
@click.pass_context
def comfy_stop(ctx, instance_id):
    c = _client(ctx)
    emit(c.call("POST", f"/api/comfy/{instance_id}/stop"), ctx.obj["fmt"])


@comfy.command("stop-all")
@click.pass_context
def comfy_stop_all(ctx):
    c = _client(ctx)
    emit(c.call("POST", "/api/comfy/stop-all"), ctx.obj["fmt"])


@comfy.command("logs")
@click.argument("instance_id")
@click.option("--lines", default=100, type=int)
@click.pass_context
def comfy_logs(ctx, instance_id, lines):
    c = _client(ctx)
    data = c.call("GET", f"/api/comfy/{instance_id}/logs", params={"lines": lines})
    for line in data.get("lines", []):
        click.echo(line)


@comfy.command("interrupt")
@click.argument("instance_id")
@click.pass_context
def comfy_interrupt(ctx, instance_id):
    c = _client(ctx)
    emit(c.call("POST", f"/api/comfy/{instance_id}/proxy/interrupt"), ctx.obj["fmt"])


@comfy.command("free")
@click.argument("instance_id")
@click.option("--unload-models", is_flag=True)
@click.pass_context
def comfy_free(ctx, instance_id, unload_models):
    c = _client(ctx)
    body = {"unload_models": unload_models, "free_memory": True}
    emit(c.call("POST", f"/api/comfy/{instance_id}/proxy/free", json=body), ctx.obj["fmt"])


@comfy.command("watch")
@click.argument("instance_id")
@click.option("--client-id", default=None,
              help="Filter previews to a specific ComfyUI client_id")
@click.option("--no-previews", is_flag=True,
              help="Skip preview frames (status-only)")
@click.pass_context
def comfy_watch(ctx, instance_id, client_id, no_previews):
    """Stream live ComfyUI events via the previews/stream SSE endpoint.

    Pretty-prints sampling progress, executing nodes, and (optionally)
    saves preview frames to /tmp. The simpler-to-write SSE form is used
    here instead of opening the raw WS so we don't pull websockets into
    the CLI's runtime path.
    """
    import base64
    import binascii
    import datetime as _dt
    import tempfile
    c = _client(ctx)
    params = {}
    if client_id:
        params["client_id"] = client_id
    url = f"/api/comfy/{quote(instance_id, safe='')}/previews/stream"

    click.echo(f"watching {instance_id}... (Ctrl-C to stop)", err=True)
    try:
        with c.stream("GET", url, params=params) as resp:
            if resp.status_code != 200:
                click.echo(f"[{resp.status_code}] {resp.read().decode('utf-8','replace')[:300]}",
                           err=True)
                sys.exit(2)
            for raw in resp.iter_lines():
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    evt = json.loads(raw[len("data:"):].strip())
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type", "?")
                ts = _dt.datetime.now().strftime("%H:%M:%S")
                if etype == "progress":
                    bar = ""
                    if evt.get("max"):
                        ratio = evt["value"] / evt["max"]
                        bar_len = 20
                        fill = int(bar_len * ratio)
                        bar = "[" + "#" * fill + "-" * (bar_len - fill) + "]"
                    click.echo(f"{ts} progress {evt.get('value')}/{evt.get('max')} {bar}")
                elif etype == "executing":
                    click.echo(f"{ts} node={evt.get('node')} prompt={evt.get('prompt_id', '?')[:8]}")
                elif etype == "executed":
                    click.echo(f"{ts} done node={evt.get('node')}")
                elif etype == "preview":
                    if no_previews:
                        continue
                    image_b64 = evt.get("image_b64")
                    if not image_b64:
                        continue
                    fmt = evt.get("format", "jpeg")
                    fmt = fmt if fmt in {"jpeg", "jpg", "png", "webp"} else "jpg"
                    try:
                        data = base64.b64decode(image_b64, validate=True)
                        with tempfile.NamedTemporaryFile(prefix="omni-preview-", suffix="." + fmt, delete=False) as fh:
                            fname = fh.name
                            fh.write(data)
                        click.echo(f"{ts} preview saved: {fname} ({fmt})")
                    except (binascii.Error, ValueError, KeyError, OSError) as e:
                        click.echo(f"{ts} preview skipped: {e}", err=True)
                elif etype == "error":
                    click.echo(f"{ts} error: {evt.get('message')}", err=True)
                else:
                    click.echo(f"{ts} {etype}: {evt}")
    except KeyboardInterrupt:
        click.echo("watch stopped", err=True)
        sys.exit(0)


# ---- comfy assets ----------------------------------------------------------
@comfy.group("assets")
def comfy_assets():
    """ComfyUI checkpoint / VAE / LoRA / etc. management."""


@comfy_assets.command("list")
@click.argument("category", required=False)
@click.pass_context
def assets_list(ctx, category):
    c = _client(ctx)
    if category:
        data = c.call("GET", f"/api/assets/comfy/{category}")
        emit(data.get("files", []), ctx.obj["fmt"], columns=["name", "size_mb"])
    else:
        emit(c.call("GET", "/api/assets/comfy/scan"), ctx.obj["fmt"])


@comfy_assets.command("install")
@click.argument("category")
@click.argument("repo")
@click.option("--file", "file_", default=None)
@click.option("--name", default=None)
@click.pass_context
def assets_install(ctx, category, repo, file_, name):
    c = _client(ctx)
    body = {"repo": repo}
    if file_:
        body["file"] = file_
    if name:
        body["name"] = name
    emit(c.call("POST", f"/api/assets/comfy/{category}/install", json=body),
         ctx.obj["fmt"])


@comfy_assets.command("delete")
@click.argument("category")
@click.argument("filename")
@click.pass_context
def assets_delete(ctx, category, filename):
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/assets/comfy/{quote(category, safe='')}/{quote(filename, safe='')}"),
         ctx.obj["fmt"])


@comfy_assets.command("upload")
@click.argument("category")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--sha256", default=None)
@click.option("--overwrite", is_flag=True)
@click.pass_context
def assets_upload(ctx, category, path, sha256, overwrite):
    c = _client(ctx)
    fname = Path(path).name
    with open(path, "rb") as fh:
        files = {"file": (fname, fh)}
        data = {"overwrite": "true" if overwrite else "false"}
        if sha256:
            data["sha256"] = sha256
        r = c._http.post(f"/api/assets/comfy/upload/{category}", files=files, data=data)
        if r.status_code != 200:
            click.echo(f"[{r.status_code}] {r.text[:300]}", err=True)
            sys.exit(2)
        emit(r.json(), ctx.obj["fmt"])


@comfy.group("nodes")
def comfy_nodes():
    """ComfyUI custom node management."""


@comfy_nodes.command("list")
@click.pass_context
def nodes_list(ctx):
    c = _client(ctx)
    data = c.call("GET", "/api/assets/comfy/nodes")
    emit(data.get("nodes", []), ctx.obj["fmt"], columns=["name", "has_requirements"])


@comfy_nodes.command("install")
@click.argument("repo_url")
@click.option("--ref", default=None)
@click.pass_context
def nodes_install(ctx, repo_url, ref):
    c = _client(ctx)
    body = {"repo_url": repo_url}
    if ref:
        body["ref"] = ref
    emit(c.call("POST", "/api/assets/comfy/nodes/install", json=body),
         ctx.obj["fmt"])


@comfy_nodes.command("delete")
@click.argument("name")
@click.pass_context
def nodes_delete(ctx, name):
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/assets/comfy/nodes/{name}"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------
@cli.group()
def workflows():
    """Workflow JSON CRUD + queue."""


@workflows.command("list")
@click.pass_context
def wf_list(ctx):
    c = _client(ctx)
    data = c.call("GET", "/api/workflows")
    emit(data.get("workflows", []), ctx.obj["fmt"],
         columns=["filename", "size_kb", "nodes"])


@workflows.command("show")
@click.argument("name")
@click.pass_context
def wf_show(ctx, name):
    c = _client(ctx)
    if not name.endswith(".json"):
        name = name + ".json"
    r = c.get(f"/api/workflows/{quote(name, safe='')}")
    if r.status_code != 200:
        click.echo(f"[{r.status_code}] {r.text[:200]}", err=True)
        sys.exit(2)
    click.echo(r.text)


@workflows.command("save")
@click.argument("name")
@click.option("--file", "file_", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--overwrite", is_flag=True)
@click.pass_context
def wf_save(ctx, name, file_, overwrite):
    c = _client(ctx)
    if not name.endswith(".json"):
        name = name + ".json"
    payload = json.loads(Path(file_).read_text(encoding="utf-8"))
    body = {"workflow": payload, "overwrite": overwrite}
    emit(c.call("PUT", f"/api/workflows/{name}", json=body), ctx.obj["fmt"])


@workflows.command("delete")
@click.argument("name")
@click.pass_context
def wf_delete(ctx, name):
    c = _client(ctx)
    if not name.endswith(".json"):
        name = name + ".json"
    emit(c.call("DELETE", f"/api/workflows/{name}"), ctx.obj["fmt"])


@workflows.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--overwrite", is_flag=True)
@click.pass_context
def wf_import(ctx, path, overwrite):
    c = _client(ctx)
    fname = Path(path).name
    with open(path, "rb") as fh:
        files = {"file": (fname, fh, "application/json")}
        r = c._http.post("/api/workflows/import", files=files,
                          params={"overwrite": "true" if overwrite else "false"})
        if r.status_code != 200:
            click.echo(f"[{r.status_code}] {r.text[:300]}", err=True)
            sys.exit(2)
        emit(r.json(), ctx.obj["fmt"])


@workflows.command("run")
@click.option("--file", "file_", default=None, type=click.Path(exists=True, dir_okay=False))
@click.option("--name", default=None, help="Saved workflow filename (without path).")
@click.option("--instance", "instance_id", default=None)
@click.pass_context
def wf_run(ctx, file_, name, instance_id):
    c = _client(ctx)
    if file_:
        payload = json.loads(Path(file_).read_text(encoding="utf-8"))
        body = {"workflow": payload}
        if instance_id:
            body["instance_id"] = instance_id
        emit(c.call("POST", "/api/workflows/run", json=body), ctx.obj["fmt"])
    elif name:
        if not name.endswith(".json"):
            name = name + ".json"
        params = {}
        if instance_id:
            params["instance_id"] = instance_id
        emit(c.call("POST", f"/api/workflows/{name}/queue", params=params),
             ctx.obj["fmt"])
    else:
        click.echo("--file or --name is required", err=True)
        sys.exit(2)


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------
@cli.group()
def outputs():
    """Generated media files."""


@outputs.command("list")
@click.option("--since", default=None)
@click.option("--kind", type=click.Choice(["output", "input", "temp", "omni"]),
              default="output", help="Storage root to list.")
@click.option("--media-kind",
              type=click.Choice(["image", "video", "audio", "data", "other"]),
              default=None, help="Optional media-type filter.")
@click.option("--subdir", default="", help="Optional subdirectory under the root.")
@click.option("--prefix", default="", help="Optional filename-prefix filter.")
@click.option("--prompt-id", default=None, help="Optional Comfy prompt ID filter.")
@click.option("--probe/--no-probe", default=False,
              help="Probe media metadata (maximum list size 50).")
@click.option("--limit", type=int, default=100)
@click.pass_context
def out_list(ctx, since, kind, media_kind, subdir, prefix, prompt_id, probe, limit):
    c = _client(ctx)
    params = {"kind": kind, "limit": limit, "probe": probe}
    if since:
        params["since"] = since
    if media_kind:
        params["media_kind"] = media_kind
    if subdir:
        params["subdir"] = subdir
    if prefix:
        params["prefix"] = prefix
    if prompt_id:
        params["prompt_id"] = prompt_id
    data = c.call("GET", "/api/outputs", params=params)
    emit(data.get("files", data), ctx.obj["fmt"],
         columns=["path", "size", "mtime", "mime"])


@outputs.command("get")
@click.option("--kind", type=click.Choice(["output", "input", "temp", "omni"]), default="output")
@click.argument("relpath")
@click.option("--out", "out_path", default=None,
              type=click.Path(dir_okay=False))
@click.pass_context
def out_get(ctx, relpath, out_path, kind):
    c = _client(ctx)
    target = Path(out_path) if out_path else Path(Path(relpath).name)
    with c.stream("GET", f"/api/outputs/{quote(relpath, safe='/')}", params={"kind": kind}) as resp:
        if resp.status_code not in (200, 206):
            click.echo(f"[{resp.status_code}] {resp.read().decode('utf-8', 'replace')[:300]}",
                       err=True)
            sys.exit(2)
        with open(target, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    click.echo(f"saved {target}")


@outputs.command("delete")
@click.option("--kind", type=click.Choice(["output", "input", "temp", "omni"]), default="output")
@click.argument("relpath")
@click.pass_context
def out_delete(ctx, relpath, kind):
    c = _client(ctx)
    emit(c.call("DELETE", f"/api/outputs/{quote(relpath, safe='/')}", params={"kind": kind}), ctx.obj["fmt"])


@outputs.command("zip")
@click.option("--kind", type=click.Choice(["output", "input", "temp", "omni"]), default="output")
@click.argument("relpaths", nargs=-1, required=True)
@click.option("--out", "out_path", default="outputs.zip", type=click.Path(dir_okay=False))
@click.option("--name", default=None)
@click.pass_context
def out_zip(ctx, relpaths, out_path, name, kind):
    c = _client(ctx)
    body = {"paths": list(relpaths), "kind": kind}
    if name:
        body["name"] = name
    with c.stream("POST", "/api/outputs/zip", json=body) as resp:
        if resp.status_code != 200:
            click.echo(f"[{resp.status_code}] {resp.read().decode('utf-8', 'replace')[:300]}",
                       err=True)
            sys.exit(2)
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
    click.echo(f"saved {out_path}")


@outputs.command("prune")
@click.option("--kind", type=click.Choice(["output", "input", "temp", "omni"]), default="output")
@click.option("--older-than", default=None, help="e.g. 7d, 30d, 12h")
@click.option("--max-gb", type=float, default=None)
@click.option("--dry-run", is_flag=True)
@click.pass_context
def out_prune(ctx, older_than, max_gb, dry_run, kind):
    c = _client(ctx)
    body = {"dry_run": dry_run, "kind": kind}
    if older_than:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([dhm])", older_than)
        if not match:
            raise click.BadParameter("Use a positive duration such as 7d, 12h, or 30m", param_hint="--older-than")
        body["older_than_days"] = float(match[1]) / {"d": 1, "h": 24, "m": 1440}[match[2]]
    if max_gb is not None:
        body["max_total_gb"] = max_gb
    emit(c.call("POST", "/api/outputs/prune", json=body), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
@cli.group()
def setup():
    """Model + LoRA installs and HF token storage."""


@setup.command("install")
@click.argument("model")
@click.option("--variant", default=None)
@click.pass_context
def setup_install(ctx, model, variant):
    c = _client(ctx)
    if variant:
        emit(c.call("POST", "/api/setup/install-variant",
                    json={"model": model, "variant_id": variant}),
             ctx.obj["fmt"])
    else:
        emit(c.call("POST", f"/api/setup/install/{model}"), ctx.obj["fmt"])


@setup.command("install-lora")
@click.argument("repo")
@click.option("--name", default=None)
@click.pass_context
def setup_install_lora(ctx, repo, name):
    c = _client(ctx)
    body = {"repo": repo}
    if name:
        body["name"] = name
    emit(c.call("POST", "/api/loras/install", json=body), ctx.obj["fmt"])


@setup.command("hf-token")
@click.option("--token-stdin", is_flag=True, help="Read the token from stdin instead of a hidden prompt")
@click.pass_context
def setup_hf_token(ctx, token_stdin):
    token = sys.stdin.readline().strip() if token_stdin else click.prompt("Hugging Face token", hide_input=True)
    c = _client(ctx)
    emit(c.call("POST", "/api/setup/hf-token", json={"token": token}), ctx.obj["fmt"])


@setup.command("search")
@click.argument("query")
@click.option("--kind", type=click.Choice(["model", "lora"]), default="model")
@click.option("--limit", type=int, default=20)
@click.pass_context
def setup_search(ctx, query, kind, limit):
    c = _client(ctx)
    data = c.call("GET", "/api/search/hf",
                  params={"q": query, "kind": kind, "limit": limit})
    emit(data.get("results", []), ctx.obj["fmt"],
         columns=["repo_id", "downloads", "likes", "pipeline_tag"])


@setup.command("status")
@click.pass_context
def setup_status(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/setup/status"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
@cli.group()
def jobs():
    """Background job inspection + control."""


@jobs.command("list")
@click.option("--kind", default=None)
@click.option("--status", "job_status", default=None)
@click.pass_context
def jobs_list(ctx, kind, job_status):
    c = _client(ctx)
    params = {}
    if kind:
        params["kind"] = kind
    if job_status:
        params["status"] = job_status
    data = c.call("GET", "/api/jobs", params=params)
    emit(data.get("jobs", []), ctx.obj["fmt"],
         columns=["job_id", "kind", "status", "started_at"])


@jobs.command("show")
@click.argument("job_id")
@click.option("--follow", is_flag=True, help="Tail SSE log stream until done")
@click.pass_context
def jobs_show(ctx, job_id, follow):
    c = _client(ctx)
    if not follow:
        emit(c.call("GET", f"/api/jobs/{job_id}"), ctx.obj["fmt"])
        return
    with c.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
        if resp.status_code != 200:
            click.echo(f"[{resp.status_code}]", err=True)
            sys.exit(2)
        for line in resp.iter_lines():
            if line:
                click.echo(line)


@jobs.command("cancel")
@click.argument("job_id")
@click.pass_context
def jobs_cancel(ctx, job_id):
    c = _client(ctx)
    emit(c.call("POST", f"/api/jobs/{job_id}/cancel"), ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------
@cli.group()
def system():
    """Host system info + lifecycle."""


@system.command("info")
@click.pass_context
def sys_info(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/system/info"), ctx.obj["fmt"])


@system.command("disk")
@click.pass_context
def sys_disk(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/system/disk"), ctx.obj["fmt"])


@system.command("gpu")
@click.pass_context
def sys_gpu(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/system/gpu"), ctx.obj["fmt"])


@system.command("refresh")
@click.pass_context
def sys_refresh(ctx):
    c = _client(ctx)
    emit(c.call("POST", "/api/system/refresh"), ctx.obj["fmt"])


@system.command("restart")
@click.pass_context
def sys_restart(ctx):
    c = _client(ctx)
    emit(c.call("POST", "/api/system/restart", json={"confirm": True}),
         ctx.obj["fmt"])


@system.command("shutdown")
@click.pass_context
def sys_shutdown(ctx):
    c = _client(ctx)
    emit(c.call("POST", "/api/system/shutdown", json={"confirm": True}),
         ctx.obj["fmt"])


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------
@cli.group()
def maintenance():
    """Periodic prune / GC / repair tasks."""


@maintenance.command("status")
@click.pass_context
def maint_status(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/maintenance/status"), ctx.obj["fmt"])


@maintenance.command("run")
@click.argument("task")
@click.pass_context
def maint_run(ctx, task):
    c = _client(ctx)
    emit(c.call("POST", f"/api/maintenance/run/{task}"), ctx.obj["fmt"])


@maintenance.group("policy")
def maint_policy():
    pass


@maint_policy.command("show")
@click.pass_context
def policy_show(ctx):
    c = _client(ctx)
    emit(c.call("GET", "/api/maintenance/policy"), ctx.obj["fmt"])


@maint_policy.command("set")
@click.argument("task")
@click.argument("key")
@click.argument("value")
@click.pass_context
def policy_set(ctx, task, key, value):
    c = _client(ctx)
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = value
    emit(c.call("PUT", "/api/maintenance/policy",
                json={"policy": {task: {key: parsed}}}),
         ctx.obj["fmt"])


@cli.command("install-completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def install_completion(shell):
    """Print a shell completion script. Pipe into your rc file:

    \b
      omni-cli install-completion bash >> ~/.bashrc
      omni-cli install-completion zsh  >> ~/.zshrc
      omni-cli install-completion fish > ~/.config/fish/completions/omni-cli.fish
    """
    if shell == "bash":
        click.echo('eval "$(_OMNI_CLI_COMPLETE=bash_source omni-cli)"')
    elif shell == "zsh":
        click.echo('eval "$(_OMNI_CLI_COMPLETE=zsh_source omni-cli)"')
    else:
        click.echo("_OMNI_CLI_COMPLETE=fish_source omni-cli | source")


# Register the Audio Lab command group (Stable Audio + CLAP) from its
# dedicated module so this file doesn't balloon.
from cli.audio_lab_commands import audio_lab as _audio_lab_group  # noqa: E402
cli.add_command(_audio_lab_group)

# Register the ACE-Step command group (DiT song generation).
from cli.ace_step_commands import ace_step as _ace_step_group  # noqa: E402
cli.add_command(_ace_step_group)


def main():
    cli(prog_name="omni-cli")


if __name__ == "__main__":
    main()
