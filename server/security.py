"""Small security helpers shared by the gateway and tests."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

TOKEN_MIN_LENGTH = 32
_LOOPBACK_ORIGIN_RE = re.compile(
    r"^https?://(127\.0\.0\.1|localhost|\[::1\]):[0-9]+$"
)
_HF_TOKEN_RE = re.compile(r"^hf_[A-Za-z0-9_-]{20,}$")


def is_loopback_origin(origin: str | None) -> bool:
    """Return True when Origin is absent or is an HTTP loopback origin."""
    if not origin:
        return True
    return bool(_LOOPBACK_ORIGIN_RE.fullmatch(origin))


def origin_matches_host(origin: str | None, host: str | None, *, allow_remote: bool | None = None) -> bool:
    """Return True when Origin is absent or exactly matches the request host."""
    if not origin:
        return True
    if allow_remote is None:
        allow_remote = os.environ.get("OMNI_API_ALLOW_REMOTE", "").lower() in {"1", "true", "yes", "on"}
    if not host or (not allow_remote and not is_loopback_origin(origin)):
        return False
    parsed = urlparse(origin)
    return (parsed.scheme in {"http", "https"} and not parsed.username
            and not parsed.password and parsed.path in {"", "/"}
            and not parsed.query and not parsed.fragment
            and parsed.netloc.lower() == host.lower())


def required_scope(method: str, path: str, *, autospawn: bool = False) -> str:
    """Classify existing route families; unknown mutations require management."""
    if path == "/api/keys/whoami":
        return "read"
    if (path.startswith("/api/keys")
            or path in {"/api/setup/hf-token", "/api/app/shutdown", "/api/restart", "/api/shutdown"}
            or (method.upper() not in {"GET", "HEAD", "OPTIONS"}
                and path.startswith(("/api/system/", "/api/maintenance")))):
        return "admin"
    if method.upper() in {"GET", "HEAD", "OPTIONS"}:
        if autospawn and path in {"/api/ace_step/state", "/api/audio_lab/state", "/api/minimax_music3/state"}:
            return "manage"
        return "read"
    if path in {"/api/workers/analyze", "/api/workflows/analyze", "/api/workflows/probe", "/api/outputs/zip"}:
        return "read"
    if (path == "/api/outputs/audio/compose"
            or path.startswith(("/v1/", "/api/chat/", "/api/sessions", "/api/stt/", "/api/tts/"))
            or re.fullmatch(r"/api/workflows/(?:[^/]+/)?(?:run|queue)", path)
            or re.fullmatch(r"/api/comfy/[^/]+/proxy/(?:prompt|interrupt)", path)
            or re.fullmatch(r"/api/(?:ace_step|audio_lab|moss|minimax_music3)/(?:generate(?:-ranked)?|sfx|score|analyze|a2a|inpaint|uncond|cancel|repaint|edit|extend|cover|vocal2bgm|extract|lego|complete|create-sample|format-sample|understand|simple|lyric2vocal|text2samples|vae/(?:encode|decode|reconstruct))", path)):
        return "generate"
    return "manage"


_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"})


def is_loopback_peer(host: str | None) -> bool:
    """Return True when a TCP peer address is the local host.

    Covers IPv4 loopback (127.0.0.0/8), IPv6 loopback (``::1``), the
    IPv4-mapped form, and the literal ``localhost``. Used to gate the
    token-dispensing ``/api/session`` route: a request that reaches a
    127.0.0.1-bound socket necessarily has a loopback peer, so this never
    rejects legitimate traffic when the gateway is loopback-bound, but it
    blocks token disclosure if the gateway is ever directly exposed.
    """
    if not host:
        return False
    return host in _LOOPBACK_PEERS or host.startswith("127.")


def write_secret_file(path: Path, value: str) -> None:
    """Write a secret with 0600 permissions from creation time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
        os.chmod(str(path), 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= TOKEN_MIN_LENGTH else None


def get_or_create_token(path: Path, env_value: str | None = None) -> str:
    """Read an existing local API token or create one atomically."""
    if env_value and len(env_value.strip()) >= TOKEN_MIN_LENGTH:
        return env_value.strip()

    existing = _read_token(path)
    if existing:
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_token(path)
        if existing:
            return existing
        write_secret_file(path, token)
        return token

    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    os.chmod(str(path), 0o600)
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except TypeError:
        # Starlette decodes header/query values as latin-1, so a credential
        # containing non-ASCII bytes makes hmac.compare_digest raise TypeError.
        # Compare as bytes instead so a malformed token is rejected (False)
        # rather than crashing the auth middleware with a 500.
        try:
            return hmac.compare_digest(
                provided.encode("utf-8", "ignore"), expected.encode("utf-8")
            )
        except Exception:
            return False


def is_valid_hf_token(token: str) -> bool:
    return bool(_HF_TOKEN_RE.fullmatch(token.strip()))
