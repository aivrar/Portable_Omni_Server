"""Worker Manager - Spawns, monitors, and kills omni model worker subprocesses."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from config import (
    BASE_DIR, PYTHON_PATH, WORKER_PORT_MIN, WORKER_PORT_MAX,
    WORKER_HEALTH_INTERVAL, WORKER_STARTUP_TIMEOUT,
    WORKER_MAX_HEALTH_FAILURES, WORKER_LOG_DIR, WORKER_DEFAULT_DEVICE,
    MODELS_DIR, CACHE_DIR, PID_DIR, APP_DIR,
    MOSS_TTS_MODEL_ID, MOSS_SFX_MODEL_ID,
    MOSS_TTS_VENV_DIR, MOSS_SFX_VENV_DIR,
    MINIMAX_MUSIC3_MODEL_ID,
)
from worker_registry import WorkerRegistry, WorkerInfo
from omni_placement import analyze_worker_placement
from resource_limits import place_process_in_workload_cgroup, release_empty_workload_cache

from process_identity import process_matches, process_start_time, owned_process_group, process_alive

logger = logging.getLogger(__name__)


def normalize_worker_launch_devices(
    device: str, placement_plan: dict | None,
) -> tuple[str, list[str], dict[str, str], str | None, str]:
    """Return primary, pool, map, CUDA visibility, and worker-local device."""
    primary = str((placement_plan or {}).get("primary_device") or device)
    gpu_pool = list((placement_plan or {}).get("gpu_pool") or [])
    if not gpu_pool and primary.startswith("cuda:"):
        gpu_pool = [primary]
    if len(gpu_pool) != len(set(gpu_pool)):
        raise ValueError("Worker gpu_pool contains duplicate devices")
    for physical in gpu_pool:
        prefix, separator, index = str(physical).partition(":")
        if prefix != "cuda" or not separator or not index.isdigit():
            raise ValueError(f"Invalid physical CUDA device in gpu_pool: {physical}")
    if gpu_pool and primary != gpu_pool[0]:
        raise ValueError("Worker primary_device must be first in gpu_pool")
    gpu_device_map = dict((placement_plan or {}).get("gpu_device_map") or {})
    if not gpu_device_map and gpu_pool:
        gpu_device_map = {
            physical: f"cuda:{index}" for index, physical in enumerate(gpu_pool)
        }
    expected_map = {
        physical: f"cuda:{index}" for index, physical in enumerate(gpu_pool)
    }
    if gpu_device_map != expected_map:
        raise ValueError("Worker gpu_device_map does not match primary-first gpu_pool")
    visible = ",".join(item.split(":", 1)[1] for item in gpu_pool) if gpu_pool else None
    worker_device = "cuda:0" if gpu_pool else primary
    return primary, gpu_pool, gpu_device_map, visible, worker_device


def _apply_model_runtime_env(env: dict[str, str], model: str) -> dict[str, str]:
    """Apply narrowly-scoped compatibility policy before worker imports."""
    if model == MOSS_SFX_MODEL_ID:
        # MOSS-SFX decorates its DiT entry point with fullgraph torch.compile.
        # Current PyTorch cannot trace einops' set.symmetric_difference call,
        # so the compiled graph fails only after the expensive cold load.
        # Eager execution is supported by the upstream pipeline and is the
        # reliable default until that graph becomes traceable.
        env["TORCHDYNAMO_DISABLE"] = "1"
    if model == MINIMAX_MUSIC3_MODEL_ID:
        # Generate was calling the Hub mid-run ("Fetching 2 files") and
        # leaking descriptors on WSL. The snapshot is already local.
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return env


def _is_live_gateway_pid(pid: int) -> bool:
    """Return true only when *pid* is the still-running Omni gateway."""
    if pid <= 0:
        return False
    try:
        if not process_alive(pid):
            return False
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ")
        return b"omni_comfy_server.py" in cmdline
    except (OSError, ProcessLookupError, PermissionError):
        return False


class WorkerManager:
    """Manages the lifecycle of omni model worker subprocesses."""

    # Placement and post-unload verification are VRAM-sensitive. A long cache
    # can falsely block a safe warm run or conceal successful cleanup.
    _DEVICE_CACHE_TTL = 2

    def __init__(self, registry: WorkerRegistry | None = None):
        self.registry = registry or WorkerRegistry(WORKER_PORT_MIN, WORKER_PORT_MAX)
        self._health_task: asyncio.Task | None = None
        self._device_cache: list[dict] | None = None
        self._device_cache_time: float = 0.0
        WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        PID_DIR.mkdir(parents=True, exist_ok=True)

    def analyze_placement(self, model: str, variant: str | None = None,
                          device: str | None = None,
                          placement: dict | object | None = None,
                          precision: str | None = None) -> dict:
        managed_pids = {
            int(worker.process.pid)
            for worker in self.registry.all_workers()
            if worker.process and worker.process.pid
        }
        return analyze_worker_placement(
            model=model,
            variant=variant,
            legacy_device=device or (WORKER_DEFAULT_DEVICE if placement is None else None),
            placement=placement,
            devices=self.detect_devices(refresh=True),
            managed_pids=managed_pids,
            memory_state=self.detect_app_memory_state(),
            precision=precision,
        )

    @staticmethod
    def detect_app_memory_state() -> dict:
        limit_mb = max(1024, int(os.environ.get("OMNI_MEMORY_LIMIT_GB", "24")) * 1024)
        reserve_mb = max(1024, int(os.environ.get("OMNI_WORKER_CPU_RESERVE_MB", "4096")))
        usage_bytes = 0
        cache_bytes = 0
        rss_bytes = 0
        swap_bytes = 0
        peak_bytes = 0
        fail_count = 0
        active_pids = 0
        memory_group = Path(
            os.environ.get(
                "OMNI_WORKLOAD_MEMORY_CGROUP",
                "/sys/fs/cgroup/memory/omni_studio/workloads",
            )
        )
        if not memory_group.is_dir():
            memory_group = Path("/sys/fs/cgroup/memory/omni_studio")
        try:
            raw_limit = int((memory_group / "memory.limit_in_bytes").read_text().strip())
            if 0 < raw_limit < (1 << 60):
                limit_mb = int(raw_limit / 1024 / 1024)
            usage_bytes = int((memory_group / "memory.usage_in_bytes").read_text().strip())
            for line in (memory_group / "memory.stat").read_text().splitlines():
                key, _, raw_value = line.partition(" ")
                if key in ("total_cache", "cache"):
                    cache_bytes = max(cache_bytes, int(raw_value or 0))
                elif key in ("total_rss", "rss"):
                    rss_bytes = max(rss_bytes, int(raw_value or 0))
            for name in ("tasks", "cgroup.procs"):
                task_path = memory_group / name
                if task_path.is_file():
                    active_pids = len({
                        row.strip() for row in task_path.read_text().splitlines()
                        if row.strip()
                    })
                    break
            try:
                peak_bytes = int(
                    (memory_group / "memory.max_usage_in_bytes").read_text().strip()
                )
            except (OSError, ValueError):
                pass
            try:
                fail_count = int((memory_group / "memory.failcnt").read_text().strip())
            except (OSError, ValueError):
                pass
            try:
                memsw_bytes = int(
                    (memory_group / "memory.memsw.usage_in_bytes").read_text().strip()
                )
                swap_bytes = max(0, memsw_bytes - usage_bytes)
            except (OSError, ValueError):
                pass
        except (OSError, ValueError):
            pass
        usage_mb = int(usage_bytes / 1024 / 1024)
        cache_mb = min(usage_mb, int(cache_bytes / 1024 / 1024))
        effective_usage_mb = max(0, usage_mb - cache_mb)
        available_worker_mb = max(0, limit_mb - effective_usage_mb - reserve_mb)
        host_available_mb = 0
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    host_available_mb = int(line.split()[1]) // 1024
                    break
        except (OSError, ValueError, IndexError):
            pass

        def _psi_avg10(resource: str) -> float | None:
            try:
                first = Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
                for field in first.split():
                    if field.startswith("avg10="):
                        return round(float(field.split("=", 1)[1]), 2)
            except (OSError, ValueError, IndexError):
                return None
            return None

        memory_psi = _psi_avg10("memory")
        io_psi = _psi_avg10("io")
        pressure_alerts: list[str] = []
        if available_worker_mb < 1024:
            pressure_alerts.append("less than 1 GiB remains in the safe workload budget")
        if host_available_mb and host_available_mb < reserve_mb:
            pressure_alerts.append("host available RAM is below the configured reserve")
        if memory_psi is not None and memory_psi >= 5.0:
            pressure_alerts.append(f"memory PSI avg10 is elevated at {memory_psi:.2f}%")
        if io_psi is not None and io_psi >= 20.0:
            pressure_alerts.append(f"I/O PSI avg10 is elevated at {io_psi:.2f}%")
        return {
            "limit_mb": limit_mb,
            "usage_mb": usage_mb,
            "reclaimable_cache_mb": cache_mb,
            "rss_mb": int(rss_bytes / 1024 / 1024),
            "swap_mb": int(swap_bytes / 1024 / 1024),
            "peak_usage_mb": int(peak_bytes / 1024 / 1024),
            "fail_count": fail_count,
            "active_pids": active_pids,
            "effective_usage_mb": effective_usage_mb,
            "reserve_mb": reserve_mb,
            "available_worker_mb": available_worker_mb,
            "host_available_mb": host_available_mb,
            "memory_psi_avg10": memory_psi,
            "io_psi_avg10": io_psi,
            "pressure_status": "degraded" if pressure_alerts else "ok",
            "pressure_alerts": pressure_alerts,
        }

    async def spawn_worker(self, model: str, device: str | None = None,
                           precision: str | None = None,
                           variant: str | None = None,
                           lora: str | None = None,
                           placement_plan: dict | None = None) -> WorkerInfo:
        if placement_plan and not placement_plan.get("valid", False):
            raise ValueError(
                "Invalid worker placement: "
                + "; ".join(placement_plan.get("blockers") or ["unknown blocker"])
            )
        requested_device = str(
            (placement_plan or {}).get("primary_device")
            or device
            or WORKER_DEFAULT_DEVICE
        )
        device, gpu_pool, gpu_device_map, cuda_visible, worker_device = (
            normalize_worker_launch_devices(requested_device, placement_plan)
        )
        placement_mode = str((placement_plan or {}).get("mode") or "single")
        port = self.registry.allocate_port()
        # Any failure between allocating the port and a successfully launched
        # process must return the port to the bounded pool, otherwise repeated
        # failures exhaust it. The launch try-block below already releases on
        # spawn failure; this guards the setup window before it.
        try:
            worker_id = self.registry.next_worker_id(model)

            worker_script = str(BASE_DIR / "omni_worker.py")
            python_exe = str(PYTHON_PATH)
            if model == MOSS_TTS_MODEL_ID:
                moss_python = MOSS_TTS_VENV_DIR / "bin" / "python3"
                if moss_python.exists():
                    python_exe = str(moss_python)
            elif model == MOSS_SFX_MODEL_ID:
                moss_python = MOSS_SFX_VENV_DIR / "bin" / "python3"
                if moss_python.exists():
                    python_exe = str(moss_python)

            env = os.environ.copy()
            env.pop("HF_TOKEN", None)
            env["HF_HOME"] = str(MODELS_DIR)
            env["HUGGINGFACE_HUB_CACHE"] = str(MODELS_DIR / "hub")
            env["HF_XET_CACHE"] = str(MODELS_DIR / "xet")
            env["TORCH_HOME"] = str(MODELS_DIR / "torch")
            env["TRANSFORMERS_CACHE"] = str(MODELS_DIR / "hub")
            env["PIP_CACHE_DIR"] = str(CACHE_DIR / "pip")
            env["XDG_CACHE_HOME"] = str(CACHE_DIR / "xdg")
            env["TMPDIR"] = str(CACHE_DIR / "tmp")
            env["PYTHONPYCACHEPREFIX"] = str(CACHE_DIR / "pycache")
            env["TORCH_EXTENSIONS_DIR"] = str(CACHE_DIR / "torch_extensions")
            env["TRITON_CACHE_DIR"] = str(CACHE_DIR / "triton")
            env["CUDA_CACHE_PATH"] = str(CACHE_DIR / "cuda")
            env["NUMBA_CACHE_DIR"] = str(CACHE_DIR / "numba")
            env["MPLCONFIGDIR"] = str(CACHE_DIR / "matplotlib")
            env["HF_DATASETS_CACHE"] = str(MODELS_DIR / "hf_datasets")
            env["IMAGEIO_FFMPEG_EXE"] = "/usr/bin/ffmpeg"
            env["PYTHONUNBUFFERED"] = "1"
            env["OMNI_APP_INSTANCE"] = str(APP_DIR)
            env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
            _apply_model_runtime_env(env, model)

            if cuda_visible:
                env["CUDA_VISIBLE_DEVICES"] = cuda_visible
            if placement_plan:
                env["OMNI_WORKER_PLACEMENT"] = json.dumps(
                    placement_plan, separators=(",", ":")
                )

            cmd = [
                python_exe, worker_script,
                "--model", model,
                "--port", str(port),
                "--device", worker_device,
            ]
            if precision:
                cmd.extend(["--precision", precision])

            # Resolve variant weights_dir from the registry (if variant specified)
            variant_weights_dir = None
            if variant:
                from config import get_variant
                v = get_variant(model, variant)
                if v:
                    variant_weights_dir = v["weights_dir"]
                    cmd.extend(["--variant", variant,
                               "--variant-weights-dir", variant_weights_dir])

            # LoRA path: resolve name to absolute path in models/lora/
            if lora:
                from config import LORA_DIR
                lora_path = str(LORA_DIR / lora)
                cmd.extend(["--lora", lora_path])

            log_file = WORKER_LOG_DIR / f"worker_{model}_{port}.log"
            logger.info("Spawning worker %s: model=%s port=%d device=%s variant=%s lora=%s",
                        worker_id, model, port, device,
                        variant or "default", lora or "none")
        except Exception:
            # Release the port before the launch try-block is ever entered.
            self.registry.release_port(port)
            raise

        try:
            log_fh = open(log_file, "w", encoding="utf-8", buffering=1)
            process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(BASE_DIR),
                start_new_session=True,
            )
            cgroup_result = place_process_in_workload_cgroup(process.pid)
            if not cgroup_result["applied"]:
                raise RuntimeError("Workload cgroup admission failed: " + str(cgroup_result.get("reason", "unknown")))
            pid_file = PID_DIR / f"worker_{worker_id}.json"
            pid_file.write_text(json.dumps({
                "kind": "worker",
                "pid": process.pid,
                "pgid": os.getpgid(process.pid),
                "start_time": process_start_time(process.pid),
                "worker_id": worker_id,
                "model": model,
                "port": port,
                "placement_mode": placement_mode,
                "gpu_pool": gpu_pool,
                "gpu_device_map": gpu_device_map,
                "app_instance": str(APP_DIR),
                "owner_pid": os.getpid(),
                "script": worker_script,
                "cwd": str(BASE_DIR),
                "started_at": time.time(),
            }), encoding="utf-8")
        except Exception as e:
            self.registry.release_port(port)
            if 'process' in locals() and process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            if 'log_fh' in locals():
                log_fh.close()
            raise RuntimeError(f"Failed to launch worker process: {e}")

        worker = WorkerInfo(
            worker_id=worker_id,
            model=model,
            port=port,
            device=device,
            variant=variant,
            lora=lora,
            precision=precision,
            placement_mode=placement_mode,
            gpu_pool=gpu_pool,
            gpu_device_map=gpu_device_map,
            placement_plan=dict(placement_plan or {}),
            process=process,
            log_fh=log_fh,
            pid_file=str(pid_file),
            status="starting",
        )
        self.registry.register(worker)

        try:
            await self._wait_for_healthy(worker)
            await self._load_model(worker)
        except Exception as e:
            try:
                log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                logger.error("Worker %s failed. Log tail:\n%s", worker_id, log_tail)
            except Exception:
                pass
            await self._force_kill(worker)
            self.registry.unregister(worker_id)
            self.registry.release_port(port)
            # Failed cold loads can leave many GiB of checkpoint pages in the
            # workload child even though no workload PID remains. Reclaim only
            # that scoped cgroup cache; release_empty_workload_cache() refuses
            # to act if another Omni workload is still active.
            self._reclaim_memory()
            raise RuntimeError(f"Worker {worker_id} failed: {e}") from e

        return worker

    async def _wait_for_healthy(self, worker: WorkerInfo) -> None:
        url = f"http://127.0.0.1:{worker.port}/health"
        deadline = time.time() + WORKER_STARTUP_TIMEOUT

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.time() < deadline:
                if worker.process and worker.process.poll() is not None:
                    raise RuntimeError(
                        f"Worker {worker.worker_id} exited with code "
                        f"{worker.process.returncode} during startup"
                    )
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        logger.info("Worker %s FastAPI is up (pid=%d)",
                                    worker.worker_id,
                                    worker.process.pid if worker.process else 0)
                        return
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass
                await asyncio.sleep(1.0)

        raise RuntimeError(
            f"Worker {worker.worker_id} did not start within {WORKER_STARTUP_TIMEOUT}s"
        )

    async def _load_model(self, worker: WorkerInfo) -> None:
        load_url = f"http://127.0.0.1:{worker.port}/load"
        health_url = f"http://127.0.0.1:{worker.port}/health"

        self.registry.mark_loading(worker.worker_id)
        logger.info("Loading model on worker %s ...", worker.worker_id)

        # 30 min load budget for cold cache on slow WSL2 ext4 disks. Some
        # of the larger checkpoints (7B+ omni models) take 15-25 min on
        # first load when the OS page cache hasn't warmed up yet.
        async with httpx.AsyncClient(timeout=1800.0) as client:
            try:
                resp = await client.post(load_url)
                if resp.status_code != 200:
                    detail = resp.text[:500]
                    raise RuntimeError(
                        f"Worker {worker.worker_id} /load returned "
                        f"{resp.status_code}: {detail}"
                    )
                await self._load_audio_lab_boot_assets(client, worker)
            except httpx.TimeoutException:
                raise RuntimeError(
                    f"Worker {worker.worker_id} model load timed out (1800s)"
                )

            try:
                resp = await client.get(health_url)
                if resp.status_code == 200:
                    data = resp.json()
                    worker.vram_used_mb = data.get("vram_used_mb", 0)
                    worker.vram_total_mb = data.get("vram_total_mb", 0)
                    worker.gpu_memory = list(data.get("gpu_memory") or [])
            except Exception:
                pass

        self.registry.mark_ready(worker.worker_id)
        self.registry.update_health(worker.worker_id,
                                     vram_used_mb=worker.vram_used_mb,
                                     vram_total_mb=worker.vram_total_mb,
                                     gpu_memory=worker.gpu_memory)
        logger.info("Worker %s model loaded and ready", worker.worker_id)

    async def _load_audio_lab_boot_assets(self, client: httpx.AsyncClient,
                                          worker: WorkerInfo) -> None:
        if worker.model != "audio_lab":
            return
        if worker.variant:
            resp = await client.post(
                f"http://127.0.0.1:{worker.port}/audio_lab/load_sa",
                json={"sa_variant": worker.variant},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Audio Lab worker {worker.worker_id} failed to load "
                    f"{worker.variant}: {resp.text[:500]}"
                )
        try:
            from config import CLAP_MODELS, is_clap_installed
            default_clap = None
            for clap_id, meta in CLAP_MODELS.items():
                if meta.get("default"):
                    default_clap = clap_id
                    break
            if default_clap and is_clap_installed(default_clap):
                resp = await client.post(
                    f"http://127.0.0.1:{worker.port}/audio_lab/load_clap",
                    json={"clap_variant": default_clap},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Audio Lab worker %s could not auto-load CLAP %s: %s",
                        worker.worker_id, default_clap, resp.text[:500],
                    )
        except Exception as e:
            logger.warning("Audio Lab CLAP auto-load skipped: %s", e)

    async def kill_worker(self, worker_id: str) -> bool:
        worker = self.registry.get(worker_id)
        if not worker:
            return self.kill_orphan_workers(worker_id=worker_id) > 0

        logger.info("Killing worker %s (port=%d)", worker_id, worker.port)

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"http://127.0.0.1:{worker.port}/unload")
        except Exception:
            pass

        await self._force_kill(worker)
        self.registry.unregister(worker_id)
        self.registry.release_port(worker.port)
        self._reclaim_memory()
        return True

    async def _force_kill(self, worker: WorkerInfo) -> None:
        pid = worker.process.pid if worker.process else None
        if worker.process:
            try:
                try:
                    if worker.process.poll() is None:
                        pgid = owned_process_group(pid)
                        if pgid is not None:
                            os.killpg(pgid, signal.SIGKILL)
                        else:
                            worker.process.kill()
                except (OSError, ProcessLookupError):
                    worker.process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(worker.process.wait), timeout=5
                    )
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    try:
                        worker.process.kill()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Error killing worker %s (pid=%s): %s",
                               worker.worker_id, pid, e)
            worker.process = None
        if worker.log_fh:
            try:
                worker.log_fh.close()
            except Exception:
                pass
            worker.log_fh = None
        if worker.pid_file:
            try:
                Path(worker.pid_file).unlink(missing_ok=True)
            except OSError:
                pass
            worker.pid_file = None

    async def kill_all_workers(self) -> int:
        workers = self.registry.all_workers()
        if not workers:
            killed_orphans = self.kill_orphan_workers()
            if killed_orphans:
                self._reclaim_memory()
            return killed_orphans
        results = await asyncio.gather(
            *(self.kill_worker(w.worker_id) for w in workers),
            return_exceptions=True,
        )
        killed = sum(1 for r in results if r is True)
        killed += self.kill_orphan_workers()
        if killed:
            self._reclaim_memory()
        return killed

    @staticmethod
    def _reclaim_memory():
        import gc as _gc
        _gc.collect()
        result = release_empty_workload_cache()
        if result.get("released"):
            logger.info(
                "Released empty workload cgroup cache: %d MiB -> %d MiB",
                result["before_mb"], result["after_mb"],
            )

    def kill_orphan_workers(self, worker_id: str | None = None) -> int:
        # Active pids tracked by THIS gateway's registry — never kill these,
        # they're real workers that may still be loading. Without this guard
        # the scheduled gc-pid-dir task kills currently-loading workers
        # because their pid files exist before the registry promotes them
        # past `loading`.
        active_pids = set()
        try:
            for w in self.registry.all_workers():
                if w.process and w.process.pid:
                    active_pids.add(int(w.process.pid))
        except Exception:
            pass

        killed = 0
        preserved_live_record = False
        PID_DIR.mkdir(parents=True, exist_ok=True)
        for pid_path in PID_DIR.glob("worker_*.json"):
            try:
                record = json.loads(pid_path.read_text(encoding="utf-8"))
                if record.get("app_instance") != str(APP_DIR):
                    continue
                if worker_id and record.get("worker_id") != worker_id:
                    continue
                pid = int(record.get("pid", 0))
                pgid = int(record.get("pgid", pid))
                if pid <= 0:
                    pid_path.unlink(missing_ok=True)
                    continue
                if pid in active_pids:
                    # Currently-tracked worker; not an orphan.
                    preserved_live_record = True
                    continue
                owner_pid = int(record.get("owner_pid", 0) or 0)
                if _is_live_gateway_pid(owner_pid):
                    # Cleanup may run through a manager without this gateway's
                    # in-memory registry.  Trust the durable owner only after
                    # confirming it is still the Omni gateway process.
                    preserved_live_record = True
                    continue
                if not process_alive(pid):
                    pid_path.unlink(missing_ok=True)
                    continue
                if not process_matches(pid, BASE_DIR / "omni_worker.py", port=int(record.get("port") or 0), start_time=record.get("start_time")):
                    logger.warning("Skipping stale worker record %s", pid_path.name)
                    continue
                logger.warning("Killing app-owned orphan worker pid=%d", pid)
                try:
                    group = owned_process_group(pid, pgid)
                    if group is not None:
                        os.killpg(group, signal.SIGKILL)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                pid_path.unlink(missing_ok=True)
                killed += 1
            except Exception:
                continue

        if killed or preserved_live_record:
            return killed
        if worker_id:
            return 0

        # Compatibility fallback for workers launched before pid files existed.
        expected_script = str(BASE_DIR / "omni_worker.py")
        try:
            result = subprocess.run(
                ["pgrep", "-f", "python.*omni_worker.py.*--port"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return 0
            my_pid = os.getpid()
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid == my_pid or pid in active_pids:
                    continue
                try:
                    if not process_matches(pid, expected_script):
                        continue
                    with open(f"/proc/{pid}/status") as f:
                        ppid = None
                        for status_line in f:
                            if status_line.startswith("PPid:"):
                                ppid = int(status_line.split()[1])
                                break
                        if ppid is None or ppid not in (1, my_pid):
                            continue
                except (FileNotFoundError, ValueError, OSError):
                    continue
                logger.warning("Killing orphan worker pid=%d (ppid=%s)", pid, ppid)
                try:
                    os.kill(pid, 9)
                    killed += 1
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass
        return killed

    async def health_check_loop(self) -> None:
        logger.info("Worker health check loop started (interval=%ds)",
                    WORKER_HEALTH_INTERVAL)

        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                try:
                    await asyncio.sleep(WORKER_HEALTH_INTERVAL)
                    workers = self.registry.all_workers()
                    to_cleanup = []

                    for w in workers:
                        if w.status == "dead":
                            to_cleanup.append(w)
                            continue
                        if w.status in ("starting", "loading"):
                            continue
                        if w.status == "busy":
                            if w.process and w.process.poll() is not None:
                                logger.warning(
                                    "Worker %s died while busy (exit code %d)",
                                    w.worker_id, w.process.returncode)
                                to_cleanup.append(w)
                            continue

                        if w.process and w.process.poll() is not None:
                            logger.warning("Worker %s died (exit code %d)",
                                          w.worker_id, w.process.returncode)
                            to_cleanup.append(w)
                            continue

                        try:
                            resp = await client.get(
                                f"http://127.0.0.1:{w.port}/health"
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                self.registry.update_health(
                                    w.worker_id,
                                    vram_used_mb=data.get("vram_used_mb", 0),
                                    vram_total_mb=data.get("vram_total_mb", 0),
                                    gpu_memory=data.get("gpu_memory") or [],
                                )
                            else:
                                failures = self.registry.record_health_failure(w.worker_id)
                                if failures >= WORKER_MAX_HEALTH_FAILURES:
                                    to_cleanup.append(w)
                        except Exception:
                            failures = self.registry.record_health_failure(w.worker_id)
                            if failures >= WORKER_MAX_HEALTH_FAILURES:
                                to_cleanup.append(w)

                    for w in to_cleanup:
                        logger.info("Cleaning up dead worker %s", w.worker_id)
                        await self._force_kill(w)
                        self.registry.unregister(w.worker_id)
                        self.registry.release_port(w.port)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Health check error: %s", e)

    def start_health_checks(self) -> None:
        if self._health_task is None or self._health_task.done():
            loop = asyncio.get_running_loop()
            self._health_task = loop.create_task(self.health_check_loop())

    def stop_health_checks(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()

    async def detect_devices_async(self) -> list[dict]:
        return await asyncio.to_thread(self.detect_devices)

    def detect_devices(self, refresh: bool = False) -> list[dict]:
        if refresh:
            self._device_cache = None
            self._device_cache_time = 0.0
        now = time.time()
        if self._device_cache is not None and (now - self._device_cache_time) < self._DEVICE_CACHE_TTL:
            for dev in self._device_cache:
                dev["workers"] = [w.worker_id
                                  for w in self.registry.workers_on_device(dev["id"])]
            return self._device_cache

        devices = []
        try:
            result = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=index,uuid,name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 5:
                        idx = parts[0]
                        device_id = f"cuda:{idx}"
                        workers_on = [w.worker_id
                                      for w in self.registry.workers_on_device(device_id)]
                        devices.append({
                            "id": device_id,
                            "uuid": parts[1],
                            "stable_id": parts[1],
                            "name": parts[2],
                            "vram_total_mb": int(float(parts[3])),
                            "vram_free_mb": int(float(parts[4])),
                            "workers": workers_on,
                            "compute_pids": [],
                        })
                try:
                    process_result = subprocess.run(
                        ["nvidia-smi",
                         "--query-compute-apps=pid,gpu_uuid",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if process_result.returncode == 0:
                        pids_by_uuid: dict[str, set[int]] = {}
                        for line in process_result.stdout.strip().split("\n"):
                            parts = [p.strip() for p in line.split(",")]
                            if len(parts) < 2:
                                continue
                            try:
                                pid = int(parts[0])
                            except ValueError:
                                continue
                            pids_by_uuid.setdefault(parts[1], set()).add(pid)
                        for device in devices:
                            device["compute_pids"] = sorted(
                                pids_by_uuid.get(str(device.get("uuid") or ""), set())
                            )
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        cpu_workers = [w.worker_id for w in self.registry.workers_on_device("cpu")]
        devices.append({
            "id": "cpu",
            "uuid": None,
            "stable_id": "cpu",
            "name": "CPU",
            "vram_total_mb": 0,
            "vram_free_mb": 0,
            "workers": cpu_workers,
        })

        self._device_cache = devices
        self._device_cache_time = now
        return devices
