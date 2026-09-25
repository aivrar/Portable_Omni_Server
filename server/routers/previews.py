"""ComfyUI preview-frame extraction.

ComfyUI emits binary WebSocket frames during sampling: a 4-byte event-type
header (``1`` for preview_image), a 4-byte format header (``1`` JPEG / ``2``
PNG), then the image bytes. We expose two routes that talk to ComfyUI's
``/ws`` directly so non-WS callers can grab live previews without
implementing the WS protocol themselves:

* ``GET /api/comfy/{instance_id}/previews/current[?timeout=2]`` returns
  the next binary preview frame as ``image/jpeg`` or ``image/png``. 408
  if nothing arrives in ``timeout`` seconds.

* ``GET /api/comfy/{instance_id}/previews/stream`` opens an SSE channel
  that proxies both ComfyUI's text status events and binary preview
  frames (base64-encoded under ``image_b64``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import struct
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from state import comfy_registry

logger = logging.getLogger(__name__)

router = APIRouter()

_PREVIEW_EVENT_TYPE = 1  # ComfyUI binary header: 1=preview_image
_FMT_JPEG = 1
_FMT_PNG = 2
_DEFAULT_TIMEOUT_S = 2.0
_MAX_TIMEOUT_S = 60.0
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _upstream_ws_url(port: int, client_id: str | None) -> str:
    clean = str(client_id or "").strip()
    if clean and not _CLIENT_ID_RE.fullmatch(clean):
        raise HTTPException(status_code=400, detail="Invalid ComfyUI client_id")
    suffix = "?" + urlencode({"clientId": clean}) if clean else ""
    return f"ws://127.0.0.1:{port}/ws{suffix}"


def _decode_preview_frame(raw: bytes) -> tuple[bytes, str] | None:
    """Strip ComfyUI's 8-byte preview header and return (bytes, mime).

    Returns None when the frame isn't a preview (different event_type).
    """
    if len(raw) < 8:
        return None
    event_type = struct.unpack(">I", raw[:4])[0]
    if event_type != _PREVIEW_EVENT_TYPE:
        return None
    fmt = struct.unpack(">I", raw[4:8])[0]
    img = raw[8:]
    if fmt == _FMT_JPEG:
        return img, "image/jpeg"
    if fmt == _FMT_PNG:
        return img, "image/png"
    return img, "application/octet-stream"


def _resolve_ready_instance(instance_id: str):
    inst = comfy_registry.get(instance_id)
    if not inst:
        raise HTTPException(status_code=404, detail="Instance not found")
    if inst.status != "ready":
        raise HTTPException(status_code=503,
                            detail=f"Instance {instance_id} not ready")
    return inst


@router.get("/api/comfy/{instance_id}/previews/current")
async def get_current_preview(
    instance_id: str,
    timeout: float = Query(default=_DEFAULT_TIMEOUT_S, ge=0.1, le=_MAX_TIMEOUT_S),
    client_id: str = Query(default="", max_length=128),
):
    """Return the next preview frame ComfyUI emits within ``timeout``.

    Useful when the caller already knows a generation is running and just
    wants a snapshot of the current sampling step.
    """
    import websockets

    inst = _resolve_ready_instance(instance_id)
    upstream = _upstream_ws_url(inst.port, client_id)

    from websockets.exceptions import (
        ConnectionClosed, InvalidHandshake, InvalidURI, WebSocketException,
    )
    try:
        async with websockets.connect(upstream, max_size=64 * 1024 * 1024) as ws:
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    if isinstance(msg, (bytes, bytearray)):
                        decoded = _decode_preview_frame(bytes(msg))
                        if decoded is None:
                            continue
                        body, mime = decoded
                        return Response(content=body, media_type=mime)
                    # text status events - keep waiting for a binary frame
            except asyncio.TimeoutError:
                raise HTTPException(status_code=408,
                                    detail="No preview frame arrived in window")
            except ConnectionClosed:
                raise HTTPException(status_code=502,
                                    detail="ComfyUI closed the WS mid-wait")
    except (OSError, ConnectionRefusedError):
        raise HTTPException(status_code=502, detail="ComfyUI WS not reachable")
    except (InvalidHandshake, InvalidURI, WebSocketException) as e:
        logger.warning("WS handshake failed for %s: %s", instance_id, e)
        raise HTTPException(status_code=502,
                            detail=f"ComfyUI WS handshake failed: {e}")


@router.get("/api/comfy/{instance_id}/previews/stream")
async def stream_previews(
    instance_id: str,
    include_status: bool = Query(default=True,
                                  description="Forward ComfyUI text status events too"),
    client_id: str = Query(default="", max_length=128),
):
    """SSE stream that proxies ComfyUI's WS into structured events.

    Event payload shapes:

    * ``{"type": "preview", "image_b64": "...", "format": "jpeg"}``
    * ``{"type": "status", "data": {...}}`` (when ``include_status=true``)
    * ``{"type": "executing", "node": ..., "prompt_id": ...}``
    * ``{"type": "executed", "node": ..., "output": {...}}``
    * ``{"type": "progress", "value": N, "max": M}``
    """
    import websockets
    from websockets.exceptions import ConnectionClosed

    inst = _resolve_ready_instance(instance_id)
    upstream = _upstream_ws_url(inst.port, client_id)

    async def _events():
        try:
            async with websockets.connect(upstream, max_size=64 * 1024 * 1024,
                                           ping_interval=20) as ws:
                async for msg in ws:
                    try:
                        if isinstance(msg, (bytes, bytearray)):
                            decoded = _decode_preview_frame(bytes(msg))
                            if decoded is None:
                                continue
                            img, mime = decoded
                            evt = {
                                "type": "preview",
                                "image_b64": base64.b64encode(img).decode("ascii"),
                                "format": mime.split("/", 1)[-1],
                            }
                            yield ("data: " + json.dumps(evt) + "\n\n").encode("utf-8")
                            continue
                        # text frame
                        try:
                            parsed = json.loads(msg)
                        except (ValueError, json.JSONDecodeError):
                            continue
                        kind = parsed.get("type")
                        if kind in ("executing", "executed", "progress"):
                            evt = {"type": kind, **(parsed.get("data") or {})}
                            yield ("data: " + json.dumps(evt) + "\n\n").encode("utf-8")
                        elif include_status and kind == "status":
                            yield ("data: " + json.dumps(
                                {"type": "status", "data": parsed.get("data")}
                            ) + "\n\n").encode("utf-8")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("preview stream: drop event %s: %s",
                                       type(msg).__name__, e)
        except (OSError, ConnectionRefusedError, ConnectionClosed) as e:
            yield ("data: " + json.dumps(
                {"type": "error", "message": str(e)}
            ) + "\n\n").encode("utf-8")

    return StreamingResponse(_events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
