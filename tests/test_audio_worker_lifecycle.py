import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import HTTPException


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import ace_step, audio_lab, workers  # noqa: E402


class _Response:
    status_code = 200
    text = ""

    @staticmethod
    def json():
        return {"ok": True}


@pytest.mark.parametrize("module", [ace_step, audio_lab])
def test_specialized_inference_is_busy_then_ready(module):
    worker = SimpleNamespace(worker_id="worker-1", port=8123)
    client = SimpleNamespace(post=mock.AsyncMock(return_value=_Response()))
    registry = mock.Mock()
    registry.get.return_value = SimpleNamespace(status="busy")
    with mock.patch.object(module, "worker_registry", registry):
        result = asyncio.run(module._worker_post(client, worker, "/infer/test", {}))

    assert result == {"ok": True}
    registry.claim_ready.assert_called_once()
    registry.mark_ready.assert_called_once_with("worker-1")


@pytest.mark.parametrize("module", [ace_step, audio_lab])
def test_specialized_timeout_retires_exact_worker(module):
    worker = SimpleNamespace(worker_id="worker-1", port=8123)
    client = SimpleNamespace(post=mock.AsyncMock(side_effect=httpx.ReadTimeout("late")))
    registry = mock.Mock()
    registry.get.return_value = None
    manager = SimpleNamespace(kill_worker=mock.AsyncMock(return_value=True))
    with mock.patch.object(module, "worker_registry", registry), \
         mock.patch.object(module, "worker_manager", manager):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(module._worker_post(client, worker, "/infer/test", {}))

    assert raised.value.status_code == 504
    registry.mark_dead.assert_called_once_with("worker-1")
    manager.kill_worker.assert_awaited_once_with("worker-1")


def test_generic_timeout_retirement_marks_dead_before_kill():
    manager = SimpleNamespace(kill_worker=mock.AsyncMock(return_value=True))
    registry = mock.Mock()
    with mock.patch.object(workers, "worker_manager", manager), \
         mock.patch.object(workers, "worker_registry", registry):
        asyncio.run(workers._retire_timed_out_worker("worker-7"))

    registry.mark_dead.assert_called_once_with("worker-7")
    manager.kill_worker.assert_awaited_once_with("worker-7")
