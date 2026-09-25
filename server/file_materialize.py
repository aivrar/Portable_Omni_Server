"""Materialize immutable cached model files without retaining WSL page cache."""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from pathlib import Path

_COPY_BUFFER_BYTES = 8 * 1024 * 1024
_HARDLINK_FALLBACK_ERRNOS = {
    errno.EXDEV,
    errno.EPERM,
    errno.EACCES,
    getattr(errno, "ENOTSUP", errno.EPERM),
    getattr(errno, "EOPNOTSUPP", errno.EPERM),
}


def _advise_dontneed(fd: int, offset: int = 0, length: int = 0) -> None:
    advise = getattr(os, "posix_fadvise", None)
    mode = getattr(os, "POSIX_FADV_DONTNEED", None)
    if not callable(advise) or mode is None:
        return
    try:
        advise(fd, offset, length, mode)
    except OSError:
        pass


def evict_file_pages(path: str | os.PathLike, *, sync: bool = True) -> bool:
    """Ask Linux to release cached pages for one durable regular file."""
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError:
        return False
    try:
        if sync:
            try:
                os.fsync(fd)
            except OSError:
                pass
        _advise_dontneed(fd)
        return True
    finally:
        os.close(fd)


def _copy_without_cache(source: Path, part: Path) -> None:
    source_fd = os.open(source, os.O_RDONLY)
    try:
        destination_fd = os.open(
            part,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            source.stat().st_mode & 0o777,
        )
        try:
            offset = 0
            while True:
                chunk = os.read(source_fd, _COPY_BUFFER_BYTES)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
                _advise_dontneed(source_fd, offset, len(chunk))
                offset += len(chunk)
            os.fsync(destination_fd)
            _advise_dontneed(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        _advise_dontneed(source_fd)
        os.close(source_fd)
    shutil.copystat(source, part, follow_symlinks=True)


def materialize_cached_file(
    source: str | os.PathLike,
    destination: str | os.PathLike,
) -> str:
    """Atomically expose a cache blob as an independent model-directory name.

    A same-filesystem hard link is preferred: it is a real, independently
    deletable directory entry, survives cache pruning, avoids duplicate disk
    space, and does not read the entire model into page cache. Cross-filesystem
    or link-restricted destinations use a bounded streaming copy and explicitly
    evict both source and destination pages after syncing.
    """
    src = Path(source).resolve(strict=True)
    dest = Path(destination)
    if not src.is_file():
        raise ValueError(f"Cached model source is not a regular file: {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")

    mode = "hardlink"
    try:
        try:
            os.link(src, part)
        except OSError as exc:
            if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
                raise
            mode = "copy"
            _copy_without_cache(src, part)
        evict_file_pages(part, sync=True)
        os.replace(part, dest)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return mode
