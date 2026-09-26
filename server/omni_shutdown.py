"""Hard shutdown sweep for Omni Studio.

Kills the API gateway, model workers, ComfyUI instances, and any app-owned
orphans recorded under the runtime pid directory. Safe to run from:

  * bridge.py on SIGTERM (subprocess via the venv python)
  * bridge_watchdog.py after the bridge exits
  * omni_comfy_server.py signal handler / lifespan teardown
  * ``python omni_shutdown.py`` for manual cleanup

Workers and ComfyUI are spawned with ``start_new_session=True``, so they live
outside the gateway process group and must be killed explicitly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_BASE_DIR = Path(__file__).parent.resolve()
if str(_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(_BASE_DIR))

from config import (  # noqa: E402
    API_TOKEN_FILE,
    APP_DIR,
    COMFYUI_DIR,
    DEFAULT_API_PORT,
    PID_DIR,
    RUNTIME_DIR,
    SERVER_DIR,
)

from process_identity import process_matches, process_start_time, owned_process_group, process_alive

logger = logging.getLogger(__name__)

GATEWAY_PID_FILE = RUNTIME_DIR / "gateway.pid"
# The watchdog inherits the bridge's API_PORT. Honor it when the helper is
# invoked without --api-url, including isolated or relocated installations.
DEFAULT_API_URL = "http://127.0.0.1:" + str(int(
    os.environ.get("API_PORT") or os.environ.get("OMNI_API_PORT") or DEFAULT_API_PORT
))


def _read_api_token() -> str | None:
    try:
        token = API_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if len(token) >= 32 else None


def _pid_alive(pid: int) -> bool:
    return process_alive(pid)


def _same_app_instance(value: object) -> bool:
    if value in (None, ""):
        return False
    return Path(str(value)).as_posix() == Path(str(APP_DIR)).as_posix()


def _terminate_pgid(pgid: int, *, grace: float = 3.0) -> None:
    if pgid <= 0:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (OSError, ProcessLookupError):
            return
        time.sleep(0.15)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _kill_pid_tree(pid: int, pgid: int | None = None, *, grace: float = 3.0) -> None:
    if pid <= 0:
        return
    group = owned_process_group(pid, pgid)
    if group is not None:
        _terminate_pgid(group, grace=grace)
        return
    # Legacy processes can share the bridge's group. Signal only the verified
    # PID, and verify its start time again before escalation.
    identity = process_start_time(pid)
    if identity is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if process_start_time(pid) != identity:
                return
            time.sleep(0.15)
        if process_start_time(pid) == identity:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def try_graceful_gateway_shutdown(
    api_url: str = DEFAULT_API_URL,
    timeout: float = 8.0,
) -> bool:
    """Ask the gateway to shut itself down (workers/comfy/jobs first)."""
    token = _read_api_token()
    if not token:
        return False
    payload = json.dumps({"confirm": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/system/shutdown",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Omni-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status not in (200, 202):
                return False
    except (urllib.error.URLError, TimeoutError, OSError):
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{api_url.rstrip('/')}/health", timeout=1):
                time.sleep(0.25)
        except (urllib.error.URLError, TimeoutError, OSError):
            return True
    return False


def _try_worker_unload(port: int) -> None:
    if port <= 0:
        return
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/unload",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=2)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def _kill_pid_records(pattern: str, *, unload_workers: bool = False) -> int:
    killed = 0
    PID_DIR.mkdir(parents=True, exist_ok=True)
    for pid_path in PID_DIR.glob(pattern):
        try:
            record = json.loads(pid_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not _same_app_instance(record.get("app_instance")):
            continue
        pid = int(record.get("pid") or 0)
        pgid = int(record.get("pgid") or pid or 0)
        if pid <= 0:
            pid_path.unlink(missing_ok=True)
            continue
        if not _pid_alive(pid):
            pid_path.unlink(missing_ok=True)
            continue
        script = COMFYUI_DIR / "main.py" if record.get("kind") == "comfy" else SERVER_DIR / "omni_worker.py"
        if not process_matches(pid, script, port=int(record.get("port") or 0), start_time=record.get("start_time")):
            logger.warning("Skipping stale process record %s", pid_path.name)
            continue
        if unload_workers and record.get("kind") == "worker":
            _try_worker_unload(int(record.get("port") or 0))
        logger.info("Killing %s pid=%d pgid=%d", pid_path.name, pid, pgid)
        _kill_pid_tree(pid, pgid)
        pid_path.unlink(missing_ok=True)
        killed += 1
    return killed


def _kill_gateway_processes(*, grace: float = 5.0) -> int:
    killed = 0
    targets: list[tuple[int, int | None]] = []

    if GATEWAY_PID_FILE.exists():
        try:
            pid = int(GATEWAY_PID_FILE.read_text(encoding="utf-8").strip())
            if _pid_alive(pid) and process_matches(pid, SERVER_DIR / "omni_comfy_server.py"):
                targets.append((pid, None))
        except (OSError, ValueError):
            pass
        try:
            GATEWAY_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    marker = str(SERVER_DIR / "omni_comfy_server.py")
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python.*omni_comfy_server.py"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                try:
                    pid = int(line.strip())
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                if process_matches(pid, marker):
                    targets.append((pid, None))
    except (OSError, subprocess.TimeoutExpired):
        pass

    seen: set[int] = set()
    for pid, pgid in targets:
        if pid in seen or not _pid_alive(pid) or not process_matches(pid, SERVER_DIR / "omni_comfy_server.py"):
            continue
        seen.add(pid)
        logger.info("Stopping gateway pid=%d", pid)
        _kill_pid_tree(pid, pgid, grace=grace)
        killed += 1
    return killed


def _kill_pattern_orphans(
    pgrep_pattern: str,
    marker: str,
    *,
    grace: float = 2.0,
) -> int:
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", pgrep_pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0

    for line in result.stdout.strip().splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == os.getpid() or not _pid_alive(pid):
            continue
        if not process_matches(pid, marker):
            continue
        logger.info("Killing orphan pid=%d (%s)", pid, pgrep_pattern)
        _kill_pid_tree(pid, grace=grace)
        killed += 1
    return killed


def sweep_all(
    *,
    graceful: bool = True,
    api_url: str = DEFAULT_API_URL,
    include_gateway: bool = True,
) -> dict:
    """Kill every Omni Studio process owned by this app instance."""
    summary = {
        "graceful": False,
        "gateway_killed": 0,
        "pid_records_killed": 0,
        "orphan_workers_killed": 0,
        "orphan_comfy_killed": 0,
        "fallback_killed": 0,
    }

    if graceful:
        summary["graceful"] = try_graceful_gateway_shutdown(api_url=api_url)
        if summary["graceful"]:
            # Gateway signal handler should have cleared workers/comfy; still
            # sweep pid files in case anything was detached.
            time.sleep(0.5)

    if include_gateway and not summary["graceful"]:
        summary["gateway_killed"] = _kill_gateway_processes()

    # Unload + kill anything recorded in pid files (workers, ComfyUI, etc.).
    summary["pid_records_killed"] = (
        _kill_pid_records("worker_*.json", unload_workers=True)
        + _kill_pid_records("comfy_*.json")
    )

    # Registry-aware orphan sweep (cmdline-verified).
    try:
        from worker_manager import WorkerManager  # noqa: WPS433
        from comfy_manager import ComfyManager  # noqa: WPS433

        WorkerManager().kill_orphan_workers()
        ComfyManager().kill_orphan_comfy()
        summary["orphan_workers_killed"] = 1
        summary["orphan_comfy_killed"] = 1
    except Exception as exc:
        logger.warning("Orphan manager sweep failed: %s", exc)

    # Last-resort pattern sweep scoped to this app's paths.
    summary["fallback_killed"] = (
        _kill_pattern_orphans(
            "python.*omni_worker.py.*--port",
            str(SERVER_DIR / "omni_worker.py"),
        )
        + _kill_pattern_orphans(
            "python.*main.py.*--port",
            str(COMFYUI_DIR / "main.py"),
        )
        + _kill_pattern_orphans(
            "python.*omni_comfy_server.py",
            str(SERVER_DIR / "omni_comfy_server.py"),
        )
        + _kill_pattern_orphans(
            "bash.*install_model.sh",
            str(SERVER_DIR / "install_model.sh"),
        )
    )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kill all Omni Studio processes")
    parser.add_argument(
        "--no-graceful",
        action="store_true",
        help="Skip POST /api/system/shutdown and kill immediately",
    )
    parser.add_argument(
        "--gateway-only",
        action="store_true",
        help="Only stop the gateway process",
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help=f"Gateway base URL (default: {DEFAULT_API_URL})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.gateway_only:
        count = _kill_gateway_processes()
        print(json.dumps({"gateway_killed": count}))
        return 0

    summary = sweep_all(
        graceful=not args.no_graceful,
        api_url=args.api_url,
    )
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
