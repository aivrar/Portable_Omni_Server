import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from omni_placement import analyze_worker_placement, estimate_model_vram_mb
from worker_manager import WorkerManager, normalize_worker_launch_devices
from worker_registry import WorkerInfo, WorkerRegistry


GPU_3060 = "GPU-3060-STABLE"
GPU_3090 = "GPU-3090-STABLE"


def devices(*, external_pid=False):
    return [
        {
            "id": "cuda:0",
            "uuid": GPU_3060,
            "stable_id": GPU_3060,
            "name": "RTX 3060",
            "vram_total_mb": 12288,
            "vram_free_mb": 10700,
            "compute_pids": [777] if external_pid else [],
        },
        {
            "id": "cuda:1",
            "uuid": GPU_3090,
            "stable_id": GPU_3090,
            "name": "RTX 3090",
            "vram_total_mb": 24576,
            "vram_free_mb": 24300,
            "compute_pids": [],
        },
        {"id": "cpu", "stable_id": "cpu", "vram_free_mb": 0},
    ]


def test_documented_vram_estimates_are_conservative():
    assert estimate_model_vram_mb("qwen_omni_7b", "gptq-int4") == 6 * 1024
    assert estimate_model_vram_mb("qwen3_omni", "instruct") == 60 * 1024
    assert estimate_model_vram_mb("nemotron_nano_omni", "nvfp4") == 21 * 1024


def test_auto_uses_stable_uuid_and_smallest_viable_subset():
    plan = analyze_worker_placement(
        model="qwen_omni_7b",
        variant="gptq-int4",
        legacy_device=None,
        placement={
            "mode": "auto",
            "eligible_devices": [GPU_3060, GPU_3090],
            "primary_device": GPU_3090,
        },
        devices=devices(),
    )

    assert plan["valid"] is True
    assert plan["gpu_pool"] == ["cuda:1"]
    assert plan["gpu_uuids"] == [GPU_3090]
    assert plan["gpu_device_map"] == {"cuda:1": "cuda:0"}
    assert plan["hf_device_map"] == "auto"


def test_ace_step_can_split_dit_and_lm_across_both_gpus():
    plan = analyze_worker_placement(
        model="ace_step",
        variant=None,
        legacy_device=None,
        placement={
            "mode": "auto",
            "eligible_devices": [GPU_3060, GPU_3090],
            "primary_device": GPU_3090,
            "require_all": True,
            "reserve_mb": 512,
        },
        devices=devices(),
    )

    assert plan["valid"] is True
    assert plan["gpu_pool"] == ["cuda:1", "cuda:0"]
    assert plan["gpu_device_map"] == {
        "cuda:1": "cuda:0",
        "cuda:0": "cuda:1",
    }
    assert any("independent components" in item for item in plan["warnings"])


def test_require_all_keeps_primary_first_and_reports_foreign_pid():
    plan = analyze_worker_placement(
        model="qwen_omni_7b",
        variant="base",
        legacy_device=None,
        placement={
            "mode": "auto",
            "eligible_devices": [GPU_3060, GPU_3090],
            "primary_device": GPU_3090,
            "require_all": True,
            "reserve_mb": 512,
        },
        devices=devices(external_pid=True),
        managed_pids=set(),
    )

    assert plan["valid"] is True
    assert plan["gpu_pool"] == ["cuda:1", "cuda:0"]
    assert plan["gpu_device_map"] == {
        "cuda:1": "cuda:0",
        "cuda:0": "cuda:1",
    }
    assert plan["external_compute_pids"] == {"cuda:0": [777]}


def test_qwen_gptq_auto_multigpu_is_blocked_after_live_backend_failure():
    plan = analyze_worker_placement(
        model="qwen_omni_7b",
        variant="gptq-int4",
        legacy_device=None,
        placement={
            "mode": "auto",
            "eligible_devices": [GPU_3090, GPU_3060],
            "primary_device": GPU_3090,
            "max_memory_mb": {GPU_3090: 4096, GPU_3060: 4096},
            "require_all": True,
        },
        devices=devices(),
    )

    assert plan["valid"] is False
    assert any("GPTQ backend" in item for item in plan["blockers"])


def test_oversized_qwen3_is_a_hard_stop_across_both_gpus():
    plan = analyze_worker_placement(
        model="qwen3_omni",
        variant="instruct",
        legacy_device=None,
        placement={"mode": "auto", "require_all": True},
        devices=devices(),
    )

    assert plan["valid"] is False
    assert any("Estimated requirement is 61440 MB" in item for item in plan["blockers"])


def test_nemotron_nvfp4_fits_3090_without_unnecessary_sharding():
    plan = analyze_worker_placement(
        model="nemotron_nano_omni",
        variant="nvfp4",
        legacy_device=None,
        placement={
            "mode": "auto",
            "eligible_devices": [GPU_3060, GPU_3090],
            "primary_device": GPU_3090,
        },
        devices=devices(),
    )

    assert plan["valid"] is True
    assert plan["gpu_pool"] == ["cuda:1"]


def test_manual_map_resolves_physical_uuid_to_worker_logical_device():
    plan = analyze_worker_placement(
        model="qwen_omni_7b",
        variant="base",
        legacy_device=None,
        placement={
            "mode": "manual",
            "eligible_devices": [GPU_3090, GPU_3060],
            "primary_device": GPU_3090,
            "device_map": {
                "model.embed_tokens": GPU_3090,
                "model.layers.0": GPU_3060,
            },
        },
        devices=devices(),
    )

    assert plan["valid"] is True
    assert plan["hf_device_map"] == {
        "model.embed_tokens": "cuda:0",
        "model.layers.0": "cuda:1",
    }


def test_unshardable_model_rejects_multi_gpu_pool():
    plan = analyze_worker_placement(
        model="minicpm_o",
        variant="2_6",
        legacy_device=None,
        placement={"mode": "auto", "require_all": True},
        devices=devices(),
    )

    assert plan["valid"] is False
    assert any("does not support" in item for item in plan["blockers"])


def test_cpu_offload_requires_an_explicit_budget():
    plan = analyze_worker_placement(
        model="qwen3_omni",
        variant="instruct",
        legacy_device=None,
        placement={"mode": "auto", "require_all": True, "allow_cpu": True},
        devices=devices(),
    )

    assert plan["valid"] is False
    assert "allow_cpu requires an explicit positive cpu_memory_mb budget" in plan["blockers"]


def test_registry_exposes_multi_gpu_worker_on_every_physical_device():
    registry = WorkerRegistry(9100, 9101)
    worker = WorkerInfo(
        worker_id="qwen3_omni-1",
        model="qwen3_omni",
        port=9100,
        device="cuda:1",
        placement_mode="auto",
        gpu_pool=["cuda:1", "cuda:0"],
        gpu_device_map={"cuda:1": "cuda:0", "cuda:0": "cuda:1"},
    )
    registry.register(worker)

    assert registry.workers_on_device("cuda:1") == [worker]
    assert registry.workers_on_device("cuda:0") == [worker]
    state = registry.to_dict_list()[0]
    assert state["gpu_pool"] == ["cuda:1", "cuda:0"]
    assert state["gpu_device_map"]["cuda:0"] == "cuda:1"


def test_launcher_preserves_primary_first_visibility_mapping():
    primary, pool, device_map, visible, worker_device = normalize_worker_launch_devices(
        "cuda:1",
        {
            "primary_device": "cuda:1",
            "gpu_pool": ["cuda:1", "cuda:0"],
            "gpu_device_map": {"cuda:1": "cuda:0", "cuda:0": "cuda:1"},
        },
    )

    assert primary == "cuda:1"
    assert pool == ["cuda:1", "cuda:0"]
    assert device_map == {"cuda:1": "cuda:0", "cuda:0": "cuda:1"}
    assert visible == "1,0"
    assert worker_device == "cuda:0"


def test_cpu_offload_cannot_exceed_current_app_cgroup_budget():
    plan = analyze_worker_placement(
        model="qwen3_omni",
        variant="instruct",
        legacy_device=None,
        placement={
            "mode": "auto",
            "require_all": True,
            "allow_cpu": True,
            "cpu_memory_mb": 16000,
        },
        devices=devices(),
        memory_state={
            "limit_mb": 24576,
            "usage_mb": 9000,
            "reclaimable_cache_mb": 1000,
            "effective_usage_mb": 8000,
            "reserve_mb": 4096,
            "available_worker_mb": 12480,
        },
    )

    assert plan["valid"] is False
    assert any("safe app-cgroup budget of 12480 MB" in item for item in plan["blockers"])


def test_memory_pressure_alerts_are_visible_without_inventing_a_hard_blocker():
    plan = analyze_worker_placement(
        model="nemotron_nano_omni",
        variant="nvfp4",
        legacy_device="cuda:1",
        placement={"mode": "single", "primary_device": "cuda:1"},
        devices=devices(),
        memory_state={
            "limit_mb": 21504,
            "usage_mb": 1024,
            "reclaimable_cache_mb": 0,
            "effective_usage_mb": 1024,
            "reserve_mb": 4096,
            "available_worker_mb": 16384,
            "pressure_alerts": ["I/O PSI avg10 is elevated at 25.00%"],
        },
    )

    assert any("I/O PSI avg10" in item for item in plan["warnings"])


def test_memory_state_uses_workload_child_limit():
    with tempfile.TemporaryDirectory() as temp_dir:
        group = Path(temp_dir)
        (group / "memory.limit_in_bytes").write_text(
            str(21 * 1024 ** 3), encoding="ascii"
        )
        (group / "memory.usage_in_bytes").write_text(
            str(10 * 1024 ** 3), encoding="ascii"
        )
        (group / "memory.stat").write_text(
            f"cache {6 * 1024 ** 3}\nrss {3 * 1024 ** 3}\n", encoding="ascii"
        )
        (group / "memory.memsw.usage_in_bytes").write_text(
            str(11 * 1024 ** 3), encoding="ascii"
        )
        (group / "memory.max_usage_in_bytes").write_text(
            str(12 * 1024 ** 3), encoding="ascii"
        )
        (group / "memory.failcnt").write_text("2", encoding="ascii")
        (group / "tasks").write_text("101\n202\n", encoding="ascii")
        with mock.patch.dict(
            os.environ,
            {
                "OMNI_WORKLOAD_MEMORY_CGROUP": str(group),
                "OMNI_WORKER_CPU_RESERVE_MB": "4096",
            },
        ):
            state = WorkerManager.detect_app_memory_state()

    assert state["limit_mb"] == 21 * 1024
    assert state["usage_mb"] == 10 * 1024
    assert state["reclaimable_cache_mb"] == 6 * 1024
    assert state["rss_mb"] == 3 * 1024
    assert state["swap_mb"] == 1024
    assert state["peak_usage_mb"] == 12 * 1024
    assert state["fail_count"] == 2
    assert state["active_pids"] == 2
    assert state["effective_usage_mb"] == 4 * 1024
    assert state["available_worker_mb"] == 13 * 1024
