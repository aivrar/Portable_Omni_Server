import ast
import http.server
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import windows_loopback_relay as relay


class _ResponseHandler(http.server.BaseHTTPRequestHandler):
    def handle(self):
        if self.rfile.read(64) != b"r" * 64:
            return
        super().handle()

    def do_GET(self):
        body = b"omni-relay-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class WindowsLoopbackRelayTests(unittest.TestCase):
    def test_bundled_python_wins_without_host_discovery(self):
        # Extract the pure resolver without importing bridge startup globals.
        source = Path(__file__).resolve().parents[1] / "bridge.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        resolver = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_find_windows_pythonw")
        namespace = {"Path": Path, "os": os, "subprocess": subprocess}
        exec(compile(ast.Module(body=[resolver], type_ignores=[]),
                     str(source), "exec"), namespace)
        with tempfile.TemporaryDirectory(prefix="omni relocated ") as tmp:
            bundled = Path(tmp) / "runtime" / "python" / "pythonw.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with patch.dict(os.environ, {"TQ_APP_DIR": tmp}):
                with patch.object(subprocess, "run") as discover:
                    self.assertEqual(namespace["_find_windows_pythonw"](), bundled)
                    discover.assert_not_called()

    def test_relay_forwards_http_bytes(self):
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ResponseHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        server = relay._RelayServer(
            ("127.0.0.1", 0),
            ("127.0.0.1", upstream.server_address[1]),
            b"r" * 64,
        )
        relay_thread = threading.Thread(target=server.serve_forever, daemon=True)
        relay_thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/", timeout=3
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"omni-relay-ok")
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_stale_or_missing_lease_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.lease"
            self.assertFalse(relay._lease_is_current(missing, 8))
            current = Path(tmp) / "current.lease"
            current.touch()
            self.assertTrue(relay._lease_is_current(current, 8))


if __name__ == "__main__":
    unittest.main()
