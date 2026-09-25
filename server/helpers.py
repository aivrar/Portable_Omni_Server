"""Shared helpers used by gateway routers and lifecycle code.

These were inline static methods on the FastAPI app module before the
phase-1 router split. Keep call sites identical so the move is invisible.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi import HTTPException


def declared_size_gb(size_text: str | None, default: int = 8) -> int:
    """Parse '~14GB' -> 14. Used for HF download disk-space sanity checks."""
    if not size_text:
        return default
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*GB", size_text, re.IGNORECASE)
    if not match:
        return default
    return max(1, int(float(match.group(1)) + 0.999))


def bounded_lines(lines: int, default: int = 100, maximum: int = 1000) -> int:
    """Clamp a user-supplied ``lines`` query value into a sane range."""
    try:
        parsed = int(lines)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def safe_child_path(base: Path, name: str, suffix: str | None = None) -> Path:
    """Resolve ``base / name`` and require it stays inside ``base``.

    Single-segment names only — multi-segment subtree access uses
    ``safe_subtree_path``.
    """
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or ".." in name or ":" in name or "\x00" in name):
        raise HTTPException(status_code=400, detail="Invalid path name")
    if suffix and Path(name).suffix.lower() != suffix:
        raise HTTPException(status_code=400, detail=f"Expected {suffix} file")
    base_resolved = base.resolve()
    candidate = base_resolved / name
    resolved = candidate.resolve()
    if resolved == base_resolved or not is_relative_to(resolved, base_resolved):
        raise HTTPException(status_code=400, detail="Invalid path name")
    # Preserve the requested directory entry. Deletion must never follow a
    # final symlink and remove its target instead of the named entry.
    return candidate


def safe_subtree_path(base: Path, relpath: str) -> Path:
    """Resolve ``base / relpath`` for multi-segment paths and keep it inside ``base``.

    Rejects: NUL, ``..`` segments, empty segments, absolute paths, drive
    letters, leading separators, backslashes. Symlinks whose resolved target
    escapes ``base`` are rejected post-resolve.
    """
    if not relpath or "\x00" in relpath:
        raise HTTPException(status_code=400, detail="Invalid path")
    # Reject Windows-style absolute paths and drive letters even on POSIX.
    if "\\" in relpath:
        raise HTTPException(status_code=400, detail="Invalid path")
    if relpath.startswith("/") or (len(relpath) >= 2 and relpath[1] == ":"):
        raise HTTPException(status_code=400, detail="Invalid path")
    parts = relpath.split("/")
    for seg in parts:
        if not seg or seg in (".", "..") or ":" in seg:
            raise HTTPException(status_code=400, detail="Invalid path")
    base_resolved = base.resolve()
    candidate = base_resolved / relpath
    resolved = candidate.resolve(strict=False)
    if resolved == base_resolved or not is_relative_to(resolved, base_resolved):
        raise HTTPException(status_code=400, detail="Invalid path")
    return candidate


def prune_files(root: Path, pattern: str, max_age_seconds: int, keep_latest: int) -> None:
    """Drop files older than max_age_seconds, keeping at most keep_latest by mtime."""
    if not root.exists():
        return
    now = time.time()
    try:
        files = sorted(
            [p for p in root.glob(pattern) if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for idx, path in enumerate(files):
        try:
            age = now - path.stat().st_mtime
            if idx >= keep_latest or age > max_age_seconds:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def prune_runtime_files() -> None:
    """Default runtime log prune used during gateway startup."""
    from config import WORKER_LOG_DIR, MODELS_DIR
    prune_files(WORKER_LOG_DIR, "*.log",
                max_age_seconds=30 * 24 * 3600, keep_latest=200)
    prune_files(MODELS_DIR / "xet" / "logs", "*.log",
                max_age_seconds=7 * 24 * 3600, keep_latest=20)
