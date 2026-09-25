"""ComfyUI proxy allowlist + header filter regression tests."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from proxy import (  # noqa: E402
    is_allowed_subpath,
    _filter_request_headers,
    _filter_response_headers,
)


class AllowlistTests(unittest.TestCase):
    def test_allowed_exact(self):
        for p in ("prompt", "queue", "interrupt", "free", "system_stats",
                  "object_info", "embeddings", "extensions", "view", "models",
                  "history", "api/jobs", "upload/image"):
            self.assertTrue(is_allowed_subpath(p), p)

    def test_allowed_prefixed(self):
        for p in ("history/abcd", "object_info/SomeNode",
                  "models/checkpoints", "models/checkpoints/sd_xl.safetensors",
                  "api/jobs/6f9619ff-8b86-d011-b42d-00cf4fc964ff",
                  "api/jobs/6f9619ff-8b86-d011-b42d-00cf4fc964ff/cancel",
                  "api/jobs/cancel"):
            self.assertTrue(is_allowed_subpath(p), p)

    def test_rejects_unlisted(self):
        for p in ("admin", "settings", "api", "exec",
                  "prompt/extra", "free/all", "view/secret"):
            self.assertFalse(is_allowed_subpath(p), p)

    def test_rejects_traversal(self):
        for p in ("..", "history/../etc", "models/..", "prompt\x00"):
            self.assertFalse(is_allowed_subpath(p), p)

    def test_rejects_empty(self):
        self.assertFalse(is_allowed_subpath(""))


class ComfyExecutionUiContractTests(unittest.TestCase):
    def test_execution_controls_use_native_comfy_routes_and_refresh_state(self):
        source = (ROOT / "server" / "static" / "tab-comfy.js").read_text(
            encoding="utf-8",
        )
        for label in ("Clear Pending", "Cancel All", "Unload Models"):
            self.assertIn(label, source)
        for route in (
            "api/jobs?limit=50",
            "api/jobs?status=pending%2Cin_progress",
            "/cancel'), {}",
            "executionPath('queue'), { clear: true }",
            "executionPath('free'), { unload_models: true, free_memory: true }",
        ):
            self.assertIn(route, source)
        self.assertGreaterEqual(source.count("await this.refreshExecution(true)"), 4)


class HeaderFilterTests(unittest.TestCase):
    def test_strips_hop_by_hop(self):
        src = {
            "Connection": "close",
            "Upgrade": "h2c",
            "Transfer-Encoding": "chunked",
            "Host": "evil.example.com",
            "Content-Type": "application/json",
        }
        out = _filter_request_headers(src)
        self.assertNotIn("Connection", out)
        self.assertNotIn("Upgrade", out)
        self.assertNotIn("Transfer-Encoding", out)
        self.assertNotIn("Host", out)
        self.assertEqual(out.get("Content-Type"), "application/json")

    def test_strips_gateway_internal(self):
        src = {"X-Omni-Token": "secret", "Cookie": "s=1", "Accept": "*/*"}
        out = _filter_request_headers(src)
        self.assertNotIn("X-Omni-Token", out)
        self.assertNotIn("Cookie", out)
        self.assertEqual(out.get("Accept"), "*/*")

    def test_keeps_content_length(self):
        src = {"Content-Length": "42", "Content-Type": "application/json"}
        out = _filter_request_headers(src)
        # Content-Length is end-to-end (RFC 7230) and required for fixed-size
        # bodies through stdlib upstreams; it must NOT be stripped.
        self.assertEqual(out.get("Content-Length"), "42")

    def test_response_header_filter_drops_hop(self):
        src = {"Connection": "close", "Content-Type": "image/png", "X-Stuff": "ok"}
        out = dict(_filter_response_headers(src))
        self.assertNotIn("Connection", out)
        self.assertEqual(out["Content-Type"], "image/png")
        self.assertEqual(out["X-Stuff"], "ok")


if __name__ == "__main__":
    unittest.main()
