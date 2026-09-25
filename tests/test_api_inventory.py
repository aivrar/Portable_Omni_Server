"""Keep the durable API inventory aligned with registered gateway routes."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi.routing import APIRoute, APIWebSocketRoute  # noqa: E402

import omni_comfy_server  # noqa: E402


def test_inventory_mentions_every_registered_route_path():
    inventory = (ROOT / "docs" / "api-capability-inventory.md").read_text(
        encoding="utf-8"
    )
    paths = {
        route.path
        for route in omni_comfy_server.app.routes
        if isinstance(route, (APIRoute, APIWebSocketRoute))
    }
    missing = sorted(path for path in paths if path not in inventory)
    assert not missing, f"API inventory is missing route paths: {missing}"
