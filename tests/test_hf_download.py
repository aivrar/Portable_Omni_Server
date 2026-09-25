"""Retry policy tests for Hugging Face installer downloads."""

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from hf_download import is_non_retryable_hf_error, run_with_hf_retries  # noqa: E402


class _HTTPishError(RuntimeError):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = type("Response", (), {"status_code": status_code})()


class HFDownloadRetryTests(unittest.TestCase):
    def test_transient_error_retries_then_returns(self):
        calls = {"count": 0}

        def flaky():
            calls["count"] += 1
            if calls["count"] == 1:
                raise _HTTPishError("temporary upstream issue", 503)
            return "ok"

        with patch("hf_download.time.sleep") as sleep, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = run_with_hf_retries(
                "demo",
                "org/model",
                flaky,
                attempts=3,
                base_sleep_seconds=0,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once()

    def test_access_error_is_not_retried_and_prints_model_url(self):
        calls = {"count": 0}

        def denied():
            calls["count"] += 1
            raise _HTTPishError("403 gated repo", 403)

        stderr = io.StringIO()
        with patch("hf_download.time.sleep") as sleep, redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            with self.assertRaises(_HTTPishError):
                run_with_hf_retries(
                    "demo",
                    "org/private-model",
                    denied,
                    attempts=3,
                    base_sleep_seconds=0,
                )

        self.assertEqual(calls["count"], 1)
        sleep.assert_not_called()
        self.assertIn("ACCEPT_URL: https://huggingface.co/org/private-model", stderr.getvalue())

    def test_known_access_phrases_are_non_retryable_without_response(self):
        self.assertTrue(is_non_retryable_hf_error(RuntimeError("Repository Not Found")))
        self.assertTrue(is_non_retryable_hf_error(RuntimeError("requires authorization")))


if __name__ == "__main__":
    unittest.main()
