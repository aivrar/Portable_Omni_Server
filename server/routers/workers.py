"""Omni model worker lifecycle and chat/inference dispatch.

Phase 8 additions:

* ``ChatRequest`` accepts ``top_p``, ``video``, and ``model_params`` (forwards
  them straight to the worker - older clients keep working because every new
  field defaults to ``None`` / its prior value).
* ``POST /api/chat/{model}/stream`` opens an SSE token stream from the
  worker's ``/infer/stream`` endpoint.
* ``POST /api/chat/{model}/cancel/{job_id}`` flips the worker's abort flag
  for a streaming job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
import anyio
from contextlib import aclosing
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from config import (
    DEFAULT_INFER_TIMEOUT,
    INFER_MAX_IMAGE_BASE64_CHARS,
    INFER_MAX_NEW_TOKENS,
    INFER_MAX_TEMPERATURE,
    INFER_MAX_TEXT_CHARS,
    INFER_MAX_TOP_P,
    LORA_COMPATIBLE_MODELS,
    LORA_DIR,
    MODEL_INFER_TIMEOUT,
    OMNI_MODEL_SETUP,
    OMNI_OPENAI_ALIASES,
    WORKER_LOG_DIR,
    get_variant,
)
from helpers import bounded_lines, safe_child_path
from state import worker_manager, worker_registry
from tools_compat import (
    coerce_json_response,
    parse_tool_calls,
    render_json_mode_preamble,
    render_tool_preamble,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class WorkerPlacementRequest(BaseModel):
    mode: Literal["single", "auto", "manual"] = "single"
    eligible_devices: list[str] = Field(default_factory=list, max_length=16)
    primary_device: str | None = None
    reserve_mb: int = Field(default=1024, ge=0, le=65536)
    device_reserve_mb: dict[str, int] = Field(default_factory=dict)
    max_memory_mb: dict[str, int] = Field(default_factory=dict)
    require_all: bool = False
    allow_cpu: bool = False
    cpu_memory_mb: int = Field(default=0, ge=0, le=1048576)
    device_map: dict[str, str | int] | None = None


class SpawnWorkerRequest(BaseModel):
    model: str
    device: str | None = None
    precision: Literal["fp16", "bf16", "fp32"] | None = None
    variant: str | None = None
    lora: str | None = None
    placement: WorkerPlacementRequest | None = None


class ChatRequest(BaseModel):
    model: str
    text: str = Field(default="", max_length=INFER_MAX_TEXT_CHARS)
    image: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    audio: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    video: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    max_new_tokens: int = Field(default=512, ge=1, le=INFER_MAX_NEW_TOKENS)
    temperature: float = Field(default=0.7, ge=0.0, le=INFER_MAX_TEMPERATURE)
    top_p: float = Field(default=0.9, ge=0.0, le=INFER_MAX_TOP_P)
    model_params: dict | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    response_format: dict | None = None


def _apply_request_preambles(req: ChatRequest) -> ChatRequest:
    """Prepend tool registry + JSON-mode instructions to ``text`` as needed.

    Both modes can be active at once - tool calls take priority in the
    output parser since a tool_call block is structurally JSON-bracketed.
    """
    parts: list[str] = []
    if req.tools:
        try:
            p = render_tool_preamble(req.tools, req.tool_choice or "auto")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if p:
            parts.append(p)
    if req.response_format:
        try:
            p = render_json_mode_preamble(req.response_format)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if p:
            parts.append(p)
    if not parts:
        return req
    parts.append(req.text or "")
    try:
        return ChatRequest.model_validate({**req.model_dump(), "text": "\n\n".join(parts)})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Prompt including tools/format exceeds worker limits") from exc


try:
    OMNI_AUTOSPAWN_TIMEOUT_S = max(30, int(os.environ.get("OMNI_AUTOSPAWN_TIMEOUT_S", "240")))
except (TypeError, ValueError):
    OMNI_AUTOSPAWN_TIMEOUT_S = 240
OMNI_AUTOSPAWN_DEFAULT = os.environ.get("OMNI_AUTOSPAWN_DEFAULT", "0") == "1"


async def _wait_for_worker_ready(worker_id: str,
                                 timeout: float = OMNI_AUTOSPAWN_TIMEOUT_S) -> bool:
    """Poll the registry until ``worker_id`` reports status='ready'.

    Returns True if it reached ready inside ``timeout``, False on timeout
    or if the worker died. Polls every 1.5 s.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        w = worker_registry.get(worker_id)
        if w is None or w.status == "dead":
            return False
        if w.status == "ready":
            return True
        await asyncio.sleep(1.5)
    return False


async def _resolve_busy_worker(
    model: str, autospawn: bool = False, device: str | None = None,
    job_id: str | None = None,
):
    """Pick a ready worker, mark it busy, return ``(worker, job_id)``.

    With ``autospawn=True`` (or ``OMNI_AUTOSPAWN_DEFAULT=1``), if no workers
    are running for ``model`` we spawn one and wait for it to load.

    Raises 503 if no workers are loaded and autospawn is off (or it timed
    out), or if every loaded worker is busy and autospawn can't help.
    """
    if model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    do_autospawn = autospawn or OMNI_AUTOSPAWN_DEFAULT

    ready = worker_registry.get_ready_workers(model, device=device)
    if not ready:
        if any(w.model == model and w.status == "busy"
               and (device is None or w.device == device)
               for w in worker_registry.all_workers()):
            raise HTTPException(status_code=503, detail="All workers busy")
        if not do_autospawn:
            raise HTTPException(status_code=503,
                                detail=f"No workers loaded for {model} "
                                       f"(use ?autospawn=true to spawn one)")
        # Refuse if a worker is already starting for this model — let it
        # finish rather than stacking parallel boots.
        starting = [w for w in worker_registry.all_workers()
                    if w.model == model and w.status in ("starting", "loading")
                    and (device is None or w.device == device)]
        if starting:
            ok = await _wait_for_worker_ready(starting[0].worker_id)
            if not ok:
                raise HTTPException(
                    status_code=504,
                    detail=f"Worker for {model} did not become ready in {OMNI_AUTOSPAWN_TIMEOUT_S}s",
                )
        else:
            try:
                spawned = await worker_manager.spawn_worker(model=model, device=device)
            except Exception as e:
                raise HTTPException(status_code=500,
                                    detail=f"Auto-spawn failed: {e}")
            ok = await _wait_for_worker_ready(spawned.worker_id)
            if not ok:
                raise HTTPException(
                    status_code=504,
                    detail=f"Auto-spawned worker for {model} did not become ready in {OMNI_AUTOSPAWN_TIMEOUT_S}s",
                )

    job_id = job_id or str(uuid.uuid4())
    worker = worker_registry.atomic_pick_and_mark_busy(model, job_id, device=device)
    if not worker:
        raise HTTPException(status_code=503, detail="All workers busy")
    return worker, job_id


def _release_worker(worker_id: str):
    """Outcome-aware cleanup - matches the pattern in the non-stream chat path."""
    w = worker_registry.get(worker_id)
    if w and w.status == "busy":
        if w.process and w.process.poll() is not None:
            worker_registry.mark_dead(worker_id)
        else:
            worker_registry.mark_ready(worker_id)


async def _retire_timed_out_worker(worker_id: str) -> None:
    """Retire a timed-out or failed worker instead of advertising it as ready."""
    worker_registry.mark_dead(worker_id)
    try:
        await worker_manager.kill_worker(worker_id)
    except Exception:
        logger.warning("Could not retire timed-out worker %s", worker_id, exc_info=True)


@router.get("/api/workers")
async def list_workers():
    return {"workers": worker_registry.to_dict_list()}


def _validate_worker_request(req: SpawnWorkerRequest) -> None:
    if req.model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")

    if req.variant:
        v = get_variant(req.model, req.variant)
        if not v:
            raise HTTPException(status_code=400,
                                detail=f"Unknown variant {req.variant} for {req.model}")

    if req.lora:
        if req.model not in LORA_COMPATIBLE_MODELS:
            raise HTTPException(status_code=400,
                                detail=f"{req.model} does not support LoRA adapters")
        lora_path = safe_child_path(LORA_DIR, req.lora)
        if not lora_path.exists():
            raise HTTPException(status_code=404,
                                detail=f"LoRA not found: {req.lora}")


@router.post("/api/workers/analyze")
async def analyze_worker(req: SpawnWorkerRequest):
    _validate_worker_request(req)
    try:
        plan = worker_manager.analyze_placement(
            model=req.model,
            variant=req.variant,
            device=req.device,
            placement=req.placement,
            precision=req.precision,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ready_to_spawn": bool(plan.get("valid")), "placement_plan": plan}


@router.post("/api/workers/spawn")
async def spawn_worker(req: SpawnWorkerRequest):
    _validate_worker_request(req)
    plan = worker_manager.analyze_placement(
        model=req.model,
        variant=req.variant,
        device=req.device,
        placement=req.placement,
        precision=req.precision,
    )
    if not plan.get("valid"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Worker placement is invalid",
                "placement_plan": plan,
            },
        )

    try:
        worker = await worker_manager.spawn_worker(
            model=req.model, device=req.device, precision=req.precision,
            variant=req.variant, lora=req.lora, placement_plan=plan)
        return {
            "worker_id": worker.worker_id,
            "model": worker.model,
            "port": worker.port,
            "device": worker.device,
            "variant": worker.variant,
            "lora": worker.lora,
            "placement_mode": worker.placement_mode,
            "gpu_pool": worker.gpu_pool,
            "gpu_device_map": worker.gpu_device_map,
            "placement_plan": worker.placement_plan,
            "status": worker.status,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/workers/{worker_id}")
async def kill_worker(worker_id: str):
    ok = await worker_manager.kill_worker(worker_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Worker not found")
    return {"status": "killed"}


@router.post("/api/workers/kill-all")
async def kill_all_workers():
    count = await worker_manager.kill_all_workers()
    return {"killed": count}


@router.get("/api/workers/{worker_id}/logs")
async def get_worker_logs(worker_id: str, lines: int = 100):
    worker = worker_registry.get(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    log_file = WORKER_LOG_DIR / f"worker_{worker.model}_{worker.port}.log"
    if not log_file.exists():
        return {"lines": []}
    n = bounded_lines(lines)
    # Tail efficiently: read only the last chunk of the file rather than
    # loading a potentially huge worker log fully into memory. 256KB is far
    # more than enough to contain the last `n` lines for any sane `n`.
    tail_bytes = 256 * 1024
    try:
        size = log_file.stat().st_size
        with open(log_file, "rb") as fh:
            if size > tail_bytes:
                fh.seek(-tail_bytes, os.SEEK_END)
            data = fh.read()
    except OSError:
        return {"lines": []}
    text = data.decode("utf-8", errors="replace")
    return {"lines": text.strip().splitlines()[-n:]}


@router.post("/api/chat/{model}")
async def chat(model: str, req: ChatRequest, autospawn: bool = False):
    effective = _apply_request_preambles(req)
    worker, _job_id = await _resolve_busy_worker(model, autospawn=autospawn)
    timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Worker doesn't know about tools/response_format — strip those
            # to keep the InferRequest schema clean.
            payload = effective.model_dump()
            for k in ("tools", "tool_choice", "response_format"):
                payload.pop(k, None)
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/infer",
                json=payload,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code,
                                    detail=f"Worker error: {resp.text[:500]}")
            result = resp.json()
    except httpx.TimeoutException:
        await _retire_timed_out_worker(worker.worker_id)
        raise HTTPException(status_code=504,
                            detail=f"Inference timed out after {timeout}s")
    except httpx.HTTPError:
        worker_registry.mark_dead(worker.worker_id)
        raise HTTPException(status_code=502,
                            detail="Worker process died during inference")
    finally:
        _release_worker(worker.worker_id)

    raw_text = result.get("text", "")
    tool_calls: list[dict] = []
    if req.tools:
        raw_text, tool_calls = parse_tool_calls(raw_text)

    if req.response_format and req.response_format.get("type") in {"json_object", "json_schema"} and not tool_calls:
        # JSON mode coerces the visible text to a parseable JSON string. We
        # skip coercion when tool_calls fired — those already imply JSON
        # output and the visible text is the model's commentary alongside.
        coerced, err = coerce_json_response(raw_text)
        result["text"] = coerced
        if err:
            result["json_parse_error"] = err
    else:
        result["text"] = raw_text

    if tool_calls:
        result["tool_calls"] = tool_calls
        result["finish_reason"] = "tool_calls"
    else:
        result.setdefault("finish_reason", "stop")
    return result


async def _abort_stream_worker(worker, job_id: str) -> None:
    """Release only after the producer is idle; retire a stuck producer."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.post(f"http://127.0.0.1:{worker.port}/abort", json={"job_id": job_id})
            for _ in range(20):
                response = await client.get(f"http://127.0.0.1:{worker.port}/health")
                health = response.json() if response.status_code == 200 else {}
                if health.get("busy") is False:
                    return
                await asyncio.sleep(0.25)
    except (httpx.HTTPError, ValueError):
        pass
    await _retire_timed_out_worker(worker.worker_id)


def _format_generated_text(text: str, req: ChatRequest) -> dict:
    result = {"text": text, "finish_reason": "stop"}
    if req.tools:
        result["text"], calls = parse_tool_calls(text)
        if calls:
            result.update(tool_calls=calls, finish_reason="tool_calls")
    if req.response_format and req.response_format.get("type") in {"json_object", "json_schema"} and not result.get("tool_calls"):
        result["text"], error = coerce_json_response(result["text"])
        if error:
            result["json_parse_error"] = error
    return result


async def _worker_stream_events(model: str, req: ChatRequest, effective: ChatRequest,
                                job_id: str, autospawn: bool = False):
    # Reservation belongs to the response body: a disconnect before its first
    # iteration cannot strand a worker in busy state.
    worker = None
    finished = False
    timeout = MODEL_INFER_TIMEOUT.get(model, DEFAULT_INFER_TIMEOUT)
    client = httpx.AsyncClient(timeout=timeout)
    buffered = []
    structured = bool(req.tools or (req.response_format and req.response_format.get("type") in {"json_object", "json_schema"}))
    try:
        worker, _ = await _resolve_busy_worker(model, autospawn=autospawn, job_id=job_id)
        payload = effective.model_dump(exclude={"tools", "tool_choice", "response_format"})
        payload["job_id"] = job_id
        yield {"job_id": job_id}
        async with client.stream("POST", f"http://127.0.0.1:{worker.port}/infer/stream", json=payload) as resp:
            if resp.status_code != 200:
                detail = (await resp.aread()).decode("utf-8", errors="replace")[:500]
                finished = True
                if resp.status_code >= 500:
                    worker_registry.mark_dead(worker.worker_id)
                yield {"error": f"Worker {resp.status_code}: {detail}"}
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if "delta" in event and structured:
                    buffered.append(str(event["delta"]))
                    continue
                if event.get("done") or event.get("cancelled") or "error" in event:
                    finished = True
                    if structured and buffered:
                        result = _format_generated_text("".join(buffered), req)
                        if result["text"]:
                            yield {"delta": result["text"]}
                        event.update({k: v for k, v in result.items() if k != "text"})
                    yield event
                    return
                yield event
            yield {"error": "Worker stream ended without a terminal event"}
    except HTTPException as exc:
        yield {"error": exc.detail}
    except httpx.TimeoutException:
        if worker:
            await _retire_timed_out_worker(worker.worker_id)
        yield {"error": f"Inference timed out after {timeout}s"}
    except httpx.HTTPError:
        if worker:
            worker_registry.mark_dead(worker.worker_id)
        yield {"error": "Worker transport failed"}
    finally:
        with anyio.CancelScope(shield=True):
            await client.aclose()
            if worker:
                if not finished:
                    await _abort_stream_worker(worker, job_id)
                _release_worker(worker.worker_id)


@router.post("/api/chat/{model}/stream")
async def chat_stream(model: str, req: ChatRequest, autospawn: bool = False):
    """SSE tokens; structured output is buffered until it can be parsed."""
    effective = _apply_request_preambles(req)
    job_id = str(uuid.uuid4())

    async def body():
        async with aclosing(_worker_stream_events(model, req, effective, job_id, autospawn)) as events:
            async for event in events:
                yield ("data: " + json.dumps(event) + "\n\n").encode("utf-8")

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Job-ID": job_id})


@router.post("/api/chat/{model}/cancel/{job_id}")
async def chat_cancel(model: str, job_id: str):
    """Tell every ready worker for ``model`` to abort the named job.

    The job_id was generated by the gateway when the stream started; we don't
    track which worker it ran on, so we broadcast the abort to all running
    workers of this model. Idempotent and cheap (single POST per worker).
    """
    if model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {model}")
    targets = [w for w in worker_registry.all_workers()
               if w.model == model and w.status in ("ready", "busy")]
    if not targets:
        raise HTTPException(status_code=404, detail="No workers running this model")
    aborted: list[str] = []
    async with httpx.AsyncClient(timeout=5.0) as client:
        for w in targets:
            try:
                r = await client.post(
                    f"http://127.0.0.1:{w.port}/abort",
                    json={"job_id": job_id},
                )
                if r.status_code == 200:
                    aborted.append(w.worker_id)
            except httpx.HTTPError:
                continue
    return {"job_id": job_id, "model": model, "workers": aborted}


# ---------------------------------------------------------------------------
# OpenAI-compatible shim (phase 8)
# ---------------------------------------------------------------------------
class _OAIChatMessage(BaseModel):
    role: str
    content: str | list[dict] | None = None


class _OAIChatRequest(BaseModel):
    model: str
    messages: list[_OAIChatMessage]
    max_tokens: int | None = Field(default=None, ge=1, le=INFER_MAX_NEW_TOKENS)
    temperature: float | None = Field(default=None, ge=0.0, le=INFER_MAX_TEMPERATURE)
    top_p: float | None = Field(default=None, ge=0.0, le=INFER_MAX_TOP_P)
    stream: bool = False
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    response_format: dict | None = None


class _OAICompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    max_tokens: int | None = Field(default=None, ge=1, le=INFER_MAX_NEW_TOKENS)
    temperature: float | None = Field(default=None, ge=0.0, le=INFER_MAX_TEMPERATURE)
    top_p: float | None = Field(default=None, ge=0.0, le=INFER_MAX_TOP_P)
    stream: bool = False


def _resolve_openai_model(name: str) -> str:
    """Map an OpenAI-style model name to a native Omni ID."""
    if name in OMNI_MODEL_SETUP:
        return name
    native = OMNI_OPENAI_ALIASES.get(name)
    if native and native in OMNI_MODEL_SETUP:
        return native
    raise HTTPException(
        status_code=404,
        detail={
            "error": {
                "message": f"Unknown model: {name}",
                "available": sorted(set(OMNI_OPENAI_ALIASES.keys()) | set(OMNI_MODEL_SETUP.keys())),
            }
        },
    )


def _flatten_oai_messages(messages: list[_OAIChatMessage]) -> str:
    """Collapse OpenAI-style messages into a single prompt string.

    The Omni workers don't understand OpenAI's role-tagged format directly,
    so we render a simple ``Role: content`` log. Multi-modal image parts are
    carried separately by ``_first_oai_image_b64``.
    """
    parts: list[str] = []
    for m in messages:
        c = m.content
        if isinstance(c, list):
            text = " ".join(
                str(part.get("text", "")) for part in c
                if isinstance(part, dict)
                and part.get("type") in ("text", "input_text")
            )
        else:
            text = c or ""
        parts.append(f"{m.role.capitalize()}: {text}")
    parts.append("Assistant:")
    return "\n".join(parts)


def _image_url_part_to_b64(part: dict) -> str | None:
    raw = part.get("image_url") or part.get("input_image") or part.get("image")
    if isinstance(raw, dict):
        raw = raw.get("url")
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    if url.startswith("data:"):
        marker = ";base64,"
        if marker not in url:
            return None
        return url.split(marker, 1)[1].strip()
    # Native ChatRequest expects base64 bytes, so allow already-normalized
    # values but do not fetch arbitrary remote URLs inside the shim.
    if not url.startswith(("http://", "https://")):
        return url
    return None


def _first_oai_image_b64(messages: list[_OAIChatMessage]) -> str | None:
    for m in reversed(messages):
        if not isinstance(m.content, list):
            continue
        for part in m.content:
            if not isinstance(part, dict):
                continue
            if part.get("type") not in ("image_url", "input_image", "image"):
                continue
            image = _image_url_part_to_b64(part)
            if image:
                return image
    return None


def _make_oai_chat_payload(req: _OAIChatRequest) -> ChatRequest:
    return ChatRequest(
        model=_resolve_openai_model(req.model),
        text=_flatten_oai_messages(req.messages),
        image=_first_oai_image_b64(req.messages),
        max_new_tokens=req.max_tokens or 512,
        temperature=0.7 if req.temperature is None else req.temperature,
        top_p=0.9 if req.top_p is None else req.top_p,
        tools=req.tools,
        tool_choice=req.tool_choice,
        response_format=req.response_format,
    )


async def _oai_stream_response(
    *,
    chat_req: ChatRequest,
    completion_id: str,
    created: int,
    public_model: str,
    object_name: str,
    delta_key: str,
    autospawn: bool = False,
) -> StreamingResponse:
    """Common OpenAI-shape SSE wrapper.

    Used by both ``/v1/chat/completions`` (object=``chat.completion.chunk``,
    delta wrapped in ``{"content": ...}``) and ``/v1/completions``
    (object=``text_completion``, delta is the raw string under ``text``).
    """
    effective = _apply_request_preambles(chat_req)
    job_id = str(uuid.uuid4())

    def _wrap_delta(piece: str, finish_reason: str | None) -> dict:
        if delta_key == "content":
            choice = {
                "index": 0,
                "delta": {"content": piece} if piece is not None else {},
                "finish_reason": finish_reason,
            }
        else:  # text completion shape
            choice = {
                "index": 0,
                "text": piece if piece is not None else "",
                "finish_reason": finish_reason,
            }
        return {
            "id": completion_id,
            "object": object_name,
            "created": created,
            "model": public_model,
            "choices": [choice],
        }

    async def _stream():
        async with aclosing(_worker_stream_events(chat_req.model, chat_req, effective, job_id, autospawn)) as events:
            async for evt in events:
                if "delta" in evt:
                    chunk = _wrap_delta(evt["delta"], None)
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
                if evt.get("done") or evt.get("cancelled"):
                    chunk = _wrap_delta(None, evt.get("finish_reason") or ("cancelled" if evt.get("cancelled") else "stop"))
                    if evt.get("tool_calls") and delta_key == "content":
                        chunk["choices"][0]["delta"]["tool_calls"] = [
                            {"index": i, **call} for i, call in enumerate(evt["tool_calls"])]
                    if evt.get("json_parse_error"):
                        chunk["json_parse_error"] = evt["json_parse_error"]
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
                if "error" in evt:
                    yield ("data: " + json.dumps({"error": {"message": evt["error"], "type": "worker_error"}}) + "\n\n").encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/v1/chat/completions")
async def openai_chat_completions(req: _OAIChatRequest):
    """OpenAI Chat Completions shim. Honours ``stream=true`` for SSE deltas."""
    chat_req = _make_oai_chat_payload(req)
    created = int(time.time())
    completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"

    if not req.stream:
        result = await chat(chat_req.model, chat_req)
        text = (result or {}).get("text", "")
        tool_calls = (result or {}).get("tool_calls")
        finish = (result or {}).get("finish_reason") or "stop"
        message: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": req.model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": finish,
            }],
            "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
        }

    return await _oai_stream_response(
        chat_req=chat_req,
        completion_id=completion_id,
        created=created,
        public_model=req.model,
        object_name="chat.completion.chunk",
        delta_key="content",
    )


@router.post("/v1/completions")
async def openai_completions(req: _OAICompletionRequest):
    """Single-turn /v1/completions shim. Streaming uses the OpenAI text shape."""
    prompt = req.prompt if isinstance(req.prompt, str) else "\n".join(req.prompt)
    chat_req = ChatRequest(
        model=_resolve_openai_model(req.model),
        text=prompt,
        max_new_tokens=req.max_tokens or 256,
        temperature=0.7 if req.temperature is None else req.temperature,
        top_p=0.9 if req.top_p is None else req.top_p,
    )
    completion_id = f"cmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    if not req.stream:
        result = await chat(chat_req.model, chat_req)
        text = (result or {}).get("text", "")
        return {
            "id": completion_id,
            "object": "text_completion",
            "created": created,
            "model": req.model,
            "choices": [{"text": text, "index": 0, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1},
        }

    return await _oai_stream_response(
        chat_req=chat_req,
        completion_id=completion_id,
        created=created,
        public_model=req.model,
        object_name="text_completion",
        delta_key="text",
    )


@router.get("/v1/models")
async def openai_list_models():
    """OpenAI-compatible model listing - native IDs plus aliases."""
    created = int(time.time())
    items = []
    for native in OMNI_MODEL_SETUP.keys():
        items.append({
            "id": native, "object": "model", "created": created, "owned_by": "omni-studio",
        })
    for alias, native in OMNI_OPENAI_ALIASES.items():
        if native in OMNI_MODEL_SETUP:
            items.append({
                "id": alias, "object": "model", "created": created,
                "owned_by": "omni-studio-alias", "root": native,
            })
    return {"object": "list", "data": items}
