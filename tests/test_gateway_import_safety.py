"""Importing the gateway for schema tests must not own process shutdown."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gateway_import_preserves_existing_signal_handlers():
    code = """
import signal
import sys

signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.path.insert(0, 'server')
import omni_comfy_server
assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
