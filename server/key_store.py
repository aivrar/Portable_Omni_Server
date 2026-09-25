"""Multi-key bearer auth with scope tags.

Storage: ``RUNTIME_DIR/api_keys.json``. Each key is stored with its raw
public id and a SHA-256 hash of its secret — the secret is shown to the
operator exactly once at create time, then only the hash is on disk.

Scopes (free-form strings, but the gateway honours these conventionally):

* ``admin`` -- everything (workers, install, maintenance, key management)
* ``generate`` -- chat, /v1, sessions/messages, workflows/run, comfy/prompt
* ``read`` -- GETs on jobs, sessions, outputs, comfy listings, system info
* ``manage`` -- worker spawn/kill, comfy start/stop, asset installs

A request whose key has ``["admin"]`` scope passes every scope check. The
gateway falls back to legacy ``loopback-token`` mode by default; bearer
mode is opted into with ``OMNI_AUTH_MODE=bearer``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import RUNTIME_DIR

logger = logging.getLogger(__name__)

KEY_STORE_PATH = Path(RUNTIME_DIR) / "api_keys.json"
KEY_ID_LEN = 16            # hex chars in the public id
KEY_SECRET_LEN = 48        # urlsafe chars in the secret
SCOPE_ADMIN = "admin"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class ApiKey:
    id: str
    secret_hash: str
    scopes: list[str] = field(default_factory=lambda: [SCOPE_ADMIN])
    label: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float | None = None

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "scopes": self.scopes,
            "label": self.label,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class ApiKeyStore:
    """Thread-safe key store with atomic JSON persistence."""

    def __init__(self, path: Path = KEY_STORE_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._keys: dict[str, ApiKey] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("api_keys.json unreadable: %s", e)
            return
        if not isinstance(data, dict) or not isinstance(data.get("keys", []), list):
            logger.warning("api_keys.json has an invalid structure")
            return
        for entry in data.get("keys", []):
            try:
                if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                        or not isinstance(entry.get("secret_hash"), str)
                        or not isinstance(entry.get("scopes"), list)
                        or not all(isinstance(s, str) for s in entry["scopes"])):
                    continue
                k = ApiKey(
                    id=entry["id"],
                    secret_hash=entry["secret_hash"],
                    scopes=list(entry["scopes"]),
                    label=entry.get("label", ""),
                    created_at=float(entry.get("created_at") or time.time()),
                    last_used_at=entry.get("last_used_at"),
                )
                self._keys[k.id] = k
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".api-keys-", dir=self.path.parent)
        tmp = Path(tmp_name)
        payload = json.dumps(
            {"keys": [asdict(k) for k in self._keys.values()]},
            indent=2,
        )
        try:
            # Create the temp file 0600 from the start (JOB-3): writing it with
            # the default umask first left a brief world-readable window over
            # the secret hashes before chmod ran. O_EXCL-less O_TRUNC is fine —
            # the .tmp name is per-store and the write is lock-held.
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp, self.path)
            # os.replace preserves the source file's 0600 mode; re-assert it
            # defensively in case the destination pre-existed with wider perms.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as e:
            logger.warning("Could not save api_keys.json: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ---- mutators ----
    def create(self, scopes: list[str] | None = None,
               label: str = "") -> tuple[ApiKey, str]:
        """Create a new key. Returns ``(record, plaintext_secret)``.

        The plaintext secret is the only chance to learn it - it isn't
        stored on disk, only its sha256.
        """
        scopes = list([SCOPE_ADMIN] if scopes is None else scopes)
        if not scopes or any(s not in {"admin", "read", "generate", "manage"} for s in scopes):
            raise ValueError("At least one valid scope is required")
        with self._lock:
            while True:
                kid = "key_" + secrets.token_hex(KEY_ID_LEN // 2)
                if kid not in self._keys:
                    break
            secret = secrets.token_urlsafe(KEY_SECRET_LEN)
            key = ApiKey(
                id=kid,
                secret_hash=_hash_secret(secret),
                scopes=scopes,
                label=label[:128],
            )
            self._keys[kid] = key
            try:
                self._save()
            except OSError:
                self._keys.pop(kid, None)
                raise
            return key, secret

    def revoke(self, key_id: str) -> bool:
        with self._lock:
            previous = self._keys.pop(key_id, None)
            if previous is not None:
                try:
                    self._save()
                except OSError:
                    self._keys[key_id] = previous
                    raise
            return previous is not None

    def touch_last_used(self, key_id: str) -> None:
        with self._lock:
            k = self._keys.get(key_id)
            if k:
                k.last_used_at = time.time()
                # Skip persisting on every request - the in-memory copy is
                # authoritative. ``last_used_at`` is best-effort metadata.

    # ---- accessors ----
    def list(self) -> list[ApiKey]:
        with self._lock:
            return list(self._keys.values())

    def get(self, key_id: str) -> ApiKey | None:
        with self._lock:
            return self._keys.get(key_id)

    def authenticate(self, presented: str | None) -> ApiKey | None:
        """Match a bearer token of form ``<id>.<secret>`` or just ``<secret>``.

        Both formats walk every key and compare the hashed secret with
        ``hmac.compare_digest`` so timing leaks neither the id nor the
        secret. The id-prefix form is a hint, not a fast-path.
        """
        if not presented:
            return None
        with self._lock:
            keys = list(self._keys.values())
        if "." in presented:
            kid, secret = presented.split(".", 1)
        else:
            kid, secret = "", presented
        target_hash = _hash_secret(secret)
        match: ApiKey | None = None
        # Walk every key with constant-time compare; do NOT short-circuit on
        # id mismatch (which would leak whether the id was valid via timing).
        for k in keys:
            id_ok = (kid == "") or hmac.compare_digest(kid, k.id)
            hash_ok = hmac.compare_digest(target_hash, k.secret_hash)
            if id_ok and hash_ok and match is None:
                match = k
        return match


# Module-level singleton. Initialised once; the routers import this directly.
api_keys = ApiKeyStore()
