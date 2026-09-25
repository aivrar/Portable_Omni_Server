#!/usr/bin/env python3
"""Small guarded client for Omni Studio's existing ComfyUI API.

Read-only commands execute immediately. Mutations require ``--execute`` and
otherwise print the request that would be sent. The API token is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:9200"


def _json_file(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Expected a JSON object in {path}")
    return value


def _token(args: argparse.Namespace) -> str:
    value = str(args.token or os.environ.get("OMNI_API_TOKEN") or "").strip()
    if not value and args.token_file:
        try:
            value = Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"Cannot read token file: {exc}") from exc
    if not value:
        raise SystemExit("Set OMNI_API_TOKEN or pass --token-file (the token is never printed).")
    return value


def _request(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: dict | None = None,
) -> Any:
    base = str(args.base_url or DEFAULT_BASE_URL).rstrip("/")
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        base + path,
        data=payload,
        method=method,
        headers={
            "X-Omni-Token": _token(args),
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )
    try:
        with urlopen(request, timeout=float(args.timeout)) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:4000]
        raise SystemExit(f"HTTP {exc.code} for {method} {path}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"Request failed for {method} {path}: {exc}") from exc
    if not raw:
        return {"status": "ok"}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"text": raw.decode("utf-8", errors="replace")}


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _mutation(
    args: argparse.Namespace,
    method: str,
    path: str,
    body: dict | None = None,
) -> None:
    if not args.execute:
        _print({
            "execute": False,
            "message": "Mutation not sent; repeat with --execute after reviewing the target.",
            "method": method,
            "path": path,
            "body": body,
        })
        return
    _print(_request(args, method, path, body))


def _policy(args: argparse.Namespace) -> dict | None:
    if args.policy:
        return _json_file(args.policy)
    return None


def _workflow_body(args: argparse.Namespace) -> dict:
    body: dict[str, Any] = {
        "workflow": _json_file(args.workflow),
        "filename": Path(args.workflow).name,
        "instance_id": args.instance,
    }
    policy = _policy(args)
    if policy is not None:
        body["placement"] = policy
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded client for Omni Studio's existing ComfyUI API.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", help=argparse.SUPPRESS)
    parser.add_argument("--token-file")
    parser.add_argument("--timeout", type=float, default=60.0)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("devices", help="List detected devices and stable UUIDs.")
    commands.add_parser("instances", help="List running Comfy instances and GPU pools.")
    commands.add_parser("openapi", help="Fetch OpenAPI when OMNI_ENABLE_DOCS=1 enabled it.")

    analyze = commands.add_parser("analyze", help="Analyze an API-format workflow without loading models.")
    analyze.add_argument("workflow")
    analyze.add_argument("--instance")
    analyze.add_argument("--policy")

    run = commands.add_parser("run", help="Analyze and queue an inline workflow.")
    run.add_argument("workflow")
    run.add_argument("--instance", required=True)
    run.add_argument("--policy")
    run.add_argument("--client-id", default="omni-skill-client")
    run.add_argument("--execute", action="store_true")

    start = commands.add_parser("start", help="Start Comfy with a primary-first GPU pool.")
    start.add_argument("--device", required=True)
    start.add_argument("--gpu", action="append", default=[], help="Pool member; repeat for each GPU.")
    start.add_argument("--vram-mode", default="normal")
    start.add_argument("--precision", choices=["fp16", "bf16", "fp32"])
    start.add_argument("--execute", action="store_true")

    stop = commands.add_parser("stop", help="Stop one exact Comfy instance.")
    stop.add_argument("instance")
    stop.add_argument("--execute", action="store_true")

    stop_all = commands.add_parser("stop-all", help="Stop every managed Comfy instance.")
    stop_all.add_argument("--execute", action="store_true")

    core = commands.add_parser("update-core", help="Update Comfy core; separate from custom nodes.")
    core.add_argument("--ref")
    core.add_argument("--restart-instances", action="store_true")
    core.add_argument("--execute", action="store_true")

    nodes = commands.add_parser("update-nodes", help="Run Manager/custom-node update_all.")
    nodes.add_argument("--instance", required=True)
    nodes.add_argument("--auto-restart", action=argparse.BooleanOptionalAction, default=True)
    nodes.add_argument("--operation-timeout", type=int, default=1800)
    nodes.add_argument("--execute", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    command = args.command
    if command == "devices":
        _print(_request(args, "GET", "/api/devices"))
    elif command == "instances":
        _print(_request(args, "GET", "/api/comfy/instances"))
    elif command == "openapi":
        _print(_request(args, "GET", "/openapi.json"))
    elif command == "analyze":
        _print(_request(args, "POST", "/api/workflows/analyze", _workflow_body(args)))
    elif command == "run":
        body = _workflow_body(args)
        body.pop("filename", None)
        body["client_id"] = args.client_id
        _mutation(args, "POST", "/api/workflows/run", body)
    elif command == "start":
        pool = list(dict.fromkeys([args.device, *args.gpu]))
        body = {
            "device": args.device,
            "gpu_pool": pool if args.device.startswith("cuda:") else [],
            "vram_mode": args.vram_mode,
            "precision": args.precision,
            "preview_method": "auto",
            "disable_pinned_memory": False,
            "startup_options": {},
        }
        _mutation(args, "POST", "/api/comfy/start", body)
    elif command == "stop":
        _mutation(args, "POST", f"/api/comfy/{quote(args.instance, safe='')}/stop")
    elif command == "stop-all":
        _mutation(args, "POST", "/api/comfy/stop-all")
    elif command == "update-core":
        _mutation(args, "POST", "/api/comfy/installation/update", {
            "ref": args.ref,
            "dry_run": False,
            "restart_instances": bool(args.restart_instances),
        })
    elif command == "update-nodes":
        _mutation(args, "POST", "/api/comfy/extensions/manage", {
            "action": "update_all",
            "instance_id": args.instance,
            "dry_run": False,
            "auto_restart": bool(args.auto_restart),
            "timeout_s": int(args.operation_timeout),
            "expected_nodes": [],
        })
    else:  # pragma: no cover - argparse enforces a command
        parser.error(f"Unsupported command: {command}")


if __name__ == "__main__":
    main()
