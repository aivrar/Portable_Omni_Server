"""Final audit regressions: temporary files, fake workers, no live gateway."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request
from key_store import ApiKeyStore
from output_meta import OutputMetaStore
from snapshot_install import inspect_installed_snapshot, verify_installed_models
from omni_placement import analyze_worker_placement
from routers import ace_step, comfy, extensions, outputs, setup, workers


class CompletionRepairs(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows query API regression")
    def test_windows_alive_probe_never_sends_a_signal(self):
        import process_identity
        with patch.object(process_identity.os, "kill", side_effect=AssertionError("Windows signal forbidden")):
            self.assertTrue(process_identity.process_alive(os.getpid()))
            self.assertFalse(process_identity.process_alive(0))

    def test_installed_status_requires_marker_and_complete_nested_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "omni" / "demo"
            nested = model / "transformer"
            nested.mkdir(parents=True)
            (nested / "a.safetensors").write_bytes(b"a")
            with patch.object(setup, "MODELS_DIR", root):
                self.assertFalse(setup._model_weights_present("demo"))
                (model / ".install_complete").write_text("owner/demo")
                self.assertTrue(setup._model_weights_present("demo"))
                (nested / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"x": "missing.safetensors"}}))
                self.assertFalse(setup._model_weights_present("demo"))
            report = verify_installed_models(root / "omni")
            self.assertEqual((report["checked"], report["ok"]), (1, 0))
            self.assertEqual(report["validation_level"], "file-presence-and-shard-map")
            self.assertEqual(report["broken"][0]["checked_indexes"], 1)
            (nested / "missing.safetensors").touch()
            self.assertFalse(inspect_installed_snapshot(model)["valid"])

    def test_ace_primary_cannot_borrow_unused_auxiliary_capacity(self):
        plan = analyze_worker_placement(model="ace_step", variant="ace-xl-sft", legacy_device="cuda:0",
            placement={"mode": "auto", "primary_device": "cuda:0", "eligible_devices": ["cuda:0", "cuda:1"], "require_all": True},
            devices=[{"id": "cuda:0", "vram_free_mb": 8000}, {"id": "cuda:1", "vram_free_mb": 40000}])
        self.assertFalse(plan["valid"])
        self.assertTrue(any("components on cuda:0" in b for b in plan["blockers"]))

    def test_ace_exact_lm_capacity_and_visibility_checked_before_load(self):
        worker = SimpleNamespace(device="cuda:0", gpu_pool=["cuda:0", "cuda:1"])
        req = ace_step._LoadRequest(model_variant="ace-1.5", lm_variant="ace-lm-4b", lm_device="cuda:1")
        with patch.object(ace_step.worker_manager, "detect_devices", return_value=[{"id": "cuda:0", "vram_free_mb": 24000}, {"id": "cuda:1", "vram_free_mb": 5000}]), patch.object(ace_step.worker_manager, "detect_app_memory_state", return_value={"available_worker_mb": 24000}), patch.object(ace_step.worker_registry, "all_workers", return_value=[]):
            with self.assertRaises(HTTPException) as error:
                ace_step._check_load_capacity(worker, req, {}, "ace-lm-4b", True, True)
            self.assertEqual(error.exception.status_code, 409)
            self.assertIn("cuda:1", str(error.exception.detail))
            req.lm_device = "cuda:9"
            with self.assertRaises(HTTPException):
                ace_step._check_load_capacity(worker, req, {}, "ace-lm-4b", True, True)

    def test_manager_failed_start_response_stops_owner(self):
        async def request(_instance, _method, path, *args, **kwargs):
            if path.endswith("/start"):
                raise extensions.ManagerAPIError("connection lost after mutation started")
            return {"is_processing": False}
        with patch.object(extensions, "_manager_request", request), patch.object(extensions, "_manager_log_offset", return_value=(Path("unused"), 0)), patch.object(extensions.comfy_manager, "stop_instance", AsyncMock(return_value=True)) as stop:
            with self.assertRaises(extensions.ManagerAPIError):
                asyncio.run(extensions._run_queue(SimpleNamespace(instance_id="exact-owner"), "update", {}, 1, asyncio.Event()))
            stop.assert_awaited_once_with("exact-owner")

    def test_failed_delete_preserves_metadata_and_prune_counts(self):
        async def run(root):
            media = root / "keep.wav"
            media.write_bytes(b"audio")
            store = OutputMetaStore(root / "metadata.json")
            store.upsert("keep.wav", tags=["retain"])
            captured = {}
            def enqueue(**kwargs):
                captured.update(kwargs)
                return SimpleNamespace(job_id="test", status="queued")
            original = Path.unlink
            def deny_media(path, *args, **kwargs):
                if path == media:
                    raise PermissionError("test denied")
                return original(path, *args, **kwargs)
            with patch.dict(outputs._OUTPUT_ROOTS, {"omni": root}), patch.object(outputs, "output_meta", store), patch.object(outputs.jobs, "enqueue_callable", side_effect=enqueue), patch.object(Path, "unlink", deny_media):
                # The direct deletion uses kind=output for the metadata record.
                with self.assertRaises(PermissionError):
                    store.delete_file("keep.wav", media)
                self.assertEqual(store.get("keep.wav").tags, ["retain"])
                self.assertEqual(OutputMetaStore(store.path).get("keep.wav").tags, ["retain"])
                await outputs.prune_outputs(outputs.PruneRequest(kind="omni", older_than_days=0))
                # Keep the metadata store outside candidates for this count check.
                with patch.object(Path, "rglob", return_value=iter([media])):
                    result = await captured["fn"](Mock(), asyncio.Event())
                self.assertEqual((result["deleted"], result["freed_bytes"]), (0, 0))
                self.assertEqual(len(result["failures"]), 1)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_gateway_scopes_enforced_without_entering_lifespan(self):
        import omni_comfy_server as gateway
        async def run(store, secret):
            for method, path, query, expected in [
                ("GET", "/api/workers", b"", 200),
                ("POST", "/api/workflows/analyze", b"", 200),
                ("POST", "/api/ace_step/analyze", b"", 403),
                ("POST", "/api/workers/spawn", b"", 403),
                ("GET", "/api/ace_step/state", b"autospawn=true", 403),
                ("GET", "/api/ace_step/state", b"autospawn=false", 200),
                ("POST", "/api/shutdown", b"", 403),
            ]:
                request = Request({"type": "http", "method": method, "path": path, "query_string": query,
                    "headers": [(b"authorization", ("Bearer " + secret).encode())], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8200), "scheme": "http"})
                downstream = AsyncMock(return_value=JSONResponse({"ok": True}))
                result = await gateway.require_local_session_token(request, downstream)
                self.assertEqual(result.status_code, expected, path)
                if expected == 403:
                    downstream.assert_not_awaited()
        with tempfile.TemporaryDirectory() as tmp:
            store = ApiKeyStore(Path(tmp) / "keys.json")
            _, secret = store.create(["read"])
            with patch.object(gateway, "api_keys", store), patch.object(gateway, "_AUTH_MODE", "bearer"), patch.object(gateway, "get_api_token", return_value="synthetic-unrelated-token"):
                asyncio.run(run(store, secret))

    def test_existing_proxy_operations_have_unique_schema_ids(self):
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(comfy.router)
        operations = app.openapi()["paths"]["/api/comfy/{instance_id}/proxy/{subpath}"]
        self.assertEqual(set(operations), {"get", "post", "put", "delete", "patch"})
        self.assertEqual(len({item["operationId"] for item in operations.values()}), 5)

    def test_comfy_tail_reads_only_bounded_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "comfy_8188.log").write_text("old\n" * 200000 + "final\n")
            with patch.object(comfy, "WORKER_LOG_DIR", root), patch.object(comfy.comfy_registry, "get", return_value=SimpleNamespace(port=8188)):
                result = asyncio.run(comfy.get_comfy_logs("synthetic", lines=2))
                self.assertEqual(result["lines"], ["old", "final"])

    def test_manifest_setup_from_arbitrary_clone_preserves_exit_code(self):
        bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else Path("/bin/bash")
        if not bash.exists():
            self.skipTest("native Bash unavailable")
        command = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))["setup"]
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "arbitrary clone"
            (clone / "server").mkdir(parents=True)
            (clone / "app.json").write_text("{}")
            (clone / "server" / "setup.sh").write_text("#!/bin/bash\nexit 7\n", encoding="utf-8")
            result = subprocess.run([str(bash), "-c", command], cwd=clone, capture_output=True, timeout=10,
                                    env={**os.environ, "OMNI_STUDIO_SOURCE_DIR": clone.as_posix()})
            self.assertEqual(result.returncode, 7)


class StructuredStreamingRepairs(unittest.IsolatedAsyncioTestCase):
    async def test_native_and_compat_streams_keep_tool_calls_and_release_worker(self):
        from worker_registry import WorkerInfo, WorkerRegistry
        registry = WorkerRegistry()
        worker = WorkerInfo("fake", "qwen_omni_3b", 8201, "cpu", status="ready")
        registry.register(worker)
        class Response:
            status_code = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def aiter_lines(self):
                for event in ({"delta": '<tool_call>{"name":"lookup","arguments":{"x":1}}</tool_call>'}, {"done": True}):
                    yield "data: " + json.dumps(event)
        client = SimpleNamespace(stream=Mock(return_value=Response()), aclose=AsyncMock())
        request = workers.ChatRequest(model=worker.model, tools=[{"function": {"name": "lookup"}}])
        with patch.object(workers, "worker_registry", registry), patch.object(workers.httpx, "AsyncClient", return_value=client):
            response = await workers.chat_stream(worker.model, request)
            events = [json.loads(chunk.decode()[5:]) async for chunk in response.body_iterator]
            self.assertEqual(events[-1]["finish_reason"], "tool_calls")
            self.assertEqual(worker.status, "ready")
            response = await workers._oai_stream_response(chat_req=request, completion_id="fake", created=1, public_model=worker.model, object_name="chat.completion.chunk", delta_key="content")
            chunks = [chunk async for chunk in response.body_iterator]
            event = json.loads(chunks[-2].decode()[5:])
            self.assertEqual(event["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "lookup")
            self.assertEqual(chunks[-1], b"data: [DONE]\n\n")
            self.assertEqual(worker.status, "ready")
