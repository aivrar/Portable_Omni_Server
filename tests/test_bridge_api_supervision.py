"""Gateway restart supervision owned by the long-lived Omni bridge."""

from __future__ import annotations

import io
import base64
import json
import sys
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402


def _reset_supervisor(monkeypatch):
    monkeypatch.setattr(bridge, "_api_process", None)
    monkeypatch.setattr(bridge, "_api_log_fh", None)
    monkeypatch.setattr(bridge, "_shutdown_in_progress", False)
    monkeypatch.setattr(bridge, "_shutdown_completed", False)
    monkeypatch.setattr(bridge, "_gateway_restart_pid", None)
    monkeypatch.setattr(bridge, "_gateway_restart_deadline", None)
    bridge._bridge_stopping.clear()


def test_supervisor_restarts_an_exited_owned_gateway(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.poll.return_value = 15
    log_handle = Mock()
    monkeypatch.setattr(bridge, "_api_process", process)
    monkeypatch.setattr(bridge, "_api_log_fh", log_handle)
    monkeypatch.setattr(bridge, "_remove_gateway_pid", Mock())
    start = Mock(return_value=True)
    monkeypatch.setattr(bridge, "start_api_server", start)
    monkeypatch.setattr(bridge._bridge_stopping, "wait", Mock(return_value=False))

    assert bridge._supervise_api_once() is True
    log_handle.close.assert_called_once_with()
    bridge._remove_gateway_pid.assert_called_once_with()
    start.assert_called_once_with()


def test_supervisor_leaves_a_running_gateway_alone(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(bridge, "_api_process", process)
    start = Mock(return_value=True)
    monkeypatch.setattr(bridge, "start_api_server", start)

    assert bridge._supervise_api_once() is False
    start.assert_not_called()


def test_supervisor_forces_only_an_expired_requested_restart(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.pid = 4321
    process.poll.return_value = None
    monkeypatch.setattr(bridge, "_api_process", process)
    monkeypatch.setattr(bridge, "_gateway_restart_pid", process.pid)
    monkeypatch.setattr(bridge, "_gateway_restart_deadline", 10.0)
    monkeypatch.setattr(bridge.time, "monotonic", Mock(return_value=10.1))
    stop = Mock()
    start = Mock(return_value=True)
    monkeypatch.setattr(bridge, "_kill_owned_gateway_process", stop)
    monkeypatch.setattr(bridge, "start_api_server", start)

    assert bridge._supervise_api_once() is True
    stop.assert_called_once_with(grace=0.0)
    start.assert_called_once_with()


def test_restart_request_arms_the_owned_gateway_deadline(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.pid = 9876
    process.poll.return_value = None
    monkeypatch.setattr(bridge, "_api_process", process)
    monkeypatch.setattr(bridge.time, "monotonic", Mock(return_value=100.0))
    monkeypatch.setattr(bridge, "GATEWAY_RESTART_GRACE_SECONDS", 20.0)

    bridge._note_gateway_restart_requested()

    assert bridge._gateway_restart_pid == 9876
    assert bridge._gateway_restart_deadline == 120.0


def test_gateway_kill_never_signals_a_shared_bridge_process_group(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.pid = 2468
    process.poll.return_value = None
    monkeypatch.setattr(bridge, "_api_process", process)
    import server.process_identity as identity
    monkeypatch.setattr(identity, "process_start_time", Mock(return_value="123"))
    monkeypatch.setattr(identity, "process_matches", Mock(return_value=True))
    monkeypatch.setattr(identity, "owned_process_group", Mock(return_value=None))
    monkeypatch.setattr(bridge, "_remove_gateway_pid", Mock())
    monkeypatch.setattr(bridge.os, "getpgid", Mock(return_value=1357), raising=False)
    monkeypatch.setattr(bridge.signal, "SIGKILL", 9, raising=False)
    kill = Mock()
    killpg = Mock()
    monkeypatch.setattr(bridge.os, "kill", kill)
    monkeypatch.setattr(bridge.os, "killpg", killpg, raising=False)

    bridge._kill_owned_gateway_process(grace=0.0)

    killpg.assert_not_called()
    assert kill.call_count >= 2


def test_supervisor_never_restarts_during_bridge_shutdown(monkeypatch):
    _reset_supervisor(monkeypatch)
    process = Mock()
    process.poll.return_value = 0
    monkeypatch.setattr(bridge, "_api_process", process)
    start = Mock(return_value=True)
    monkeypatch.setattr(bridge, "start_api_server", start)
    bridge._bridge_stopping.set()

    assert bridge._supervise_api_once() is False
    start.assert_not_called()
    bridge._bridge_stopping.clear()


def test_bridge_preserves_media_range_headers():
    assert "Range" in bridge.ProxyHandler._FORWARDED_REQUEST_HEADERS
    assert "If-Range" in bridge.ProxyHandler._FORWARDED_REQUEST_HEADERS
    assert "content-range" not in bridge.ProxyHandler._SKIPPED_RESPONSE_HEADERS
    assert "content-length" not in bridge.ProxyHandler._SKIPPED_RESPONSE_HEADERS


def test_bridge_timeout_covers_longest_supported_audio_inference():
    assert bridge.API_PROXY_TIMEOUT_SECONDS > 1800


def test_bridge_shutdown_sweep_is_idempotent(monkeypatch):
    _reset_supervisor(monkeypatch)
    sweep = Mock()
    monkeypatch.setattr(bridge, "_run_omni_shutdown_sweep", sweep)
    monkeypatch.setattr(bridge, "_kill_owned_gateway_process", Mock())
    monkeypatch.setattr(bridge, "_remove_gateway_pid", Mock())

    bridge.stop_api_server()
    bridge.stop_api_server()

    sweep.assert_called_once_with()


def test_full_shutdown_schedules_only_matching_windows_host(monkeypatch):
    monkeypatch.setenv("TQ_APP_DIR", "/mnt/e/linux/template/app/apps/Omni_Studio")
    monkeypatch.setattr(bridge.Path, "exists", lambda self: True)
    popen = Mock()
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)

    assert bridge._schedule_windows_host_shutdown(0.25) is True
    args = popen.call_args.args[0]
    kwargs = popen.call_args.kwargs
    assert args[0].lower().endswith("powershell.exe")
    decoded = base64.b64decode(args[-1]).decode("utf-16le")
    assert "Stop-Process -Force" in decoded
    assert r"E:\linux\template\app\apps\Omni_Studio\Omni_Studio.exe" in decoded
    assert kwargs["start_new_session"] is True


def test_full_shutdown_rejects_unsafe_host_path(monkeypatch):
    monkeypatch.setenv("TQ_APP_DIR", "/mnt/e/linux/template/../Omni_Studio")
    popen = Mock()
    monkeypatch.setattr(bridge.subprocess, "Popen", popen)

    assert bridge._schedule_windows_host_shutdown() is False
    popen.assert_not_called()


def test_full_shutdown_endpoint_requires_token_and_confirmation(monkeypatch):
    handler = object.__new__(bridge.ProxyHandler)
    payload = json.dumps({"confirm": True}).encode("utf-8")
    handler.headers = {
        "Content-Length": str(len(payload)),
        "X-Omni-Token": "x" * 32,
    }
    handler.rfile = io.BytesIO(payload)
    handler.server = object()
    handler._allowed_origin = lambda: None
    sent = []
    handler._send_json = lambda status, body: sent.append((status, body))
    monkeypatch.setattr(bridge, "_read_runtime_api_token", lambda: "x" * 32)

    started = []

    class FakeThread:
        def __init__(self, *, target, args, name, daemon):
            started.append((target, args, name, daemon))

        def start(self):
            started.append("started")

    monkeypatch.setattr(bridge.threading, "Thread", FakeThread)

    handler._handle_app_shutdown()

    assert sent[0][0] == 202
    assert sent[0][1]["status"] == "shutting_down"
    assert started[-1] == "started"


def test_full_shutdown_endpoint_rejects_wrong_token(monkeypatch):
    handler = object.__new__(bridge.ProxyHandler)
    payload = json.dumps({"confirm": True}).encode("utf-8")
    handler.headers = {
        "Content-Length": str(len(payload)),
        "X-Omni-Token": "wrong" * 8,
    }
    handler.rfile = io.BytesIO(payload)
    handler.server = object()
    handler._allowed_origin = lambda: None
    sent = []
    handler._send_json = lambda status, body: sent.append((status, body))
    monkeypatch.setattr(bridge, "_read_runtime_api_token", lambda: "x" * 32)

    handler._handle_app_shutdown()

    assert sent[0][0] == 401
