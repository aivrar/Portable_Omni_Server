"""Generalized jobs API. Surfaces every kind tracked by the JobStore.

Legacy ``/api/setup/jobs*`` endpoints continue to live in ``routers/setup.py``
and reuse ``state.jobs`` via a translation adapter. These routes are the
forward-looking surface used by the new CLI and any non-UI client.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from jobs import JOB_KINDS
from state import jobs

router = APIRouter()


@router.get("/api/jobs")
async def list_jobs(
    kind: str | None = Query(default=None, description="Filter by kind"),
    status: str | None = Query(default=None, description="Filter by status"),
    limit: int = Query(default=200, ge=1, le=1000),
):
    if kind and kind not in JOB_KINDS:
        raise HTTPException(status_code=400, detail=f"Unknown kind: {kind}")
    items = jobs.list(kind=kind, status=status)[:limit]
    return {"jobs": [j.to_dict() for j in items]}


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    return j.to_dict()


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")
    new_status = jobs.cancel(job_id)
    return {"job_id": job_id, "status": new_status}


@router.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    """SSE stream of stdout lines + status transitions for one job."""
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_generator():
        seen = 0
        last_status = None
        last_emit = time.time()
        keepalive_after = 15.0
        while True:
            current = jobs.get(job_id)
            if not current:
                yield f"event: gone\ndata: {{\"job_id\": \"{job_id}\"}}\n\n"
                return
            emitted = False
            tail = list(current.stdout_tail)
            end = getattr(current.stdout_tail, "total", len(tail))
            start = end - len(tail)
            if end > seen:
                if seen and seen < start:
                    yield "data: " + json.dumps({"type": "gap", "dropped_lines": start - seen}) + "\n\n"
                for line in tail[max(0, seen - start):]:
                    payload = json.dumps({
                        "type": "line",
                        "ts": time.time(),
                        "line": line.rstrip("\n"),
                    })
                    yield f"data: {payload}\n\n"
                    emitted = True
                seen = end
            if current.status != last_status:
                payload = json.dumps({
                    "type": "status",
                    "status": current.status,
                    "progress": current.progress,
                })
                yield f"data: {payload}\n\n"
                last_status = current.status
                emitted = True
            if current.status in ("done", "error", "cancelled"):
                final = json.dumps({
                    "type": "final",
                    "status": current.status,
                    "result": current.result,
                    "error": current.error,
                })
                yield f"data: {final}\n\n"
                return
            now = time.time()
            if emitted:
                last_emit = now
            elif (now - last_emit) >= keepalive_after:
                yield ": keepalive\n\n"
                last_emit = now
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
