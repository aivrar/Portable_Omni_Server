import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import snapshot_install  # noqa: E402


def test_snapshot_integrity_rejects_missing_shards_and_partial_markers():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "model-00001-of-00002.safetensors").write_bytes(b"ok")
        (root / "download.incomplete").write_bytes(b"partial")
        (root / "model.safetensors.index.json").write_text(
            json.dumps({
                "weight_map": {
                    "a": "model-00001-of-00002.safetensors",
                    "b": "model-00002-of-00002.safetensors",
                },
            }),
            encoding="utf-8",
        )

        report = snapshot_install.inspect_snapshot(root)

    assert report["valid"] is False
    assert report["checked_indexes"] == 1
    assert any("partial download marker" in item for item in report["blockers"])
    assert any("missing or empty declared shard" in item for item in report["blockers"])


def test_commit_replaces_complete_directory_and_removes_backup():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        target = parent / "model"
        stage = snapshot_install.staging_directory(target)
        target.mkdir()
        stage.mkdir()
        (target / "old.bin").write_bytes(b"old")
        (stage / "new.bin").write_bytes(b"new")

        snapshot_install.commit_staged_directory(stage, target)

        assert not stage.exists()
        assert not target.with_name(".model.omni-previous").exists()
        assert (target / "new.bin").read_bytes() == b"new"
        assert not (target / "old.bin").exists()


def test_commit_restores_prior_target_if_publish_fails():
    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        target = parent / "model"
        stage = snapshot_install.staging_directory(target)
        target.mkdir()
        stage.mkdir()
        (target / "old.bin").write_bytes(b"old")
        (stage / "new.bin").write_bytes(b"new")
        real_replace = os.replace

        def fail_stage_publish(source, destination):
            if Path(source) == stage and Path(destination) == target:
                raise OSError("simulated publish failure")
            return real_replace(source, destination)

        with mock.patch.object(snapshot_install.os, "replace", side_effect=fail_stage_publish):
            try:
                snapshot_install.commit_staged_directory(stage, target)
            except OSError:
                pass

        assert (target / "old.bin").read_bytes() == b"old"
        assert (stage / "new.bin").read_bytes() == b"new"
