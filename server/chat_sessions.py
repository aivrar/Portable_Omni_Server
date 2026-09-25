"""In-memory chat session store with TTL eviction.

A session is an append-only list of messages keyed by a UUID. Sessions live
for ``OMNI_CHAT_SESSION_TTL_S`` seconds of idle time; the store is capped at
``OMNI_CHAT_SESSION_MAX`` entries (oldest-idle evicted first).

Persistence is intentionally NOT added: chat history can be sensitive, and
the gateway's existing posture is "no secrets on disk except API tokens."
Operators who want durable history should mirror events into their own
store via the future webhook.
"""

from __future__ import annotations

import os
import json
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

try:
    OMNI_CHAT_SESSION_TTL_S = max(60, int(os.environ.get("OMNI_CHAT_SESSION_TTL_S", "86400")))
except (TypeError, ValueError):
    OMNI_CHAT_SESSION_TTL_S = 86400

try:
    OMNI_CHAT_SESSION_MAX = max(8, int(os.environ.get("OMNI_CHAT_SESSION_MAX", "1000")))
except (TypeError, ValueError):
    OMNI_CHAT_SESSION_MAX = 1000

OMNI_CHAT_HISTORY_MAX_MESSAGES = max(
    4, int(os.environ.get("OMNI_CHAT_HISTORY_MAX_MESSAGES", "100"))
)
OMNI_CHAT_SESSION_MAX_BYTES = 64 * 1024 * 1024
OMNI_CHAT_STORE_MAX_BYTES = 256 * 1024 * 1024


def _message_bytes(message) -> int:
    return len(message.content) * 4 + sum(len(getattr(message, k) or "") for k in ("image", "audio", "video"))


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    image: str | None = None
    audio: str | None = None
    video: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"role": self.role, "content": self.content,
                                "created_at": self.created_at}
        for k in ("image", "audio", "video", "tool_calls",
                   "tool_call_id", "name"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        return out


@dataclass
class ChatSession:
    session_id: str
    model: str
    messages: list[ChatMessage] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)

    def append(self, message: ChatMessage) -> None:
        if _message_bytes(message) > OMNI_CHAT_SESSION_MAX_BYTES:
            raise ValueError("Message exceeds the session memory budget")
        self.messages.append(message)
        self.last_activity = time.time()
        # Trim oldest non-system messages once we exceed the bound. Keep the
        # original system prompt so the model's persona doesn't flap.
        if len(self.messages) > OMNI_CHAT_HISTORY_MAX_MESSAGES:
            system_msgs = [m for m in self.messages if m.role == "system"]
            other = [m for m in self.messages if m.role != "system"]
            keep_other = other[-(OMNI_CHAT_HISTORY_MAX_MESSAGES - len(system_msgs)):]
            self.messages = system_msgs + keep_other
        while sum(_message_bytes(m) for m in self.messages) > OMNI_CHAT_SESSION_MAX_BYTES:
            oldest = next((i for i, m in enumerate(self.messages[:-1]) if m.role != "system"), None)
            if oldest is None:
                raise ValueError("Session exceeds its memory budget")
            del self.messages[oldest]

    def summary(self) -> dict:
        last_msg = self.messages[-1] if self.messages else None
        return {
            "session_id": self.session_id,
            "model": self.model,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "message_count": len(self.messages),
            "last_role": last_msg.role if last_msg else None,
        }

    def to_dict(self) -> dict:
        return {
            **self.summary(),
            "messages": [m.to_dict() for m in self.messages],
        }


class ChatSessionStore:
    """Thread-safe in-memory session store with TTL + size bound."""

    def __init__(self,
                 ttl_seconds: int = OMNI_CHAT_SESSION_TTL_S,
                 max_sessions: int = OMNI_CHAT_SESSION_MAX):
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}
        self._active_turns: set[str] = set()
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions

    def _evict(self) -> None:
        now = time.time()
        # TTL pass first - cheap, drops most expired entries.
        expired = [sid for sid, s in self._sessions.items()
                   if sid not in self._active_turns and (now - s.last_activity) > self.ttl_seconds]
        for sid in expired:
            self._sessions.pop(sid, None)
        # Size pass: drop oldest-idle until under cap.
        if len(self._sessions) > self.max_sessions:
            ordered = sorted(((sid, s) for sid, s in self._sessions.items() if sid not in self._active_turns),
                             key=lambda kv: kv[1].last_activity)
            for sid, _ in ordered[: len(self._sessions) - self.max_sessions]:
                self._sessions.pop(sid, None)
        total = sum(_message_bytes(m) for s in self._sessions.values() for m in s.messages)
        for sid, session in sorted(self._sessions.items(), key=lambda item: item[1].last_activity):
            if total <= OMNI_CHAT_STORE_MAX_BYTES:
                break
            if sid not in self._active_turns:
                total -= sum(_message_bytes(m) for m in session.messages)
                self._sessions.pop(sid, None)

    def create(self, model: str, system: str | None = None,
               metadata: dict | None = None) -> ChatSession:
        if len(json.dumps(metadata or {})) > 65536:
            raise ValueError("Session metadata exceeds 64 KiB")
        with self._lock:
            self._evict()
            if len(self._active_turns) >= self.max_sessions:
                raise ValueError("All session slots are active")
            sid = uuid.uuid4().hex
            session = ChatSession(session_id=sid, model=model,
                                   metadata=metadata or {})
            if system:
                session.append(ChatMessage(role="system", content=system))
            self._sessions[sid] = session
            self._evict()
            return session

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            self._evict()
            return deepcopy(self._sessions.get(session_id))

    def begin_turn(self, session_id: str) -> ChatSession | None:
        with self._lock:
            self._evict()
            if session_id in self._active_turns:
                raise ValueError("A turn is already running in this session")
            session = self._sessions.get(session_id)
            if session is not None:
                self._active_turns.add(session_id)
            return deepcopy(session)

    def end_turn(self, session_id: str) -> None:
        with self._lock:
            self._active_turns.discard(session_id)
            self._evict()

    def snapshot(self, session_id: str) -> dict | None:
        """Return a thread-safe deep snapshot of the session as a dict.

        Use this from request handlers instead of ``get(...).to_dict()`` —
        the latter races against ``append_message`` because dict iteration
        happens outside the store lock.
        """
        with self._lock:
            self._evict()
            session = self._sessions.get(session_id)
            if session is None:
                return None
            return deepcopy(session.to_dict())

    def list(self) -> list[ChatSession]:
        with self._lock:
            self._evict()
            return sorted(self._sessions.values(),
                          key=lambda s: s.last_activity, reverse=True)

    def list_summaries(self) -> list[dict]:
        """Snapshot of every session's summary, taken under the lock."""
        with self._lock:
            self._evict()
            ordered = sorted(self._sessions.values(),
                             key=lambda s: s.last_activity, reverse=True)
            return [s.summary() for s in ordered]

    def delete(self, session_id: str) -> bool:
        with self._lock:
            if session_id in self._active_turns:
                raise ValueError("Cancel the active turn before deleting the session")
            return self._sessions.pop(session_id, None) is not None

    def append_message(self, session_id: str, message: ChatMessage) -> ChatSession | None:
        with self._lock:
            self._evict()
            session = self._sessions.get(session_id)
            if session is None:
                return None
            previous = list(session.messages)
            session.append(message)
            self._evict()
            if sum(_message_bytes(m) for s in self._sessions.values() for m in s.messages) > OMNI_CHAT_STORE_MAX_BYTES:
                session.messages = previous
                raise ValueError("Active sessions exceed the chat memory budget")
            return session


# Module-level singleton wired into ``state.py``.
chat_sessions = ChatSessionStore()


def render_history_for_worker(session: ChatSession,
                               new_user_message: ChatMessage, *, max_chars: int = 32768) -> str:
    """Flatten session history + the new user message into a worker prompt.

    Workers don't speak OpenAI's role array; we render a clean transcript
    they can complete. System messages are concatenated up front, then the
    Role: content turn-list, then ``Assistant:`` to anchor the next reply.
    """
    parts: list[str] = []
    system_lines: list[str] = []
    for m in session.messages:
        if m.role == "system":
            system_lines.append(m.content)
    if system_lines:
        parts.append("\n\n".join(system_lines))

    for m in session.messages:
        if m.role == "system":
            continue
        role_label = m.role.capitalize()
        if m.role == "tool":
            tag = m.name or m.tool_call_id or "tool"
            parts.append(f"Tool[{tag}]: {m.content}")
        else:
            parts.append(f"{role_label}: {m.content}")
    required = (["\n\n".join(system_lines)] if system_lines else []) + [f"User: {new_user_message.content}", "Assistant:"]
    if len("\n".join(required)) > max_chars:
        raise ValueError("System prompt and current message exceed the worker text limit")
    parts.extend([f"User: {new_user_message.content}", "Assistant:"])
    # Preserve the system prompt and newest request, dropping oldest history.
    while len("\n".join(parts)) > max_chars:
        del parts[1 if system_lines else 0]
    return "\n".join(parts)
