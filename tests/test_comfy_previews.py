"""ComfyUI preview WebSockets must share the submitting client identity."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi import HTTPException  # noqa: E402
from routers import previews  # noqa: E402


def test_preview_upstream_includes_encoded_client_id():
    assert previews._upstream_ws_url(8188, "tq-krea2:preview") == (
        "ws://127.0.0.1:8188/ws?clientId=tq-krea2%3Apreview"
    )


def test_preview_upstream_rejects_unsafe_client_id():
    with pytest.raises(HTTPException):
        previews._upstream_ws_url(8188, "bad/client")


def test_setup_declares_preview_websocket_runtime_dependency():
    setup = (ROOT / "server" / "setup.sh").read_text(encoding="utf-8")
    assert '"websockets": "websockets"' in setup
    assert '"websockets>=13,<17"' in setup
