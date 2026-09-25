#!/usr/bin/env python3
"""Small token-safe client for Omni Studio's local audio APIs."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:9200"
DEFAULT_TOKEN_FILES = (
    Path("/opt/omni_studio/cache/runtime/api_token"),
    Path(r"\\wsl.localhost\linbox-Omni_Studio\opt\omni_studio\cache\runtime\api_token"),
)
_TOKEN_CACHE: str | None = None


def _session_token(args: argparse.Namespace) -> str:
    request = Request(
        str(args.base_url).rstrip("/") + "/api/session",
        method="GET",
        headers={"Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=min(float(args.timeout), 10.0)) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(value.get("token") or "").strip() if isinstance(value, dict) else ""


def _load_token(args: argparse.Namespace) -> str:
    global _TOKEN_CACHE
    if _TOKEN_CACHE:
        return _TOKEN_CACHE
    token = str(os.environ.get("OMNI_API_TOKEN") or "").strip()
    candidates = [Path(args.token_file)] if args.token_file else []
    for path in candidates:
        if token:
            break
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    if not token and not args.token_file:
        token = _session_token(args)
    if not token:
        for path in DEFAULT_TOKEN_FILES:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                break
    if not token:
        raise SystemExit(
            "Set OMNI_API_TOKEN or pass --token-file; the token is never printed."
        )
    _TOKEN_CACHE = token
    return token


def _body(path: str | None, base64_fields: list[str] | None = None) -> bytes | None:
    if path:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Cannot read JSON body {path}: {exc}") from exc
    else:
        value = {}
    fields = base64_fields or []
    if fields and not isinstance(value, dict):
        raise SystemExit("Base64 fields require a JSON object request body")
    for item in fields:
        name, separator, filename = item.partition("=")
        if separator != "=" or name not in {"image", "audio", "video"} or not filename:
            raise SystemExit(
                "--base64-field must be image=PATH, audio=PATH, or video=PATH"
            )
        try:
            value[name] = base64.b64encode(Path(filename).read_bytes()).decode("ascii")
        except OSError as exc:
            raise SystemExit(f"Cannot read {name} file {filename}: {exc}") from exc
    if not path and not fields:
        return None
    return json.dumps(value).encode("utf-8")


def _call(
    args: argparse.Namespace,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
) -> tuple[bytes, dict[str, str], str | None]:
    if not path.startswith("/"):
        raise SystemExit("API path must start with /")
    req = Request(
        str(args.base_url).rstrip("/") + path,
        data=body,
        method=method,
        headers={
            "X-Omni-Token": _load_token(args),
            "Accept": "application/json, audio/*",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urlopen(req, timeout=float(args.timeout)) as response:
            raw = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            content_type = response.headers.get_content_type()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:8000]
        raise SystemExit(f"HTTP {exc.code} for {method} {path}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"Request failed for {method} {path}: {exc}") from exc
    return raw, headers, content_type


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _decode_json(raw: bytes, *, context: str) -> Any:
    if not raw:
        return {"status": "ok"}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Expected JSON from {context}; use --output for binary data") from exc


def _request_command(args: argparse.Namespace) -> None:
    raw, headers, content_type = _call(
        args,
        args.method.upper(),
        args.path,
        body=_body(args.body, args.base64_field),
    )
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        _print_json({
            "bytes": len(raw),
            "content_type": content_type,
            "output": str(target),
            "omni_output_path": headers.get("x-omni-output-path"),
            "omni_output_ref": headers.get("x-omni-output-ref"),
            "omni_output_url": headers.get("x-omni-output-url"),
        })
        return
    _print_json(_decode_json(raw, context=f"{args.method} {args.path}"))


def _wait_job(args: argparse.Namespace) -> None:
    deadline = time.monotonic() + float(args.wait_timeout)
    path = f"/api/jobs/{args.job_id}"
    while True:
        raw, _headers, _content_type = _call(args, "GET", path)
        job = _decode_json(raw, context=path)
        status = str(job.get("status") or "")
        if status in {"done", "error", "cancelled"}:
            _print_json(job)
            if status != "done":
                raise SystemExit(2)
            return
        if time.monotonic() >= deadline:
            raise SystemExit(f"Timed out waiting for {args.job_id}; last status={status!r}")
        time.sleep(float(args.interval))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-file")
    parser.add_argument("--timeout", type=float, default=60.0)
    commands = parser.add_subparsers(dest="command", required=True)

    request = commands.add_parser("request", help="Send one API request")
    request.add_argument("method", choices=("GET", "POST", "PUT", "DELETE", "PATCH"))
    request.add_argument("path")
    request.add_argument("--body", help="Path to a JSON request body")
    request.add_argument(
        "--base64-field",
        action="append",
        default=[],
        metavar="FIELD=PATH",
        help="Embed image, audio, or video file bytes as raw base64 in the JSON body",
    )
    request.add_argument("--output", help="Write a binary response to this file")
    request.set_defaults(handler=_request_command)

    wait = commands.add_parser("wait-job", help="Poll a background job to a terminal state")
    wait.add_argument("job_id")
    wait.add_argument("--interval", type=float, default=2.0)
    wait.add_argument("--wait-timeout", type=float, default=3600.0)
    wait.set_defaults(handler=_wait_job)
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
