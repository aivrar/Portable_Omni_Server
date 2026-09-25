"""Install job API adapter regression tests."""

import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from jobs import Job  # noqa: E402
from routers import setup  # noqa: E402


class SetupJobAdapterTests(unittest.TestCase):
    def test_legacy_job_dict_surfaces_progress_and_log_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "install_demo_12345678.log"
            log_path.write_text("line one\nline two\n", encoding="utf-8")
            job = Job(
                job_id="abc12345",
                kind="model_install",
                status="running",
                meta={"model": "moshi", "log_path": str(log_path)},
            )
            job.progress = {
                "current": 5,
                "total": 10,
                "message": "Fetching files: 50%",
            }
            job.stdout_tail.append("line two\n")
            job.process_pid = 4321

            data = setup._legacy_job_dict(job)

        self.assertEqual(data["status"], "running")
        self.assertEqual(data["model"], "moshi")
        self.assertEqual(data["progress_percent"], 50.0)
        self.assertEqual(data["progress"]["message"], "Fetching files: 50%")
        self.assertEqual(data["phase"], "Fetching files: 50%")
        self.assertEqual(data["pid"], 4321)
        self.assertGreaterEqual(data["elapsed_seconds"], 0)
        self.assertTrue(data["log_available"])
        self.assertEqual(data["log_name"], "install_demo_12345678.log")

    def test_tail_job_log_is_bounded_to_requested_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "install_demo_12345678.log"
            log_path.write_text("a\nb\nc\n", encoding="utf-8")

            data = setup._tail_job_log(log_path, 2)

        self.assertEqual(data["lines"], ["b", "c"])
        self.assertFalse(data["truncated"])

    def test_completed_job_classifies_known_pip_warning_without_failure(self):
        job = Job(
            job_id="abc12345",
            kind="model_install",
            status="done",
            meta={"model": "moshi"},
        )
        job.stdout_tail.append(
            "WARNING: pip's dependency resolver does not currently take into account "
            "all the packages that are installed.\n"
        )

        data = setup._legacy_job_dict(job)

        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["known_warning_count"], 1)
        self.assertIn("pip dependency resolver", data["warning_summary"])
        self.assertIsNone(data["failure_kind"])

    def test_failed_job_classifies_actionable_failure(self):
        job = Job(
            job_id="abc12345",
            kind="model_install",
            status="error",
            meta={"model": "moshi"},
        )
        job.stdout_tail.append(
            "WARNING: pip's dependency resolver does not currently take into account all packages.\n"
            "ERROR: moshi needs at least 16GB free in /opt/omni_studio/models (4 GB available)\n"
        )

        data = setup._legacy_job_dict(job)

        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["failure_kind"], "disk")
        self.assertIn("Disk space", data["failure_summary"])
        self.assertEqual(data["known_warning_count"], 1)

    def test_grouping_marks_old_failed_retry_as_superseded(self):
        old = Job(
            job_id="oldfail1",
            kind="model_install",
            status="error",
            meta={"model": "moshi", "target_key": "model:moshi"},
            started_at=100.0,
        )
        new = Job(
            job_id="newdone2",
            kind="model_install",
            status="done",
            meta={"model": "moshi", "target_key": "model:moshi"},
            started_at=200.0,
        )

        grouped = setup._group_install_jobs([new, old])
        old_data = setup._legacy_job_dict(old, grouped[old.job_id])
        new_data = setup._legacy_job_dict(new, grouped[new.job_id])

        self.assertEqual(old_data["attempt"], 1)
        self.assertFalse(old_data["latest_for_target"])
        self.assertTrue(old_data["superseded"])
        self.assertEqual(old_data["final_status_for_target"], "completed")
        self.assertEqual(new_data["attempt"], 2)
        self.assertEqual(new_data["previous_failures"], 1)
        self.assertTrue(new_data["latest_for_target"])
        self.assertFalse(new_data["superseded"])

    def test_default_install_job_is_exposed_as_defaults(self):
        job = Job(
            job_id="defaults1",
            kind="default_install",
            status="running",
            meta={
                "name": "Missing recommended defaults",
                "target_key": "defaults:missing",
            },
        )

        grouped = setup._group_install_jobs([job])
        data = setup._legacy_job_dict(job, grouped[job.job_id])

        self.assertEqual(data["kind"], "defaults")
        self.assertEqual(data["name"], "Missing recommended defaults")
        self.assertEqual(data["target_key"], "defaults:missing")
        self.assertTrue(data["latest_for_target"])

    def test_comfy_asset_download_is_exposed_by_install_adapter(self):
        self.assertIn("comfy_asset_install", setup._INSTALL_KINDS)
        job = Job(
            job_id="comfy001",
            kind="comfy_asset_install",
            status="running",
            meta={"name": "demo.safetensors", "category": "checkpoints"},
        )

        data = setup._legacy_job_dict(job)

        self.assertEqual(data["kind"], "comfy_asset_install")
        self.assertEqual(data["name"], "demo.safetensors")
        self.assertEqual(data["phase"], "starting")

    def test_download_required_gb_uses_configurable_headroom(self):
        with patch.dict("os.environ", {
            "OMNI_DOWNLOAD_HEADROOM_MULTIPLIER": "1.2",
            "OMNI_DOWNLOAD_HEADROOM_GB": "3",
        }):
            self.assertEqual(setup._download_required_gb(10), 15)


if __name__ == "__main__":
    unittest.main()
