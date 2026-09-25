"""Best-effort process placement into Omni's bounded workload cgroup."""

from __future__ import annotations

import os
from pathlib import Path
import threading


WORKLOAD_MEMORY_CGROUP = Path(
    os.environ.get(
        "OMNI_WORKLOAD_MEMORY_CGROUP",
        "/sys/fs/cgroup/memory/omni_studio/workloads",
    )
)

_WORKLOAD_CGROUP_LOCK = threading.Lock()

# WSL/CUDA workers mmap many safetensors and dxgresource handles. The default
# 1024 soft nofile limit is what turned long Music 3 runs into
# "Too many open files" and then "CUDA driver error: unknown error".
_WORKER_NOFILE_MINIMUM = 65536


def raise_nofile_limit(minimum: int = _WORKER_NOFILE_MINIMUM) -> dict:
    """Raise this process RLIMIT_NOFILE up to *minimum* when the kernel allows.

    Returns the applied soft/hard pair. Missing ``resource`` (Windows unit
    tests) or a hard cap below *minimum* is a supported degraded result.
    """
    try:
        import resource
    except ImportError:
        return {"applied": False, "reason": "resource-module-unavailable", "soft": 0, "hard": 0}
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = max(int(minimum), int(soft))
        if hard != resource.RLIM_INFINITY and hard >= 0:
            want = min(want, int(hard))
        if want <= soft:
            return {"applied": False, "reason": "already-at-or-above-target", "soft": soft, "hard": hard}
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        new_soft, new_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return {"applied": True, "reason": None, "soft": new_soft, "hard": new_hard}
    except (ValueError, OSError) as exc:
        return {"applied": False, "reason": str(exc), "soft": 0, "hard": 0}


def _active_workload_members() -> list[str]:
    """Return the union of task and process membership for the workload group."""
    members: set[str] = set()
    for name in ("tasks", "cgroup.procs"):
        path = WORKLOAD_MEMORY_CGROUP / name
        if not path.exists():
            continue
        members.update(line.strip() for line in path.read_text().splitlines() if line.strip())
    return sorted(members)


def place_process_in_workload_cgroup(pid: int) -> dict:
    """Move one freshly spawned model process below the workload memory cap.

    A missing cgroup means the host does not expose the optional boundary and
    remains a supported degraded mode. If the bridge created the cgroup but a
    process cannot be placed into it, fail visibly so an unbounded heavyweight
    process is not allowed to starve the API control plane.
    """
    process_id = int(pid)
    if process_id <= 0:
        raise ValueError("pid must be a positive integer")
    tasks_file = WORKLOAD_MEMORY_CGROUP / "tasks"
    if not WORKLOAD_MEMORY_CGROUP.is_dir():
        return {
            "applied": False,
            "pid": process_id,
            "cgroup": str(WORKLOAD_MEMORY_CGROUP),
            "reason": "workload-cgroup-unavailable",
        }
    try:
        with _WORKLOAD_CGROUP_LOCK:
            tasks_file.write_text(str(process_id), encoding="ascii")
    except OSError as exc:
        raise RuntimeError(
            f"Could not place process {process_id} in workload cgroup: {exc}"
        ) from exc
    return {
        "applied": True,
        "pid": process_id,
        "cgroup": str(WORKLOAD_MEMORY_CGROUP),
        "reason": None,
    }


def release_empty_workload_cache() -> dict:
    """Reclaim only Omni workload page cache when no workload task remains."""
    force_empty = WORKLOAD_MEMORY_CGROUP / "memory.force_empty"
    usage_file = WORKLOAD_MEMORY_CGROUP / "memory.usage_in_bytes"
    if not WORKLOAD_MEMORY_CGROUP.is_dir() or not force_empty.exists():
        return {
            "released": False,
            "reason": "workload-cgroup-unavailable",
            "before_mb": 0,
            "after_mb": 0,
        }
    try:
        with _WORKLOAD_CGROUP_LOCK:
            before = int(usage_file.read_text().strip() or 0)
            if _active_workload_members():
                return {
                    "released": False,
                    "reason": "workload-tasks-active",
                    "before_mb": int(before / 1024 / 1024),
                    "after_mb": int(before / 1024 / 1024),
                }
            force_empty.write_text("0", encoding="ascii")
            after = int(usage_file.read_text().strip() or 0)
    except (OSError, ValueError) as exc:
        return {
            "released": False,
            "reason": f"workload-cache-release-failed: {exc}",
            "before_mb": 0,
            "after_mb": 0,
        }
    return {
        "released": True,
        "reason": None,
        "before_mb": int(before / 1024 / 1024),
        "after_mb": int(after / 1024 / 1024),
    }
