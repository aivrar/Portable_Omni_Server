"""CLI surface tests: token resolution + click command tree integrity."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli.client import resolve_token  # noqa: E402
from cli.omni import cli  # noqa: E402


class TokenResolveTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("OMNI_API_TOKEN", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["OMNI_API_TOKEN"] = self._saved

    def test_explicit_wins(self):
        token = resolve_token("provided-by-flag")
        self.assertEqual(token, "provided-by-flag")

    def test_env_used_when_no_flag(self):
        os.environ["OMNI_API_TOKEN"] = "from-env"
        try:
            self.assertEqual(resolve_token(None), "from-env")
        finally:
            os.environ.pop("OMNI_API_TOKEN", None)


class ClickGroupShapeTests(unittest.TestCase):
    def test_top_level_groups_present(self):
        groups = set(cli.commands.keys())
        expected = {"chat", "comfy", "devices", "jobs", "maintenance",
                    "outputs", "session", "setup", "status", "system",
                    "workers", "workflows"}
        missing = expected - groups
        self.assertFalse(missing, f"Missing groups: {missing}")

    def test_workers_group_has_kill_all(self):
        workers = cli.commands["workers"]
        self.assertIn("kill-all", workers.commands)
        self.assertIn("spawn", workers.commands)
        self.assertIn("logs", workers.commands)

    def test_chat_send_has_streaming_flag(self):
        chat = cli.commands["chat"]
        send = chat.commands["send"]
        opt_names = {p.name for p in send.params}
        self.assertIn("stream", opt_names)
        self.assertIn("top_p", opt_names)
        self.assertIn("video", opt_names)

    def test_restart_confirms_destructive_request(self):
        client = mock.Mock()
        client.call.return_value = {"status": "restarting"}
        with mock.patch("cli.omni._client", return_value=client):
            result = CliRunner().invoke(cli, ["system", "restart"])
        self.assertEqual(result.exit_code, 0, result.output)
        client.call.assert_called_once_with(
            "POST", "/api/system/restart", json={"confirm": True})

    def test_shutdown_confirms_destructive_request(self):
        client = mock.Mock()
        client.call.return_value = {"status": "shutting_down"}
        with mock.patch("cli.omni._client", return_value=client):
            result = CliRunner().invoke(cli, ["system", "shutdown"])
        self.assertEqual(result.exit_code, 0, result.output)
        client.call.assert_called_once_with(
            "POST", "/api/system/shutdown", json={"confirm": True})

    def test_outputs_list_keeps_root_and_media_kind_distinct(self):
        client = mock.Mock()
        client.call.return_value = {"files": []}
        with mock.patch("cli.omni._client", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["outputs", "list", "--kind", "output", "--media-kind", "video",
                 "--subdir", "capability_tests", "--prefix", "H3_Native",
                 "--probe", "--limit", "5"],
            )
        self.assertEqual(result.exit_code, 0, result.output)
        client.call.assert_called_once_with(
            "GET",
            "/api/outputs",
            params={
                "kind": "output",
                "limit": 5,
                "probe": True,
                "media_kind": "video",
                "subdir": "capability_tests",
                "prefix": "H3_Native",
            },
        )


if __name__ == "__main__":
    unittest.main()
