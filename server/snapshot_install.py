"""Transactional helpers for Hugging Face snapshot model installations."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf")
PARTIAL_SUFFIXES = (".part", ".partial", ".incomplete")
VALIDATION_LEVEL = "file-presence-and-shard-map"


def staging_directory(target: str | os.PathLike) -> Path:
    """Return a stable sibling staging directory so retries can resume."""
    dest = Path(target)
    return dest.with_name(f".{dest.name}.omni-partial")


def inspect_snapshot(path: str | os.PathLike, *, require_weights: bool = True) -> dict:
    """Cheaply validate completed files and every declared weight shard."""
    root = Path(path)
    blockers: list[str] = []
    weight_files = 0
    checked_indexes = 0
    if not root.is_dir():
        blockers.append("snapshot directory is missing")
        return {
            "valid": False,
            "path": str(root),
            "weight_files": 0,
            "checked_indexes": 0,
            "blockers": blockers,
        }

    try:
        files = [item for item in root.rglob("*") if item.is_file()]
    except OSError as exc:
        blockers.append(f"snapshot scan failed: {exc}")
        files = []

    for item in files:
        lower = item.name.lower()
        if lower.endswith(PARTIAL_SUFFIXES):
            blockers.append(f"partial download marker remains: {item.relative_to(root)}")
        if item.suffix.lower() in WEIGHT_SUFFIXES:
            weight_files += 1
            try:
                if item.stat().st_size <= 0:
                    blockers.append(f"weight file is empty: {item.relative_to(root)}")
            except OSError as exc:
                blockers.append(f"weight file cannot be read: {item.relative_to(root)}: {exc}")
        if not lower.endswith(".index.json"):
            continue
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"invalid shard index {item.relative_to(root)}: {exc}")
            continue
        weight_map = payload.get("weight_map") if isinstance(payload, dict) else None
        if not isinstance(weight_map, dict):
            continue
        checked_indexes += 1
        for relative in sorted({str(value) for value in weight_map.values()}):
            shard = item.parent / relative
            try:
                if (not relative or "\\" in relative or ":" in relative
                        or relative.startswith("/") or ".." in relative.split("/")
                        or not shard.resolve().is_relative_to(root.resolve())):
                    raise ValueError("declared shard escapes snapshot")
                if not shard.is_file() or shard.stat().st_size <= 0:
                    blockers.append(
                        f"missing or empty declared shard: {shard.relative_to(root)}"
                    )
            except (OSError, ValueError):
                blockers.append(f"invalid declared shard path in {item.relative_to(root)}")

    if require_weights and weight_files == 0:
        blockers.append("snapshot contains no model weight files")
    return {
        "valid": not blockers,
        "path": str(root),
        "weight_files": weight_files,
        "checked_indexes": checked_indexes,
        "blockers": blockers,
    }


def write_install_complete(path: str | os.PathLike, repo_id: str) -> None:
    root = Path(path)
    marker = root / ".install_complete"
    temporary = root / ".install_complete.part"
    temporary.write_text(str(repo_id).strip() + "\n", encoding="utf-8")
    os.replace(temporary, marker)


def inspect_installed_snapshot(path: str | os.PathLike) -> dict:
    """Check publication and file completeness, without claiming tensor integrity."""
    report = inspect_snapshot(path)
    if not (Path(path) / ".install_complete").is_file():
        report["blockers"].append("installation completion marker is missing")
    report["valid"] = not report["blockers"]
    report["validation_level"] = VALIDATION_LEVEL
    return report


def verify_installed_models(root: str | os.PathLike, *, target: str | None = None,
                            progress=None, cancelled=None) -> dict:
    """Inspect indexed, unindexed and nested snapshots in the model store."""
    report = {"checked": 0, "ok": 0, "broken": [],
              "validation_level": VALIDATION_LEVEL}
    directory = Path(root)
    candidates = sorted(directory.iterdir()) if directory.is_dir() else []
    for model_dir in candidates:
        if not model_dir.is_dir() or (target and model_dir.name != target):
            continue
        if model_dir.name.startswith("."):
            continue  # resumable staging/rollback trees are not installations
        if cancelled:
            cancelled()
        result = inspect_installed_snapshot(model_dir)
        report["checked"] += 1
        if result["valid"]:
            report["ok"] += 1
        else:
            report["broken"].append({"model": model_dir.name, **result})
        if progress:
            progress(report["checked"], None, f"checked {model_dir.name}")
    if target and not report["checked"]:
        report["broken"].append({"model": target, "blockers": ["model directory is missing"]})
    return report


def commit_staged_directory(
    staging: str | os.PathLike,
    target: str | os.PathLike,
) -> None:
    """Atomically publish a staged tree with rollback of the prior target."""
    stage = Path(staging)
    dest = Path(target)
    if not stage.is_dir():
        raise ValueError(f"Staged snapshot is missing: {stage}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = dest.with_name(f".{dest.name}.omni-previous")

    # Recover an interrupted prior commit before replacing anything else.
    if backup.exists() and not dest.exists():
        os.replace(backup, dest)
    if backup.exists():
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()

    had_target = dest.exists() or dest.is_symlink()
    if had_target:
        os.replace(dest, backup)
    try:
        os.replace(stage, dest)
    except Exception:
        if had_target and backup.exists() and not dest.exists():
            os.replace(backup, dest)
        raise
    if backup.exists():
        if backup.is_dir() and not backup.is_symlink():
            shutil.rmtree(backup)
        else:
            backup.unlink()
