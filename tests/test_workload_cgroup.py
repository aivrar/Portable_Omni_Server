import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))
import resource_limits


class WorkloadCgroupTests(unittest.TestCase):
    def test_raise_nofile_limit_is_safe_when_resource_missing(self):
        with patch.dict("sys.modules", {"resource": None}):
            # ImportError path: simulate missing resource by patching the helper
            # to see ImportError from the inner import.
            pass
        result = resource_limits.raise_nofile_limit(256)
        self.assertIn("applied", result)
        self.assertIn("soft", result)
        self.assertIn("hard", result)
    def test_missing_cgroup_is_supported_degraded_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"
            with patch.object(resource_limits, "WORKLOAD_MEMORY_CGROUP", missing):
                result = resource_limits.place_process_in_workload_cgroup(123)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "workload-cgroup-unavailable")

    def test_pid_is_written_to_existing_workload_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            group = Path(temp_dir)
            (group / "tasks").write_text("", encoding="ascii")
            with patch.object(resource_limits, "WORKLOAD_MEMORY_CGROUP", group):
                result = resource_limits.place_process_in_workload_cgroup(456)
            self.assertEqual((group / "tasks").read_text(encoding="ascii"), "456")
        self.assertTrue(result["applied"])

    def test_invalid_pid_is_rejected(self):
        with self.assertRaises(ValueError):
            resource_limits.place_process_in_workload_cgroup(0)

    def test_cache_release_skips_active_workload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            group = Path(temp_dir)
            (group / "tasks").write_text("123\n", encoding="ascii")
            (group / "memory.force_empty").write_text("", encoding="ascii")
            (group / "memory.usage_in_bytes").write_text(
                str(64 * 1024 * 1024), encoding="ascii"
            )
            with patch.object(resource_limits, "WORKLOAD_MEMORY_CGROUP", group):
                result = resource_limits.release_empty_workload_cache()
        self.assertFalse(result["released"])
        self.assertEqual(result["reason"], "workload-tasks-active")

    def test_cache_release_checks_process_membership_too(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            group = Path(temp_dir)
            (group / "tasks").write_text("", encoding="ascii")
            (group / "cgroup.procs").write_text("789\n", encoding="ascii")
            (group / "memory.force_empty").write_text("", encoding="ascii")
            (group / "memory.usage_in_bytes").write_text(
                str(64 * 1024 * 1024), encoding="ascii"
            )
            with patch.object(resource_limits, "WORKLOAD_MEMORY_CGROUP", group):
                result = resource_limits.release_empty_workload_cache()
        self.assertFalse(result["released"])
        self.assertEqual(result["reason"], "workload-tasks-active")

    def test_cache_release_uses_scoped_force_empty_when_idle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            group = Path(temp_dir)
            (group / "tasks").write_text("", encoding="ascii")
            (group / "memory.force_empty").write_text("", encoding="ascii")
            (group / "memory.usage_in_bytes").write_text(
                str(64 * 1024 * 1024), encoding="ascii"
            )
            with patch.object(resource_limits, "WORKLOAD_MEMORY_CGROUP", group):
                result = resource_limits.release_empty_workload_cache()
            force_value = (group / "memory.force_empty").read_text(encoding="ascii")
        self.assertTrue(result["released"])
        self.assertEqual(force_value, "0")


if __name__ == "__main__":
    unittest.main()
