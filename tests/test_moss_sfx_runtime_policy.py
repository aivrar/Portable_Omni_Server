import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from worker_manager import _apply_model_runtime_env
from worker_registry import WorkerInfo, WorkerRegistry


def test_moss_sfx_disables_unsupported_fullgraph_compile():
    env = _apply_model_runtime_env({"KEEP": "yes"}, "moss_sfx")
    assert env == {"KEEP": "yes", "TORCHDYNAMO_DISABLE": "1"}


def test_other_workers_do_not_inherit_moss_compile_policy():
    env = _apply_model_runtime_env({"KEEP": "yes"}, "audio_lab")
    assert env == {"KEEP": "yes"}


def test_minimax_music3_forces_local_hub():
    env = _apply_model_runtime_env({"KEEP": "yes"}, "minimax_music3")
    assert env["KEEP"] == "yes"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"


def test_worker_registry_filters_ready_and_busy_selection_by_device():
    registry = WorkerRegistry(9101, 9102)
    first = WorkerInfo("moss_sfx-1", "moss_sfx", 9101, "cuda:0", status="ready")
    second = WorkerInfo("moss_sfx-2", "moss_sfx", 9102, "cuda:1", status="ready")
    registry.register(first)
    registry.register(second)
    assert registry.get_ready_workers("moss_sfx", device="cuda:1") == [second]
    selected = registry.atomic_pick_and_mark_busy(
        "moss_sfx", "job-on-3090", device="cuda:1",
    )
    assert selected is second
    assert first.status == "ready"
    assert second.current_job == "job-on-3090"
