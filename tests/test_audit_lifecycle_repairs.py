"""Workflow and lifecycle regressions with all process signals mocked."""
import asyncio
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from fastapi import HTTPException, UploadFile
import process_identity
import jobs as jobs_module
from jobs import Job, JobStore
from routers import workflows, jobs_routes
from cli.omni import cli
from click.testing import CliRunner


GRAPH = {"1": {"class_type": "EmptyImage", "inputs": {}}}


class WorkflowPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = [patch.object(workflows, "WORKFLOWS_DIR", self.root),
                      patch.object(workflows, "_METADATA_DIR", self.root / ".metadata")]
        for p in self.paths:
            p.start()

    def tearDown(self):
        for p in reversed(self.paths):
            p.stop()
        self.tmp.cleanup()

    async def test_invalid_metadata_leaves_graph_unchanged(self):
        target = self.root / "test.json"
        target.write_text('{"prior":true}')
        request = workflows.WorkflowSaveRequest(workflow=GRAPH, overwrite=True, tags=["\x00"])
        with self.assertRaises(HTTPException):
            await workflows.save_workflow(target.name, request)
        self.assertEqual(json.loads(target.read_text()), {"prior": True})

    async def test_metadata_disk_failure_rolls_back_graph(self):
        target = self.root / "test.json"
        target.write_text('{"prior":true}')
        with patch.object(workflows, "_write_metadata", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                await workflows.save_workflow(target.name, workflows.WorkflowSaveRequest(workflow=GRAPH, overwrite=True))
        self.assertEqual(json.loads(target.read_text()), {"prior": True})

    async def test_null_policy_clears_saved_value(self):
        (self.root / "test.json").write_text(json.dumps(GRAPH))
        workflows._write_metadata("test.json", [], "", {"mode": "single"})
        await workflows.set_workflow_metadata("test.json", workflows.WorkflowMetadataRequest(placement_policy=None))
        self.assertIsNone(workflows._load_metadata("test.json")["placement_policy"])

    async def test_concurrent_import_without_overwrite_has_one_winner(self):
        a = UploadFile(io.BytesIO(json.dumps(GRAPH).encode()), filename="test.json")
        b = UploadFile(io.BytesIO(json.dumps({"2": GRAPH["1"]}).encode()), filename="test.json")
        result = await asyncio.gather(workflows.import_workflow(a), workflows.import_workflow(b), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in result), 1)
        self.assertEqual(sum(isinstance(r, HTTPException) and r.status_code == 409 for r in result), 1)

    async def test_import_overwrite_drops_unrelated_policy(self):
        (self.root / "test.json").write_text(json.dumps(GRAPH))
        workflows._write_metadata("test.json", [], "", {"mode": "single"})
        upload = UploadFile(io.BytesIO(json.dumps(GRAPH).encode()), filename="test.json")
        await workflows.import_workflow(upload, overwrite=True)
        self.assertIsNone(workflows._load_metadata("test.json")["placement_policy"])

    def test_corrupt_metadata_fails_closed(self):
        metadata = self.root / ".metadata"
        metadata.mkdir()
        (metadata / "test.json.meta.json").write_text("{")
        with self.assertRaises(HTTPException):
            workflows._load_metadata("test.json")

    def test_partial_graph_and_dangling_links_are_blocked(self):
        self.assertTrue(workflows._api_graph_issues({**GRAPH, "bad": 7}))
        self.assertTrue(workflows._api_graph_issues({"1": {"class_type": "Node", "inputs": {"x": ["missing", 0]}}}))
        self.assertTrue(workflows._api_graph_issues(GRAPH, {"EmptyImage": {"input": {"required": {"width": ["INT"]}}}}))


class ProcessIdentityTests(unittest.TestCase):
    def test_exact_script_port_and_generation_are_required(self):
        script = Path("/opt/omni_studio/server/omni_worker.py")
        command = b"/opt/venv/bin/python\0" + str(script).encode() + b"\0--port\08211\0"
        with patch.object(process_identity.Path, "read_bytes", return_value=command), patch.object(process_identity, "process_start_time", return_value="123"):
            self.assertTrue(process_identity.process_matches(999999, script, port=8211, start_time="123"))
            self.assertFalse(process_identity.process_matches(999999, script, port=8212))
            self.assertFalse(process_identity.process_matches(999999, script, port=8211, start_time="old"))
            self.assertFalse(process_identity.process_matches(999999, Path("/foreign/omni_worker.py"), port=8211))

    def test_recorded_group_cannot_redirect_signal(self):
        with patch.object(process_identity.os, "getpgid", return_value=100, create=True), patch.object(process_identity.os, "getpgrp", return_value=200, create=True):
            self.assertIsNone(process_identity.owned_process_group(100, 300))
            self.assertEqual(process_identity.owned_process_group(100, 100), 100)
            self.assertIsNone(process_identity.owned_process_group(101, 100))

    def test_dead_parent_group_cleanup_uses_captured_group(self):
        proc = types.SimpleNamespace(pid=999999, returncode=0, _omni_group_owned=True)
        with patch.object(jobs_module.os, "killpg", create=True) as kill, patch.object(jobs_module.os, "getpgrp", return_value=100, create=True), patch.object(jobs_module.signal, "SIGKILL", 9, create=True):
            jobs_module._kill_proc_tree(proc)
        kill.assert_called_once_with(999999, 9)


class JobRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_stream_continues_after_tail_rollover(self):
        job = Job("test-job", "maintenance", status="running")
        job.stdout_tail.extend(f"line-{i}\n" for i in range(200))
        with patch.object(jobs_routes.jobs, "get", return_value=job):
            response = await jobs_routes.job_stream(job.job_id)
            for _ in range(201):
                await anext(response.body_iterator)
            job.stdout_tail.append("after-rollover\n")
            event = await anext(response.body_iterator)
            self.assertIn("after-rollover", event)
            await response.body_iterator.aclose()

    async def test_cancel_during_spawn_cleans_created_process(self):
        gate = asyncio.Event()
        proc = types.SimpleNamespace(pid=999999, returncode=None)
        async def spawn(*args, **kwargs):
            await gate.wait()
            return proc
        with patch.object(jobs_module.asyncio, "create_subprocess_exec", side_effect=spawn), patch.object(jobs_module, "_kill_proc_tree") as kill:
            store = JobStore()
            job = store.enqueue_subprocess(kind="maintenance", argv=["test-command"], meta={})
            await asyncio.sleep(0)
            store.cancel(job.job_id)
            gate.set()
            await job.task
            self.assertEqual(job.status, "cancelled")
            self.assertTrue(any(call.args == (proc,) for call in kill.call_args_list))


class CliContractRepairTests(unittest.TestCase):
    def test_output_root_and_duration(self):
        client = Mock()
        client.call.return_value = {}
        with patch("cli.omni._client", return_value=client):
            result = CliRunner().invoke(cli, ["outputs", "prune", "--kind", "omni", "--older-than", "12h", "--dry-run"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(client.call.call_args.kwargs["json"], {"kind": "omni", "older_than_days": 0.5, "dry_run": True})

    def test_policy_payload_and_memory_free(self):
        client = Mock()
        client.call.return_value = {}
        with patch("cli.omni._client", return_value=client):
            result = CliRunner().invoke(cli, ["maintenance", "policy", "set", "prune-outputs", "enabled", "false"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(client.call.call_args.kwargs["json"], {"policy": {"prune-outputs": {"enabled": False}}})
            result = CliRunner().invoke(cli, ["comfy", "free", "test-instance"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(client.call.call_args.kwargs["json"]["free_memory"])


if __name__ == "__main__":
    unittest.main()
