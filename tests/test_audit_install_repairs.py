"""Storage and Manager mutation regressions; upstream calls are mocked."""
import asyncio
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from fastapi import HTTPException
from jobs import Job, JobStore, JobCancelled, DuplicateJobError
from routers import assets, extensions, setup
import maintenance


class InstallRepairs(unittest.TestCase):
    def test_inventory_requires_nonempty_exact_relative_name(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(assets, "COMFYUI_DIR", Path(tmp)):
            root = Path(tmp)/"models"/"vae"
            (root/"nested").mkdir(parents=True)
            (root/"empty.safetensors").touch()
            (root/"nested"/"valid.safetensors").write_bytes(b"weight")
            inventory = assets._installed_asset_inventory()
            self.assertFalse(assets._asset_installed("vae", "empty.safetensors", inventory=inventory))
            self.assertFalse(assets._asset_installed("vae", "valid.safetensors", inventory=inventory))
            self.assertTrue(assets._asset_installed("vae", "nested/valid.safetensors", inventory=inventory))

    def test_destination_conflict_is_independent_of_repo(self):
        job = Job("one", "comfy_asset_install", status="queued", meta={"repo": "owner/other", "target_path": str(Path(tempfile.gettempdir())/"same.safetensors")})
        with patch.object(assets.jobs, "list", return_value=[job]), patch.object(assets.comfy_manager, "_maintenance_reason", None):
            with self.assertRaises(HTTPException):
                assets._guard_asset_mutation(Path(job.meta["target_path"]))

    def test_no_overwrite_upload_preserves_concurrent_file(self):
        async def run(root):
            target = root/"vae"/"weight.safetensors"
            count = 0
            async def read(_size):
                nonlocal count
                count += 1
                if count == 1:
                    target.write_bytes(b"concurrent winner")
                    return b"upload bytes"
                return b""
            file = SimpleNamespace(filename="weight.safetensors", read=read)
            with patch.object(assets, "_model_root", return_value=root), patch.object(assets, "_guard_asset_mutation"), patch.object(assets.shutil, "disk_usage", return_value=SimpleNamespace(free=20*1024**3)):
                with self.assertRaises(HTTPException) as error:
                    await assets.upload_asset(SimpleNamespace(headers={}), "vae", file, sha256=None, overwrite=False)
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(target.read_bytes(), b"concurrent winner")
                self.assertFalse(list(target.parent.glob("*.part")))
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_snapshot_does_not_reuse_old_identifier(self):
        with patch.object(extensions, "_manager_request", AsyncMock(side_effect=[{"items": ["old"]}, {}, {"items": ["old"]}])):
            with self.assertRaises(extensions.ManagerAPIError):
                asyncio.run(extensions._save_snapshot(object()))

    def test_manager_cancel_stops_mutating_instance_before_completion(self):
        async def run():
            cancel = asyncio.Event()
            async def request(instance, method, path, *args, **kwargs):
                if path.endswith("/start"):
                    cancel.set()
                return {"is_processing": False}
            with patch.object(extensions, "_manager_request", request), patch.object(extensions, "_manager_log_offset", return_value=(Path("unused"), 0)), patch.object(extensions.comfy_manager, "stop_instance", AsyncMock(return_value=True)) as stop:
                with self.assertRaises(JobCancelled):
                    await extensions._run_queue(SimpleNamespace(instance_id="exact"), "install_model", {}, 20, cancel)
                stop.assert_awaited_once_with("exact")
        asyncio.run(run())

    def test_update_all_requires_previously_working_classes(self):
        with patch.object(extensions, "_manager_request", AsyncMock(side_effect=[{}, {"Remaining": {}}])):
            with self.assertRaises(extensions.ManagerAPIError):
                asyncio.run(extensions._verify(object(), "update_all", "all", {"items": [], "expected_nodes_before": ["LostClass"]}))

    def test_manager_model_job_kind_is_executable(self):
        async def run():
            store = JobStore()
            job = store.enqueue_callable("comfy_model_install", AsyncMock(return_value={"ok": True}))
            await job.task
            self.assertEqual(job.status, "done")
        asyncio.run(run())

    def test_failed_reinstall_stays_failed_with_old_weights(self):
        job = Job("failed", "model_install", status="error", error="exit 1", meta={"model": "qwen_omni_3b"})
        with patch.object(setup, "_model_weights_present", return_value=True):
            self.assertEqual(setup._legacy_job_dict(job)["status"], "failed")
            self.assertEqual(setup._group_install_jobs([job])["failed"]["final_status_for_target"], "failed")

    def test_transient_hub_status_is_unknown(self):
        setup._HF_TOKEN_STATUS_CACHE.clear()
        with patch.object(setup.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://huggingface.co", 503, "unavailable", {}, None)):
            self.assertIsNone(setup._check_hf_token("hf_" + "T"*30)["token_valid"])
        setup._HF_TOKEN_STATUS_CACHE.clear()

    def test_hub_cleanup_protects_installed_blob_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models = root/"models"
            repos = []
            for name in ("referenced", "unused"):
                repo_path = models/"hub"/name
                repo_path.mkdir(parents=True)
                blob = repo_path/"blob"
                blob.write_bytes(b"weights")
                revision = SimpleNamespace(commit_hash=name, last_modified=0, size_on_disk=7, files=[SimpleNamespace(blob_path=blob)])
                repos.append(SimpleNamespace(repo_path=repo_path, revisions=[revision]))
            installed = models/"omni"/"model.safetensors"
            installed.parent.mkdir()
            os.link(models/"hub"/"referenced"/"blob", installed)
            strategy = SimpleNamespace(execute=Mock())
            info = SimpleNamespace(repos=repos, delete_revisions=Mock(return_value=strategy))
            with patch.dict(sys.modules, {"huggingface_hub": SimpleNamespace(scan_cache_dir=Mock(return_value=info))}), patch.object(maintenance, "MODELS_DIR", models), patch.object(maintenance, "COMFYUI_DIR", root/"comfy"), patch("state.jobs.list", return_value=[]):
                result = asyncio.run(maintenance.run_prune_hub(Mock(), asyncio.Event(), stale_days=7))
            info.delete_revisions.assert_called_once_with("unused")
            self.assertEqual(result["protected_repositories"], 1)

    def test_install_cannot_start_during_hub_cleanup(self):
        async def run():
            gate = maintenance._HUB_PRUNE_GATE
            gate.owner = asyncio.current_task()
            try:
                with self.assertRaises(DuplicateJobError):
                    JobStore().enqueue_subprocess("model_install", [sys.executable, "-c", "pass"])
            finally:
                gate.owner = None
        asyncio.run(run())

    def test_cancelled_pip_purge_terminates_its_child(self):
        async def run(root):
            event, terminated = asyncio.Event(), asyncio.Event()
            proc = SimpleNamespace(returncode=None)
            async def communicate():
                event.set()
                await terminated.wait()
                return b"", None
            def terminate():
                proc.returncode = -15
                terminated.set()
            proc.communicate = communicate
            proc.terminate = Mock(side_effect=terminate)
            proc.kill = Mock(side_effect=terminate)
            with patch.object(maintenance, "CACHE_DIR", root), patch.object(maintenance, "_dir_size_bytes", return_value=100), patch.object(maintenance.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
                with self.assertRaises(JobCancelled):
                    await maintenance.run_prune_pip(Mock(), event, max_gb=0)
            proc.terminate.assert_called_once()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"pip").mkdir()
            asyncio.run(run(root))
