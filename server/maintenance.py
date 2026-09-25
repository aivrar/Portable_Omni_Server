"""Maintenance task implementations.

Each ``run_<task>`` function returns a dict summary and accepts the standard
``(progress, cancel_event)`` pair so it can be enqueued through the
``JobStore`` runner. The scheduler in ``scheduler.py`` calls these on a
cadence; the ``/api/maintenance/run/{task}`` route also calls them on demand.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from config import (
    CACHE_DIR,
    COMFYUI_DIR,
    MODELS_DIR,
    OUTPUT_DIR,
    PID_DIR,
    RUNTIME_DIR,
    WORKER_LOG_DIR,
)
from helpers import prune_files
from jobs import JobCancelled
from operation_gate import OperationGate

_HUB_PRUNE_GATE = OperationGate("Hub cache maintenance")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — overridable via /api/maintenance/policy or env
# ---------------------------------------------------------------------------
DEFAULTS = {
    "prune-outputs": {
        "cadence_s": 6 * 3600,
        "max_gb": float(os.environ.get("OMNI_OUTPUT_MAX_GB", "50")),
    },
    "prune-omni-outputs": {
        "cadence_s": 6 * 3600,
        "max_gb": float(os.environ.get("OMNI_OMNI_OUTPUT_MAX_GB", "5")),
    },
    "prune-logs": {
        "cadence_s": 3600,
        "max_age_days": 30,
        "keep_latest": 200,
    },
    "prune-pip": {
        "cadence_s": 24 * 3600,
        "max_gb": float(os.environ.get("OMNI_PIP_CACHE_MAX_GB", "5")),
    },
    "prune-hub": {
        "cadence_s": 24 * 3600,
        "stale_days": 7,
    },
    "prune-thumbs": {
        "cadence_s": 6 * 3600,
        "max_gb": 1.0,
    },
    "prune-tmp": {
        "cadence_s": 24 * 3600,
        "max_age_days": 2,
    },
    "gc-pid-dir": {
        "cadence_s": 3600,
    },
    "gc-jobs": {
        "cadence_s": 300,
    },
    "verify-models": {
        "cadence_s": 24 * 3600,
    },
}

POLICY_FILE = RUNTIME_DIR / "maintenance.json"


def load_policy() -> dict:
    """Read the persisted policy file, falling back to DEFAULTS."""
    out = {"policy": {k: dict(v) for k, v in DEFAULTS.items()}, "last_run": {}}
    try:
        if POLICY_FILE.exists():
            with open(POLICY_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f) or {}
            for task, overrides in (disk.get("policy") or {}).items():
                if task in out["policy"] and isinstance(overrides, dict):
                    out["policy"][task].update(overrides)
            for task, ts in (disk.get("last_run") or {}).items():
                try:
                    out["last_run"][task] = float(ts)
                except (TypeError, ValueError):
                    pass
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("maintenance policy read failed: %s", e)
    return out


def save_policy(data: dict) -> None:
    POLICY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POLICY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(POLICY_FILE)


def _dir_size_bytes(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


# ---------------------------------------------------------------------------
# Task implementations
# ---------------------------------------------------------------------------
def _prune_root_sync(root: Path, max_gb: float, progress, cancel_event, kind: str = "output"):
    """Shared body for ``prune-outputs`` and ``prune-omni-outputs``.

    Files under a ``keep/`` segment, and files marked ``pinned`` in
    ``output_meta``, are exempt. Returns the same shape both callers
    expose so the maintenance UI doesn't have to special-case.

    Synchronous: runs the blocking filesystem walk/unlink. Invoke via
    ``asyncio.to_thread`` from an async entrypoint so the event loop
    stays responsive. ``cancel_event`` is still polled internally.
    """
    if not root.exists():
        return {"deleted": 0, "freed_bytes": 0, "max_gb": max_gb}
    from output_meta import has_keep_segment, output_meta
    pinned_paths = {r.path for r in output_meta.list() if r.pinned and r.kind == kind}

    candidates: list[tuple[Path, os.stat_result, str]] = []
    keep_skipped = 0
    for p in root.rglob("*"):
        if cancel_event.is_set():
            raise JobCancelled()
        if p.is_file():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if has_keep_segment(rel):
                keep_skipped += 1
                continue
            try:
                candidates.append((p, p.stat(), rel))
            except OSError:
                continue
    candidates.sort(key=lambda i: i[1].st_mtime)
    total = sum(st.st_size for _, st, _ in candidates)
    pinned_bytes = sum(st.st_size for _, st, rel in candidates if rel in pinned_paths)
    cap = int(max_gb * 1024 ** 3)
    deleted: list[dict] = []
    skipped_pinned = 0
    progress(0, len(candidates),
             f"total {total} bytes vs cap {cap} (pinned {pinned_bytes})")
    if total <= cap:
        return {"deleted": 0, "freed_bytes": 0, "total_bytes": total,
                "pinned_bytes": pinned_bytes, "max_gb": max_gb}
    for idx, (p, st, rel) in enumerate(candidates):
        if cancel_event.is_set():
            raise JobCancelled()
        if total <= cap:
            break
        if rel in pinned_paths:
            skipped_pinned += 1
            continue
        try:
            output_meta.delete_file(rel, p, kind=kind)
            deleted.append({"path": rel, "size": st.st_size})
            total -= st.st_size
        except OSError:
            pass
        progress(idx + 1, len(candidates), f"freeing... now {total}")
    return {"deleted": len(deleted),
            "freed_bytes": sum(d["size"] for d in deleted),
            "remaining_bytes": total,
            "pinned_skipped": skipped_pinned,
            "pinned_bytes": pinned_bytes,
            "keep_skipped": keep_skipped,
            "max_gb": max_gb}


async def run_prune_outputs(progress, cancel_event, *, max_gb: float):
    return await asyncio.to_thread(
        _prune_root_sync, OUTPUT_DIR / "comfyui", max_gb, progress, cancel_event)


async def run_prune_omni_outputs(progress, cancel_event, *, max_gb: float):
    return await asyncio.to_thread(
        _prune_root_sync, OUTPUT_DIR / "omni", max_gb, progress, cancel_event, "omni")


def _prune_tmp_sync(progress, cancel_event, max_age_days: int) -> dict:
    """Remove abandoned immediate children of the app-owned temp directory.

    The installer timeout is 12 hours, so the two-day default leaves a wide
    safety margin. Only immediate children are selected, and symlinks are
    unlinked rather than followed.
    """
    root = CACHE_DIR / "tmp"
    if not root.exists():
        return {"deleted": 0, "failed": 0, "max_age_days": max_age_days}

    cutoff = time.time() - int(max_age_days * 86400)
    deleted = 0
    failed = 0
    entries = list(root.iterdir())
    progress(0, len(entries), "checking temporary entries")
    for index, entry in enumerate(entries, start=1):
        if cancel_event.is_set():
            raise JobCancelled()
        try:
            if entry.lstat().st_mtime >= cutoff:
                continue
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink()
            else:
                shutil.rmtree(entry)
            deleted += 1
        except OSError:
            failed += 1
            logger.warning("temporary entry prune failed: %s", entry)
        progress(index, len(entries), f"removed {deleted} stale entries")
    return {"deleted": deleted, "failed": failed, "max_age_days": max_age_days}


async def run_prune_tmp(progress, cancel_event, *, max_age_days: int):
    return await asyncio.to_thread(
        _prune_tmp_sync, progress, cancel_event, max_age_days)


async def run_prune_logs(progress, cancel_event, *, max_age_days: int, keep_latest: int):
    age_s = int(max_age_days * 86400)
    progress(0, None, "pruning install logs")
    prune_files(WORKER_LOG_DIR, "install_*.log",
                max_age_seconds=14 * 24 * 3600, keep_latest=100)
    if cancel_event.is_set():
        raise JobCancelled()
    progress(0, None, "pruning worker logs")
    prune_files(WORKER_LOG_DIR, "*.log", max_age_seconds=age_s, keep_latest=keep_latest)
    if cancel_event.is_set():
        raise JobCancelled()
    xet_logs = MODELS_DIR / "xet" / "logs"
    progress(0, None, "pruning xet logs")
    prune_files(xet_logs, "*.log", max_age_seconds=7 * 86400, keep_latest=20)
    return {"target": str(WORKER_LOG_DIR), "max_age_days": max_age_days,
            "keep_latest": keep_latest}


async def run_prune_pip(progress, cancel_event, *, max_gb: float):
    pip_cache = CACHE_DIR / "pip"
    if not pip_cache.exists():
        return {"size_bytes": 0, "purged": False}
    size = await asyncio.to_thread(_dir_size_bytes, pip_cache)
    cap = int(max_gb * 1024 ** 3)
    if size <= cap:
        return {"size_bytes": size, "max_gb": max_gb, "purged": False}
    if cancel_event.is_set():
        raise JobCancelled()
    progress(0, None, "purging pip cache")
    # Use the running interpreter's pip (the gateway venv) so this works
    # regardless of whether bare 'pip' is on PATH.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "cache", "purge",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PIP_CACHE_DIR": str(pip_cache)},
    )
    communication = asyncio.create_task(proc.communicate())
    cancellation = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({communication, cancellation}, return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done:
            raise JobCancelled()
        out, _ = await communication
        if proc.returncode:
            raise RuntimeError("pip cache purge failed: " + out.decode(errors="replace")[-1000:])
    finally:
        cancellation.cancel()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(communication), 5)
            except asyncio.TimeoutError:
                proc.kill()
        await asyncio.gather(communication, cancellation, return_exceptions=True)
    after = await asyncio.to_thread(_dir_size_bytes, pip_cache)
    return {"size_bytes_before": size, "size_bytes_after": after,
            "max_gb": max_gb, "purged": True,
            "stdout": out.decode(errors="replace")[:1000]}


def _installed_hub_references() -> set[Path]:
    """Collect installed symlinks without following directory links or the cache."""
    references: set[Path] = set()
    hub = (MODELS_DIR / "hub").resolve()
    def onerror(error):
        raise error  # Fail closed if installed references cannot be inspected.
    for root in (MODELS_DIR, COMFYUI_DIR / "models"):
        if not root.exists():
            continue
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            for name in list(dirs):
                entry = Path(parent) / name
                if entry.resolve() == hub:
                    dirs.remove(name)
                elif entry.is_symlink():
                    references.add(entry.resolve(strict=True))
                    dirs.remove(name)
            for name in files:
                entry = Path(parent) / name
                if entry.is_symlink():
                    references.add(entry.resolve(strict=True))
    return references


def _hub_repo_referenced(repo, references: set[Path]) -> bool:
    root = Path(repo.repo_path).resolve()
    if any(path == root or root in path.parents or path in root.parents for path in references):
        return True
    return any(Path(file.blob_path).stat().st_nlink > 1 for rev in repo.revisions for file in rev.files)


@_HUB_PRUNE_GATE
async def run_prune_hub(progress, cancel_event, *, stale_days: int):
    """Drop HF cache revisions not referenced by installed models for N days."""
    progress(0, None, "scanning hub cache")
    try:
        from huggingface_hub import scan_cache_dir
    except ImportError:
        return {"error": "huggingface_hub not available"}
    try:
        info = await asyncio.to_thread(scan_cache_dir,
                                       str(MODELS_DIR / "hub"))
    except Exception as e:
        return {"error": f"scan_cache_dir failed: {e}"}

    from state import jobs
    from jobs import DOWNLOAD_JOB_KINDS
    if any(job.status in {"queued", "running", "cancelling"} for job in jobs.list(kinds=set(DOWNLOAD_JOB_KINDS))):
        return {"deleted_revisions": 0, "reason": "asset install is active"}
    try:
        references = await asyncio.to_thread(_installed_hub_references)
        protected = await asyncio.to_thread(lambda: {str(repo.repo_path) for repo in info.repos if _hub_repo_referenced(repo, references)})
    except (OSError, ValueError, RuntimeError) as exc:
        return {"deleted_revisions": 0, "error": f"Could not verify installed references: {type(exc).__name__}"}
    cutoff = time.time() - stale_days * 86400
    revisions_to_drop: list[str] = []
    freed = 0
    for repo in info.repos:
        if str(repo.repo_path) in protected:
            continue
        for rev in repo.revisions:
            if cancel_event.is_set():
                raise JobCancelled()
            if rev.last_modified < cutoff:
                revisions_to_drop.append(rev.commit_hash)
                freed += rev.size_on_disk

    if revisions_to_drop:
        try:
            delete_strategy = info.delete_revisions(*revisions_to_drop)
            await asyncio.to_thread(delete_strategy.execute)
        except Exception as e:
            return {"error": f"delete failed: {e}",
                    "candidates": len(revisions_to_drop)}
    return {"deleted_revisions": len(revisions_to_drop),
            "freed_bytes": freed, "stale_days": stale_days, "protected_repositories": len(protected)}


def _run_prune_thumbs_sync(progress, cancel_event, *, max_gb: float):
    thumbs = CACHE_DIR / "thumbs"
    if not thumbs.exists():
        return {"deleted": 0, "freed_bytes": 0}
    files = sorted(
        (p for p in thumbs.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    total = sum(p.stat().st_size for p in files)
    cap = int(max_gb * 1024 ** 3)
    if total <= cap:
        return {"size_bytes": total, "deleted": 0}
    deleted = 0
    freed = 0
    for p in files:
        if cancel_event.is_set():
            raise JobCancelled()
        if total <= cap:
            break
        try:
            sz = p.stat().st_size
            p.unlink()
            total -= sz
            freed += sz
            deleted += 1
        except OSError:
            pass
    return {"deleted": deleted, "freed_bytes": freed,
            "remaining_bytes": total, "max_gb": max_gb}


async def run_prune_thumbs(progress, cancel_event, *, max_gb: float):
    return await asyncio.to_thread(
        _run_prune_thumbs_sync, progress, cancel_event, max_gb=max_gb)


async def run_gc_pid_dir(progress, cancel_event):
    """Reap orphan worker + ComfyUI processes whose pid files we own."""
    from state import comfy_manager, worker_manager
    progress(0, None, "scanning worker pids")
    await asyncio.to_thread(worker_manager.kill_orphan_workers)
    if cancel_event.is_set():
        raise JobCancelled()
    progress(0, None, "scanning comfy pids")
    await asyncio.to_thread(comfy_manager.kill_orphan_comfy)
    return {"pid_dir": str(PID_DIR)}


async def run_gc_jobs(progress, cancel_event):
    """Trim the in-memory JobStore by TTL + count."""
    from state import jobs
    before = len(jobs._jobs)  # type: ignore[attr-defined]
    jobs._evict()  # type: ignore[attr-defined]
    after = len(jobs._jobs)  # type: ignore[attr-defined]
    return {"before": before, "after": after, "evicted": before - after}


def _run_verify_models_sync(progress, cancel_event):
    """Check completion markers, nonempty weights and every shard index."""
    from snapshot_install import verify_installed_models

    def check_cancelled():
        if cancel_event.is_set():
            raise JobCancelled()

    summary = verify_installed_models(
        MODELS_DIR / "omni", progress=progress, cancelled=check_cancelled,
    )
    health_path = RUNTIME_DIR / "model_health.json"
    try:
        health_path.parent.mkdir(parents=True, exist_ok=True)
        health_path.write_text(json.dumps(summary, indent=2))
    except OSError:
        pass
    return summary


async def run_verify_models(progress, cancel_event):
    return await asyncio.to_thread(_run_verify_models_sync, progress, cancel_event)


# Mapping from task name to (callable, kwargs-from-policy-keys)
TASK_REGISTRY = {
    "prune-outputs": (run_prune_outputs, ("max_gb",)),
    "prune-omni-outputs": (run_prune_omni_outputs, ("max_gb",)),
    "prune-logs": (run_prune_logs, ("max_age_days", "keep_latest")),
    "prune-pip": (run_prune_pip, ("max_gb",)),
    "prune-hub": (run_prune_hub, ("stale_days",)),
    "prune-thumbs": (run_prune_thumbs, ("max_gb",)),
    "prune-tmp": (run_prune_tmp, ("max_age_days",)),
    "gc-pid-dir": (run_gc_pid_dir, ()),
    "gc-jobs": (run_gc_jobs, ()),
    "verify-models": (run_verify_models, ()),
}


def build_task_callable(task: str, policy_for_task: dict):
    """Return an async fn(progress, cancel_event) for the named task."""
    if task not in TASK_REGISTRY:
        raise KeyError(task)
    fn, keys = TASK_REGISTRY[task]
    kwargs = {k: policy_for_task[k] for k in keys if k in policy_for_task}

    async def _runner(progress, cancel_event):
        return await fn(progress, cancel_event, **kwargs)

    return _runner
