"""Worker Registry - Tracks active workers, their status, and provides load balancing."""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess
    from typing import IO

logger = logging.getLogger(__name__)


@dataclass
class WorkerInfo:
    """State for a single worker subprocess."""
    worker_id: str          # e.g. "qwen_omni_3b-1"
    model: str              # "qwen_omni_3b"
    port: int               # 8202
    device: str             # "cuda:0", "cuda:1", "cpu"
    variant: str | None = None   # variant_id from OMNI_MODEL_VARIANTS
    lora: str | None = None      # LoRA adapter name (dir under models/lora/)
    precision: str | None = None # 'fp16' | 'bf16' | 'fp32' | None (auto)
    placement_mode: str = "single"
    gpu_pool: list[str] = field(default_factory=list)
    gpu_device_map: dict[str, str] = field(default_factory=dict)
    placement_plan: dict = field(default_factory=dict)
    gpu_memory: list[dict] = field(default_factory=list)
    process: subprocess.Popen | None = None
    log_fh: IO[str] | None = None
    pid_file: str | None = None
    status: str = "starting"  # "starting" | "loading" | "ready" | "busy" | "dead"
    current_job: str | None = None
    last_health: float = 0.0
    health_failures: int = 0
    vram_used_mb: int = 0
    vram_total_mb: int = 0


@dataclass
class ComfyInstance:
    """State for a running ComfyUI instance."""
    instance_id: str        # e.g. "comfy-gpu0-8188"
    port: int               # 8188
    device: str             # "cuda:0", "cuda:1", "cpu"
    vram_mode: str          # "normal", "low", "none", "cpu"
    precision: str | None = None  # 'fp16' | 'bf16' | 'fp32' | None (auto)
    preview_method: str = "auto"
    disable_pinned_memory: bool = False
    startup_options: dict = field(default_factory=dict)
    gpu_pool: list[str] = field(default_factory=list)
    gpu_device_map: dict[str, str] = field(default_factory=dict)
    process: subprocess.Popen | None = None
    log_fh: IO[str] | None = None
    pid_file: str | None = None
    status: str = "starting"  # "starting" | "ready" | "dead"
    last_health: float = 0.0
    health_failures: int = 0
    vram_used_mb: int = 0
    vram_total_mb: int = 0


class WorkerRegistry:
    """Thread-safe registry of omni workers with port pool and load balancing."""

    def __init__(self, port_min: int = 8201, port_max: int = 8250):
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerInfo] = {}
        self._port_pool: set[int] = set(range(port_min, port_max + 1))
        self._model_counters: dict[str, int] = {}
        self._round_robin: dict[str, int] = {}

    def allocate_port(self) -> int:
        with self._lock:
            if not self._port_pool:
                raise RuntimeError("No available worker ports")
            return self._port_pool.pop()

    def release_port(self, port: int) -> None:
        with self._lock:
            self._port_pool.add(port)

    def next_worker_id(self, model: str) -> str:
        with self._lock:
            count = self._model_counters.get(model, 0) + 1
            self._model_counters[model] = count
            return f"{model}-{count}"

    def register(self, worker: WorkerInfo) -> None:
        with self._lock:
            self._workers[worker.worker_id] = worker
        logger.info("Registered worker %s (model=%s, port=%d, device=%s)",
                    worker.worker_id, worker.model, worker.port, worker.device)

    def unregister(self, worker_id: str) -> WorkerInfo | None:
        with self._lock:
            return self._workers.pop(worker_id, None)

    def get(self, worker_id: str) -> WorkerInfo | None:
        with self._lock:
            return self._workers.get(worker_id)

    def get_ready_workers(self, model: str, device: str | None = None) -> list[WorkerInfo]:
        with self._lock:
            return [w for w in self._workers.values()
                    if w.model == model and w.status == "ready"
                    and (device is None or w.device == device)]

    def atomic_pick_and_mark_busy(
        self, model: str, job_id: str, device: str | None = None,
    ) -> WorkerInfo | None:
        with self._lock:
            ready = [w for w in self._workers.values()
                     if w.model == model and w.status == "ready"
                     and (device is None or w.device == device)]
            if not ready:
                return None
            idx = self._round_robin.get(model, 0) % len(ready)
            self._round_robin[model] = (idx + 1) % max(len(ready), 1)
            worker = ready[idx]
            worker.status = "busy"
            worker.current_job = job_id
            return worker

    def claim_ready(self, worker_id: str, job_id: str) -> bool:
        """Reserve this exact worker without overwriting another caller's job."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None or worker.status != "ready":
                return False
            worker.status = "busy"
            worker.current_job = job_id
            return True

    def mark_busy(self, worker_id: str, job_id: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = "busy"
                w.current_job = job_id

    def mark_ready(self, worker_id: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = "ready"
                w.current_job = None

    def mark_loading(self, worker_id: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = "loading"

    def mark_dead(self, worker_id: str) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.status = "dead"
                w.current_job = None

    def update_health(self, worker_id: str, vram_used_mb: int = 0,
                      vram_total_mb: int = 0,
                      gpu_memory: list[dict] | None = None) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.last_health = time.time()
                w.health_failures = 0
                w.vram_used_mb = vram_used_mb
                w.vram_total_mb = vram_total_mb
                if gpu_memory is not None:
                    w.gpu_memory = list(gpu_memory)

    def record_health_failure(self, worker_id: str) -> int:
        with self._lock:
            w = self._workers.get(worker_id)
            if w:
                w.health_failures += 1
                return w.health_failures
        return 0

    def all_workers(self) -> list[WorkerInfo]:
        with self._lock:
            return list(self._workers.values())

    def workers_on_device(self, device: str) -> list[WorkerInfo]:
        with self._lock:
            return [
                w for w in self._workers.values()
                if w.device == device or device in w.gpu_pool
            ]

    def to_dict_list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "worker_id": w.worker_id,
                    "model": w.model,
                    "port": w.port,
                    "device": w.device,
                    "variant": w.variant,
                    "lora": w.lora,
                    "precision": w.precision,
                    "placement_mode": w.placement_mode,
                    "gpu_pool": list(w.gpu_pool),
                    "gpu_device_map": dict(w.gpu_device_map),
                    "placement_plan": dict(w.placement_plan),
                    "status": w.status,
                    "current_job": w.current_job,
                    "pid": w.process.pid if w.process else None,
                    "vram_used_mb": w.vram_used_mb,
                    "vram_total_mb": w.vram_total_mb,
                    "gpu_memory": list(w.gpu_memory),
                }
                for w in self._workers.values()
            ]


class ComfyRegistry:
    """Thread-safe registry of ComfyUI instances."""

    def __init__(self, port_min: int = 8188, port_max: int = 8199):
        self._lock = threading.Lock()
        self._instances: dict[str, ComfyInstance] = {}
        self._port_pool: set[int] = set(range(port_min, port_max + 1))

    def allocate_port(self) -> int:
        with self._lock:
            if not self._port_pool:
                raise RuntimeError("No available ComfyUI ports")
            port = min(self._port_pool)  # prefer lowest port
            self._port_pool.remove(port)
            return port

    def release_port(self, port: int) -> None:
        with self._lock:
            self._port_pool.add(port)

    def register(self, instance: ComfyInstance) -> None:
        with self._lock:
            self._instances[instance.instance_id] = instance
            self._port_pool.discard(instance.port)
        logger.info("Registered ComfyUI instance %s (port=%d, device=%s)",
                    instance.instance_id, instance.port, instance.device)

    def unregister(self, instance_id: str) -> ComfyInstance | None:
        with self._lock:
            return self._instances.pop(instance_id, None)

    def get(self, instance_id: str) -> ComfyInstance | None:
        with self._lock:
            return self._instances.get(instance_id)

    def all_instances(self) -> list[ComfyInstance]:
        with self._lock:
            return list(self._instances.values())

    def mark_ready(self, instance_id: str) -> None:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst:
                inst.status = "ready"

    def mark_dead(self, instance_id: str) -> None:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst:
                inst.status = "dead"

    def update_health(self, instance_id: str, vram_used_mb: int = 0,
                      vram_total_mb: int = 0) -> None:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst:
                inst.last_health = time.time()
                inst.health_failures = 0
                inst.vram_used_mb = vram_used_mb
                inst.vram_total_mb = vram_total_mb

    def record_health_failure(self, instance_id: str) -> int:
        with self._lock:
            inst = self._instances.get(instance_id)
            if inst:
                inst.health_failures += 1
                return inst.health_failures
        return 0

    def instance_count(self) -> int:
        with self._lock:
            return len(self._instances)

    def to_dict_list(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "instance_id": i.instance_id,
                    "port": i.port,
                    "device": i.device,
                    "vram_mode": i.vram_mode,
                    "precision": i.precision,
                    "preview_method": i.preview_method,
                    "disable_pinned_memory": i.disable_pinned_memory,
                    "startup_options": dict(i.startup_options),
                    "gpu_pool": list(i.gpu_pool),
                    "gpu_device_map": dict(i.gpu_device_map),
                    "status": i.status,
                    "pid": i.process.pid if i.process else None,
                    "vram_used_mb": i.vram_used_mb,
                    "vram_total_mb": i.vram_total_mb,
                }
                for i in self._instances.values()
            ]
