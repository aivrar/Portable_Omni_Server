"""Guarded ComfyUI-Manager extension lifecycle regression tests."""

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

import comfy_manager as manager_module  # noqa: E402
import config as config_module  # noqa: E402
from jobs import JOB_KINDS  # noqa: E402
from routers import extensions  # noqa: E402


class ExtensionInventoryTests(unittest.TestCase):
    def test_inventory_includes_current_and_legacy_disabled_layouts(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            comfy_dir = Path(raw)
            nodes = comfy_dir / "custom_nodes"
            (nodes / "active" / "requirements.txt").parent.mkdir(parents=True)
            (nodes / "active" / "requirements.txt").write_text("x", encoding="utf-8")
            (nodes / "legacy.disabled").mkdir()
            (nodes / ".disabled" / "modern").mkdir(parents=True)
            registry = mock.Mock()
            with mock.patch.object(manager_module, "COMFYUI_DIR", comfy_dir):
                manager = manager_module.ComfyManager(registry)
                result = manager.get_custom_nodes()

        by_name = {item["name"]: item for item in result}
        self.assertTrue(by_name["active"]["enabled"])
        self.assertTrue(by_name["active"]["has_requirements"])
        self.assertFalse(by_name["legacy"]["enabled"])
        self.assertFalse(by_name["modern"]["enabled"])
        self.assertNotIn(".disabled", by_name)

    def test_extension_job_kind_is_registered(self):
        self.assertIn("comfy_extension", JOB_KINDS)

    def test_cold_comfy_startup_has_a_bounded_realistic_default(self):
        self.assertGreaterEqual(config_module.COMFYUI_STARTUP_TIMEOUT, 240)
        self.assertLessEqual(config_module.COMFYUI_STARTUP_TIMEOUT, 900)

    def test_manager_model_target_accepts_nested_catalog_path(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            comfy_dir = Path(raw)
            with mock.patch.object(extensions, "COMFYUI_DIR", comfy_dir):
                target = extensions._safe_manager_model_target(
                    "checkpoints/SDXL", "model.safetensors",
                )
            self.assertEqual(
                target,
                (comfy_dir / "models/checkpoints/SDXL/model.safetensors").resolve(),
            )

    def test_manager_model_target_rejects_traversal(self):
        with mock.patch.object(extensions, "COMFYUI_DIR", Path("/opt/test-comfy")):
            with self.assertRaises(ValueError):
                extensions._safe_manager_model_target(
                    "checkpoints/../../escape", "model.safetensors",
                )


class ExtensionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = types.SimpleNamespace(
            instance_id="comfy-cpu-8188",
            port=8188,
            device="cpu",
            vram_mode="cpu",
            precision=None,
            preview_method="latent2rgb",
            disable_pinned_memory=True,
            startup_options={"cache_lru": 3},
            status="ready",
        )

    async def test_restart_preserves_complete_start_configuration(self):
        replacement = types.SimpleNamespace(instance_id="comfy-cpu-8188")
        with mock.patch.object(
            extensions.comfy_manager, "stop_instance",
            mock.AsyncMock(return_value=True),
        ) as stop, mock.patch.object(
            extensions.comfy_manager, "start_instance",
            mock.AsyncMock(return_value=replacement),
        ) as start, mock.patch.object(extensions.comfy_registry, "get", return_value=self.instance):
            result = await extensions._restart(self.instance)

        self.assertIs(result, replacement)
        stop.assert_awaited_once_with(self.instance.instance_id)
        start.assert_awaited_once_with(
            device="cpu",
            vram_mode="cpu",
            precision=None,
            preview_method="latent2rgb",
            disable_pinned_memory=True,
            startup_options={"cache_lru": 3},
            gpu_pool=[],
        )

    async def test_manager_mutation_rejects_core_maintenance_window(self):
        with mock.patch.object(
            extensions.comfy_manager, "_maintenance_reason", "core-update:test",
        ):
            with self.assertRaises(Exception) as raised:
                await extensions.manage_extension(
                    extensions.ManageExtensionRequest(
                        action="update_all", dry_run=False,
                    )
                )

        self.assertEqual(getattr(raised.exception, "status_code", None), 409)

    async def test_dry_run_resolves_manager_metadata_without_enqueuing(self):
        context = {"key": "example", "body": {"id": "example"}, "installed_match": None}
        with mock.patch.object(extensions, "_ready_instance", return_value=self.instance), \
             mock.patch.object(extensions, "_operation_context", mock.AsyncMock(return_value=context)), \
             mock.patch.object(extensions, "_manager_request", mock.AsyncMock(return_value={"is_processing": False})), \
             mock.patch.object(extensions.jobs, "enqueue_callable") as enqueue:
            result = await extensions.manage_extension(extensions.ManageExtensionRequest(
                action="install",
                repo_url="https://github.com/example/example",
                dry_run=True,
            ))

        self.assertEqual(result["status"], "planned")
        self.assertTrue(result["snapshot"])
        self.assertTrue(result["auto_restart"])
        enqueue.assert_not_called()

    async def test_catalog_id_can_resolve_install_without_repo_url(self):
        context = {"key": "example", "body": {"id": "example"}, "installed_match": None}
        with mock.patch.object(extensions, "_ready_instance", return_value=self.instance), \
             mock.patch.object(extensions, "_operation_context", mock.AsyncMock(return_value=context)) as resolve, \
             mock.patch.object(extensions, "_manager_request", mock.AsyncMock(return_value={"is_processing": False})):
            result = await extensions.manage_extension(extensions.ManageExtensionRequest(
                action="install",
                node="example",
                dry_run=True,
            ))

        self.assertEqual(result["status"], "planned")
        resolve.assert_awaited_once_with(self.instance, "install", "example", "")

    async def test_verify_confirms_expected_classes_in_live_object_info(self):
        async def request(instance, method, path, body=None, **kwargs):
            if path == "/customnode/installed":
                return {"example": {"cnr_id": "example", "enabled": True}}
            if path == "/object_info":
                return {"ExampleLoader": {}, "ExampleSampler": {}}
            raise AssertionError(path)

        with mock.patch.object(extensions, "_manager_request", side_effect=request):
            result = await extensions._verify(
                self.instance,
                "install",
                "example",
                {"id": "example"},
                ["ExampleLoader", "ExampleSampler"],
            )

        self.assertEqual(result["mode"], "manager_inventory_and_object_info")
        self.assertEqual(result["node_classes_checked"], 2)

    async def test_verify_rejects_installed_extension_with_unloaded_classes(self):
        async def request(instance, method, path, body=None, **kwargs):
            if path == "/customnode/installed":
                return {"example": {"cnr_id": "example", "enabled": True}}
            if path == "/object_info":
                return {"ExampleLoader": {}}
            raise AssertionError(path)

        with mock.patch.object(extensions, "_manager_request", side_effect=request):
            with self.assertRaises(extensions.ManagerAPIError) as raised:
                await extensions._verify(
                    self.instance,
                    "install",
                    "example",
                    {"id": "example"},
                    ["ExampleLoader", "ExampleSampler"],
                )

        self.assertIn("ExampleSampler", str(raised.exception))

    async def test_catalog_verify_reports_small_mapping_drift_without_rollback(self):
        expected = [f"Node{index}" for index in range(10)]

        async def request(instance, method, path, body=None, **kwargs):
            if path == "/customnode/installed":
                return {"example": {"cnr_id": "example", "enabled": True}}
            if path == "/object_info":
                return {name: {} for name in expected[:-1]}
            raise AssertionError(path)

        with mock.patch.object(extensions, "_manager_request", side_effect=request):
            result = await extensions._verify(
                self.instance,
                "install",
                "example",
                {"id": "example"},
                expected,
                catalog_expected_nodes=True,
            )

        self.assertTrue(result["catalog_mapping_drift"])
        self.assertEqual(result["catalog_classes_missing"], ["Node9"])
        self.assertEqual(result["node_classes_loaded"], expected[:-1])

    async def test_mutation_bundles_snapshot_queue_restart_and_verify(self):
        captured = {}
        context = {
            "key": "example",
            "body": {"id": "example", "version": "1.0.0"},
            "installed_match": ("example", {"enabled": True}),
        }

        def enqueue(kind, fn, **kwargs):
            captured.update(kind=kind, fn=fn, kwargs=kwargs)
            return types.SimpleNamespace(job_id="job-1")

        progress_events = []
        with mock.patch.object(extensions, "_ready_instance", return_value=self.instance), \
             mock.patch.object(extensions, "_operation_context", mock.AsyncMock(return_value=context)), \
             mock.patch.object(extensions, "_manager_request", mock.AsyncMock(return_value={"is_processing": False})), \
             mock.patch.object(extensions, "_save_snapshot", mock.AsyncMock(return_value="snapshot-1")) as snapshot, \
             mock.patch.object(extensions, "_run_queue", mock.AsyncMock(return_value={"done_count": 1})) as queue, \
             mock.patch.object(extensions, "_restart", mock.AsyncMock(return_value=self.instance)) as restart, \
             mock.patch.object(extensions, "_verify", mock.AsyncMock(return_value={"extension": "example"})) as verify, \
             mock.patch.object(extensions.jobs, "enqueue_callable", side_effect=enqueue):
            submitted = await extensions.manage_extension(extensions.ManageExtensionRequest(
                action="update",
                node="example",
                dry_run=False,
            ))
            result = await captured["fn"](
                lambda current, total, message: progress_events.append((current, total, message)),
                asyncio.Event(),
            )

        self.assertEqual(submitted["job_id"], "job-1")
        self.assertEqual(captured["kind"], "comfy_extension")
        self.assertEqual(captured["kwargs"]["active_key"], "comfy:manager")
        snapshot.assert_awaited_once()
        queue.assert_awaited_once()
        restart.assert_awaited_once()
        verify.assert_awaited_once()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["snapshot"], "snapshot-1")
        self.assertEqual(progress_events[-1][0], 5)

    async def test_busy_manager_rejects_before_job_submission(self):
        context = {"key": "all", "body": {}, "installed_match": None}
        with mock.patch.object(extensions, "_ready_instance", return_value=self.instance), \
             mock.patch.object(extensions, "_update_all_context", mock.AsyncMock(return_value=context)), \
             mock.patch.object(extensions, "_manager_request", mock.AsyncMock(return_value={"is_processing": True})), \
             mock.patch.object(extensions.jobs, "enqueue_callable") as enqueue:
            with self.assertRaises(Exception) as raised:
                await extensions.manage_extension(extensions.ManageExtensionRequest(
                    action="update_all", dry_run=False,
                ))
        self.assertEqual(getattr(raised.exception, "status_code", None), 409)
        enqueue.assert_not_called()

    async def test_update_all_uses_only_manager_installed_inventory(self):
        installed = {
            "ComfyUI-Manager": {
                "ver": "abc", "cnr_id": "comfyui-manager",
                "aux_id": "ltdrdata/ComfyUI-Manager", "enabled": True,
            },
            "custom": {
                "ver": "1.0", "cnr_id": "custom", "aux_id": "", "enabled": True,
            },
        }
        catalog = {"node_packs": {
            "comfyui-manager": {"id": "comfyui-manager", "version": "nightly"},
            "custom": {"id": "custom", "version": "1.0"},
            "omni_bridge": {"id": "omni_bridge", "version": "unknown"},
        }}

        async def request(instance, method, path, body=None, **kwargs):
            if path == "/customnode/installed":
                return installed
            if path.startswith("/customnode/getlist"):
                return catalog
            raise AssertionError(path)

        with mock.patch.object(extensions, "_manager_request", side_effect=request):
            context = await extensions._update_all_context(self.instance)

        ids = [item["body"]["id"] for item in context["body"]["items"]]
        self.assertEqual(ids, ["comfyui-manager", "custom"])
        self.assertNotIn("omni_bridge", ids)

    async def test_reinstall_uses_observable_uninstall_install_primitives(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            log_path = Path(raw) / "comfy.log"
            log_path.write_text(
                "[ComfyUI-Manager] Queued works are completed.\n"
                "{'uninstall': 1, 'install': 1}\n",
                encoding="utf-8",
            )
            calls = []

            async def request(instance, method, path, body=None, **kwargs):
                calls.append((method, path))
                if path == "/manager/queue/status":
                    return {"is_processing": False}
                return {}

            with mock.patch.object(extensions, "_manager_request", side_effect=request), \
                 mock.patch.object(extensions, "_manager_log_offset", return_value=(log_path, 0)):
                result = await extensions._run_queue(
                    self.instance,
                    "reinstall",
                    {"id": "custom", "version": "1.0"},
                    30,
                    asyncio.Event(),
                )

        self.assertIn(("POST", "/manager/queue/uninstall"), calls)
        self.assertIn(("POST", "/manager/queue/install"), calls)
        self.assertNotIn(("POST", "/manager/queue/reinstall"), calls)
        self.assertEqual(result["evidence"]["stats"], {"uninstall": 1, "install": 1})

    def test_manager_log_evidence_surfaces_errors(self):
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "comfy.log"
            path.write_text(
                "ERROR: An error occurred while updating 'omni_bridge'.\n"
                "[ComfyUI-Manager] Queued works are completed.\n"
                "{'update-main': 3}\n",
                encoding="utf-8",
            )
            evidence = extensions._manager_log_evidence(path, 0)
        self.assertEqual(evidence["stats"], {"update-main": 3})
        self.assertIn("omni_bridge", evidence["errors"][0])

    async def test_snapshot_restore_does_not_claim_success_when_manager_logs_error(self):
        evidence = {"stats": {}, "errors": ["ERROR: Failed to restore snapshot."]}
        with mock.patch.object(extensions, "_manager_log_offset", return_value=(Path("log"), 0)), \
             mock.patch.object(extensions, "_manager_request", mock.AsyncMock(return_value={})), \
             mock.patch.object(extensions, "_restart", mock.AsyncMock(return_value=self.instance)), \
             mock.patch.object(extensions, "_manager_log_evidence", return_value=evidence):
            _instance, recovery = await extensions._restore_snapshot(
                self.instance, "snapshot-1",
            )

        self.assertFalse(recovery["restored"])
        self.assertEqual(recovery["errors"], evidence["errors"])


if __name__ == "__main__":
    unittest.main()
