"""Stateful chat sessions: server-side multi-turn conversation history.

Each session is a list of role-tagged messages. Append a user message via
``POST /api/chat/sessions/{id}/messages`` and the gateway forwards the full
flattened transcript to the worker, then appends the assistant reply to
the session history so the next turn picks up where this one left off.

Streaming variants of the message-append route exist for both native SSE
and OpenAI-compat shapes; cancel works the same way as the stateless
``/api/chat/{model}/stream``.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import aclosing

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from chat_sessions import (
    ChatMessage,
    OMNI_CHAT_HISTORY_MAX_MESSAGES,
    render_history_for_worker,
)
from config import (
    INFER_MAX_IMAGE_BASE64_CHARS,
    INFER_MAX_NEW_TOKENS,
    INFER_MAX_TEMPERATURE,
    INFER_MAX_TEXT_CHARS,
    INFER_MAX_TOP_P,
    OMNI_MODEL_SETUP,
)
from routers.workers import ChatRequest, chat, _apply_request_preambles, _worker_stream_events
from state import chat_sessions

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------
class CreateSessionRequest(BaseModel):
    model: str
    system: str | None = Field(default=None, max_length=INFER_MAX_TEXT_CHARS)
    metadata: dict | None = None


class SessionMessageRequest(BaseModel):
    content: str = Field(default="", max_length=INFER_MAX_TEXT_CHARS)
    image: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    audio: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    video: str | None = Field(default=None, max_length=INFER_MAX_IMAGE_BASE64_CHARS)
    max_new_tokens: int = Field(default=512, ge=1, le=INFER_MAX_NEW_TOKENS)
    temperature: float = Field(default=0.7, ge=0.0, le=INFER_MAX_TEMPERATURE)
    top_p: float = Field(default=0.9, ge=0.0, le=INFER_MAX_TOP_P)
    response_format: dict | None = None


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------
@router.post("/api/chat/sessions", status_code=201)
async def create_session(req: CreateSessionRequest):
    if req.model not in OMNI_MODEL_SETUP:
        raise HTTPException(status_code=400, detail=f"Unknown model: {req.model}")
    try:
        session = chat_sessions.create(model=req.model, system=req.system, metadata=req.metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return session.summary()


@router.get("/api/chat/sessions")
async def list_sessions():
    return {"sessions": chat_sessions.list_summaries(),
            "history_max_messages": OMNI_CHAT_HISTORY_MAX_MESSAGES}


@router.get("/api/chat/sessions/{session_id}")
async def get_session(session_id: str):
    snap = chat_sessions.snapshot(session_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return snap


@router.delete("/api/chat/sessions/{session_id}")
async def delete_session(session_id: str):
    try:
        deleted = chat_sessions.delete(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


def _begin_turn(session_id: str):
    try:
        session = chat_sessions.begin_turn(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _session_request(session, req: SessionMessageRequest):
    message = ChatMessage(role="user", content=req.content, image=req.image, audio=req.audio, video=req.video)
    # A worker accepts one item of each medium; retain the most recent supplied
    # item when the current turn refers to previous media.
    media = {}
    for kind in ("image", "audio", "video"):
        media[kind] = getattr(req, kind) or next((getattr(m, kind) for m in reversed(session.messages) if getattr(m, kind)), None)
    try:
        blank = ChatRequest(model=session.model, response_format=req.response_format)
        overhead = len(_apply_request_preambles(blank).text)
        text = render_history_for_worker(session, message, max_chars=INFER_MAX_TEXT_CHARS - overhead)
        native = ChatRequest(model=session.model, text=text, **media,
                             max_new_tokens=req.max_new_tokens, temperature=req.temperature,
                             top_p=req.top_p, response_format=req.response_format)
        effective = _apply_request_preambles(native)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return message, native, effective


@router.post("/api/chat/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SessionMessageRequest):
    session = _begin_turn(session_id)
    try:
        user_msg, native, _ = _session_request(session, req)
        chat_sessions.append_message(session_id, user_msg)
        result = await chat(session.model, native)
        assistant = ChatMessage(role="assistant", content=result.get("text", ""))
        chat_sessions.append_message(session_id, assistant)
        response = {"session_id": session_id, "message": assistant.to_dict(),
                    "usage": result.get("usage", {"prompt_tokens": -1, "completion_tokens": -1, "total_tokens": -1})}
        if result.get("json_parse_error"):
            response["json_parse_error"] = result["json_parse_error"]
        return response
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        chat_sessions.end_turn(session_id)


@router.post("/api/chat/sessions/{session_id}/messages/stream")
async def send_message_stream(session_id: str, req: SessionMessageRequest):
    session = chat_sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _session_request(session, req)  # Reject malformed input before response headers.
    job_id = str(uuid.uuid4())

    async def body():
        acquired = False
        accumulated = []
        try:
            session = _begin_turn(session_id)
            acquired = True
            message, native, effective = _session_request(session, req)
            chat_sessions.append_message(session_id, message)
            async with aclosing(_worker_stream_events(session.model, native, effective, job_id)) as events:
                async for event in events:
                    if "delta" in event:
                        accumulated.append(str(event["delta"]))
                    yield ("data: " + json.dumps(event) + "\n\n").encode("utf-8")
        except (HTTPException, ValueError) as exc:
            yield ("data: " + json.dumps({"error": exc.detail if isinstance(exc, HTTPException) else str(exc)}) + "\n\n").encode("utf-8")
        finally:
            if acquired:
                try:
                    if accumulated:
                        chat_sessions.append_message(session_id, ChatMessage(role="assistant", content="".join(accumulated)))
                finally:
                    chat_sessions.end_turn(session_id)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Job-ID": job_id})
