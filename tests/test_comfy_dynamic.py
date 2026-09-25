"""Dynamic ComfyUI discovery and guarded update regression tests."""

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import comfy, registry  # noqa: E402


class ComfyUpdateTests(unittest.IsolatedAsyncioTestCase):
    def _status(self):
        return {
            "installed": True,
            "current_commit": "a" * 40,
            "dirty": False,
            "remote_commit": "b" * 40,
            "update_available": True,
            "running_instances": [],
            "template_packages": [{"name": "templates", "version": "1.0"}],
            "errors": [],
        }

    async def test_update_dry_run_returns_resolved_plan_without_job(self):
        with mock.patch.object(comfy, "_installation_status", return_value=self._status()), \
             mock.patch.object(comfy.jobs, "enqueue_callable") as enqueue:
            result = await comfy.update_comfy_installation(
                comfy.UpdateComfyRequest(ref="main", dry_run=True)
            )

        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["target_commit"], "b" * 40)
        self.assertIsNone(result["job_id"])
        enqueue.assert_not_called()

    async def test_update_enqueues_bounded_guarded_job(self):
        fake_job = types.SimpleNamespace(job_id="job-1")
        with mock.patch.object(comfy, "_installation_status", return_value=self._status()), \
             mock.patch.object(comfy.jobs, "list", return_value=[]), \
             mock.patch.object(comfy.comfy_manager, "begin_maintenance") as begin, \
             mock.patch.object(comfy.comfy_manager, "end_maintenance") as end, \
             mock.patch.object(comfy.jobs, "enqueue_callable", return_value=fake_job) as enqueue:
            result = await comfy.update_comfy_installation(
                comfy.UpdateComfyRequest(ref="main", dry_run=False)
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["job_id"], "job-1")
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["kind"], "comfy_update")
        self.assertTrue(callable(kwargs["fn"]))
        self.assertEqual(kwargs["active_key"], "comfy:update")
        begin.assert_called_once()
        end.assert_not_called()

    async def test_update_refuses_running_instances(self):
        status = self._status()
        status["running_instances"] = [{"instance_id": "comfy-1", "status": "ready"}]
        with mock.patch.object(comfy, "_installation_status", return_value=status):
            with self.assertRaises(Exception) as raised:
                await comfy.update_comfy_installation(
                    comfy.UpdateComfyRequest(ref="main", dry_run=False)
                )
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    async def test_static_post_update_qualification_checks_commit_and_templates(self):
        status = self._status()
        status["current_commit"] = status["remote_commit"]
        with mock.patch.object(comfy, "_installation_status", return_value=status), \
             mock.patch("routers.assets._template_files", return_value=[{"template_id": "one"}]):
            result = await comfy._qualify_comfy_update(status["remote_commit"], [])

        self.assertTrue(result["valid"])
        self.assertEqual(result["level"], "checkout-and-templates")
        self.assertEqual(result["template_count"], 1)

    async def test_post_update_qualification_rejects_commit_mismatch(self):
        status = self._status()
        with mock.patch.object(comfy, "_installation_status", return_value=status), \
             mock.patch("routers.assets._template_files", return_value=[]):
            result = await comfy._qualify_comfy_update(status["remote_commit"], [])

        self.assertFalse(result["valid"])
        self.assertTrue(any("does not match target" in item for item in result["blockers"]))

    async def test_update_dry_run_can_plan_guarded_instance_restart(self):
        status = self._status()
        status["running_instances"] = [{"instance_id": "comfy-1", "device": "cuda:0"}]
        with mock.patch.object(comfy, "_installation_status", return_value=status), \
             mock.patch.object(comfy.jobs, "enqueue_callable") as enqueue:
            result = await comfy.update_comfy_installation(
                comfy.UpdateComfyRequest(
                    ref="main", dry_run=True, restart_instances=True,
                )
            )

        self.assertTrue(result["restart_instances"])
        self.assertEqual(result["instances_before"], status["running_instances"])
        enqueue.assert_not_called()

    async def test_one_shot_update_stops_updates_and_restarts_instances(self):
        status = self._status()
        status["running_instances"] = [{"instance_id": "comfy-cuda0-8188"}]
        instance = types.SimpleNamespace(
            instance_id="comfy-cuda0-8188",
            port=8188,
            device="cuda:0",
            vram_mode="low",
            precision="fp16",
            preview_method="taesd",
            disable_pinned_memory=True,
            startup_options={"cache_lru": 2},
        )
        captured = {}

        def enqueue(kind, fn, **kwargs):
            captured.update(kind=kind, fn=fn, kwargs=kwargs)
            return types.SimpleNamespace(job_id="job-1")

        progress_events = []
        with mock.patch.object(comfy, "_installation_status", return_value=status), \
             mock.patch.object(comfy.jobs, "list", return_value=[]), \
             mock.patch.object(
                 comfy.comfy_registry, "all_instances", side_effect=[[instance], []],
             ), \
             mock.patch.object(comfy.comfy_registry, "get", return_value=None), \
             mock.patch.object(comfy.comfy_manager, "begin_maintenance") as begin, \
             mock.patch.object(comfy.comfy_manager, "end_maintenance") as end, \
             mock.patch.object(comfy.comfy_manager, "stop_all", mock.AsyncMock(return_value=1)) as stop, \
             mock.patch.object(
                 comfy, "_run_update_script",
                 mock.AsyncMock(return_value={"returncode": 0}),
             ) as update, \
             mock.patch.object(
                 comfy, "_restart_instances",
                 mock.AsyncMock(return_value=([{"instance_id": "comfy-cuda0-8188"}], [])),
             ) as restart, \
             mock.patch.object(
                 comfy, "_qualify_comfy_update",
                 mock.AsyncMock(return_value={"valid": True, "blockers": []}),
             ) as qualify, \
             mock.patch.object(comfy.jobs, "enqueue_callable", side_effect=enqueue):
            submitted = await comfy.update_comfy_installation(
                comfy.UpdateComfyRequest(
                    ref="main", dry_run=False, restart_instances=True,
                )
            )
            result = await captured["fn"](
                lambda current, total, message: progress_events.append(
                    (current, total, message)
                ),
                asyncio.Event(),
            )

        self.assertEqual(submitted["job_id"], "job-1")
        self.assertEqual(captured["kind"], "comfy_update")
        stop.assert_awaited_once()
        update.assert_awaited_once()
        restart.assert_awaited_once()
        qualify.assert_awaited_once_with("b" * 40, [{"instance_id": "comfy-cuda0-8188"}])
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["stopped"], 1)
        self.assertEqual(len(result["restarted"]), 1)
        self.assertEqual(result["qualification"]["valid"], True)
        self.assertEqual(progress_events[-1], (5, 5, "ComfyUI update complete"))
        begin.assert_called_once()
        end.assert_called_once()

    async def test_start_forwards_preview_method(self):
        instance = types.SimpleNamespace(
            instance_id="comfy-cuda1-8188", port=8188, device="cuda:1", status="ready",
            disable_pinned_memory=True,
        )
        start = mock.AsyncMock(return_value=instance)
        with mock.patch.object(comfy.comfy_manager, "start_instance", start):
            result = await comfy.start_comfy_instance(comfy.StartComfyRequest(
                device="cuda:1", vram_mode="low", preview_method="latent2rgb",
                disable_pinned_memory=True,
            ))
        self.assertEqual(result["instance_id"], "comfy-cuda1-8188")
        start.assert_awaited_once_with(
            device="cuda:1", vram_mode="low", precision=None,
            preview_method="latent2rgb",
            disable_pinned_memory=True,
            startup_options={},
            gpu_pool=[],
        )

    async def test_start_rejects_core_maintenance_window(self):
        with mock.patch.object(
            comfy.comfy_manager, "_maintenance_reason", "core-update:test",
        ), mock.patch.object(comfy.comfy_manager, "start_instance") as start:
            with self.assertRaises(Exception) as raised:
                await comfy.start_comfy_instance(comfy.StartComfyRequest())

        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        start.assert_not_called()

    def test_update_status_distinguishes_not_checked_from_current(self):
        with mock.patch.object(comfy, "COMFYUI_DIR") as comfy_dir, \
             mock.patch.object(comfy, "_git", side_effect=[
                 (0, "a" * 40), (0, ""), (0, "https://example.invalid/comfy.git"),
             ]), \
             mock.patch.object(comfy, "_template_package_versions", return_value=[]):
            (comfy_dir / ".git").is_dir.return_value = True
            status = comfy._installation_status(check_remote=False)

        self.assertFalse(status["remote_checked"])
        self.assertEqual(status["update_status"], "not-checked")
        self.assertFalse(status["update_available"])

    def test_managed_layout_deletions_do_not_mark_comfy_source_dirty(self):
        porcelain = "\n".join([
            # The command helper strips the first record's leading space.
            "D input/example.png",
            " D models/checkpoints/put_checkpoints_here",
            " D user/default/comfy.settings.json",
        ])
        self.assertEqual(comfy._tracked_comfy_source_changes(porcelain), [])

    def test_real_comfy_source_change_remains_update_blocker(self):
        porcelain = "\n".join([
            " D models/checkpoints/put_checkpoints_here",
            " M comfy/model_management.py",
        ])
        self.assertEqual(
            comfy._tracked_comfy_source_changes(porcelain),
            [" M comfy/model_management.py"],
        )


class DynamicModelRegistryTests(unittest.TestCase):
    def test_component_paths_do_not_fall_back_to_standalone_checkpoints(self):
        self.assertEqual(
            registry._infer_model_category("text_encoder/model.safetensors", []),
            ("text_encoders", "repository component path"),
        )
        self.assertEqual(
            registry._infer_model_category("Text Encoder/model.safetensors", []),
            ("text_encoders", "repository component path"),
        )
        self.assertEqual(
            registry._infer_model_category(
                "Krea2_Turbo_int8mixed.safetensors", [],
                "Winnougan/Krea-2-Base-Turbo-NVFP4-FP8-INT8",
            ),
            ("diffusion_models", "filename/tags"),
        )
        self.assertEqual(
            registry._infer_model_category("scheduler/state.safetensors", []),
            ("unknown", "unmapped repository component path"),
        )
        self.assertEqual(
            registry._infer_model_category("standalone.safetensors", []),
            ("checkpoints", "fallback"),
        )

    def test_live_huggingface_discovery_returns_actionable_files(self):
        sibling = types.SimpleNamespace(
            rfilename="controlnet/control_v11p_sd15.safetensors",
            size=1234,
            lfs=None,
        )
        model = types.SimpleNamespace(
            id="example/controlnet",
            downloads=99,
            likes=7,
            tags=["controlnet"],
        )
        info = types.SimpleNamespace(
            siblings=[sibling],
            tags=["controlnet"],
            sha="c" * 40,
        )

        class FakeApi:
            def list_models(self, **kwargs):
                return [model]

            def model_info(self, repo_id, files_metadata=False):
                self.repo_id = repo_id
                self.files_metadata = files_metadata
                return info

        fake_module = types.SimpleNamespace(HfApi=FakeApi)
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_module}):
            result = registry._search_comfy_models_sync(
                "control", "controlnet", repo_limit=3, file_limit=10,
            )

        self.assertEqual(result["source"], "live-huggingface")
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["repo"], "example/controlnet")
        self.assertEqual(candidate["file"], "controlnet/control_v11p_sd15.safetensors")
        self.assertEqual(candidate["category"], "controlnet")
        self.assertTrue(candidate["category_confident"])

    def test_exact_repository_inspection_lists_current_files(self):
        sibling = types.SimpleNamespace(
            rfilename="text_encoders/qwen3vl_4b_fp8_scaled.safetensors",
            size=5242467968,
            lfs=None,
        )
        info = types.SimpleNamespace(
            siblings=[sibling], tags=["comfyui"], sha="d" * 40,
        )

        class FakeApi:
            def model_info(self, repo_id, files_metadata=False):
                self.repo_id = repo_id
                self.files_metadata = files_metadata
                return info

        fake_module = types.SimpleNamespace(HfApi=FakeApi)
        with mock.patch.dict(sys.modules, {"huggingface_hub": fake_module}):
            result = registry._inspect_comfy_model_repo_sync(
                "Comfy-Org/Krea-2", "text_encoders", 100,
            )
        self.assertEqual(result["source"], "exact-huggingface-repository")
        self.assertEqual(result["revision"], "d" * 40)
        self.assertEqual(result["candidates"][0]["category"], "text_encoders")


if __name__ == "__main__":
    unittest.main()
