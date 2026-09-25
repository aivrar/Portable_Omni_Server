"""Maintenance scheduler + policy validator regression tests."""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from fastapi import HTTPException  # noqa: E402

import maintenance  # noqa: E402
from routers.maintenance_routes import _validate_policy_overrides  # noqa: E402


class PolicyValidatorTests(unittest.TestCase):
    def test_accepts_known_numeric_keys(self):
        out = _validate_policy_overrides("prune-outputs",
                                          {"cadence_s": 3600, "max_gb": 10})
        self.assertEqual(out, {"cadence_s": 3600, "max_gb": 10})

    def test_rejects_unknown_key(self):
        with self.assertRaises(HTTPException):
            _validate_policy_overrides("prune-outputs", {"bogus_key": 1})

    def test_rejects_non_numeric(self):
        with self.assertRaises(HTTPException):
            _validate_policy_overrides("prune-outputs", {"cadence_s": "soon"})

    def test_rejects_bool_for_numeric(self):
        with self.assertRaises(HTTPException):
            _validate_policy_overrides("prune-outputs", {"cadence_s": True})

    def test_rejects_negative(self):
        with self.assertRaises(HTTPException):
            _validate_policy_overrides("prune-outputs", {"cadence_s": -5})


class SchedulerTickClampTests(unittest.TestCase):
    def test_tick_clamps_to_minimum(self):
        # Reload scheduler with extreme env values to confirm clamp.
        os.environ["OMNI_MAINTENANCE_TICK_S"] = "0"
        try:
            import importlib
            import scheduler  # noqa: F401
            importlib.reload(scheduler)
            self.assertGreaterEqual(scheduler.TICK_SECONDS, 30)
        finally:
            os.environ.pop("OMNI_MAINTENANCE_TICK_S", None)
            import importlib
            import scheduler
            importlib.reload(scheduler)

    def test_tick_default_when_unset(self):
        os.environ.pop("OMNI_MAINTENANCE_TICK_S", None)
        import importlib
        import scheduler
        importlib.reload(scheduler)
        self.assertEqual(scheduler.TICK_SECONDS, 600)


class TempPruneTests(unittest.TestCase):
    def test_prunes_only_stale_immediate_entries(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            cache_dir = Path(raw_tmp)
            tmp_dir = cache_dir / "tmp"
            old_dir = tmp_dir / "old-install"
            fresh_dir = tmp_dir / "active-install"
            old_dir.mkdir(parents=True)
            fresh_dir.mkdir()
            (old_dir / "partial.bin").write_bytes(b"old")
            (fresh_dir / "partial.bin").write_bytes(b"fresh")
            old_time = time.time() - (3 * 86400)
            os.utime(old_dir, (old_time, old_time))

            with mock.patch.object(maintenance, "CACHE_DIR", cache_dir):
                result = asyncio.run(maintenance.run_prune_tmp(
                    lambda *_args: None,
                    asyncio.Event(),
                    max_age_days=2,
                ))

            self.assertFalse(old_dir.exists())
            self.assertTrue(fresh_dir.exists())
            self.assertEqual(result["deleted"], 1)
            self.assertEqual(result["failed"], 0)


if __name__ == "__main__":
    unittest.main()
