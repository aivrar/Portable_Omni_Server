"""Server-Sent Events log stream with per-client fan-out.

The background tail task multiplexes every ``*.log`` under ``WORKER_LOG_DIR``
into one stream. Each connected client gets its own bounded queue, so a slow
reader cannot stall the broadcast.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import WORKER_LOG_DIR

router = APIRouter()

_LOG_READ_SIZE = 64 * 1024


def _complete_line_bytes(raw: bytes, read_size: int) -> bytes:
    """Return the leading complete-line byte slice of ``raw``.

    Returns bytes up to and including the last newline, so an incomplete
    trailing line is carried to the next tick. If there's no newline but the
    buffer is full (a single line longer than ``read_size``), returns the whole
    buffer to avoid stalling forever; otherwise ``b""`` (wait for more). Working
    in bytes keeps the caller's byte-offset cursor exact even when the log
    contains non-UTF-8 bytes (decode+re-encode would inflate the advance and
    skip data).
    """
    nl = raw.rfind(b"\n")
    if nl >= 0:
        return raw[:nl + 1]
    if len(raw) >= read_size:
        return raw
    return b""


_sse_clients: set[asyncio.Queue] = set()
_sse_tail_task: asyncio.Task | None = None


def _ensure_sse_tail():
    global _sse_tail_task
    if _sse_tail_task is None or _sse_tail_task.done():
        _sse_tail_task = asyncio.get_running_loop().create_task(_tail_log_files())


def _broadcast(entry: str):
    """Push a log entry to every connected SSE client; drop oldest on full queue."""
    dead = []
    for q in list(_sse_clients):
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(entry)
            except Exception:
                dead.append(q)
    for q in dead:
        _sse_clients.discard(q)


async def _tail_log_files():
    cursors: dict[str, int] = {}
    while True:
        try:
            if WORKER_LOG_DIR.exists():
                for f in WORKER_LOG_DIR.glob("*.log"):
                    key = str(f)
                    try:
                        size = f.stat().st_size
                    except OSError:
                        continue
                    prev = cursors.get(key, 0)
                    if size < prev:
                        prev = 0  # truncated/rotated
                    if size > prev:
                        try:
                            # Read in binary so the cursor (a byte offset) stays
                            # exact even when the log holds non-UTF-8 bytes:
                            # decoding then re-encoding to measure the advance
                            # turns each bad byte into a 3-byte U+FFFD and would
                            # push the cursor past the real position, skipping
                            # data. Carry an incomplete trailing line to the next
                            # tick so a line straddling the 64KB boundary isn't
                            # split.
                            with open(f, "rb") as fh:
                                fh.seek(prev)
                                raw = fh.read(_LOG_READ_SIZE)
                            complete = _complete_line_bytes(raw, _LOG_READ_SIZE)
                            advance = len(complete)
                            text = complete.decode("utf-8", "replace")
                            for line in text.strip().splitlines():
                                entry = json.dumps({
                                    "source": f.stem,
                                    "message": line,
                                    "timestamp": time.time(),
                                })
                                _broadcast(entry)
                            cursors[key] = min(prev + advance, size)
                        except Exception:
                            cursors[key] = min(prev + 64 * 1024, size)
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2)


@router.get("/api/logs/stream")
async def log_stream():
    _ensure_sse_tail()
    client_q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _sse_clients.add(client_q)

    async def event_generator():
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(client_q.get(), timeout=15)
                    yield f"data: {entry}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _sse_clients.discard(client_q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
