"""Persistence and discovery helpers for omni-worker generated content.

Binary inference routes keep a copy beneath
``OUTPUT_DIR/omni/{kind}/{model}/{ISO-timestamp}.{ext}`` so the Media tab can
list, replay, delete, and export it like any other output.

Best-effort by design — a write failure must NOT break inference. The
caller already has the bytes in hand to ship to the client; persistence
is a side-effect we silently log and move on from.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from config import OUTPUT_DIR

logger = logging.getLogger(__name__)

OMNI_ROOT = OUTPUT_DIR / "omni"

_SAFE_SEG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_segment(seg: str) -> str:
    """Reduce arbitrary strings to filesystem-safe path segments."""
    cleaned = _SAFE_SEG_RE.sub("_", str(seg or "")).strip("._-")
    return cleaned[:64] or "unknown"


def _timestamp_iso() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())


def persist_omni_bytes(
    data: bytes,
    *,
    model: str,
    kind: str,
    ext: str,
    subdir: str | None = None,
) -> Path | None:
    """Atomic write under ``OUTPUT_DIR/omni/{kind}/{model}/{timestamp}.{ext}``.

    ``kind`` is "tts" / "stt" / "image" / "video" / etc. Returns the final
    path on success, ``None`` on any failure (logged at WARNING level).
    """
    if not data:
        return None
    safe_kind = _sanitize_segment(kind)
    safe_model = _sanitize_segment(model)
    safe_ext = _sanitize_segment(ext) or "bin"
    parts = [safe_kind, safe_model]
    if subdir:
        parts.append(_sanitize_segment(subdir))
    target_dir = OMNI_ROOT.joinpath(*parts)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("omni_outputs: mkdir failed for %s: %s", target_dir, e)
        return None

    target = target_dir / f"{_timestamp_iso()}-{uuid.uuid4().hex[:12]}.{safe_ext}"
    tmp_str = None
    try:
        tmp_fd, tmp_str = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target_dir),
        )
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_str, target)
    except OSError as e:
        try:
            if tmp_str is not None:
                os.unlink(tmp_str)
        except OSError:
            pass
        logger.warning("omni_outputs: write failed for %s: %s", target, e)
        return None
    return target


def omni_output_headers(path: Path | None) -> dict[str, str]:
    """Return stable discovery headers for a file persisted under OMNI_ROOT."""
    if path is None:
        return {}
    try:
        relpath = Path(path).resolve().relative_to(OMNI_ROOT.resolve()).as_posix()
    except (OSError, ValueError):
        return {}
    return {
        "X-Omni-Output-Path": relpath,
        "X-Omni-Output-Ref": f"omni://outputs/{relpath}",
        "X-Omni-Output-URL": f"/api/outputs/{relpath}?kind=omni",
    }
