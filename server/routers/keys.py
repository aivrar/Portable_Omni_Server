"""API key management routes (admin-only).

Endpoints under ``/api/keys`` let operators provision, list, and revoke
bearer keys for the gateway. Each new key is shown ONCE at creation time;
only its sha256 hash is stored. Revocation is immediate (in-memory drop +
disk save).

These routes only matter when ``OMNI_AUTH_MODE=bearer``. In the default
loopback-token mode the keys are still creatable but no requests reach
the gateway with them, so they're inert.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from key_store import SCOPE_ADMIN, api_keys

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_SCOPES = frozenset({"admin", "read", "generate", "manage"})


class CreateKeyRequest(BaseModel):
    label: str = Field(default="", max_length=128)
    scopes: list[str] = Field(default_factory=lambda: [SCOPE_ADMIN], min_length=1)


def _require_admin(request: Request) -> None:
    """Allow only callers with ``admin`` scope (or the loopback token)."""
    scopes = getattr(request.state, "scopes", None) or []
    if SCOPE_ADMIN in scopes:
        return
    raise HTTPException(status_code=403, detail="admin scope required")


@router.get("/api/keys")
async def list_keys(request: Request):
    _require_admin(request)
    return {"keys": [k.to_public() for k in api_keys.list()]}


@router.post("/api/keys", status_code=201)
async def create_key(request: Request, req: CreateKeyRequest):
    _require_admin(request)
    bad = [s for s in req.scopes if s not in _VALID_SCOPES]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Unknown scopes: {bad}. Valid: {sorted(_VALID_SCOPES)}")
    try:
        key, secret = api_keys.create(scopes=req.scopes, label=req.label)
    except OSError:
        raise HTTPException(status_code=503, detail="Could not persist API key")
    return {
        **key.to_public(),
        "secret": secret,
        "warning": "This is the only time the secret will be shown.",
        "presentation": f"{key.id}.{secret}",
    }


@router.delete("/api/keys/{key_id}")
async def revoke_key(request: Request, key_id: str):
    _require_admin(request)
    try:
        revoked = api_keys.revoke(key_id)
    except OSError:
        raise HTTPException(status_code=503, detail="Could not persist key revocation")
    if not revoked:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "revoked", "id": key_id}


@router.get("/api/keys/whoami")
async def whoami(request: Request):
    """Inspect the credential the current request was authenticated with.

    Useful for debugging which scopes a key actually carries. Returns
    ``{"loopback_token": true}`` when the loopback session token was used
    (no key record), else the matched key's public fields.
    """
    key = getattr(request.state, "api_key", None)
    if key is None:
        return {"loopback_token": True, "scopes": ["admin"]}
    return {"loopback_token": False, **key.to_public()}
