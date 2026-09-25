"""bridge_watchdog — supervises bridge.py for Omni Studio.

Launched inside WSL by the parent C launcher (main.c:start_bridge), with a
fresh session via `setsid`. Owns the lifecycle of bridge.py: writes pidfiles
the launcher can read, restarts bridge.py with capped backoff if it dies,
and tears the bridge down cleanly on SIGTERM.

Environment (set by the C launcher):
  BRIDGE_PORT          — port the bridge proxy should bind (default 9200)
  TQ_AUTH_TOKEN        — bearer token; bridge validates and exchanges for cookie
  TQ_APP_DIR           — WSL path to the app dir, where bridge.py lives
  TQ_BRIDGE_LOG_DIR    — log dir; pidfiles + watchdog.log written here
  PYTHONUNBUFFERED, PYTHONFAULTHANDLER — passed through

Pidfile contract (read by main.c:stop_bridge_with_backend):
  $TQ_BRIDGE_LOG_DIR/watchdog.pid — this watchdog's PID
  $TQ_BRIDGE_LOG_DIR/bridge.pid   — current bridge.py PID

Stdlib only. Designed to be small and obvious.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime

LOG_DIR = os.environ.get("TQ_BRIDGE_LOG_DIR") or "/tmp/linux_template"
APP_DIR = os.environ.get("TQ_APP_DIR") or os.path.dirname(os.path.abspath(__file__))
BRIDGE_PY = os.path.join(APP_DIR, "bridge.py")
WATCHDOG_PID_FILE = os.path.join(LOG_DIR, "watchdog.pid")
BRIDGE_PID_FILE = os.path.join(LOG_DIR, "bridge.pid")
WATCHDOG_LOG = os.path.join(LOG_DIR, "watchdog.log")
BRIDGE_STDIO = os.path.join(LOG_DIR, "bridge-stdio.log")
OPT_DIR = "/opt/omni_studio"
VENV_PY = os.path.join(OPT_DIR, "venv", "bin", "python3")
SHUTDOWN_SCRIPT = os.path.join(OPT_DIR, "server", "omni_shutdown.py")

# Crash-loop guard: if bridge.py exits within FAST_FAIL_SECS of starting,
# count it as a fast failure. After MAX_FAST_FAILS in a row, give up — the
# launcher's appStartBridge fetch poll will time out and surface the error.
FAST_FAIL_SECS = 8
MAX_FAST_FAILS = 5
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0
CPU_LIMIT = max(1, int(os.environ.get("OMNI_CPU_LIMIT", "6")))
MEMORY_LIMIT_GB = max(1, int(os.environ.get("OMNI_MEMORY_LIMIT_GB", "24")))
SWAP_LIMIT_GB = max(0, int(os.environ.get("OMNI_SWAP_LIMIT_GB", "2")))
PID_LIMIT = max(64, int(os.environ.get("OMNI_PID_LIMIT", "512")))


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    try:
        with open(WATCHDOG_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass


def write_pid(path: str, pid: int) -> None:
    try:
        with open(path, "w") as f:
            f.write(str(pid))
    except Exception as e:
        log(f"warn: could not write {path}: {e}")


def remove_pid(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"warn: could not remove {path}: {e}")


def _apply_resource_boundary() -> None:
    """Place the supervisor itself in the same limits as every app child."""
    cpu_count = os.cpu_count() or 1
    try:
        os.sched_setaffinity(0, set(range(min(CPU_LIMIT, cpu_count))))
    except (AttributeError, OSError) as exc:
        log(f"watchdog CPU affinity unavailable: {exc}")
    try:
        os.nice(5)
    except OSError as exc:
        log(f"watchdog priority adjustment unavailable: {exc}")

    controls = (
        ("cpu", {
            "cpu.cfs_period_us": "100000",
            "cpu.cfs_quota_us": str(CPU_LIMIT * 100000),
        }),
        ("memory", {
            "memory.limit_in_bytes": str(MEMORY_LIMIT_GB * 1024 ** 3),
            "memory.memsw.limit_in_bytes": str((MEMORY_LIMIT_GB + SWAP_LIMIT_GB) * 1024 ** 3),
            "memory.swappiness": "10",
        }),
        ("pids", {"pids.max": str(PID_LIMIT)}),
        ("cpuacct", {}),
        ("blkio", {"blkio.weight": "500"}),
    )
    applied = []
    for controller, values in controls:
        group = f"/sys/fs/cgroup/{controller}/omni_studio"
        try:
            os.makedirs(group, exist_ok=True)
            for filename, value in values.items():
                with open(os.path.join(group, filename), "w", encoding="ascii") as control:
                    control.write(value)
            with open(os.path.join(group, "tasks"), "w", encoding="ascii") as tasks:
                tasks.write(str(os.getpid()))
            applied.append(controller)
        except OSError as exc:
            log(f"watchdog {controller} cgroup unavailable: {exc}")
    log(f"watchdog resource boundary: {','.join(applied) or 'none'}")


_child: subprocess.Popen | None = None
_shutting_down = False


def _run_omni_shutdown_sweep() -> None:
    """Belt-and-suspenders cleanup if bridge.py exited without sweeping."""
    if not os.path.isfile(VENV_PY) or not os.path.isfile(SHUTDOWN_SCRIPT):
        log("shutdown sweep skipped — runtime not provisioned yet")
        return
    log("running Omni shutdown sweep from watchdog")
    try:
        result = subprocess.run(
            [VENV_PY, SHUTDOWN_SCRIPT],
            capture_output=True,
            text=True,
            timeout=45,
        )
        detail = (result.stdout or result.stderr or "").strip()
        if detail:
            log(f"shutdown sweep: {detail[:500]}")
        if result.returncode != 0:
            log(f"shutdown sweep exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        log("shutdown sweep timed out")
    except Exception as e:
        log(f"shutdown sweep failed: {e}")


def _terminate_child(timeout: float = 15.0) -> None:
    global _child
    proc = _child
    if proc is None:
        return
    if proc.poll() is not None:
        return
    log(f"sending SIGTERM to bridge (pid {proc.pid})")
    try:
        # Bridge runs in its own process group (start_new_session=True).
        # Negative PID delivers the signal to every child it spawned.
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception as e:
        log(f"warn: SIGTERM to pgrp failed ({e}); falling back to direct signal")
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log(f"bridge exited with rc={proc.returncode}")
            return
        time.sleep(0.2)

    log("bridge did not exit on SIGTERM; sending SIGKILL")
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _on_signal(signum, _frame):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log(f"received signal {signum}; shutting down")
    _terminate_child()
    _run_omni_shutdown_sweep()
    remove_pid(BRIDGE_PID_FILE)
    remove_pid(WATCHDOG_PID_FILE)
    sys.exit(0)


def _spawn_bridge() -> subprocess.Popen:
    """Launch bridge.py as a child in its own session.

    stdin tied to /dev/null so the bridge cannot read from a vanished tty.
    stdout/stderr merged into bridge-stdio.log so we never silently lose a
    crash traceback if bridge.py blows up before its own logger initializes.
    """
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONFAULTHANDLER", "1")

    stdio = open(BRIDGE_STDIO, "ab", buffering=0)
    devnull = open(os.devnull, "rb")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", BRIDGE_PY],
            cwd=APP_DIR,
            env=env,
            stdin=devnull,
            stdout=stdio,
            stderr=stdio,
            start_new_session=True,
        )
    finally:
        # The child inherits the open fds; we close our copies so the
        # watchdog process doesn't keep them pinned.
        try:
            stdio.close()
        except Exception:
            pass
        try:
            devnull.close()
        except Exception:
            pass
    return proc


def main() -> int:
    global _child

    os.makedirs(LOG_DIR, exist_ok=True)

    if not os.path.isfile(BRIDGE_PY):
        log(f"fatal: bridge.py not found at {BRIDGE_PY}")
        return 1

    write_pid(WATCHDOG_PID_FILE, os.getpid())
    log(f"watchdog up (pid {os.getpid()}); bridge.py at {BRIDGE_PY}")
    log(f"BRIDGE_PORT={os.environ.get('BRIDGE_PORT', '<unset>')} "
        f"TQ_APP_DIR={APP_DIR}")
    _apply_resource_boundary()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    # SIGHUP arrives when the parent wsl.exe disappears; treat as a stop.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _on_signal)

    fast_fails = 0
    backoff = BACKOFF_START

    try:
        while not _shutting_down:
            start_ts = time.monotonic()
            try:
                _child = _spawn_bridge()
            except Exception as e:
                log(f"failed to spawn bridge.py: {e}")
                time.sleep(min(backoff, BACKOFF_MAX))
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            write_pid(BRIDGE_PID_FILE, _child.pid)
            log(f"bridge started (pid {_child.pid})")

            try:
                rc = _child.wait()
            except KeyboardInterrupt:
                _on_signal(signal.SIGINT, None)
                return 0

            if _shutting_down:
                return 0

            ran_for = time.monotonic() - start_ts
            log(f"bridge exited rc={rc} after {ran_for:.1f}s")
            remove_pid(BRIDGE_PID_FILE)

            if ran_for < FAST_FAIL_SECS:
                fast_fails += 1
            else:
                fast_fails = 0
                backoff = BACKOFF_START

            if fast_fails >= MAX_FAST_FAILS:
                log(f"giving up: {fast_fails} fast-fails in a row "
                    f"(<{FAST_FAIL_SECS}s each). Check {BRIDGE_STDIO}.")
                return 1

            sleep_for = min(backoff, BACKOFF_MAX)
            log(f"restarting in {sleep_for:.1f}s (fast_fails={fast_fails})")
            time.sleep(sleep_for)
            backoff = min(backoff * 2, BACKOFF_MAX)
    finally:
        _run_omni_shutdown_sweep()
        remove_pid(BRIDGE_PID_FILE)
        remove_pid(WATCHDOG_PID_FILE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
