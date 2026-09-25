"""ComfyUI Instance Manager - Spawns, monitors, and kills ComfyUI server instances."""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import httpx

from config import (
    COMFYUI_CACHE_DIR, COMFYUI_DIR, COMFYUI_MODELS_DIR, PYTHON_PATH,
    COMFYUI_PORT_MIN, COMFYUI_PORT_MAX,
    COMFYUI_MAX_INSTANCES, COMFYUI_STARTUP_TIMEOUT,
    COMFYUI_HEALTH_INTERVAL, COMFYUI_MAX_HEALTH_FAILURES, COMFYUI_VRAM_MODES,
    WORKER_LOG_DIR, WORKER_DEFAULT_DEVICE,
    OUTPUT_DIR, CACHE_DIR, PID_DIR, APP_DIR, SOURCE_APP_DIR,
    COMFYUI_MODEL_CATEGORIES,
)
from worker_registry import ComfyRegistry, ComfyInstance
from comfy_startup import COMPONENT_PRECISION_KEYS, build_startup_args
from resource_limits import place_process_in_workload_cgroup, release_empty_workload_cache

from process_identity import process_matches, process_start_time, owned_process_group, process_alive

logger = logging.getLogger(__name__)

_CUDA_DEVICE_RE = re.compile(r"^cuda:(0|[1-9][0-9]*)$")


class _AttachedProcess:
    """Small Popen-compatible handle for a live process inherited after restart."""

    def __init__(self, pid: int, port: int, start_time: str | None = None):
        self.pid = int(pid)
        self.port = int(port)
        self.start_time = start_time or process_start_time(pid)
        self.returncode = None

    def poll(self):
        if not process_matches(self.pid, str(COMFYUI_DIR / "main.py"), port=self.port, start_time=self.start_time):
            self.returncode = 0
            return self.returncode
        try:
            if process_alive(self.pid):
                return None
            self.returncode = 0
            return self.returncode
        except (OSError, ProcessLookupError):
            self.returncode = 0
            return self.returncode

    def wait(self, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(0.05)
        return self.returncode

    def kill(self):
        if self.poll() is None:
            os.kill(self.pid, signal.SIGKILL)


def _comfy_process_matches(pid: int, port: int) -> bool:
    """Protect against PID reuse before adopting a durable Comfy record."""
    if pid <= 0:
        return False
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
    except (OSError, PermissionError):
        return False
    expected_main = str(COMFYUI_DIR / "main.py").encode()
    if expected_main not in parts:
        return False
    for index, value in enumerate(parts[:-1]):
        if value == b"--port" and parts[index + 1] == str(port).encode():
            return True
    return False


async def _probe_recovered_comfy(port: int) -> tuple[bool, int, int]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"http://127.0.0.1:{port}/system_stats")
        if response.status_code != 200:
            return False, 0, 0
        devices = (response.json() or {}).get("devices") or []
        if not devices:
            return True, 0, 0
        device = devices[0]
        return (
            True,
            int(device.get("vram_used", 0) / 1024 / 1024),
            int(device.get("vram_total", 0) / 1024 / 1024),
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return False, 0, 0


def normalize_gpu_pool(device: str, gpu_pool: list[str] | None) -> tuple[list[str], dict[str, str]]:
    """Return a primary-first physical GPU pool and its Comfy-local mapping.

    Comfy sees the selected physical devices through ``CUDA_VISIBLE_DEVICES``.
    Ordering the primary first makes it local ``cuda:0`` while keeping every
    auxiliary GPU visible for per-component placement nodes.
    """
    primary = str(device or "").strip().lower()
    raw_pool = list(gpu_pool or [])
    if len(raw_pool) > 64:
        raise ValueError("gpu_pool supports at most 64 CUDA devices")
    if not primary.startswith("cuda:"):
        if raw_pool:
            raise ValueError("gpu_pool is only valid when the primary device is CUDA")
        return [], {}
    if not _CUDA_DEVICE_RE.fullmatch(primary):
        raise ValueError(f"Invalid CUDA device: {device}")

    normalized: list[str] = [primary]
    for raw in raw_pool:
        candidate = str(raw or "").strip().lower()
        if not _CUDA_DEVICE_RE.fullmatch(candidate):
            raise ValueError(f"Invalid gpu_pool device: {raw}")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized, {
        physical: f"cuda:{logical_index}"
        for logical_index, physical in enumerate(normalized)
    }


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


class ComfyManager:
    """Manages multiple ComfyUI server instances across GPUs."""

    def __init__(self, registry: ComfyRegistry | None = None):
        self.registry = registry or ComfyRegistry(COMFYUI_PORT_MIN, COMFYUI_PORT_MAX)
        self._health_task: asyncio.Task | None = None
        self._maintenance_reason: str | None = None
        WORKER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        PID_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def maintenance_reason(self) -> str | None:
        return self._maintenance_reason

    def begin_maintenance(self, reason: str) -> None:
        """Block external instance starts for one guarded maintenance job."""
        if self._maintenance_reason:
            raise RuntimeError(
                f"ComfyUI maintenance already in progress: {self._maintenance_reason}"
            )
        self._maintenance_reason = str(reason)

    def end_maintenance(self, reason: str) -> None:
        """Release a maintenance reservation only for its owning job."""
        if self._maintenance_reason == str(reason):
            self._maintenance_reason = None

    def is_installed(self) -> bool:
        return (COMFYUI_DIR / "main.py").exists()

    def ensure_model_storage(self) -> None:
        """Create ComfyUI's self-contained native model tree."""
        if not COMFYUI_DIR.exists():
            return
        for category in COMFYUI_MODEL_CATEGORIES:
            if "/" in category or "\\" in category or ".." in category:
                continue
            try:
                (COMFYUI_MODELS_DIR / category).mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.debug("Could not create ComfyUI model folder %s", category, exc_info=True)
        for cache_path in (
            COMFYUI_CACHE_DIR / "huggingface" / "hub",
            COMFYUI_CACHE_DIR / "huggingface" / "xet",
            COMFYUI_CACHE_DIR / "torch",
            COMFYUI_CACHE_DIR / "datasets",
        ):
            try:
                cache_path.mkdir(parents=True, exist_ok=True)
            except OSError:
                logger.debug("Could not create ComfyUI cache folder %s", cache_path, exc_info=True)

    def ensure_omni_bridge(self) -> None:
        """Refresh the app-owned compatibility nodes before every new instance."""
        source = SOURCE_APP_DIR / "comfy_nodes" / "omni_bridge"
        nodes_dir = COMFYUI_DIR / "custom_nodes"
        destination = nodes_dir / "omni_bridge"
        if not source.is_dir():
            logger.warning("OmniBridge source is unavailable: %s", source)
            return
        nodes_dir.mkdir(parents=True, exist_ok=True)
        staging = nodes_dir / f".omni_bridge.sync-{os.getpid()}"
        backup = nodes_dir / f".omni_bridge.backup-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(source, staging)
        try:
            if destination.exists():
                os.replace(destination, backup)
            os.replace(staging, destination)
        except Exception:
            if not destination.exists() and backup.exists():
                os.replace(backup, destination)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)

    async def recover_instances(self) -> int:
        """Re-adopt healthy app-owned Comfy processes after a gateway crash."""
        recovered = 0
        PID_DIR.mkdir(parents=True, exist_ok=True)
        for pid_path in sorted(PID_DIR.glob("comfy_*.json")):
            try:
                record = json.loads(pid_path.read_text(encoding="utf-8"))
                if record.get("app_instance") != str(APP_DIR):
                    continue
                pid = int(record.get("pid", 0) or 0)
                port = int(record.get("port", 0) or 0)
                instance_id = str(record.get("instance_id") or "").strip()
                if (
                    not instance_id.startswith("comfy-")
                    or "/" in instance_id
                    or "\\" in instance_id
                    or not (COMFYUI_PORT_MIN <= port <= COMFYUI_PORT_MAX)
                    or not _comfy_process_matches(pid, port)
                    or (record.get("start_time") is not None and process_start_time(pid) != str(record["start_time"]))
                ):
                    if not process_alive(pid):
                        pid_path.unlink(missing_ok=True)
                    continue
                if self.registry.get(instance_id) is not None:
                    continue

                gpu_pool = [str(item) for item in (record.get("gpu_pool") or [])]
                gpu_device_map = {
                    str(key): str(value)
                    for key, value in (record.get("gpu_device_map") or {}).items()
                }
                device = str(record.get("device") or (gpu_pool[0] if gpu_pool else "cpu"))
                ready, vram_used, vram_total = await _probe_recovered_comfy(port)
                instance = ComfyInstance(
                    instance_id=instance_id,
                    port=port,
                    device=device,
                    vram_mode=str(record.get("vram_mode") or "normal"),
                    precision=record.get("precision"),
                    preview_method=str(record.get("preview_method") or "auto"),
                    disable_pinned_memory=bool(record.get("disable_pinned_memory", False)),
                    startup_options=dict(record.get("startup_options") or {}),
                    gpu_pool=gpu_pool,
                    gpu_device_map=gpu_device_map,
                    process=_AttachedProcess(pid, port, record.get("start_time")),
                    log_fh=None,
                    pid_file=str(pid_path),
                    status="ready" if ready else "starting",
                    vram_used_mb=vram_used,
                    vram_total_mb=vram_total,
                )
                self.registry.register(instance)
                record["owner_pid"] = os.getpid()
                temp_path = pid_path.with_suffix(pid_path.suffix + ".tmp")
                temp_path.write_text(json.dumps(record), encoding="utf-8")
                os.replace(temp_path, pid_path)
                recovered += 1
                logger.info(
                    "Recovered ComfyUI instance %s (port=%d, pid=%d, ready=%s)",
                    instance_id, port, pid, ready,
                )
            except Exception:
                logger.warning("Could not recover ComfyUI record %s", pid_path, exc_info=True)
        return recovered

    async def start_instance(self, device: str | None = None,
                             vram_mode: str = "normal",
                             precision: str | None = None,
                             preview_method: str = "auto",
                             disable_pinned_memory: bool = False,
                             startup_options: dict | None = None,
                             gpu_pool: list[str] | None = None,
                             _maintenance_override: bool = False) -> ComfyInstance:
        # NOTE (audit COM-3): a free-form `extra_args` parameter used to be
        # appended verbatim to the launch argv (cmd.extend(extra_args)), a
        # latent argv-injection surface. No caller ever supplies it — the sole
        # HTTP caller rejects extra_args and passes None — and all legitimate
        # server-derived flags (VRAM mode, precision, extra model paths) are
        # built below from controlled sources. The parameter and extend have
        # been removed so untrusted argv can never reach the subprocess. If a
        # future caller needs to pass server-derived flags, reintroduce them
        # via a strict allowlist (each must match
        # ^--[a-z0-9][a-z0-9-]*(=[\w./:,-]+)?$ and reject anything else).
        if self._maintenance_reason and not _maintenance_override:
            raise RuntimeError(
                f"ComfyUI maintenance in progress: {self._maintenance_reason}"
            )
        if not self.is_installed():
            raise RuntimeError("ComfyUI is not installed")
        self.ensure_model_storage()
        self.ensure_omni_bridge()

        # NOTE (audit COM-1): the span from this max-instances check through
        # registry.register() below contains no `await`, so it runs atomically
        # with respect to the event loop — two concurrent start_instance() calls
        # cannot interleave here and both pass the gate. If an `await` is ever
        # introduced between this check and register(), wrap the span in an
        # asyncio.Lock to preserve the invariant.
        if self.registry.instance_count() >= COMFYUI_MAX_INSTANCES:
            raise RuntimeError(f"Maximum {COMFYUI_MAX_INSTANCES} ComfyUI instances reached")

        device = str(device or WORKER_DEFAULT_DEVICE)
        normalized_gpu_pool, gpu_device_map = normalize_gpu_pool(device, gpu_pool)
        port = self.registry.allocate_port()
        instance_id = f"comfy-{device.replace(':', '')}-{port}"

        python_exe = str(PYTHON_PATH)
        comfy_main = str(COMFYUI_DIR / "main.py")
        (OUTPUT_DIR / "comfyui").mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / "comfyui_input").mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "comfyui_temp").mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.pop("HF_TOKEN", None)
        comfy_hf_cache = COMFYUI_CACHE_DIR / "huggingface"
        env["HF_HOME"] = str(comfy_hf_cache)
        env["HUGGINGFACE_HUB_CACHE"] = str(comfy_hf_cache / "hub")
        env["HF_XET_CACHE"] = str(comfy_hf_cache / "xet")
        env["TORCH_HOME"] = str(COMFYUI_CACHE_DIR / "torch")
        env["TRANSFORMERS_CACHE"] = str(comfy_hf_cache / "hub")
        env["PIP_CACHE_DIR"] = str(CACHE_DIR / "pip")
        env["XDG_CACHE_HOME"] = str(CACHE_DIR / "xdg")
        env["TMPDIR"] = str(CACHE_DIR / "tmp")
        env["PYTHONPYCACHEPREFIX"] = str(CACHE_DIR / "pycache")
        env["TORCH_EXTENSIONS_DIR"] = str(CACHE_DIR / "torch_extensions")
        env["TRITON_CACHE_DIR"] = str(CACHE_DIR / "triton")
        env["CUDA_CACHE_PATH"] = str(CACHE_DIR / "cuda")
        env["NUMBA_CACHE_DIR"] = str(CACHE_DIR / "numba")
        env["MPLCONFIGDIR"] = str(CACHE_DIR / "matplotlib")
        env["HF_DATASETS_CACHE"] = str(COMFYUI_CACHE_DIR / "datasets")
        env["IMAGEIO_FFMPEG_EXE"] = "/usr/bin/ffmpeg"
        env["PYTHONUNBUFFERED"] = "1"
        env["OMNI_APP_INSTANCE"] = str(APP_DIR)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

        # GPU visibility. The physical primary is ordered first and therefore
        # remains Comfy-local cuda:0. Auxiliary devices stay visible for the
        # native/Omni multi-GPU placement nodes.
        if normalized_gpu_pool:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(
                physical.split(":", 1)[1] for physical in normalized_gpu_pool
            )
            env["OMNI_COMFY_GPU_POOL"] = json.dumps(normalized_gpu_pool)
            env["OMNI_COMFY_GPU_DEVICE_MAP"] = json.dumps(gpu_device_map, sort_keys=True)
            env["OMNI_COMFY_PRIMARY_DEVICE"] = normalized_gpu_pool[0]

        cmd = [
            python_exe, comfy_main,
            "--port", str(port),
            "--listen", "127.0.0.1",
            "--output-directory", str(OUTPUT_DIR / "comfyui"),
            "--input-directory", str(OUTPUT_DIR / "comfyui_input"),
            "--temp-directory", str(CACHE_DIR / "comfyui_temp"),
            "--preview-method", preview_method,
        ]
        if disable_pinned_memory:
            cmd.append("--disable-pinned-memory")

        # VRAM mode flags
        vram_flags = COMFYUI_VRAM_MODES.get("cpu" if device == "cpu" else vram_mode, [])
        cmd.extend(vram_flags)

        # Structured advanced flags are converted through a closed allowlist.
        # Component precision selectors override the coarse preset. When any
        # component is overridden, resolve all inherited components explicitly
        # instead of combining mutually-conflicting argparse flags.
        resolved_options = dict(startup_options or {})
        component_override = any(
            resolved_options.get(key, "inherit") != "inherit"
            for key in COMPONENT_PRECISION_KEYS
        )
        if component_override:
            inherited = precision or "inherit"
            for key in COMPONENT_PRECISION_KEYS:
                if resolved_options.get(key, "inherit") == "inherit":
                    resolved_options[key] = inherited
        else:
            # Preserve the original preset behavior for existing API clients.
            if precision == "fp16":
                cmd.extend(["--force-fp16", "--fp16-vae", "--fp16-text-enc"])
            elif precision == "bf16":
                cmd.extend(["--bf16-unet", "--bf16-vae", "--bf16-text-enc"])
            elif precision == "fp32":
                cmd.extend(["--force-fp32", "--fp32-vae", "--fp32-text-enc"])

        startup_args, normalized_options = build_startup_args(resolved_options)
        cmd.extend(startup_args)

        log_file = WORKER_LOG_DIR / f"comfy_{port}.log"
        logger.info("Starting ComfyUI instance %s: port=%d device=%s vram=%s",
                    instance_id, port, device, vram_mode)

        try:
            log_fh = open(log_file, "w", encoding="utf-8", buffering=1)
            process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(COMFYUI_DIR),
                start_new_session=True,
            )
            cgroup_result = place_process_in_workload_cgroup(process.pid)
            if not cgroup_result["applied"]:
                raise RuntimeError("Workload cgroup admission failed: " + str(cgroup_result.get("reason", "unknown")))
            pid_file = PID_DIR / f"comfy_{instance_id}.json"
            pid_file.write_text(json.dumps({
                "kind": "comfy",
                "pid": process.pid,
                "pgid": os.getpgid(process.pid),
                "start_time": process_start_time(process.pid),
                "instance_id": instance_id,
                "port": port,
                "app_instance": str(APP_DIR),
                "owner_pid": os.getpid(),
                "script": comfy_main,
                "cwd": str(COMFYUI_DIR),
                "gpu_pool": normalized_gpu_pool,
                "gpu_device_map": gpu_device_map,
                "device": device,
                "vram_mode": vram_mode,
                "precision": precision,
                "preview_method": preview_method,
                "disable_pinned_memory": bool(disable_pinned_memory),
                "startup_options": normalized_options,
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
            raise RuntimeError(f"Failed to launch ComfyUI: {e}")

        instance = ComfyInstance(
            instance_id=instance_id,
            port=port,
            device=device,
            vram_mode=vram_mode,
            precision=precision,
            preview_method=preview_method,
            disable_pinned_memory=bool(disable_pinned_memory),
            startup_options=normalized_options,
            gpu_pool=normalized_gpu_pool,
            gpu_device_map=gpu_device_map,
            process=process,
            log_fh=log_fh,
            pid_file=str(pid_file),
            status="starting",
        )
        self.registry.register(instance)

        try:
            await self._wait_for_ready(instance)
        except Exception as e:
            try:
                log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                logger.error("ComfyUI %s failed. Log tail:\n%s", instance_id, log_tail)
            except Exception:
                pass
            await self._force_kill(instance)
            self.registry.unregister(instance_id)
            self.registry.release_port(port)
            raise RuntimeError(f"ComfyUI {instance_id} failed: {e}") from e

        return instance

    async def _wait_for_ready(self, instance: ComfyInstance) -> None:
        url = f"http://127.0.0.1:{instance.port}/system_stats"
        deadline = time.time() + COMFYUI_STARTUP_TIMEOUT

        async with httpx.AsyncClient(timeout=5.0) as client:
            while time.time() < deadline:
                if instance.process and instance.process.poll() is not None:
                    raise RuntimeError(
                        f"ComfyUI {instance.instance_id} exited with code "
                        f"{instance.process.returncode}"
                    )
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                            devices = data.get("devices", [])
                            if devices:
                                dev = devices[0]
                                self.registry.update_health(
                                    instance.instance_id,
                                    vram_used_mb=int(dev.get("vram_used", 0) / 1024 / 1024),
                                    vram_total_mb=int(dev.get("vram_total", 0) / 1024 / 1024),
                                )
                        except Exception:
                            logger.debug("Could not parse initial ComfyUI system_stats", exc_info=True)
                        self.registry.mark_ready(instance.instance_id)
                        logger.info("ComfyUI %s ready (port=%d, pid=%d)",
                                    instance.instance_id, instance.port,
                                    instance.process.pid if instance.process else 0)
                        return
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass
                await asyncio.sleep(1.0)

        raise RuntimeError(
            f"ComfyUI {instance.instance_id} did not start within {COMFYUI_STARTUP_TIMEOUT}s"
        )

    async def stop_instance(self, instance_id: str) -> bool:
        instance = self.registry.get(instance_id)
        if not instance:
            return False

        logger.info("Stopping ComfyUI %s (port=%d)", instance_id, instance.port)
        await self._force_kill(instance)
        self.registry.unregister(instance_id)
        self.registry.release_port(instance.port)
        self._reclaim_memory()
        return True

    async def _force_kill(self, instance: ComfyInstance) -> None:
        if instance.process:
            try:
                try:
                    if instance.process.poll() is None:
                        pgid = owned_process_group(instance.process.pid)
                        if pgid is not None:
                            os.killpg(pgid, signal.SIGKILL)
                        else:
                            instance.process.kill()
                except (OSError, ProcessLookupError):
                    instance.process.kill()
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(instance.process.wait), timeout=5
                    )
                except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                    pass
            except Exception as e:
                logger.warning("Error killing ComfyUI %s: %s", instance.instance_id, e)
            instance.process = None
        if instance.log_fh:
            try:
                instance.log_fh.close()
            except Exception:
                pass
            instance.log_fh = None
        if instance.pid_file:
            try:
                Path(instance.pid_file).unlink(missing_ok=True)
            except OSError:
                pass
            instance.pid_file = None

    async def stop_all(self) -> int:
        instances = self.registry.all_instances()
        if not instances:
            killed_orphans = self.kill_orphan_comfy()
            if killed_orphans:
                self._reclaim_memory()
            return killed_orphans
        results = await asyncio.gather(
            *(self.stop_instance(i.instance_id) for i in instances),
            return_exceptions=True,
        )
        count = sum(1 for r in results if r is True)
        count += self.kill_orphan_comfy()
        if count:
            self._reclaim_memory()
        return count

    @staticmethod
    def _reclaim_memory():
        import gc
        gc.collect()
        result = release_empty_workload_cache()
        if result.get("released"):
            logger.info(
                "Released empty workload cgroup cache: %d MiB -> %d MiB",
                result["before_mb"], result["after_mb"],
            )

    def kill_orphan_comfy(self) -> int:
        # PID files exist for both orphaned and currently registered ComfyUI
        # processes.  Scheduled gc-pid-dir runs while the gateway is live, so
        # registered processes must never be treated as orphans merely because
        # their PID file exists.
        active_pids = set()
        try:
            for instance in self.registry.all_instances():
                if instance.process and instance.process.pid:
                    active_pids.add(int(instance.process.pid))
        except Exception:
            pass

        killed = 0
        preserved_live_record = False
        PID_DIR.mkdir(parents=True, exist_ok=True)
        for pid_path in PID_DIR.glob("comfy_*.json"):
            try:
                record = json.loads(pid_path.read_text(encoding="utf-8"))
                if record.get("app_instance") != str(APP_DIR):
                    continue
                pid = int(record.get("pid", 0))
                pgid = int(record.get("pgid", pid))
                if pid <= 0:
                    pid_path.unlink(missing_ok=True)
                    continue
                if pid in active_pids:
                    # Currently tracked by this gateway; it is not an orphan.
                    preserved_live_record = True
                    continue
                owner_pid = int(record.get("owner_pid", 0) or 0)
                if _is_live_gateway_pid(owner_pid):
                    # A separately-instantiated cleanup manager has no access
                    # to the owner's in-memory registry.  The durable owner PID
                    # prevents it from killing a live gateway child.
                    preserved_live_record = True
                    continue
                if not process_alive(pid):
                    pid_path.unlink(missing_ok=True)
                    continue
                if not process_matches(pid, COMFYUI_DIR / "main.py", port=int(record.get("port") or 0), start_time=record.get("start_time")):
                    logger.warning("Skipping stale Comfy record %s", pid_path.name)
                    continue
                logger.warning("Killing app-owned orphan ComfyUI pid=%d", pid)
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

        # Compatibility fallback for ComfyUI launched before pid files existed.
        expected_script = str(COMFYUI_DIR / "main.py")
        try:
            result = subprocess.run(
                ["pgrep", "-f", "python.*main.py.*--port"],
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
                logger.warning("Killing orphan ComfyUI pid=%d (ppid=%s)", pid, ppid)
                try:
                    os.kill(pid, 9)
                    killed += 1
                except (OSError, ProcessLookupError):
                    pass
        except Exception:
            pass
        return killed

    async def health_check_loop(self) -> None:
        logger.info("ComfyUI health check loop started (interval=%ds)",
                    COMFYUI_HEALTH_INTERVAL)

        async with httpx.AsyncClient(timeout=5.0) as client:
            while True:
                try:
                    await asyncio.sleep(COMFYUI_HEALTH_INTERVAL)
                    instances = self.registry.all_instances()
                    to_cleanup = []

                    for inst in instances:
                        if inst.status == "dead":
                            to_cleanup.append(inst)
                            continue
                        if inst.process and inst.process.poll() is not None:
                            logger.warning("ComfyUI %s died (exit code %d)",
                                          inst.instance_id, inst.process.returncode)
                            to_cleanup.append(inst)
                            continue

                        try:
                            resp = await client.get(
                                f"http://127.0.0.1:{inst.port}/system_stats"
                            )
                            if resp.status_code == 200:
                                if inst.status == "starting":
                                    inst.status = "ready"
                                data = resp.json()
                                devices = data.get("devices", [])
                                if devices:
                                    dev = devices[0]
                                    self.registry.update_health(
                                        inst.instance_id,
                                        vram_used_mb=int(dev.get("vram_used", 0) / 1024 / 1024),
                                        vram_total_mb=int(dev.get("vram_total", 0) / 1024 / 1024),
                                    )
                            else:
                                failures = self.registry.record_health_failure(inst.instance_id)
                                if failures == COMFYUI_MAX_HEALTH_FAILURES:
                                    logger.warning(
                                        "ComfyUI %s missed %d health checks but its process "
                                        "is alive; treating it as busy instead of killing it",
                                        inst.instance_id, failures,
                                    )
                        except Exception:
                            failures = self.registry.record_health_failure(inst.instance_id)
                            if failures == COMFYUI_MAX_HEALTH_FAILURES:
                                logger.warning(
                                    "ComfyUI %s missed %d health checks but its process "
                                    "is alive; treating it as busy instead of killing it",
                                    inst.instance_id, failures,
                                )

                    for inst in to_cleanup:
                        logger.info("Cleaning up dead ComfyUI %s", inst.instance_id)
                        await self._force_kill(inst)
                        self.registry.unregister(inst.instance_id)
                        self.registry.release_port(inst.port)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("ComfyUI health check error: %s", e)

    def start_health_checks(self) -> None:
        if self._health_task is None or self._health_task.done():
            loop = asyncio.get_running_loop()
            self._health_task = loop.create_task(self.health_check_loop())

    def stop_health_checks(self) -> None:
        if self._health_task and not self._health_task.done():
            self._health_task.cancel()

    def get_workflows(self) -> list[dict]:
        """List workflow files from the workflows directory."""
        from config import WORKFLOWS_DIR
        workflows = []
        if not WORKFLOWS_DIR.exists():
            return workflows
        for f in sorted(WORKFLOWS_DIR.glob("*.json")):
            try:
                import json
                data = json.loads(f.read_text(encoding="utf-8"))
                workflows.append({
                    "name": f.stem,
                    "filename": f.name,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "nodes": len(data) if isinstance(data, dict) else 0,
                })
            except Exception:
                workflows.append({
                    "name": f.stem,
                    "filename": f.name,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                    "nodes": 0,
                })
        return workflows

    def get_installed_models(self) -> dict[str, list[dict]]:
        """List models installed in ComfyUI model directories with sizes."""
        _EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}
        result = {}
        self.ensure_model_storage()
        for cat in COMFYUI_MODEL_CATEGORIES:
            seen = {}
            cat_dir = COMFYUI_MODELS_DIR / cat
            if cat_dir.exists():
                for f in cat_dir.rglob("*"):
                    relative_name = f.relative_to(cat_dir).as_posix() if f.is_file() else ""
                    if f.is_file() and f.suffix.lower() in _EXTS and relative_name not in seen:
                        try:
                            seen[relative_name] = round(f.stat().st_size / 1024 / 1024, 1)
                        except OSError:
                            seen[relative_name] = 0
            result[cat] = [
                {"name": n, "size_mb": s}
                for n, s in sorted(seen.items())
            ]
        return result

    def get_custom_nodes(self) -> list[dict]:
        """List installed custom nodes, including Manager-disabled entries.

        Current Manager releases move disabled packages under ``.disabled/``;
        older releases renamed them with a ``.disabled`` suffix.  Treating the
        container directory as a node made the previous inventory misleading
        and hid the packages inside it.
        """
        nodes_dir = COMFYUI_DIR / "custom_nodes"
        if not nodes_dir.exists():
            return []
        nodes = []
        candidates: list[tuple[Path, bool]] = []
        for path in sorted(nodes_dir.iterdir()):
            if not path.is_dir() or path.name == "__pycache__":
                continue
            if path.name == ".disabled":
                candidates.extend(
                    (child, False) for child in sorted(path.iterdir())
                    if child.is_dir() and child.name != "__pycache__"
                )
                continue
            if path.name.startswith("."):
                continue
            candidates.append((path, not path.name.endswith(".disabled")))

        for path, enabled in candidates:
            name = path.name[:-9] if path.name.endswith(".disabled") else path.name
            nodes.append({
                "name": name,
                "path": str(path),
                "enabled": enabled,
                "has_requirements": (path / "requirements.txt").exists(),
            })
        return nodes
