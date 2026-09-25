import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from security import (  # noqa: E402
    get_or_create_token,
    is_valid_hf_token,
    origin_matches_host,
    token_matches,
    write_secret_file,
)
from worker_registry import WorkerInfo, WorkerRegistry  # noqa: E402


class SecurityHelperTests(unittest.TestCase):
    def test_origin_must_match_request_host(self):
        self.assertTrue(origin_matches_host(None, "127.0.0.1:9200"))
        self.assertTrue(origin_matches_host("http://127.0.0.1:9200", "127.0.0.1:9200"))
        self.assertFalse(origin_matches_host("http://127.0.0.1:3000", "127.0.0.1:9200"))
        self.assertFalse(origin_matches_host("https://example.com", "127.0.0.1:9200"))

    def test_token_file_created_with_private_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "api_token"
            token = get_or_create_token(path)
            self.assertTrue(token_matches(token, get_or_create_token(path)))
            if os.name != "nt":
                mode = stat.S_IMODE(os.stat(path).st_mode)
                self.assertEqual(mode, 0o600)

    def test_secret_write_and_hf_token_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hf_token"
            write_secret_file(path, "hf_" + "A" * 24)
            self.assertEqual(path.read_text(), "hf_" + "A" * 24)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(is_valid_hf_token("hf_" + "A" * 24))
        self.assertFalse(is_valid_hf_token("not-a-token"))


class WorkerRegistryTests(unittest.TestCase):
    def test_atomic_pick_marks_worker_busy(self):
        registry = WorkerRegistry(9001, 9002)
        worker = WorkerInfo(
            worker_id="qwen_omni_3b-1",
            model="qwen_omni_3b",
            port=9001,
            device="cpu",
            status="ready",
        )
        registry.register(worker)

        picked = registry.atomic_pick_and_mark_busy("qwen_omni_3b", "job-1")
        self.assertIsNotNone(picked)
        self.assertEqual(picked.status, "busy")
        self.assertEqual(picked.current_job, "job-1")
        self.assertIsNone(registry.atomic_pick_and_mark_busy("qwen_omni_3b", "job-2"))

        registry.mark_ready(worker.worker_id)
        self.assertEqual(registry.get(worker.worker_id).status, "ready")


if __name__ == "__main__":
    unittest.main()
