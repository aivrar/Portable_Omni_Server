import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import omni_shutdown  # noqa: E402


class OmniShutdownTests(unittest.TestCase):
    def test_kill_pid_records_respects_app_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = Path(tmp) / "pids"
            pid_dir.mkdir()
            foreign = pid_dir / "worker_foreign.json"
            foreign.write_text(json.dumps({
                "kind": "worker",
                "pid": os.getpid(),
                "pgid": os.getpid(),
                "port": 0,
                "app_instance": "/opt/some_other_app",
            }), encoding="utf-8")

            with mock.patch.object(omni_shutdown, "PID_DIR", pid_dir), \
                 mock.patch.object(omni_shutdown, "APP_DIR", Path("/opt/omni_studio")), \
                 mock.patch.object(omni_shutdown, "_kill_pid_tree") as kill_tree:
                killed = omni_shutdown._kill_pid_records("worker_*.json")

            self.assertEqual(killed, 0)
            kill_tree.assert_not_called()
            self.assertTrue(foreign.exists())

    def test_kill_pid_records_unloads_workers_before_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = Path(tmp) / "pids"
            pid_dir.mkdir()
            ours = pid_dir / "worker_qwen.json"
            ours.write_text(json.dumps({
                "kind": "worker",
                "pid": os.getpid(),
                "pgid": os.getpid(),
                "port": 8211,
                "app_instance": "/opt/omni_studio",
            }), encoding="utf-8")

            with mock.patch.object(omni_shutdown, "PID_DIR", pid_dir), \
                 mock.patch.object(omni_shutdown, "APP_DIR", Path("/opt/omni_studio")), \
                 mock.patch.object(omni_shutdown, "_pid_alive", return_value=True), \
                 mock.patch.object(omni_shutdown, "process_matches", return_value=True), \
                 mock.patch.object(omni_shutdown, "_try_worker_unload") as unload, \
                 mock.patch.object(omni_shutdown, "_kill_pid_tree") as kill_tree:
                killed = omni_shutdown._kill_pid_records("worker_*.json", unload_workers=True)

            self.assertEqual(killed, 1)
            unload.assert_called_once_with(8211)
            kill_tree.assert_called_once()
            self.assertFalse(ours.exists())

    def test_sweep_all_skips_gateway_when_graceful_succeeds(self):
        with mock.patch.object(omni_shutdown, "try_graceful_gateway_shutdown", return_value=True), \
             mock.patch.object(omni_shutdown, "_kill_gateway_processes") as kill_gw, \
             mock.patch.object(omni_shutdown, "_kill_pid_records", return_value=0), \
             mock.patch.object(omni_shutdown, "_kill_pattern_orphans", return_value=0), \
             mock.patch("worker_manager.WorkerManager.kill_orphan_workers"), \
             mock.patch("comfy_manager.ComfyManager.kill_orphan_comfy"):
            summary = omni_shutdown.sweep_all(graceful=True)

        self.assertTrue(summary["graceful"])
        kill_gw.assert_not_called()

    def test_sweep_all_includes_detached_installers(self):
        calls = []

        def record(pattern, marker, **kwargs):
            calls.append((pattern, marker))
            return 0

        with mock.patch.object(omni_shutdown, "_kill_pid_records", return_value=0), \
             mock.patch.object(omni_shutdown, "_kill_pattern_orphans", side_effect=record), \
             mock.patch("worker_manager.WorkerManager.kill_orphan_workers"), \
             mock.patch("comfy_manager.ComfyManager.kill_orphan_comfy"):
            omni_shutdown.sweep_all(graceful=False, include_gateway=False)

        self.assertIn(
            ("bash.*install_model.sh", str(omni_shutdown.SERVER_DIR / "install_model.sh")),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
