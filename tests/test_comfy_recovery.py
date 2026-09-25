import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import comfy_manager as manager_module
from comfy_manager import ComfyManager, _AttachedProcess
from worker_registry import ComfyRegistry


class AttachedProcessTests(unittest.TestCase):
    def test_only_matching_process_generation_is_reported_alive(self):
        with mock.patch.object(manager_module, "process_matches", return_value=True) as matches, mock.patch.object(manager_module, "process_alive", return_value=True):
            attached = _AttachedProcess(424242, 8188, "123")
            self.assertIsNone(attached.poll())
            matches.return_value = False
            self.assertIsNotNone(attached.poll())


class ComfyRecoveryTests(unittest.TestCase):
    def test_recover_registers_live_instance_and_reassigns_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = Path(tmp) / "pids"
            pid_dir.mkdir()
            app_dir = Path(tmp) / "app"
            record_path = pid_dir / "comfy_comfy-cuda1-8188.json"
            record_path.write_text(json.dumps({
                "kind": "comfy",
                "pid": 424242,
                "pgid": 424242,
                "instance_id": "comfy-cuda1-8188",
                "port": 8188,
                "app_instance": str(app_dir),
                "owner_pid": 999999,
                "gpu_pool": ["cuda:1", "cuda:0"],
                "gpu_device_map": {"cuda:1": "cuda:0", "cuda:0": "cuda:1"},
                "device": "cuda:1",
                "vram_mode": "normal",
                "preview_method": "auto",
                "startup_options": {},
            }))
            registry = ComfyRegistry(8188, 8199)
            with (
                mock.patch.object(manager_module, "PID_DIR", pid_dir),
                mock.patch.object(manager_module, "APP_DIR", app_dir),
                mock.patch.object(manager_module, "_comfy_process_matches", return_value=True),
                mock.patch.object(manager_module, "process_alive", return_value=True),
                mock.patch.object(manager_module, "process_start_time", return_value="123"),
                mock.patch.object(
                    manager_module,
                    "_probe_recovered_comfy",
                    new=mock.AsyncMock(return_value=(True, 512, 24576)),
                ),
            ):
                recovered = asyncio.run(ComfyManager(registry).recover_instances())

            self.assertEqual(recovered, 1)
            instance = registry.get("comfy-cuda1-8188")
            self.assertIsNotNone(instance)
            self.assertEqual(instance.status, "ready")
            self.assertEqual(instance.gpu_pool, ["cuda:1", "cuda:0"])
            self.assertEqual(instance.vram_total_mb, 24576)
            saved = json.loads(record_path.read_text())
            self.assertEqual(saved["owner_pid"], os.getpid())


if __name__ == "__main__":
    unittest.main()
