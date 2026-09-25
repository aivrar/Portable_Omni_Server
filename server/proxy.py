"""HTTP and WebSocket passthrough helpers for ComfyUI instances.

The gateway exposes selected ComfyUI HTTP endpoints under
``/api/comfy/{instance_id}/proxy/{path:path}`` and the WebSocket progress
channel at ``/api/comfy/{instance_id}/ws`` (phase 9). This module owns the
upstream forwarding logic so the route definitions stay declarative.

Subpath allowlist (defense-in-depth — the auth middleware already gates the
route, but we don't want a future bug elsewhere to expose every internal
ComfyUI endpoint):

    Exact:     prompt, queue, interrupt, free, system_stats, object_info,
               embeddings, extensions, view, models, api/jobs, upload/image
    Prefixed:  history[/<id>], api/jobs/<id>[/cancel],
               object_info/<node>, models/<folder>[/...]

Everything else returns 404 from the gateway without touching ComfyUI.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

# Headers we never forward.
# RFC 7230 hop-by-hop set + a few gateway-internal entries. We deliberately
# keep ``content-length`` because httpx falls back to chunked transfer
# encoding when given an async iterator without a length, and not every
# upstream (e.g. stdlib BaseHTTPRequestHandler) speaks chunked.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "host", "expect",
})
_GATEWAY_INTERNAL = frozenset({"x-omni-token", "authorization", "cookie"})

# Allowlist
_EXACT_PATHS = frozenset({
    "prompt", "queue", "interrupt", "free",
    "system_stats", "object_info", "embeddings",
    "extensions", "view", "models", "api/jobs", "upload/image",
})
_PREFIXED = ("history/", "api/jobs/", "object_info/", "models/")
_PREFIX_BARE = frozenset({"history"})  # `history` alone is also valid

# Methods the proxy accepts.
ALLOWED_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# Per-request upstream timeouts.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=300.0, pool=10.0)


def is_allowed_subpath(path: str) -> bool:
    """Check ``path`` (no leading slash) against the allowlist."""
    if not path:
        return False
    if "\x00" in path or ".." in path.split("/"):
        return False
    if path in _EXACT_PATHS or path in _PREFIX_BARE:
        return True
    return any(path.startswith(p) for p in _PREFIXED)


def build_target_url(upstream_base: str, subpath: str, query: str = "") -> str:
    """Compose the upstream URL for a forwarded request.

    ``upstream_base`` is the fixed ``http://127.0.0.1:<port>`` of a managed
    ComfyUI instance (never caller-controlled); ``subpath`` must already be
    allowlisted by ``is_allowed_subpath``. The query string is appended
    verbatim. Because the host/scheme are server-chosen, this URL cannot be
    repointed at an arbitrary upstream — SSRF-resistant by construction.
    """
    url = f"{upstream_base.rstrip('/')}/{subpath}"
    if query:
        url = f"{url}?{query}"
    return url


def _filter_request_headers(src: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in src.items():
        kl = k.lower()
        if kl in _HOP_BY_HOP or kl in _GATEWAY_INTERNAL:
            continue
        out[k] = v
    return out


def _filter_response_headers(src) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for k, v in src.items():
        kl = k.lower()
        if kl in _HOP_BY_HOP:
            continue
        out.append((k, v))
    return out


async def forward_http(
    request: Request,
    upstream_base: str,
    subpath: str,
) -> StreamingResponse:
    """Forward ``request`` to ``<upstream_base>/<subpath>`` and stream the response.

    ``upstream_base`` is e.g. ``http://127.0.0.1:8188``. ``subpath`` is the
    path part after the gateway's ``/proxy/`` segment - must be allowlisted
    by ``is_allowed_subpath`` before calling this.
    """
    method = request.method.upper()
    if method not in ALLOWED_METHODS:
        raise HTTPException(status_code=405, detail=f"Method {method} not allowed")
    target_url = build_target_url(upstream_base, subpath, request.url.query or "")

    headers = _filter_request_headers(dict(request.headers))

    async def _body_iter() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    try:
        send_kwargs: dict = {"method": method, "url": target_url, "headers": headers}
        if method in ("POST", "PUT", "PATCH"):
            send_kwargs["content"] = _body_iter()

        upstream_req = client.build_request(**send_kwargs)
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.ConnectError as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"ComfyUI not reachable: {e}")
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(status_code=504, detail="ComfyUI request timed out")
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    media_type = upstream_resp.headers.get("content-type")
    return StreamingResponse(
        _stream(),
        status_code=upstream_resp.status_code,
        headers=dict(_filter_response_headers(upstream_resp.headers)),
        media_type=media_type,
    )
