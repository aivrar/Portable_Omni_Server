"""Unified background-job runner for the Omni Studio gateway.

Replaces the four parallel ``_install_*`` globals that lived in
``omni_comfy_server.py`` (and held a temporary home in ``state.py`` during
phase 1). Every long-running operation - model installs, asset downloads,
custom-node clones, scheduled maintenance, venv repairs - flows through one
``JobStore`` instance.

Two enqueue paths:

  * ``enqueue_callable(kind, fn, meta, active_key=...)`` for in-process
    Python work. ``fn`` is awaited inside an ``asyncio.Task``; it receives a
    ``progress(current, total, message)`` callback and a
    ``cancel_event: asyncio.Event``.

  * ``enqueue_subprocess(kind, argv, env, cwd, timeout, meta, active_key=...,
    progress_parser=...)`` for shell-out work. Spawns
    ``asyncio.create_subprocess_exec(..., start_new_session=True)``, captures
    stdout/stderr line-by-line into bounded tails, optionally parses each line
    for tqdm-style progress, and cancels via ``os.killpg(SIGTERM)`` then
    ``SIGKILL`` after a 2 s grace.

Eviction runs on every insert and on a 5-minute scheduler tick (in phase 4):

  1. Drop jobs older than ``OMNI_JOB_TTL_S`` (default 86400 s) that are not
     ``running`` or ``cancelling``.
  2. After step 1, if more than ``OMNI_MAX_JOBS`` (default 500) remain, drop
     the oldest non-running first.

The ``active_key`` guard prevents duplicate concurrent submissions of the
same logical operation (e.g. installing the same model twice). A second
enqueue with the same ``active_key`` while the first is live raises
``DuplicateJobError`` (HTTP 409 mapped at the router layer).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


JOB_KINDS = (
    "model_install",
    "lora_install",
    "audio_lab_install",
    "ace_step_install",
    "audio_compose",
    "default_install",
    "comfy_asset_install",
    "comfy_model_install",
    "comfy_node_install",
    "comfy_extension",
    "maintenance",
    "comfy_update",
    "venv_repair",
    "verify_models",
)
TERMINAL_STATES = frozenset({"done", "error", "cancelled"})
DOWNLOAD_JOB_KINDS = frozenset({
    "model_install",
    "lora_install",
    "audio_lab_install",
    "ace_step_install",
    "default_install",
    "comfy_asset_install",
    "comfy_model_install",
})

ProgressFn = Callable[[int, "int | None", "str | None"], None]
ProgressParser = Callable[[str], "tuple[int, int | None, str] | None"]


class JobCancelled(Exception):
    """Raised by an in-process job's callable when it observes a cancel."""


class DuplicateJobError(Exception):
    """Raised when an active_key is already in flight."""


class SequencedTail(collections.deque):
    """Bounded text tail with a monotonic cursor across eviction."""
    def __init__(self, maxlen=200):
        super().__init__(maxlen=maxlen)
        self.total = 0

    def append(self, value):
        self.total += 1
        super().append(value)

    def extend(self, values):
        for value in values:
            self.append(value)


@dataclass
class Job:
    job_id: str
    kind: str
    status: str = "queued"          # queued | running | done | error | cancelling | cancelled
    progress: dict | None = None    # {current, total, message}
    meta: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None
    stdout_tail: collections.deque = field(
        default_factory=SequencedTail)
    stderr_tail: collections.deque = field(
        default_factory=SequencedTail)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    process: asyncio.subprocess.Process | None = None
    process_pid: int | None = None
    process_returncode: int | None = None
    task: asyncio.Task | None = None
    active_key: str | None = None
    kill_timer: asyncio.TimerHandle | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for API responses."""
        elapsed = max(0.0, (self.finished_at or time.time()) - self.started_at)
        phase = self.progress.get("message") if self.progress else None
        if not phase:
            phase = {
                "queued": "queued",
                "running": "starting",
                "cancelling": "cancelling",
                "done": "completed",
                "error": "failed",
                "cancelled": "cancelled",
            }.get(self.status, self.status)
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "progress": dict(self.progress) if self.progress else None,
            "meta": dict(self.meta),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(elapsed, 1),
            "phase": phase,
            "pid": self.process_pid,
            "returncode": self.process_returncode,
            "result": self.result,
            "error": self.error,
            "stdout_tail": "".join(self.stdout_tail)[-4000:],
            "stderr_tail": "".join(self.stderr_tail)[-4000:],
        }


class JobStore:
    """Thread-aware (single-loop) registry of in-flight and recent jobs."""

    def __init__(
        self,
        *,
        max_jobs: int | None = None,
        ttl_seconds: int | None = None,
        default_subprocess_timeout: int | None = None,
        max_concurrent_downloads: int | None = None,
        download_cpu_count: int | None = None,
    ):
        self._jobs: dict[str, Job] = {}
        self._active_keys: dict[str, str] = {}  # active_key -> job_id
        self.max_jobs = int(
            max_jobs if max_jobs is not None
            else os.environ.get("OMNI_MAX_JOBS", "500"))
        self.ttl_seconds = int(
            ttl_seconds if ttl_seconds is not None
            else os.environ.get("OMNI_JOB_TTL_S", "86400"))
        self.default_subprocess_timeout = int(
            default_subprocess_timeout if default_subprocess_timeout is not None
            else os.environ.get("OMNI_INSTALL_TIMEOUT_SECONDS", "43200"))
        self.max_concurrent_downloads = max(1, int(
            max_concurrent_downloads if max_concurrent_downloads is not None
            else os.environ.get("OMNI_MAX_CONCURRENT_DOWNLOADS", "3")))
        self.download_cpu_count = max(1, int(
            download_cpu_count if download_cpu_count is not None
            else os.environ.get("OMNI_DOWNLOAD_WORKERS", "1")))
        self._download_slots = asyncio.Semaphore(self.max_concurrent_downloads)
        # Listeners fire on every terminal-state transition (done/error/cancelled).
        # The built-in webhook listener reads ``job.meta["webhook_url"]`` and
        # POSTs the job summary; operators can register additional listeners
        # via ``add_terminal_listener`` for in-process notifications.
        self._terminal_listeners: list[Callable[[Job], Awaitable[None] | None]] = []

    def add_terminal_listener(self, fn) -> None:
        """Register a callback for any terminal-state transition.

        ``fn`` may be sync or async; both forms are handled. Exceptions
        in listeners are logged but never propagated.
        """
        self._terminal_listeners.append(fn)

    async def _fire_terminal(self, job: Job) -> None:
        for fn in list(self._terminal_listeners):
            try:
                result = fn(job)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("Terminal listener crashed for job %s", job.job_id)

    # -----------------------------------------------------------------------
    # Enqueue paths
    # -----------------------------------------------------------------------
    def enqueue_callable(
        self,
        kind: str,
        fn: Callable[[ProgressFn, asyncio.Event], Awaitable[dict | None]],
        *,
        meta: dict | None = None,
        active_key: str | None = None,
    ) -> Job:
        """Enqueue an in-process Python callable as a job."""
        if kind not in JOB_KINDS:
            raise ValueError(f"Unknown job kind: {kind}")
        if kind in DOWNLOAD_JOB_KINDS:
            from maintenance import _HUB_PRUNE_GATE
            if _HUB_PRUNE_GATE.locked():
                raise DuplicateJobError("Hub cache maintenance is active")
        self._guard_active(active_key)
        job = Job(job_id=_short_uuid(), kind=kind, meta=meta or {},
                  active_key=active_key, status="running")
        self._jobs[job.job_id] = job
        if active_key:
            self._active_keys[active_key] = job.job_id

        def _progress(current: int, total: int | None = None,
                      message: str | None = None) -> None:
            job.progress = {
                "current": int(current),
                "total": int(total) if total is not None else None,
                "message": message,
            }

        async def _runner():
            try:
                result = await fn(_progress, job.cancel_event)
                if job.status == "cancelling":
                    job.status = "cancelled"
                else:
                    job.status = "done"
                    job.result = result if isinstance(result, dict) else (
                        {"value": result} if result is not None else None)
            except JobCancelled:
                job.status = "cancelled"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as e:
                job.status = "error"
                job.error = repr(e)[:500]
                logger.exception("Job %s (%s) failed", job.job_id, kind)
            finally:
                job.finished_at = time.time()
                if job.active_key:
                    self._active_keys.pop(job.active_key, None)
                self._evict()
                if job.status in TERMINAL_STATES:
                    await self._fire_terminal(job)
                job.task = None

        job.task = asyncio.create_task(_runner())
        self._evict()
        return job

    def enqueue_subprocess(
        self,
        kind: str,
        argv: list[str],
        *,
        env: dict | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        meta: dict | None = None,
        active_key: str | None = None,
        log_path: str | os.PathLike | None = None,
        progress_parser: ProgressParser | None = None,
    ) -> Job:
        """Enqueue a subprocess job. Returns immediately with the queued Job."""
        if kind not in JOB_KINDS:
            raise ValueError(f"Unknown job kind: {kind}")
        if kind in DOWNLOAD_JOB_KINDS:
            from maintenance import _HUB_PRUNE_GATE
            if _HUB_PRUNE_GATE.locked():
                raise DuplicateJobError("Hub cache maintenance is active")
        self._guard_active(active_key)
        is_download = kind in DOWNLOAD_JOB_KINDS
        job_meta = dict(meta or {})
        if is_download:
            job_meta["resource_policy"] = {
                "cpu_fraction": "1/3",
                "cpu_threads": self.download_cpu_count,
                "max_concurrent_downloads": self.max_concurrent_downloads,
            }
        job = Job(job_id=_short_uuid(), kind=kind, meta=job_meta,
                  active_key=active_key,
                  status="queued" if is_download else "running")
        self._jobs[job.job_id] = job
        if active_key:
            self._active_keys[active_key] = job.job_id

        timeout = timeout if timeout is not None else self.default_subprocess_timeout
        env = dict(env) if env is not None else os.environ.copy()
        if is_download:
            thread_count = str(self.download_cpu_count)
            env.update({
                "OMNI_CPU_WORKERS": thread_count,
                "OMNI_DOWNLOAD_WORKERS": thread_count,
                "HF_XET_NUM_CONCURRENT_RANGE_GETS": thread_count,
                "MAX_JOBS": thread_count,
                "CMAKE_BUILD_PARALLEL_LEVEL": thread_count,
                "OMP_NUM_THREADS": thread_count,
                "OPENBLAS_NUM_THREADS": thread_count,
                "MKL_NUM_THREADS": thread_count,
                "NUMEXPR_NUM_THREADS": thread_count,
            })

        spawn_argv = list(argv)
        cpu_ids: list[int] = []
        if is_download and os.name != "nt" and shutil.which("taskset"):
            try:
                available = sorted(os.sched_getaffinity(0))
            except (AttributeError, OSError):
                available = list(range(max(1, os.cpu_count() or 1)))
            cpu_ids = available[:min(len(available), self.download_cpu_count)]
            if cpu_ids:
                spawn_argv = [
                    "taskset", "--cpu-list", ",".join(str(cpu) for cpu in cpu_ids),
                    *spawn_argv,
                ]
                job.meta["resource_policy"]["cpu_ids"] = cpu_ids

        async def _runner():
            log_fh = None
            download_slot = False
            try:
                if is_download:
                    await self._download_slots.acquire()
                    download_slot = True
                    if job.status == "cancelled":
                        return
                    job.status = "running"
                    job.progress = {
                        "current": 0,
                        "total": None,
                        "message": "starting download",
                    }
                if log_path:
                    try:
                        log_fh = open(log_path, "w", encoding="utf-8", buffering=1)
                    except OSError as e:
                        logger.warning("Job %s: cannot open log file %s: %s",
                                       job.job_id, log_path, e)

                spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(
                    *spawn_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=cwd,
                    start_new_session=True,
                ))
                try:
                    job.process = await asyncio.shield(spawn_task)
                except asyncio.CancelledError:
                    job.process = await asyncio.shield(spawn_task)
                    job.process._omni_group_owned = os.name != "nt"
                    raise
                job.process._omni_group_owned = os.name != "nt"
                if job.cancel_event.is_set() or job.status == "cancelling":
                    _kill_proc_tree(job.process)
                    raise JobCancelled()
                job.process_pid = job.process.pid
                deadline = time.time() + timeout
                while True:
                    if time.time() >= deadline:
                        raise TimeoutError(
                            f"Job {job.job_id} timed out after {timeout}s")
                    try:
                        line = await asyncio.wait_for(
                            job.process.stdout.readline(), timeout=min(30, max(0.01, deadline - time.time())))
                    except asyncio.TimeoutError:
                        if job.process.returncode is not None:
                            raise RuntimeError("Subprocess exited while a descendant retained stdout")
                        continue
                    if not line:
                        break
                    decoded = line.decode(errors="replace")
                    job.stdout_tail.append(decoded)
                    if log_fh:
                        try:
                            log_fh.write(decoded)
                            log_fh.flush()
                        except OSError:
                            pass
                    if progress_parser is not None:
                        try:
                            parsed = progress_parser(decoded.rstrip("\n"))
                        except Exception:
                            parsed = None
                        if parsed is not None:
                            current, total, message = parsed
                            job.progress = {
                                "current": int(current),
                                "total": int(total) if total else None,
                                "message": message,
                            }
                await asyncio.wait_for(job.process.wait(), timeout=max(0.01, deadline - time.time()))
                job.process_returncode = job.process.returncode

                if job.status == "cancelling":
                    job.status = "cancelled"
                elif job.process.returncode == 0:
                    job.status = "done"
                else:
                    job.status = "error"
                    job.error = f"exit code {job.process.returncode}"
            except asyncio.CancelledError:
                job.status = "cancelled"
                _kill_proc_tree(job.process)
                raise
            except JobCancelled:
                job.status = "cancelled"
                _kill_proc_tree(job.process)
            except Exception as e:
                if job.process:
                    _kill_proc_tree(job.process)
                if job.status == "cancelling":
                    job.status = "cancelled"
                else:
                    job.status = "error"
                    job.error = repr(e)[:500]
                logger.error("Job %s (%s) failed: %s", job.job_id, kind, e)
            finally:
                if job.kill_timer:
                    job.kill_timer.cancel()
                    job.kill_timer = None
                _kill_proc_tree(job.process)
                if download_slot:
                    self._download_slots.release()
                if log_fh:
                    try:
                        log_fh.close()
                    except Exception:
                        pass
                job.finished_at = time.time()
                if job.active_key:
                    self._active_keys.pop(job.active_key, None)
                self._evict()
                if job.status in TERMINAL_STATES:
                    await self._fire_terminal(job)
                if job.process is not None:
                    job.process_returncode = job.process.returncode
                job.process = None
                job.task = None

        job.task = asyncio.create_task(_runner())
        self._evict()
        return job

    # -----------------------------------------------------------------------
    # Lookup
    # -----------------------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        kinds: set[str] | None = None,
    ) -> list[Job]:
        jobs = list(self._jobs.values())
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        if kinds:
            jobs = [j for j in jobs if j.kind in kinds]
        if status:
            jobs = [j for j in jobs if j.status == status]
        jobs.sort(key=lambda j: j.started_at, reverse=True)
        return jobs

    # -----------------------------------------------------------------------
    # Cancel
    # -----------------------------------------------------------------------
    SIGKILL_GRACE_SECONDS = 2.0

    def cancel(self, job_id: str) -> str:
        """Idempotent cancel. Returns the new (or unchanged) status.

        For subprocess jobs: send SIGTERM immediately, then schedule a SIGKILL
        on the event loop after ``SIGKILL_GRACE_SECONDS`` if the process is
        still alive. The ``_runner`` will observe the EOF on stdout (or our
        kill), reach ``proc.wait()`` and finalise the job state.
        """
        job = self._jobs.get(job_id)
        if not job:
            return "missing"
        if job.status in TERMINAL_STATES:
            return job.status
        # Currently unreachable: both enqueue paths create jobs as "running".
        # Retained because "queued" is part of the documented Job state contract
        # (and the dataclass default), so a future enqueue path may reach it.
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = time.time()
            if job.active_key:
                self._active_keys.pop(job.active_key, None)
            if job.task:
                job.task.cancel()
            return "cancelled"
        # running -> cancelling
        job.status = "cancelling"
        job.cancel_event.set()
        if job.process:
            _send_term(job.process)
            # Schedule the SIGKILL escalation (if a loop is available).
            try:
                loop = asyncio.get_running_loop()
                job.kill_timer = loop.call_later(self.SIGKILL_GRACE_SECONDS,
                                                 _kill_proc_tree, job.process)
            except RuntimeError:
                # No running loop (e.g. cancel called from a sync test);
                # caller can invoke shutdown() to escalate.
                pass
        return "cancelling"

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Cancel every live job and wait briefly for them to settle."""
        live = [j for j in self._jobs.values() if j.status not in TERMINAL_STATES]
        for j in live:
            self.cancel(j.job_id)
        tasks = [j.task for j in live if j.task]
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout)
            except asyncio.TimeoutError:
                for j in live:
                    if j.process and j.process.returncode is None:
                        _kill_proc_tree(j.process)

    # -----------------------------------------------------------------------
    # Eviction (two-pass; runs on every insert)
    # -----------------------------------------------------------------------
    def _evict(self) -> None:
        now = time.time()
        # Pass 1: TTL-drop terminal jobs older than ttl_seconds
        for job_id in list(self._jobs):
            j = self._jobs[job_id]
            terminal_at = j.finished_at if j.finished_at is not None else j.started_at
            if j.status in TERMINAL_STATES and (now - terminal_at) > self.ttl_seconds:
                self._jobs.pop(job_id, None)
        # Pass 2: count-cap; drop oldest non-running first
        if len(self._jobs) > self.max_jobs:
            terminal = sorted(
                (j for j in self._jobs.values() if j.status in TERMINAL_STATES),
                key=lambda j: j.started_at)
            for j in terminal:
                if len(self._jobs) <= self.max_jobs:
                    break
                self._jobs.pop(j.job_id, None)

    # -----------------------------------------------------------------------
    # Active-key guard
    # -----------------------------------------------------------------------
    def is_active(self, active_key: str) -> bool:
        existing = self._active_keys.get(active_key)
        if not existing:
            return False
        job = self._jobs.get(existing)
        if not job or job.status in TERMINAL_STATES:
            self._active_keys.pop(active_key, None)
            return False
        return True

    def _guard_active(self, active_key: str | None) -> None:
        if active_key and self.is_active(active_key):
            raise DuplicateJobError(active_key)


def _short_uuid() -> str:
    return str(uuid.uuid4())[:8]


def _send_term(proc: asyncio.subprocess.Process | None) -> None:
    """Best-effort SIGTERM that doesn't raise on Windows / dead processes."""
    if not proc:
        return
    # Only subprocesses created with start_new_session get group privileges.
    # The stored group equals the spawn PID even after its leader exits.
    if getattr(proc, "_omni_group_owned", False) is True:
        try:
            if proc.pid > 0 and proc.pid != os.getpgrp():
                os.killpg(proc.pid, signal.SIGTERM)
                return
        except (AttributeError, OSError, ProcessLookupError):
            pass
    if proc.returncode is None:
        try:
            proc.terminate()
        except (AttributeError, OSError, ProcessLookupError):
            pass


def _kill_proc_tree(proc: asyncio.subprocess.Process | None) -> None:
    """Best-effort kill of a subprocess and its process group.

    Tries the Unix process-group kill first (so child shells / forks die
    too), then falls back to a single-process kill. AttributeError is
    caught for platforms (Windows test boxes) where ``os.killpg`` /
    ``os.getpgid`` aren't defined; ProcessLookupError handles the
    already-dead case on every platform.
    """
    if not proc:
        return
    # Only subprocesses created with start_new_session get group privileges.
    # The stored group equals the spawn PID even after its leader exits.
    if getattr(proc, "_omni_group_owned", False) is True:
        try:
            if proc.pid > 0 and proc.pid != os.getpgrp():
                os.killpg(proc.pid, signal.SIGKILL)
                return
        except (AttributeError, OSError, ProcessLookupError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except (AttributeError, OSError, ProcessLookupError):
            pass


# ---------------------------------------------------------------------------
# Progress parsers (shared by install scripts)
# ---------------------------------------------------------------------------
import re as _re

_HF_TQDM_RE = _re.compile(
    r"(?P<pct>\d+)%\|[^|]*\|\s*(?P<cur>[\d.]+)(?P<cu>[KMGTP]?i?B?)/"
    r"(?P<tot>[\d.]+)(?P<tu>[KMGTP]?i?B?)\b"
)
_UNIT_BYTES = {
    "": 1, "B": 1,
    "K": 1024, "KB": 1024,
    "KIB": 1024,
    "M": 1024 ** 2, "MB": 1024 ** 2,
    "MIB": 1024 ** 2,
    "G": 1024 ** 3, "GB": 1024 ** 3,
    "GIB": 1024 ** 3,
    "T": 1024 ** 4, "TB": 1024 ** 4,
    "TIB": 1024 ** 4,
    "P": 1024 ** 5, "PB": 1024 ** 5,
    "PIB": 1024 ** 5,
}


def hf_tqdm_parser(line: str) -> tuple[int, int | None, str] | None:
    """Best-effort parse of HuggingFace progress and installer phase lines."""
    stripped = line.strip()
    if stripped.startswith("[hf] "):
        return (0, None, stripped[5:])
    phase = _re.match(r"Installing blueprint asset\s+(\d+)/(\d+):\s*(.+)", stripped)
    if phase:
        return (int(phase.group(1)) - 1, int(phase.group(2)),
                f"Installing {phase.group(1)}/{phase.group(2)}: {phase.group(3)}")
    if stripped.startswith("Installed ") or stripped.startswith("Download complete:"):
        return (1, 1, stripped)
    m = _HF_TQDM_RE.search(line)
    if not m:
        return None
    try:
        cur = float(m.group("cur")) * _UNIT_BYTES.get(m.group("cu").upper(), 1)
        tot = float(m.group("tot")) * _UNIT_BYTES.get(m.group("tu").upper(), 1)
    except (ValueError, KeyError):
        return None
    prefix = line[:m.start()].strip(" :")
    label = f"{m.group('pct')}%"
    if prefix:
        label = f"{prefix}: {label}"
    return (int(cur), int(tot), label)
