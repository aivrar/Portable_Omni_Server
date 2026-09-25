import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import workers
from worker_registry import WorkerInfo


def request():
    return workers.SpawnWorkerRequest(
        model="qwen3_omni",
        variant="instruct",
        precision="bf16",
        placement=workers.WorkerPlacementRequest(
            mode="auto",
            eligible_devices=["GPU-A", "GPU-B"],
            primary_device="GPU-A",
            require_all=True,
        ),
    )


def test_analyze_route_returns_no_weight_plan(monkeypatch):
    plan = {"valid": False, "blockers": ["insufficient memory"]}
    monkeypatch.setattr(workers.worker_manager, "analyze_placement", lambda **kwargs: plan)

    result = asyncio.run(workers.analyze_worker(request()))

    assert result == {"ready_to_spawn": False, "placement_plan": plan}


def test_spawn_route_hard_stops_invalid_plan_before_process_creation(monkeypatch):
    plan = {"valid": False, "blockers": ["insufficient memory"]}
    monkeypatch.setattr(workers.worker_manager, "analyze_placement", lambda **kwargs: plan)
    spawn = AsyncMock()
    monkeypatch.setattr(workers.worker_manager, "spawn_worker", spawn)

    with pytest.raises(workers.HTTPException) as exc_info:
        asyncio.run(workers.spawn_worker(request()))

    assert exc_info.value.status_code == 409
    spawn.assert_not_awaited()


def test_spawn_route_forwards_fresh_valid_plan(monkeypatch):
    plan = {
        "valid": True,
        "mode": "auto",
        "primary_device": "cuda:1",
        "gpu_pool": ["cuda:1", "cuda:0"],
        "gpu_device_map": {"cuda:1": "cuda:0", "cuda:0": "cuda:1"},
    }
    worker = WorkerInfo(
        worker_id="qwen3_omni-1",
        model="qwen3_omni",
        port=8201,
        device="cuda:1",
        variant="instruct",
        precision="bf16",
        placement_mode="auto",
        gpu_pool=plan["gpu_pool"],
        gpu_device_map=plan["gpu_device_map"],
        placement_plan=plan,
        status="ready",
    )
    monkeypatch.setattr(workers.worker_manager, "analyze_placement", lambda **kwargs: plan)
    spawn = AsyncMock(return_value=worker)
    monkeypatch.setattr(workers.worker_manager, "spawn_worker", spawn)

    result = asyncio.run(workers.spawn_worker(request()))

    assert result["gpu_pool"] == ["cuda:1", "cuda:0"]
    assert result["placement_plan"] == plan
    assert spawn.await_args.kwargs["placement_plan"] == plan
