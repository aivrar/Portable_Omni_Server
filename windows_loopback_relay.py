"""Windows loopback relay for Omni Studio's WSL bridge.

This helper is launched by ``bridge.py`` with Windows Python.  It exposes only
127.0.0.1 on the Windows host and forwards bytes to a private WSL ingress
socket.  A per-bridge lease prevents an orphaned relay from surviving after
Omni exits or its distro is terminated.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import socket
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.create_connection(self.server.target, timeout=10)
        upstream.sendall(self.server.relay_secret)
        sockets = (self.request, upstream)
        try:
            while True:
                readable, _, _ = select.select(sockets, (), (), 10)
                for source in readable:
                    data = source.recv(1024 * 1024)
                    if not data:
                        return
                    destination = upstream if source is self.request else self.request
                    destination.sendall(data)
        finally:
            upstream.close()


class _RelayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], target: tuple[str, int], relay_secret: bytes):
        self.target = target
        self.relay_secret = relay_secret
        super().__init__(address, _RelayHandler)


def _write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _lease_is_current(path: Path, timeout_seconds: float) -> bool:
    try:
        return time.time() - path.stat().st_mtime <= timeout_seconds
    except OSError:
        return False


def _watch_lease(
    server: _RelayServer,
    lease_path: Path,
    lease_timeout: float,
    target: tuple[str, int],
) -> None:
    target_failures = 0
    while _lease_is_current(lease_path, lease_timeout):
        try:
            with socket.create_connection(target, timeout=1):
                target_failures = 0
        except OSError:
            target_failures += 1
            if target_failures >= 4:
                break
        time.sleep(1)
    server.shutdown()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", required=True)
    parser.add_argument("--target-port", type=int, required=True)
    parser.add_argument("--lease", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--lease-timeout", type=float, default=8.0)
    parser.add_argument("--bind-timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    relay_secret = args.lease.read_bytes()
    if len(relay_secret) != 64:
        return 2
    target = (args.target_host, args.target_port)
    deadline = time.monotonic() + max(0.0, args.bind_timeout)
    server = None
    while server is None:
        if not _lease_is_current(args.lease, args.lease_timeout):
            return 2
        try:
            server = _RelayServer((args.listen_host, args.listen_port), target, relay_secret)
        except OSError:
            if time.monotonic() >= deadline:
                return 3
            time.sleep(0.5)

    _write_status(
        args.status,
        {
            "pid": os.getpid(),
            "ready": True,
            "listen": f"{args.listen_host}:{args.listen_port}",
            "target": f"{args.target_host}:{args.target_port}",
        },
    )
    watcher = threading.Thread(
        target=_watch_lease,
        args=(server, args.lease, args.lease_timeout, target),
        daemon=True,
    )
    watcher.start()
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        try:
            current = json.loads(args.status.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        if int(current.get("pid") or -1) == os.getpid():
            try:
                args.status.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
