import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import omni_comfy_server as gateway  # noqa: E402


class DiscoveryRegistryTests(unittest.TestCase):
    def test_gateway_publish_preserves_bridge_as_primary_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_dir = Path(tmp)
            path = registry_dir / "omni_studio.json"
            path.write_text(json.dumps({
                "name": "omni_studio",
                "pid": os.getpid(),
                "app_dir": "/opt/omni_studio",
                "endpoints": {
                    "api": "http://127.0.0.1:9200",
                    "web": "http://127.0.0.1:9200/",
                    "wsl_api": "http://172.16.0.2:9200",
                },
                "auth": {"header": "X-Omni-Token", "token": "old"},
                "extra": {},
            }), encoding="utf-8")

            with mock.patch.object(gateway, "_registry_dir", return_value=registry_dir), \
                 mock.patch.object(gateway, "_local_network_host", return_value="172.16.0.2"), \
                 mock.patch.object(gateway, "_loopback_port_open", side_effect=lambda port: int(port) == 9200), \
                 mock.patch.dict(os.environ, {"OMNI_API_PORT": "8200", "OMNI_BRIDGE_PORT": "9200"}):
                gateway._publish_discovery()

            data = json.loads(path.read_text(encoding="utf-8"))
            endpoints = data["endpoints"]
            self.assertEqual(endpoints["api"], "http://127.0.0.1:9200")
            self.assertNotIn("wsl_api", endpoints)
            self.assertEqual(endpoints["gateway"], "http://127.0.0.1:8200")
            self.assertNotIn("wsl_gateway", endpoints)
            self.assertNotIn("token", data["auth"])
            self.assertEqual(
                data["auth"]["session_endpoint"],
                "http://127.0.0.1:9200/api/session",
            )
            self.assertEqual(data["extra"]["gateway_pid"], os.getpid())
            self.assertEqual(data["extra"]["bridge_port"], 9200)

    def test_gateway_unpublish_leaves_bridge_discovery_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_dir = Path(tmp)
            path = registry_dir / "omni_studio.json"
            path.write_text(json.dumps({
                "name": "omni_studio",
                "pid": os.getpid(),
                "endpoints": {
                    "api": "http://127.0.0.1:9200",
                    "web": "http://127.0.0.1:9200/",
                    "bridge": "http://127.0.0.1:9200",
                    "gateway": "http://127.0.0.1:8200",
                    "gateway_web": "http://127.0.0.1:8200/",
                    "wsl_api": "http://172.16.0.2:9200",
                    "wsl_gateway": "http://172.16.0.2:8200",
                },
                "auth": {"header": "X-Omni-Token", "token": "tok"},
                "extra": {"gateway_pid": os.getpid(), "bridge_pid": os.getpid()},
            }), encoding="utf-8")

            with mock.patch.object(gateway, "_registry_dir", return_value=registry_dir), \
                 mock.patch.dict(os.environ, {"OMNI_API_PORT": "8200"}):
                gateway._unpublish_discovery()

            data = json.loads(path.read_text(encoding="utf-8"))
            endpoints = data["endpoints"]
            self.assertEqual(endpoints["api"], "http://127.0.0.1:9200")
            self.assertEqual(endpoints["wsl_api"], "http://172.16.0.2:9200")
            self.assertNotIn("gateway", endpoints)
            self.assertNotIn("wsl_gateway", endpoints)
            self.assertNotIn("gateway_pid", data["extra"])


if __name__ == "__main__":
    unittest.main()
