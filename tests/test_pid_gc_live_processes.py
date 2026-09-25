"""Scheduled PID cleanup must preserve processes tracked by this gateway."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import comfy_manager  # noqa: E402
import worker_manager  # noqa: E402


class LiveProcessPidGcTests(unittest.TestCase):
    def test_comfy_pid_file_for_registered_instance_is_not_killed(self):
        process = types.SimpleNamespace(pid=4326)
        registry = mock.Mock()
        registry.all_instances.return_value = [types.SimpleNamespace(process=process)]
        manager = comfy_manager.ComfyManager(registry=registry)

        with tempfile.TemporaryDirectory() as raw_tmp:
            pid_dir = Path(raw_tmp)
            (pid_dir / "comfy_live.json").write_text(json.dumps({
                "app_instance": str(comfy_manager.APP_DIR),
                "pid": process.pid,
                "pgid": process.pid,
            }), encoding="utf-8")
            with mock.patch.object(comfy_manager, "PID_DIR", pid_dir), \
                 mock.patch.object(comfy_manager.os, "kill") as kill, \
                 mock.patch.object(comfy_manager.subprocess, "run") as run:
                self.assertEqual(manager.kill_orphan_comfy(), 0)

        kill.assert_not_called()
        run.assert_not_called()

    def test_worker_fallback_skips_registered_process(self):
        process = types.SimpleNamespace(pid=4081)
        registry = mock.Mock()
        registry.all_workers.return_value = [types.SimpleNamespace(process=process)]
        manager = worker_manager.WorkerManager(registry=registry)

        with tempfile.TemporaryDirectory() as raw_tmp, \
             mock.patch.object(worker_manager, "PID_DIR", Path(raw_tmp)), \
             mock.patch.object(
                 worker_manager.subprocess,
                 "run",
                 return_value=types.SimpleNamespace(returncode=0, stdout=f"{process.pid}\n"),
             ), \
             mock.patch.object(worker_manager.os, "kill") as kill, \
             mock.patch.object(worker_manager.Path, "read_text") as read_text:
            self.assertEqual(manager.kill_orphan_workers(), 0)

        kill.assert_not_called()
        read_text.assert_not_called()

    def test_comfy_record_owned_by_live_gateway_survives_fresh_manager(self):
        registry = mock.Mock()
        registry.all_instances.return_value = []
        manager = comfy_manager.ComfyManager(registry=registry)

        with tempfile.TemporaryDirectory() as raw_tmp:
            pid_dir = Path(raw_tmp)
            (pid_dir / "comfy_owned.json").write_text(json.dumps({
                "app_instance": str(comfy_manager.APP_DIR),
                "owner_pid": 4466,
                "pid": 4474,
                "pgid": 4474,
            }), encoding="utf-8")
            with mock.patch.object(comfy_manager, "PID_DIR", pid_dir), \
                 mock.patch.object(comfy_manager, "_is_live_gateway_pid", return_value=True), \
                 mock.patch.object(comfy_manager.os, "kill") as kill, \
                 mock.patch.object(comfy_manager.subprocess, "run") as run:
                self.assertEqual(manager.kill_orphan_comfy(), 0)

        kill.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
