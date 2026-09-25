"""Output-file metadata: tags, pinning, collections.

Lives in ``RUNTIME_DIR/output_metadata.json`` so it survives gateway
restarts. Keys are output-relative paths (the same string the listing
endpoint uses), values are ``{tags: [], pinned: bool, collections: []}``.

Pinned files are exempt from ``run_prune_outputs`` (Sprint D will also
add a ``keep/`` subdir exemption). Collections are arbitrary string
labels grouped by name; the same path can belong to multiple
collections.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import tempfile
from copy import deepcopy
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from config import RUNTIME_DIR

logger = logging.getLogger(__name__)

OUTPUT_META_PATH = Path(RUNTIME_DIR) / "output_metadata.json"


def has_keep_segment(rel: str) -> bool:
    """True if ``rel`` lives under a ``keep/`` directory (prune exemption).

    ``rel`` is an output-relative POSIX path. Only non-final segments count, so
    a file literally named ``keep`` is not exempt but anything inside a ``keep``
    directory is. Shared by ``run_prune_outputs`` and ``POST /api/outputs/prune``
    so the two prune paths can't drift apart (audit H2).
    """
    return any(seg == "keep" for seg in rel.split("/")[:-1])


@dataclass
class OutputMeta:
    path: str
    kind: str = "output"
    tags: list[str] = field(default_factory=list)
    pinned: bool = False
    collections: list[str] = field(default_factory=list)
    notes: str = ""
    updated_at: float = field(default_factory=time.time)


class OutputMetaStore:
    def __init__(self, path: Path = OUTPUT_META_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], OutputMeta] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("output_metadata.json unreadable: %s", e)
            return
        if not isinstance(data, dict) or not isinstance(data.get("records", []), list):
            logger.warning("output_metadata.json has an invalid structure")
            return
        for entry in data.get("records", []):
            try:
                if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                        or entry.get("kind", "output") not in {"output", "input", "temp", "omni"}
                        or any(not isinstance(entry.get(k, []), list)
                               or not all(isinstance(v, str) for v in entry.get(k, []))
                               for k in ("tags", "collections"))):
                    continue
                rec = OutputMeta(
                    path=entry["path"],
                    kind=entry.get("kind", "output"),
                    tags=list(entry.get("tags") or []),
                    pinned=bool(entry.get("pinned")),
                    collections=list(entry.get("collections") or []),
                    notes=str(entry.get("notes") or ""),
                    updated_at=float(entry.get("updated_at") or time.time()),
                )
                self._records[(rec.kind, rec.path)] = rec
            except (KeyError, TypeError, ValueError):
                continue

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".output-meta-", dir=self.path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"records": [asdict(r) for r in self._records.values()]}, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("save output_metadata: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def get(self, path: str, *, kind: str = "output") -> OutputMeta | None:
        with self._lock:
            return deepcopy(self._records.get((kind, path)))

    def list(self) -> list[OutputMeta]:
        with self._lock:
            return deepcopy(list(self._records.values()))

    def upsert(self, path: str, *,
               kind: str = "output",
               tags: list[str] | None = None,
               pinned: bool | None = None,
               collections: list[str] | None = None,
               notes: str | None = None) -> OutputMeta:
        with self._lock:
            key = (kind, path)
            previous = self._records.get(key)
            rec = deepcopy(previous) if previous is not None else OutputMeta(path=path, kind=kind)
            if tags is not None:
                # Dedup + cap length to keep JSON sane.
                rec.tags = sorted({t.strip() for t in tags if t and t.strip()})[:64]
            if pinned is not None:
                rec.pinned = bool(pinned)
            if collections is not None:
                rec.collections = sorted({c.strip() for c in collections if c and c.strip()})[:32]
            if notes is not None:
                rec.notes = str(notes)[:4096]
            rec.updated_at = time.time()
            # Drop empty records to keep the store small.
            if (not rec.tags and not rec.pinned and not rec.collections
                    and not rec.notes):
                self._records.pop(key, None)
            else:
                self._records[key] = rec
            try:
                self._save_locked()
            except OSError:
                if previous is None:
                    self._records.pop(key, None)
                else:
                    self._records[key] = previous
                raise
            return rec

    def delete(self, path: str, *, kind: str = "output") -> bool:
        with self._lock:
            key = (kind, path)
            previous = self._records.pop(key, None)
            if previous is not None:
                try:
                    self._save_locked()
                except OSError:
                    self._records[key] = previous
                    raise
            return previous is not None

    def delete_file(self, path: str, file_path: Path, *, kind: str = "output") -> None:
        """Delete one file and its record, restoring metadata if unlink fails."""
        with self._lock:
            key = (kind, path)
            previous = self._records.pop(key, None)
            try:
                if previous is not None:
                    self._save_locked()
            except OSError:
                self._records[key] = previous
                raise
            try:
                file_path.unlink()
            except OSError:
                if previous is not None:
                    self._records[key] = previous
                    self._save_locked()
                raise

    def collections_summary(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for r in self._records.values():
                for c in r.collections:
                    counts[c] = counts.get(c, 0) + 1
            return counts


output_meta = OutputMetaStore()
