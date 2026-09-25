"""JobStore lifecycle regression tests.

Covers enqueue/list/get, the ``DuplicateJobError`` active-key guard, callable
cancel via ``cancel_event``, progress callback wiring, and cross-platform
successful-subprocess cleanup. POSIX process-group cancellation remains in
the dedicated regression tests.
"""

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from jobs import (  # noqa: E402
    DuplicateJobError,
    JobCancelled,
    JobStore,
    TERMINAL_STATES,
    hf_tqdm_parser,
)


class JobStoreCallableTests(unittest.IsolatedAsyncioTestCase):
    async def test_callable_runs_to_completion_with_progress(self):
        store = JobStore()

        async def fn(progress, cancel_event):
            progress(1, 3, "step 1")
            progress(2, 3, "step 2")
            progress(3, 3, "done")
            return {"ok": True, "echoed": [1, 2, 3]}

        job = store.enqueue_callable(kind="maintenance", fn=fn,
                                      meta={"task": "test"})
        # Wait for completion (callable is fast)
        for _ in range(50):
            if job.status in TERMINAL_STATES:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(job.status, "done")
        self.assertIsNotNone(job.result)
        self.assertEqual(job.result, {"ok": True, "echoed": [1, 2, 3]})
        # progress() invocations should have updated job.progress
        self.assertIsNotNone(job.progress)
        self.assertEqual(job.progress["current"], 3)
        self.assertEqual(job.progress["total"], 3)

    async def test_callable_cancel_via_event(self):
        store = JobStore()

        async def slow(progress, cancel_event):
            for i in range(50):
                if cancel_event.is_set():
                    raise JobCancelled()
                await asyncio.sleep(0.05)
            return {"ok": True}

        job = store.enqueue_callable(kind="maintenance", fn=slow, meta={})
        await asyncio.sleep(0.1)
        self.assertEqual(job.status, "running")
        new_status = store.cancel(job.job_id)
        self.assertIn(new_status, ("cancelling", "cancelled"))
        for _ in range(80):
            if job.status in TERMINAL_STATES:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(job.status, "cancelled")

    async def test_active_key_blocks_duplicate_submit(self):
        store = JobStore()

        async def hang(progress, cancel_event):
            await asyncio.sleep(2.0)
            return {"ok": True}

        first = store.enqueue_callable(kind="maintenance", fn=hang,
                                        meta={}, active_key="task:foo")
        with self.assertRaises(DuplicateJobError):
            store.enqueue_callable(kind="maintenance", fn=hang,
                                    meta={}, active_key="task:foo")
        # Cancel first to free the key, then re-enqueue cleanly.
        store.cancel(first.job_id)
        for _ in range(40):
            if first.status in TERMINAL_STATES:
                break
            await asyncio.sleep(0.05)
        second = store.enqueue_callable(kind="maintenance", fn=hang,
                                         meta={}, active_key="task:foo")
        self.assertNotEqual(first.job_id, second.job_id)
        store.cancel(second.job_id)

    async def test_unknown_kind_rejected(self):
        store = JobStore()

        async def fn(progress, cancel_event):
            return {}

        with self.assertRaises(ValueError):
            store.enqueue_callable(kind="bogus_kind", fn=fn, meta={})

    async def test_get_and_list(self):
        store = JobStore()

        async def fn(progress, cancel_event):
            return {"ok": True}

        job = store.enqueue_callable(kind="maintenance", fn=fn, meta={"x": 1})
        for _ in range(30):
            if job.status in TERMINAL_STATES:
                break
            await asyncio.sleep(0.01)
        retrieved = store.get(job.job_id)
        self.assertIs(retrieved, job)
        listed = store.list()
        self.assertIn(job, listed)
        listed_kind = store.list(kinds={"maintenance"})
        self.assertTrue(all(j.kind == "maintenance" for j in listed_kind))


class JobStoreSubprocessLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_subprocess_releases_live_references(self):
        store = JobStore()
        job = store.enqueue_subprocess(
            kind="maintenance",
            argv=[sys.executable, "-c", "print('[hf] demo: attempt 1/1')"],
            progress_parser=hf_tqdm_parser,
        )
        task = job.task

        await asyncio.wait_for(task, timeout=10)

        self.assertEqual(job.status, "done")
        self.assertIsInstance(job.process_pid, int)
        self.assertEqual(job.process_returncode, 0)
        self.assertIsNone(job.process)
        self.assertIsNone(job.task)
        self.assertEqual(job.progress["message"], "demo: attempt 1/1")

    async def test_downloads_are_concurrent_but_bounded(self):
        store = JobStore(max_concurrent_downloads=2, download_cpu_count=1)
        jobs = [
            store.enqueue_subprocess(
                kind="model_install",
                argv=[sys.executable, "-c", "import time; print('up', flush=True); time.sleep(5)"],
                active_key=f"download:{index}",
            )
            for index in range(3)
        ]
        for _ in range(100):
            if sum(job.status == "running" for job in jobs) == 2:
                break
            await asyncio.sleep(0.02)

        self.assertEqual(sum(job.status == "running" for job in jobs), 2)
        self.assertEqual(sum(job.status == "queued" for job in jobs), 1)
        self.assertTrue(all(
            job.meta["resource_policy"]["cpu_fraction"] == "1/3"
            for job in jobs
        ))
        await store.shutdown(timeout=10)

    async def test_download_subprocess_receives_cpu_thread_budget(self):
        store = JobStore(max_concurrent_downloads=1, download_cpu_count=2)
        job = store.enqueue_subprocess(
            kind="comfy_asset_install",
            argv=[sys.executable, "-c", "import os; print(os.environ['OMNI_DOWNLOAD_WORKERS'])"],
        )
        await asyncio.wait_for(job.task, timeout=10)

        self.assertEqual(job.status, "done")
        self.assertIn("2", "".join(job.stdout_tail))
        self.assertEqual(job.meta["resource_policy"]["cpu_threads"], 2)


class JobProgressParserTests(unittest.TestCase):
    def test_hf_tqdm_parser_handles_binary_byte_units(self):
        parsed = hf_tqdm_parser(
            "model.safetensors: 50%|#####     | 1.0MiB/2.0MiB [00:01<00:01, 1.0MiB/s]"
        )

        self.assertIsNotNone(parsed)
        current, total, message = parsed
        self.assertEqual(current, 1024 * 1024)
        self.assertEqual(total, 2 * 1024 * 1024)
        self.assertEqual(message, "model.safetensors: 50%")

    def test_hf_tqdm_parser_handles_file_count_progress(self):
        parsed = hf_tqdm_parser(
            "Fetching 11 files: 18%|#8        | 2/11 [00:05<00:20, 2.22s/it]"
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, (2, 11, "Fetching 11 files: 18%"))

    def test_hf_tqdm_parser_surfaces_retry_phase(self):
        parsed = hf_tqdm_parser(
            "[hf] org/repo/model.safetensors: attempt 2/4"
        )

        self.assertEqual(parsed, (0, None, "org/repo/model.safetensors: attempt 2/4"))

    def test_hf_tqdm_parser_surfaces_blueprint_batch_phase(self):
        parsed = hf_tqdm_parser(
            "Installing blueprint asset 3/8: checkpoints/demo.safetensors"
        )

        self.assertEqual(
            parsed,
            (2, 8, "Installing 3/8: checkpoints/demo.safetensors"),
        )


class InstallerConcurrencyPolicyTests(unittest.TestCase):
    def test_independent_weight_downloads_use_per_target_locks(self):
        script = (SERVER / "install_model.sh").read_text(encoding="utf-8")

        self.assertIn("download_${LOCK_ID}.lock", script)
        self.assertIn("comfy-asset|comfy-url", script)
        self.assertIn('LOCK_FILE="$RUNTIME_DIR/install.lock"', script)


if __name__ == "__main__":
    unittest.main()
